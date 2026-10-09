#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Self

SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
PYPROJECT_VERSION_RE = re.compile(r'(?m)^(version\s*=\s*")([^"]+)(")$')
EXPERIENCE_VERSION_RE = re.compile(r'("identity"\s*:\s*\{.*?"version"\s*:\s*")[^"]+("\s*\})', re.DOTALL)
DEFAULT_PREID = "alpha"
BUMP_CHOICES = (
    "major",
    "minor",
    "patch",
    "premajor",
    "preminor",
    "prepatch",
    "prerelease",
    "release",
)
PREID_CHOICES = ("alpha", "beta", "rc")


@dataclass(frozen=True, slots=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> Self:
        match = SEMVER_RE.match(str(value).strip())
        if match is None:
            raise ValueError(f"Invalid semantic version: {value}")

        prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
        build = tuple(match.group(5).split(".")) if match.group(5) else ()
        return cls(
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3)),
            prerelease=prerelease,
            build=build,
        )

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value = f"{value}-" + ".".join(self.prerelease)
        if self.build:
            value = f"{value}+" + ".".join(self.build)
        return value

    def without_prerelease(self) -> Self:
        return SemVer(self.major, self.minor, self.patch, (), self.build)

    def with_prerelease(self, *parts: str) -> Self:
        return SemVer(self.major, self.minor, self.patch, tuple(parts), self.build)

    def stable_key(self) -> tuple[int, int, int]:
        return self.major, self.minor, self.patch

    def compare(self, other: Self) -> int:
        if self.stable_key() != other.stable_key():
            return -1 if self.stable_key() < other.stable_key() else 1
        if not self.prerelease and not other.prerelease:
            return 0
        if not self.prerelease:
            return 1
        if not other.prerelease:
            return -1
        return _compare_identifiers(self.prerelease, other.prerelease)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bump the semantic version for a PiPhi integration/sidecar across "
            "pyproject.toml and src/manifest.json."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--bump", choices=BUMP_CHOICES, help="Increment the current version.")
    mode.add_argument("--set-version", help="Set an explicit semantic version.")
    parser.add_argument(
        "--preid",
        choices=PREID_CHOICES,
        default=DEFAULT_PREID,
        help="Prerelease identifier used with pre* or prerelease bumps.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root. Defaults to the parent directory of this script.",
    )
    parser.add_argument(
        "--pyproject",
        default="pyproject.toml",
        help="Path to pyproject.toml, relative to repo-root unless absolute.",
    )
    parser.add_argument(
        "--manifest",
        default="src/manifest.json",
        help="Path to manifest.json, relative to repo-root unless absolute.",
    )
    parser.add_argument("--experience", default="experiences/air-quality/package.source.json")
    parser.add_argument(
        "--docker-image",
        default=None,
        help="Override the primary container image repository to pin in the manifest.",
    )
    parser.add_argument(
        "--no-pin-container-image",
        action="store_true",
        help="Update versions only and leave manifest container image references unchanged.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the next version without writing files.")
    return parser.parse_args()


