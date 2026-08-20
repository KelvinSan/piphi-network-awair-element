from __future__ import annotations

import importlib
import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from piphi_runtime_testkit_python import (
    assert_event_sent,
    assert_telemetry_sent,
    build_config_payload,
    build_config_snapshot,
    build_runtime_headers,
)

from com_piphi_await_element.app import app
from com_piphi_await_element.lib.store import registry, runtime_context

PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src"

config_module = importlib.import_module("com_piphi_await_element.contract.config.routes")
command_module = importlib.import_module("com_piphi_await_element.contract.command.router")
discovery_module = importlib.import_module("com_piphi_await_element.contract.discovery.discovery")
lifespan_module = importlib.import_module("com_piphi_await_element.lib.lifespan")


class _FakeAwairResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "timestamp": "2026-05-13T14:55:28.665Z",
            "score": 55,
            "dew_point": 14.65,
            "temp": 22.1,
            "humid": 47.5,
            "abs_humid": 12.13,
            "co2": 612,
            "co2_est": 584,
            "co2_est_baseline": 36389,
            "voc": 105,
            "voc_baseline": 40607,
            "voc_h2_raw": 25,
            "voc_ethanol_raw": 37,
            "pm25": 3,
            "pm10_est": 3,
        }


class _OfflineCoreClient:
    async def get(self, *_args, **_kwargs):
        raise httpx.ConnectError("All connection attempts failed")


class _CoreConfigResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class _CoreConfigClient:
    def __init__(self, payload):
        self.payload = payload

    async def get(self, *_args, **_kwargs):
        return _CoreConfigResponse(self.payload)


def reset_runtime_state() -> None:
    registry.entries.clear()
    registry.state_snapshots.clear()
    registry.recent_events.clear()
    runtime_context.auth.container_id = ""
    runtime_context.auth.internal_token = ""
    runtime_context.process_state.current_generation = None
    runtime_context.process_state.background_tasks.clear()


