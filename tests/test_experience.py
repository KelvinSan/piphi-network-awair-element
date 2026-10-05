from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_air_quality_experience_matches_runtime_manifest() -> None:
    manifest = json.loads((ROOT / "src/manifest.json").read_text(encoding="utf-8"))
    source = json.loads(
        (ROOT / "experiences/air-quality/package.source.json").read_text(
            encoding="utf-8"
        )
    )
    identity = source["identity"]
    package_id = f"{identity['publisher_id']}.{identity['package_id']}"
    references = manifest["ui"]["experience_packages"]

    assert source["owning_integration_id"] == manifest["id"]
    assert identity["version"] == manifest["version"]
    assert references == [
        {
            "registry_id": package_id,
            "version_range": ">=0.1,<1",
            "auto_install": True,
        }
    ]

    (widget,) = source["widgets"]
    assert widget["runtime"] == "declarative"
    assert widget["presentation"]["shell"] == "core"
    certification = widget["certification"]
    assert certification["schema_version"] == "1"
    assert set(certification["verified_gates"]) == {
        "runtime", "persistence", "binding", "states", "responsive",
        "themes", "accessibility", "save-reload", "performance",
    }
    assert set(certification["evidence"]) == set(certification["verified_gates"])
    assert certification["evidence"]["binding"] == [
        "repo:tests/test_experience.py::test_air_quality_experience_matches_runtime_manifest"
    ]
    slots = {slot["id"]: slot for slot in widget["binding_slots"]}
    assert len(slots) == len(widget["binding_slots"])
    assert sum(slot["required"] for slot in slots.values()) == 1
    assert all(slot["binding_modes"] == ["read"] for slot in slots.values())
    for slot in slots.values():
        assert slot["compatible_integration_ids"] == [manifest["id"]]
        assert set(slot["capability_requirements"]) <= set(manifest["capabilities"])

    recipe_slots = {item["slot_id"] for item in widget["recipe"]["items"]}
    assert recipe_slots == set(slots)
    assert {
        target["binding_slot_id"]
        for target in widget["interaction_targets"]
        if target["kind"] == "binding"
    } == set(slots)
    assert not recipe_slots & {"voc_h2_raw", "voc_ethanol_raw", "co2_est_baseline"}