def resolve_repo_root(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def resolve_path(repo_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_manifest(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_pyproject_version(text: str) -> SemVer:
    match = PYPROJECT_VERSION_RE.search(text)
    if match is None:
        raise ValueError("Unable to find project version in pyproject.toml")
    return SemVer.parse(match.group(2))


def write_pyproject_version(text: str, version: str) -> str:
    updated, count = PYPROJECT_VERSION_RE.subn(rf'\g<1>{version}\g<3>', text, count=1)
    if count != 1:
        raise ValueError("Unable to update project version in pyproject.toml")
    return updated


def resolve_target_version(current: SemVer, *, bump: str | None, set_version: str | None, preid: str) -> SemVer:
    if set_version is not None:
        target = SemVer.parse(set_version.strip())
        if target.compare(current) <= 0:
            raise ValueError(f"Explicit version must be newer than the current version ({current})")
        return target

    if bump is None:
        raise ValueError("Either --bump or --set-version is required")
    return bump_version(current, bump=bump, preid=preid)


def bump_version(current: SemVer, *, bump: str, preid: str) -> SemVer:
    stable = current.without_prerelease()
    if bump == "major":
        return SemVer(stable.major + 1, 0, 0)
    if bump == "minor":
        return SemVer(stable.major, stable.minor + 1, 0)
    if bump == "patch":
        return SemVer(stable.major, stable.minor, stable.patch + 1)
    if bump == "premajor":
        return SemVer(stable.major + 1, 0, 0).with_prerelease(preid, "1")
    if bump == "preminor":
        return SemVer(stable.major, stable.minor + 1, 0).with_prerelease(preid, "1")
    if bump == "prepatch":
        return SemVer(stable.major, stable.minor, stable.patch + 1).with_prerelease(preid, "1")
    if bump == "prerelease":
        return bump_prerelease(current, preid=preid)
    if bump == "release":
        if not current.prerelease:
            raise ValueError("Cannot promote a stable version. Use patch/minor/major or --set-version instead.")
        return current.without_prerelease()
    raise ValueError(f"Unsupported bump type: {bump}")


def bump_prerelease(current: SemVer, *, preid: str) -> SemVer:
    if not current.prerelease:
        return SemVer(current.major, current.minor, current.patch + 1).with_prerelease(preid, "1")

    stable = current.without_prerelease()
    current_preid = current.prerelease[0]
    if current_preid != preid:
        return stable.with_prerelease(preid, "1")

    suffix = list(current.prerelease[1:])
    if not suffix:
        return stable.with_prerelease(preid, "1")

    last_token = suffix[-1]
    if last_token.isdigit():
        suffix[-1] = str(int(last_token) + 1)
    else:
        suffix.append("1")
    return stable.with_prerelease(preid, *suffix)


def _compare_identifiers(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    for left_part, right_part in zip(left, right):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_part) < int(right_part) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_part < right_part else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


def image_repository(image: str) -> str:
    image = image.strip()
    if "@" in image:
        image = image.split("@", 1)[0]
    last_slash = image.rfind("/")
    last_colon = image.rfind(":")
    if last_colon > last_slash:
        return image[:last_colon]
    return image


def infer_primary_container_repo(manifest: dict) -> str | None:
    top_level_image = manifest.get("image")
    if isinstance(top_level_image, str) and top_level_image.strip():
        return image_repository(top_level_image)

    repos: set[str] = set()
    runtime = manifest.get("runtime")
    if isinstance(runtime, dict):
        for platform_config in runtime.values():
            if not isinstance(platform_config, dict):
                continue
            container = platform_config.get("container")
            if not isinstance(container, dict):
                continue
            image = container.get("image")
            if isinstance(image, str) and image.strip():
                repos.add(image_repository(image))

    if not repos:
        return None
    if len(repos) > 1:
        raise ValueError("Multiple runtime container image repositories found; pass --docker-image explicitly.")
    return next(iter(repos))


def update_primary_container_images(manifest: dict, *, docker_image: str, version: str) -> None:
    tagged_image = f"{docker_image}:{version}"

    top_level_image = manifest.get("image")
    if isinstance(top_level_image, str) and top_level_image.strip():
        if image_repository(top_level_image) == docker_image:
            manifest["image"] = tagged_image

    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        return

    for platform_config in runtime.values():
        if not isinstance(platform_config, dict):
            continue
        container = platform_config.get("container")
        if not isinstance(container, dict):
            continue
        image = container.get("image")
        if isinstance(image, str) and image.strip() and image_repository(image) == docker_image:
            container["image"] = tagged_image


def main() -> int:
    args = parse_args()
    repo_root = resolve_repo_root(args.repo_root)
    pyproject_path = resolve_path(repo_root, args.pyproject)
    manifest_path = resolve_path(repo_root, args.manifest)
    experience_path = resolve_path(repo_root, args.experience)

    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    pyproject_version = read_pyproject_version(pyproject_text)
    manifest = load_manifest(manifest_path)
    manifest_version = SemVer.parse(str(manifest.get("version") or "").strip())

    if pyproject_version.compare(manifest_version) != 0:
        raise ValueError(
            f"Version mismatch: pyproject.toml={pyproject_version} manifest.json={manifest_version}"
        )

    target = resolve_target_version(
        pyproject_version,
        bump=args.bump,
        set_version=args.set_version,
        preid=args.preid,
    )
    target_version = str(target)

    if args.dry_run:
        print(target_version)
        return 0

    manifest["version"] = target_version
    if not args.no_pin_container_image:
        docker_image = args.docker_image or infer_primary_container_repo(manifest)
        if docker_image:
            update_primary_container_images(manifest, docker_image=docker_image, version=target_version)

    pyproject_path.write_text(write_pyproject_version(pyproject_text, target_version), encoding="utf-8")
    dump_manifest(manifest_path, manifest)
    if experience_path.exists():
        experience_text = experience_path.read_text(encoding="utf-8")
        updated_experience, count = EXPERIENCE_VERSION_RE.subn(
            rf"\g<1>{target_version}\g<2>", experience_text, count=1
        )
        if count != 1:
            raise ValueError(f"Unable to update experience version in {experience_path}")
        experience_path.write_text(updated_experience, encoding="utf-8")
    print(target_version)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover
        print(f"release.py failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