def wait_for(condition, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for background delivery to complete.")


def test_startup_sync_falls_back_to_mounted_snapshot_when_core_is_offline(
    monkeypatch,
    tmp_path,
) -> None:
    reset_runtime_state()

    async def fake_poll_loop(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(config_module, "fetch_awair_data", fake_poll_loop)
    snapshot_path = tmp_path / "runtime-123.json"
    snapshot_path.write_text(
        """
        {
          "schema_version": 1,
          "container_id": "runtime-123",
          "integration_id": "com.piphi.awair_element",
          "reason": "startup_snapshot",
          "generation": 42,
          "configs": [
            {
              "id": "awair-1",
              "config_id": "awair-1",
              "container_id": "runtime-123",
              "integration_id": "com.piphi.awair_element",
              "device_ip": "10.0.0.40",
              "device_mac": "aa:bb:cc:dd:ee:ff"
            }
          ],
          "deleted_config_ids": [],
          "config_hash": "sha256:abc"
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("PIPHI_CONFIG_SNAPSHOT_PATH", str(snapshot_path))
    runtime_context.auth.update(
        container_id="runtime-123",
        internal_token="secret-token",
    )

    asyncio.run(lifespan_module.startup_sync(runtime_context, _OfflineCoreClient()))

    assert registry.get("awair-1")["device_id"] == "awair-1"
    assert runtime_context.process_state.current_generation == 42


def test_startup_sync_applies_snapshot_then_live_core_config(
    monkeypatch,
    tmp_path,
) -> None:
    reset_runtime_state()

    async def fake_poll_loop(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(config_module, "fetch_awair_data", fake_poll_loop)
    snapshot_path = tmp_path / "runtime-123.json"
    snapshot_path.write_text(
        """
        {
          "schema_version": 1,
          "container_id": "runtime-123",
          "integration_id": "com.piphi.awair_element",
          "reason": "startup_snapshot",
          "generation": 10,
          "configs": [
            {
              "id": "awair-snapshot",
              "config_id": "awair-snapshot",
              "container_id": "runtime-123",
              "integration_id": "com.piphi.awair_element",
              "device_ip": "10.0.0.40"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("PIPHI_CONFIG_SNAPSHOT_PATH", str(snapshot_path))
    runtime_context.auth.update(
        container_id="runtime-123",
        internal_token="secret-token",
    )
    live_core_configs = [
        {
            "config_data": {
                "id": "awair-core",
                "config_id": "awair-core",
                "integration_id": "com.piphi.awair_element",
                "device_ip": "10.0.0.41",
            }
        }
    ]

    asyncio.run(lifespan_module.startup_sync(runtime_context, _CoreConfigClient(live_core_configs)))

    assert registry.get("awair-core")["device_id"] == "awair-core"
    assert registry.get("awair-snapshot") is None


def test_config_apply_sends_awair_telemetry_and_event(
    mock_core,
    monkeypatch,
) -> None:
    reset_runtime_state()

    async def fake_poll_loop(*args, **kwargs) -> None:
        return None

    async def fake_get(self, url: str):
        return _FakeAwairResponse()

    monkeypatch.setattr(config_module.telemetry_client, "core_base_url", mock_core.base_url)
    monkeypatch.setattr(config_module.event_client, "core_base_url", mock_core.base_url)
    monkeypatch.setattr(config_module, "fetch_awair_data", fake_poll_loop)
    monkeypatch.setattr(config_module.httpx.AsyncClient, "get", fake_get)

    payload = build_config_payload(
        config_id="awair-1",
        container_id="runtime-123",
        integration_id="com.piphi.awair_element",
        extra={
            "device_ip": "10.0.0.40",
            "device_mac": "aa:bb:cc:dd:ee:ff",
        },
    )
    headers = build_runtime_headers(container_id="runtime-123", internal_token="secret-token")

    with TestClient(app) as client:
        response = client.post("/config", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json()["config_id"] == "awair-1"

        wait_for(lambda: len(mock_core.telemetry_requests) >= 1)
        wait_for(lambda: len(mock_core.event_requests) >= 1)

        telemetry_request = assert_telemetry_sent(mock_core, device_id="awair-1")
        event_request = assert_event_sent(
            mock_core,
            device_id="awair-1",
            config_id="awair-1",
            event_type="device.configured",
        )

        telemetry_headers = {key.lower(): value for key, value in telemetry_request.headers.items()}
        event_headers = {key.lower(): value for key, value in event_request.headers.items()}

        assert telemetry_headers["x-container-id"] == "runtime-123"
        assert telemetry_headers["x-piphi-integration-token"] == "secret-token"
        assert event_headers["x-container-id"] == "runtime-123"
        assert event_headers["x-piphi-integration-token"] == "secret-token"
        assert telemetry_request.json_body["metrics"]["temp"] == 22.1
        assert telemetry_request.json_body["metrics"]["temperature"] == 22.1
        assert telemetry_request.json_body["metrics"]["humidity"] == 47.5
        assert telemetry_request.json_body["metrics"]["absolute_humidity"] == 12.13
        assert telemetry_request.json_body["metrics"]["co2_est"] == 584
        assert telemetry_request.json_body["metrics"]["voc_baseline"] == 40607
        assert telemetry_request.json_body["metrics"]["pm10_est"] == 3
        assert telemetry_request.json_body["units"]["temp"] == "°C"
        assert telemetry_request.json_body["units"]["temperature"] == "°C"
        assert telemetry_request.json_body["units"]["humidity"] == "%"
        assert telemetry_request.json_body["units"]["pm10_est"] == "ug/m3"
        assert (event_request.json_body.get("event_type") or event_request.json_body.get("type")) == "device.configured"


def test_config_sync_replaces_awair_device_and_uses_testkit_snapshot(
    mock_core,
    monkeypatch,
) -> None:
    reset_runtime_state()

    async def fake_poll_loop(*args, **kwargs) -> None:
        return None

    async def fake_get(self, url: str):
        return _FakeAwairResponse()

    monkeypatch.setattr(config_module.telemetry_client, "core_base_url", mock_core.base_url)
    monkeypatch.setattr(config_module.event_client, "core_base_url", mock_core.base_url)
    monkeypatch.setattr(config_module, "fetch_awair_data", fake_poll_loop)
    monkeypatch.setattr(config_module.httpx.AsyncClient, "get", fake_get)

    old_payload = build_config_payload(
        config_id="awair-old",
        container_id="runtime-123",
        integration_id="com.piphi.awair_element",
        extra={
            "device_ip": "10.0.0.40",
            "device_mac": "aa:bb:cc:dd:ee:01",
        },
    )
    new_payload = build_config_payload(
        config_id="awair-new",
        container_id="runtime-123",
        integration_id="com.piphi.awair_element",
        extra={
            "device_ip": "10.0.0.41",
            "device_mac": "aa:bb:cc:dd:ee:02",
        },
    )
    snapshot = build_config_snapshot(
        configs=[new_payload],
        container_id="runtime-123",
        integration_id="com.piphi.awair_element",
        generation=4,
    )
    headers = build_runtime_headers(container_id="runtime-123", internal_token="secret-token")

    with TestClient(app) as client:
        first_response = client.post("/config", json=old_payload, headers=headers)
        assert first_response.status_code == 200
        wait_for(lambda: len(mock_core.telemetry_requests) >= 1)
        wait_for(lambda: len(mock_core.event_requests) >= 1)

        mock_core.reset()

        sync_response = client.post("/config/sync", json=snapshot, headers=headers)
        assert sync_response.status_code == 200
        wait_for(lambda: len(mock_core.telemetry_requests) >= 1)
        wait_for(lambda: len(mock_core.event_requests) >= 2)

        sync_json = sync_response.json()
        assert sync_json["applied"] == ["awair-new"]
        assert sync_json["removed"] == ["awair-old"]
        assert sync_json["active_config_ids"] == ["awair-new"]
        assert sync_json["generation"] == 4

        telemetry_request = assert_telemetry_sent(mock_core, device_id="awair-new")
        configured_event = assert_event_sent(
            mock_core,
            device_id="awair-new",
            config_id="awair-new",
            event_type="device.configured",
        )
        deconfigured_event = assert_event_sent(
            mock_core,
            device_id="awair-old",
            config_id="awair-old",
            event_type="device.deconfigured",
        )

        assert telemetry_request.json_body["metrics"]["temp"] == 22.1
        assert (configured_event.json_body.get("event_type") or configured_event.json_body.get("type")) == "device.configured"
        assert (deconfigured_event.json_body.get("event_type") or deconfigured_event.json_body.get("type")) == "device.deconfigured"


def test_state_route_refreshes_missing_awair_snapshot_with_testkit(
    mock_core,
    monkeypatch,
) -> None:
    reset_runtime_state()

    async def fake_poll_loop(*args, **kwargs) -> None:
        return None

    async def fake_get(self, url: str):
        return _FakeAwairResponse()

    monkeypatch.setattr(config_module.telemetry_client, "core_base_url", mock_core.base_url)
    monkeypatch.setattr(config_module, "fetch_awair_data", fake_poll_loop)
    monkeypatch.setattr(config_module.httpx.AsyncClient, "get", fake_get)

    payload = build_config_payload(
        config_id="awair-1",
        container_id="runtime-123",
        integration_id="com.piphi.awair_element",
        extra={
            "device_ip": "10.0.0.40",
            "device_mac": "aa:bb:cc:dd:ee:ff",
        },
    )
    headers = build_runtime_headers(container_id="runtime-123", internal_token="secret-token")

    with TestClient(app) as client:
        config_response = client.post("/config", json=payload, headers=headers)
        assert config_response.status_code == 200
        wait_for(lambda: len(mock_core.telemetry_requests) >= 1)

        mock_core.reset()
        registry.state_snapshots.clear()

        state_response = client.get("/state")
        assert state_response.status_code == 200
        assert state_response.json()["device_id"] == "awair-1"
        assert state_response.json()["state"]["temp"] == 22.1
        assert state_response.json()["state"]["temperature"] == 22.1
        assert state_response.json()["state"]["humid"] == 47.5
        assert state_response.json()["state"]["humidity"] == 47.5
        assert state_response.json()["state"]["dew_point"] == 14.65
        assert state_response.json()["state"]["dew_pt"] == 14.65
        assert state_response.json()["state"]["co2_est_baseline"] == 36389

        wait_for(lambda: len(mock_core.telemetry_requests) >= 1)
        telemetry_request = assert_telemetry_sent(mock_core, device_id="awair-1")
        assert telemetry_request.json_body["metrics"]["temp"] == 22.1
        assert telemetry_request.json_body["metrics"]["temperature"] == 22.1


def test_deconfigure_sends_awair_event_and_removes_entry(
    mock_core,
    monkeypatch,
) -> None:
    reset_runtime_state()

    async def fake_poll_loop(*args, **kwargs) -> None:
        return None

    async def fake_get(self, url: str):
        return _FakeAwairResponse()

    monkeypatch.setattr(config_module.telemetry_client, "core_base_url", mock_core.base_url)
    monkeypatch.setattr(config_module.event_client, "core_base_url", mock_core.base_url)
    monkeypatch.setattr(config_module, "fetch_awair_data", fake_poll_loop)
    monkeypatch.setattr(config_module.httpx.AsyncClient, "get", fake_get)

    payload = build_config_payload(
        config_id="awair-1",
        container_id="runtime-123",
        integration_id="com.piphi.awair_element",
        extra={
            "device_ip": "10.0.0.40",
            "device_mac": "aa:bb:cc:dd:ee:ff",
        },
    )
    headers = build_runtime_headers(container_id="runtime-123", internal_token="secret-token")

    with TestClient(app) as client:
        config_response = client.post("/config", json=payload, headers=headers)
        assert config_response.status_code == 200
        wait_for(lambda: len(mock_core.event_requests) >= 1)

        mock_core.reset()

        deconfigure_response = client.post("/deconfigure", json={"config": {"id": "awair-1"}})
        assert deconfigure_response.status_code == 200
        assert deconfigure_response.json()["removed"] is True
        assert registry.get("awair-1") is None

        wait_for(lambda: len(mock_core.event_requests) >= 1)
        event_request = assert_event_sent(
            mock_core,
            device_id="awair-1",
            config_id="awair-1",
            event_type="device.deconfigured",
        )
        assert (event_request.json_body.get("event_type") or event_request.json_body.get("type")) == "device.deconfigured"


def test_state_returns_404_when_no_awair_device_is_configured() -> None:
    reset_runtime_state()

    with TestClient(app) as client:
        response = client.get("/state")

    assert response.status_code == 404
    assert response.json()["detail"] == "No configured device found"


def test_deconfigure_requires_config_id_for_awair() -> None:
    reset_runtime_state()

    with TestClient(app) as client:
        response = client.post("/deconfigure", json={"config": {}})

    assert response.status_code == 400
    assert response.json()["detail"] == "Missing config id"


def test_awair_events_route_round_trip() -> None:
    reset_runtime_state()

    event_payload = {
        "event_type": "awair.manual.note",
        "source": "test-suite",
        "payload": {"message": "hello"},
    }

    with TestClient(app) as client:
        ingest_response = client.post("/events", json=event_payload)
        list_response = client.get("/events")

    assert ingest_response.status_code == 200
    assert ingest_response.json()["event"]["event_type"] == "awair.manual.note"
    assert list_response.status_code == 200
    assert list_response.json()["events"][-1]["event_type"] == "awair.manual.note"


def test_awair_command_refresh_returns_404_without_primary_device(monkeypatch) -> None:
    reset_runtime_state()
    monkeypatch.setattr(command_module, "load_manifest", lambda: {"commands": {"refresh": {}}})

    with TestClient(app) as client:
        response = client.post("/command", json={"command": "refresh"})

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "missing_target"


def test_awair_command_unknown_returns_404(monkeypatch) -> None:
    reset_runtime_state()
    monkeypatch.setattr(command_module, "load_manifest", lambda: {"commands": {"refresh": {}}})

    with TestClient(app) as client:
        response = client.post("/command", json={"command": "do_something_else"})

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "unsupported_command"


def test_awair_config_apply_still_succeeds_when_initial_refresh_fails(
    mock_core,
    monkeypatch,
) -> None:
    reset_runtime_state()

    async def fake_poll_loop(*args, **kwargs) -> None:
        return None

    async def fake_get(self, url: str):
        raise httpx.RequestError("network down")

    monkeypatch.setattr(config_module.event_client, "core_base_url", mock_core.base_url)
    monkeypatch.setattr(config_module, "fetch_awair_data", fake_poll_loop)
    monkeypatch.setattr(config_module.httpx.AsyncClient, "get", fake_get)

    payload = build_config_payload(
        config_id="awair-1",
        container_id="runtime-123",
        integration_id="com.piphi.awair_element",
        extra={
            "device_ip": "10.0.0.40",
            "device_mac": "aa:bb:cc:dd:ee:ff",
        },
    )
    headers = build_runtime_headers(container_id="runtime-123", internal_token="secret-token")

    with TestClient(app) as client:
        response = client.post("/config", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json()["config_id"] == "awair-1"
        wait_for(lambda: len(mock_core.event_requests) >= 1)

        configured_event = assert_event_sent(
            mock_core,
            device_id="awair-1",
            config_id="awair-1",
            event_type="device.configured",
        )

    assert registry.get("awair-1") is not None
    assert (configured_event.json_body.get("event_type") or configured_event.json_body.get("type")) == "device.configured"


@pytest.mark.parametrize("path", ["/ui", "/ui-config"])
def test_awair_ui_aliases_return_schema(path: str) -> None:
    reset_runtime_state()

    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"]["title"] == "Awair Element API Configuration"
    assert "device_ip" in payload["schema"]["properties"]
    assert "device_mac" in payload["schema"]["properties"]


@pytest.mark.parametrize("field_name", ["device_ip", "device_mac"])
def test_awair_ui_schema_contains_expected_fields(field_name: str) -> None:
    reset_runtime_state()

    with TestClient(app) as client:
        response = client.get("/ui-config")

    assert response.status_code == 200
    assert field_name in response.json()["schema"]["properties"]


@pytest.mark.parametrize("path", ["/discover", "/discovery"])
def test_awair_discovery_aliases_return_only_awair_devices(path: str, monkeypatch) -> None:
    reset_runtime_state()
    monkeypatch.setattr(
        discovery_module,
        "config",
        {
            "AWAIR-ELEM-1450C8._http._tcp.local.": {
                "addresses": ["10.0.0.83"],
                "meta": {"name": "Bedroom"},
                "port": 80,
            },
            "PiPhi Network Core._http._tcp.local.": {
                "addresses": ["10.0.0.2"],
                "meta": {"name": "Core"},
                "port": 31419,
            },
        },
    )

    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 200
    devices = response.json()["devices"]
    assert len(devices) == 1
    assert devices[0]["make"] == "Awair"
    assert devices[0]["device_ip"] == "10.0.0.83"


@pytest.mark.parametrize("path", ["/discover", "/discovery"])
def test_awair_discovery_aliases_return_empty_without_awair(path: str, monkeypatch) -> None:
    reset_runtime_state()
    monkeypatch.setattr(
        discovery_module,
        "config",
        {
            "PiPhi Network Core._http._tcp.local.": {
                "addresses": ["10.0.0.2"],
                "meta": {"name": "Core"},
                "port": 31419,
            },
        },
    )

    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.json()["devices"] == []


def test_awair_events_list_is_empty_by_default() -> None:
    reset_runtime_state()

    with TestClient(app) as client:
        response = client.get("/events")

    assert response.status_code == 200
    assert response.json()["events"] == []


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("awair.manual.note", {"message": "hello"}),
        ("awair.score.warning", {"score": 42}),
        ("awair.device.ping", {"ip": "10.0.0.40"}),
    ],
)
def test_awair_events_route_round_trip_variants(event_type: str, payload: dict[str, object]) -> None:
    reset_runtime_state()

    with TestClient(app) as client:
        ingest_response = client.post(
            "/events",
            json={
                "event_type": event_type,
                "source": "test-suite",
                "payload": payload,
            },
        )
        list_response = client.get("/events")

    assert ingest_response.status_code == 200
    assert ingest_response.json()["event"]["event_type"] == event_type
    assert list_response.status_code == 200
    assert list_response.json()["events"][-1]["payload"] == payload


@pytest.mark.parametrize("path", ["/health", "/diagnostics"])
def test_awair_health_routes_work_without_devices(path: str) -> None:
    reset_runtime_state()

    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 200


def test_awair_health_reports_configured_and_state_counts() -> None:
    reset_runtime_state()
    registry.set("awair-1", {"config_id": "awair-1", "device_id": "awair-1"})
    registry.update_state("awair-1", {"temp": 22.1})

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["metadata"]["devices_configured"] == 1
    assert response.json()["metadata"]["devices_with_state"] == 1


def test_awair_diagnostics_reports_device_ids() -> None:
    reset_runtime_state()
    registry.set("awair-1", {"config_id": "awair-1", "device_id": "awair-1"})
    registry.update_state("awair-1", {"temp": 22.1})

    with TestClient(app) as client:
        response = client.get("/diagnostics")

    assert response.status_code == 200
    diagnostics = response.json()["diagnostics"]
    assert diagnostics["configured_device_ids"] == ["awair-1"]
    assert diagnostics["devices_with_state"] == ["awair-1"]


def test_awair_entities_route_returns_manifest_sections() -> None:
    reset_runtime_state()

    with TestClient(app) as client:
        response = client.get("/entities")

    assert response.status_code == 200
    assert "entities" in response.json()
    assert "capabilities" in response.json()
    assert "commands" in response.json()


def test_awair_manifest_route_returns_identity_fields() -> None:
    reset_runtime_state()

    with TestClient(app) as client:
        response = client.get("/manifest.json")

    assert response.status_code == 200
    assert response.json()["id"]
    assert response.json()["name"]
    assert response.json()["version"]


def test_awair_manifest_exposes_all_latest_air_data_capabilities() -> None:
    manifest = json.loads((PACKAGE_SRC / "manifest.json").read_text(encoding="utf-8"))
    capabilities = set(manifest["capabilities"])
    entity_capabilities = {
        capability
        for entity in manifest["entities"]
        for capability in entity.get("capabilities", [])
    }

    expected = {
        "temperature",
        "dew_point",
        "humidity",
        "absolute_humidity",
        "co2",
        "co2_est",
        "co2_est_baseline",
        "voc",
        "voc_baseline",
        "voc_h2_raw",
        "voc_ethanol_raw",
        "pm25",
        "pm10_est",
        "score",
    }

    assert expected <= capabilities
    assert expected <= entity_capabilities


def test_awair_behaviors_expose_all_numeric_metrics_as_conditions() -> None:
    behaviors = json.loads((PACKAGE_SRC / "behaviors.json").read_text(encoding="utf-8"))
    conditions = behaviors["devices"][0]["conditions"]
    runtime_fields = {condition["runtime"]["field"] for condition in conditions}

    assert {
        "temperature",
        "dew_point",
        "humidity",
        "absolute_humidity",
        "co2",
        "co2_est",
        "co2_est_baseline",
        "voc",
        "voc_baseline",
        "voc_h2_raw",
        "voc_ethanol_raw",
        "pm25",
        "pm10_est",
        "score",
    } <= runtime_fields


def test_awair_behaviors_expose_discord_webhook_action_and_templates() -> None:
    behaviors = json.loads((PACKAGE_SRC / "behaviors.json").read_text(encoding="utf-8"))
    device = behaviors["devices"][0]
    actions = {action["id"]: action for action in device["actions"]}
    template_ids = {template["id"] for template in behaviors["templates"]}

    discord_action = actions["discord_webhook"]
    assert discord_action["runtime"]["command"] == "discord_webhook"
    assert discord_action["capability"] == "notification.discord_webhook"
    assert discord_action["safety"]["riskLevel"] == "low"
    assert [param["name"] for param in discord_action["params"]] == [
        "webhook_url",
        "message",
        "username",
    ]
    assert {
        "awair_co2_discord_alert",
        "awair_particle_discord_alert",
        "awair_sleep_humidity_alert",
    } <= template_ids

    discord_templates = [
        template for template in behaviors["templates"]
        if template["id"] in {"awair_co2_discord_alert", "awair_particle_discord_alert"}
    ]
    for template in discord_templates:
        action = template["config"]["actions"][0]
        assert action["action"] == "discord_webhook"
        assert action["sourceRef"]["optionKey"] == "discord_webhook"
        assert action["parameters"]["webhook_url"] == ""

    pm25_template = next(
        template for template in behaviors["templates"]
        if template["id"] == "awair_particle_discord_alert"
    )
    assert pm25_template["config"]["conditions"]["integrationCondition"]["params"] == {
        "ug_per_m3": 35,
    }


def test_awair_command_refresh_returns_state_for_explicit_device(monkeypatch) -> None:
    reset_runtime_state()
    monkeypatch.setattr(command_module, "load_manifest", lambda: {"commands": {"refresh": {}}})
    refresh_calls: list[str] = []

    async def fake_trigger_refresh(device_id: str):
        refresh_calls.append(device_id)
        return {"device_id": device_id, "state": {"temp": 22.1}}

    monkeypatch.setattr(command_module, "trigger_refresh", fake_trigger_refresh)
    headers = {"X-PiPhi-Idempotency-Key": "awair-refresh-idempotency-1"}

    with TestClient(app) as client:
        response = client.post(
            "/command",
            json={"command": "refresh", "device_id": "awair-1"},
            headers=headers,
        )
        replay = client.post(
            "/command",
            json={"command": "refresh", "device_id": "awair-1"},
            headers=headers,
        )

    assert response.status_code == 200
    assert replay.status_code == 200
    assert response.json()["replayed"] is False
    assert replay.json()["replayed"] is True
    assert refresh_calls == ["awair-1"]
    assert response.json()["status"] == "ok"
    assert response.json()["device_id"] == "awair-1"
    assert response.json()["result"]["state"]["temp"] == 22.1


def test_awair_command_accepts_automation_runtime_contract(monkeypatch) -> None:
    reset_runtime_state()
    monkeypatch.setattr(command_module, "load_manifest", lambda: {"commands": {"refresh": {}}})

    async def fake_trigger_refresh(device_id: str):
        return {"device_id": device_id, "state": {"temp": 22.1}}

    monkeypatch.setattr(command_module, "trigger_refresh", fake_trigger_refresh)

    with TestClient(app) as client:
        response = client.post(
            "/command",
            json={
                "contract_version": "automation.runtime.command.v1",
                "command": "refresh_readings",
                "target": {"device_id": "awair-1", "config_id": "awair-config"},
                "params": {"force": True},
                "capability": "device.refresh",
                "capability_requirements": ["device.refresh"],
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["command"] == "refresh"
    assert body["contract_version"] == "automation.runtime.command.v1"
    assert body["device_id"] == "awair-1"
    assert body["params"] == {"force": True}


def test_awair_command_sends_discord_webhook(monkeypatch) -> None:
    reset_runtime_state()
    monkeypatch.setattr(command_module, "load_manifest", lambda: {"commands": {}})

    class FakeDiscordResponse:
        status_code = 204

    class FakeAsyncClient:
        requests: list[dict[str, object]] = []

        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, object]):
            self.requests.append({"url": url, "json": json, "timeout": self.timeout})
            return FakeDiscordResponse()

    monkeypatch.setattr(command_module.httpx, "AsyncClient", FakeAsyncClient)
    headers = {"X-PiPhi-Idempotency-Key": "awair-discord-idempotency-1"}

    with TestClient(app) as client:
        response = client.post(
            "/command",
            json={
                "contract_version": "automation.runtime.command.v1",
                "command": "discord_webhook",
                "target": {"device_id": "awair-1"},
                "capability": "notification.discord_webhook",
                "capability_requirements": ["notification.discord_webhook"],
                "params": {
                    "webhook_url": "https://discord.com/api/webhooks/123/token",
                    "message": "CO2 is high",
                    "username": "PiPhi Air",
                },
            },
            headers=headers,
        )
        replay = client.post(
            "/command",
            json={
                "contract_version": "automation.runtime.command.v1",
                "command": "discord_webhook",
                "target": {"device_id": "awair-1"},
                "capability": "notification.discord_webhook",
                "capability_requirements": ["notification.discord_webhook"],
                "params": {
                    "webhook_url": "https://discord.com/api/webhooks/123/token",
                    "message": "CO2 is high",
                    "username": "PiPhi Air",
                },
            },
            headers=headers,
        )

    body = response.json()
    assert response.status_code == 200
    assert replay.status_code == 200
    assert response.json()["replayed"] is False
    assert replay.json()["replayed"] is True
    assert body["ok"] is True
    assert body["command"] == "discord_webhook"
    assert body["result"]["channel"] == "discord"
    assert FakeAsyncClient.requests == [
        {
            "url": "https://discord.com/api/webhooks/123/token",
            "json": {"content": "CO2 is high", "username": "PiPhi Air"},
            "timeout": 10.0,
        }
    ]


def test_awair_command_rejects_invalid_discord_webhook_url(monkeypatch) -> None:
    reset_runtime_state()
    monkeypatch.setattr(command_module, "load_manifest", lambda: {"commands": {}})

    with TestClient(app) as client:
        response = client.post(
            "/command",
            json={
                "command": "discord_webhook",
                "capability": "notification.discord_webhook",
                "params": {
                    "webhook_url": "https://example.com/webhook",
                    "message": "CO2 is high",
                },
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_webhook_url"


def test_awair_command_rejects_unsupported_capability(monkeypatch) -> None:
    reset_runtime_state()
    monkeypatch.setattr(command_module, "load_manifest", lambda: {"commands": {"refresh": {}}})

    with TestClient(app) as client:
        response = client.post(
            "/command",
            json={
                "command": "refresh",
                "target": {"device_id": "awair-1"},
                "capability": "switch.power",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "unsupported_capability"


def test_awair_state_returns_404_for_unknown_explicit_device() -> None:
    reset_runtime_state()

    with TestClient(app) as client:
        response = client.get("/state", params={"device_id": "missing-device"})

    assert response.status_code == 404
    assert "No state available" in response.json()["detail"]


def test_awair_deconfigure_returns_false_when_device_missing() -> None:
    reset_runtime_state()

    with TestClient(app) as client:
        response = client.post("/deconfigure", json={"config": {"id": "missing-device"}})

    assert response.status_code == 200
    assert response.json()["removed"] is False


def test_awair_refresh_route_returns_state(monkeypatch) -> None:
    reset_runtime_state()

    async def fake_trigger_refresh(device_id: str):
        return {"device_id": device_id, "state": {"temp": 22.1}}

    monkeypatch.setattr(config_module, "trigger_refresh", fake_trigger_refresh)

    with TestClient(app) as client:
        response = client.post("/refresh", json={"config": {"id": "awair-1"}})

    assert response.status_code == 200
    assert response.json()["device_id"] == "awair-1"
    assert response.json()["state"]["temp"] == 22.1
