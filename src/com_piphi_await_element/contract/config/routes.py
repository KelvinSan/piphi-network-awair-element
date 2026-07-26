import asyncio
import random
from typing import Any, Dict

import httpx
from fastapi import APIRouter, HTTPException, Request
from piphi_runtime_kit_python import (
    ConfigSyncCoordinator,
    EventClient,
    RuntimeConfigApplyResponse,
    RuntimeConfigRemoveResponse,
    TelemetryClient,
    build_config_apply_response,
    build_config_remove_response,
    create_tracked_task,
    format_config_apply_log,
    format_runtime_auth_sync_log,
    resolve_core_base_url,
    schedule_event_delivery,
    schedule_telemetry_delivery,
)
from piphi_runtime_kit_python.fastapi import (
    get_payload_container_id,
    sync_runtime_auth_from_fastapi_payload,
)

from com_piphi_await_element.lib.logging import log_event
from com_piphi_await_element.lib.manifest import load_manifest
from com_piphi_await_element.lib.schemas import (
    AwairElement,
    DeconfigureConfig,
    RuntimeConfigSnapshot,
    RuntimeConfigSyncResponse,
)
from com_piphi_await_element.lib.store import (
    append_event,
    get_runtime_context,
    registry,
    update_device_state,
)

config_router = APIRouter(tags=["config"])

CORE_BASE_URL = resolve_core_base_url("http://127.0.0.1:31419")
TELEMETRY_URL_TIMEOUT_SECONDS = 4.0
EVENT_REQUEST_TIMEOUT_SECONDS = 3.0
AWAIR_LATEST_AIR_DATA_PATH = "/air-data/latest"
POLL_INTERVAL_SECONDS = 10

runtime_context = get_runtime_context()
manifest = load_manifest()
telemetry_client = TelemetryClient(
    process_state=runtime_context.process_state,
    core_base_url=CORE_BASE_URL,
    timeout_seconds=TELEMETRY_URL_TIMEOUT_SECONDS,
)
event_client = EventClient(
    process_state=runtime_context.process_state,
    core_base_url=CORE_BASE_URL,
    timeout_seconds=EVENT_REQUEST_TIMEOUT_SECONDS,
)
config_sync = ConfigSyncCoordinator(process_state=runtime_context.process_state)


def _sync_runtime_auth_from_request(request: Request, payload: Any | None = None) -> None:
    parsed_headers = sync_runtime_auth_from_fastapi_payload(
        runtime_context,
        request,
        payload,
    )
    log_event(
        "runtime_internal_auth",
        message=format_runtime_auth_sync_log(
            parsed_headers,
            payload_container_id=get_payload_container_id(payload),
        ),
    )


def _build_telemetry_payload(telemetry_data: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(telemetry_data)
    payload["power_on"] = random.choice(["on", "off"])
    return payload


AWAIR_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "temp": ("temperature",),
    "humid": ("humidity",),
    "dew_point": ("dew_pt",),
    "abs_humid": ("absolute_humidity",),
}


def _extract_awair_metrics(awair_response: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key, value in awair_response.items():
        payload[key] = value
        for alias in AWAIR_METRIC_ALIASES.get(key, ()):
            payload[alias] = value
    return payload


async def fetch_awair_state(
    ip_address: str,
    device_id: str,
    container_id: str | None = None,
) -> Dict[str, Any]:
    device_entry = registry.get(device_id) or {}
    previous_state = device_entry.get("latest_state") or {}
    async with httpx.AsyncClient() as client:
        response = await client.get(f"http://{ip_address}{AWAIR_LATEST_AIR_DATA_PATH}")
        response.raise_for_status()

    payload = _extract_awair_metrics(response.json())
    latest_state = update_device_state(device_id=device_id, state=payload)

    if container_id:
        schedule_telemetry_delivery(
            process_state=runtime_context.process_state,
            telemetry_client=telemetry_client,
            auth_context=runtime_context.auth,
            config_id=str(device_entry.get("config_id") or device_id),
            device_id=device_id,
            metrics=_build_telemetry_payload(payload),
            container_id=container_id,
            units={
                "pm25": "ug/m3",
                "pm10_est": "ug/m3",
                "score": "%",
                "co2": "ppm",
                "co2_est": "ppm",
                "co2_est_baseline": "ppm",
                "voc": "ppb",
                "voc_baseline": "ppb",
                "voc_h2_raw": "raw",
                "voc_ethanol_raw": "raw",
                "humid": "%",
                "humidity": "%",
                "abs_humid": "g/m3",
                "absolute_humidity": "g/m3",
                "temp": "°C",
                "temperature": "°C",
                "dew_point": "°C",
                "dew_pt": "°C",
            },
            on_skipped=lambda reason, details: log_event(
                "telemetry_send_skipped",
                level="warning",
                reason=reason,
                device_id=details.get("device_id") or device_id,
            ),
            on_error=lambda exc, details: log_event(
                "telemetry_send_unexpected_error",
                level="error",
                device_id=details.get("device_id") or device_id,
                error=str(exc),
            ),
        )
        device_entry = registry.get(device_id) or {}
        if device_entry:
            previous_score = previous_state.get("score")
            current_score = payload.get("score")
            if previous_score is not None and current_score is not None and previous_score != current_score:
                schedule_event_delivery(
                    process_state=runtime_context.process_state,
                    event_client=event_client,
                    auth_context=runtime_context.auth,
                    event_type="air.score.changed",
                    device=device_entry,
                    payload={
                        "device_ip": ip_address,
                        "previous_score": previous_score,
                        "current_score": current_score,
                    },
                    source="awair_runtime",
                    record_event=append_event,
                    on_skipped=lambda reason, details: log_event(
                        "event_send_skipped",
                        level="debug",
                        reason=reason,
                        event_type=details.get("event_type") or "air.score.changed",
                        device_id=details.get("device_id") or device_id,
                    ),
                    on_error=lambda exc, details: log_event(
                        "event_send_unexpected_error",
                        level="error",
                        event_type=details.get("event_type") or "air.score.changed",
                        device_id=details.get("device_id") or device_id,
                        error=str(exc),
                    ),
                )

    return latest_state


async def trigger_refresh(device_id: str) -> Dict[str, Any]:
    device = registry.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' is not configured")

    return await fetch_awair_state(
        ip_address=device["device_ip"],
        device_id=device_id,
        container_id=device.get("container_id"),
    )


async def fetch_awair_data(
    ip_address: str,
    device_id: str,
    container_id: str | None = None,
) -> None:
    while True:
        try:
            await fetch_awair_state(
                ip_address=ip_address,
                device_id=device_id,
                container_id=container_id,
            )
            log_event("telemetry_sent", device_id=device_id, ip=ip_address)
        except httpx.RequestError as exc:
            log_event("awair_poll_request_error", level="warning", device_id=device_id, error=str(exc))
        except httpx.HTTPStatusError as exc:
            log_event(
                "awair_poll_http_error",
                level="warning",
                device_id=device_id,
                status=exc.response.status_code,
            )
        except Exception:
            log_event("awair_poll_unexpected_error", level="error", device_id=device_id, exc_info=True)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def remove_device_config(device_id: str) -> bool:
    existing_poll = registry.get(device_id)
    if existing_poll and existing_poll.get("task") and not existing_poll["task"].done():
        existing_poll["task"].cancel()
        try:
            await existing_poll["task"]
        except asyncio.CancelledError:
            pass

    removed = existing_poll is not None
    if existing_poll is not None:
        schedule_event_delivery(
            process_state=runtime_context.process_state,
            event_client=event_client,
            auth_context=runtime_context.auth,
            event_type="device.deconfigured",
            device=existing_poll,
            payload={
                "device_ip": existing_poll.get("device_ip"),
                "device_mac": existing_poll.get("device_mac"),
            },
            source="awair_runtime",
            record_event=append_event,
            on_skipped=lambda reason, details: log_event(
                "event_send_skipped",
                level="debug",
                reason=reason,
                event_type=details.get("event_type") or "device.deconfigured",
                device_id=details.get("device_id") or device_id,
            ),
            on_error=lambda exc, details: log_event(
                "event_send_unexpected_error",
                level="error",
                event_type=details.get("event_type") or "device.deconfigured",
                device_id=details.get("device_id") or device_id,
                error=str(exc),
            ),
        )

    registry.remove(device_id)
    return removed


async def apply_device_config(payload: AwairElement) -> Dict[str, Any]:
    log_event("config_apply", message=format_config_apply_log(payload))
    resolved_container_id, _ = runtime_context.auth.resolve(container_id=payload.container_id)
    resolved_integration_id = payload.integration_id or manifest.get("id")

    await remove_device_config(payload.id)
    task = create_tracked_task(
        fetch_awair_data(payload.device_ip, payload.id, resolved_container_id),
        process_state=runtime_context.process_state,
    )
    registry.set(payload.id, {
        "task": task,
        "config_id": payload.config_id or payload.id,
        "container_id": resolved_container_id,
        "device_id": payload.id,
        "device_ip": payload.device_ip,
        "device_mac": payload.device_mac,
        "integration_id": resolved_integration_id,
    })

    try:
        await trigger_refresh(payload.id)
    except httpx.HTTPError:
        log_event("awair_initial_refresh_failed", level="warning", device_id=payload.id)

    schedule_event_delivery(
        process_state=runtime_context.process_state,
        event_client=event_client,
        auth_context=runtime_context.auth,
        event_type="device.configured",
        device=registry.get(payload.id) or {},
        payload={
            "device_ip": payload.device_ip,
            "device_mac": payload.device_mac,
        },
        source="awair_runtime",
        record_event=append_event,
        on_skipped=lambda reason, details: log_event(
            "event_send_skipped",
            level="debug",
            reason=reason,
            event_type=details.get("event_type") or "device.configured",
            device_id=details.get("device_id") or payload.id,
        ),
        on_error=lambda exc, details: log_event(
            "event_send_unexpected_error",
            level="error",
            event_type=details.get("event_type") or "device.configured",
            device_id=details.get("device_id") or payload.id,
            error=str(exc),
        ),
    )

    return build_config_apply_response(
        config_id=payload.config_id or payload.id,
        container_id=resolved_container_id,
        metadata={"device_ip": payload.device_ip},
    ).model_dump()


async def apply_runtime_config_snapshot(
    payload: RuntimeConfigSnapshot,
) -> RuntimeConfigSyncResponse:
    async def apply_config_with_context(config: AwairElement) -> Dict[str, Any]:
        effective_config = config.model_copy(
            update={
                "integration_id": config.integration_id or payload.integration_id,
                "container_id": config.container_id or payload.container_id,
            }
        )
        return await apply_device_config(effective_config)

    response = await config_sync.apply_snapshot(
        snapshot=payload,
        active_config_ids=registry.ids(),
        apply_config=apply_config_with_context,
        remove_config=remove_device_config,
        get_active_config_ids=registry.ids,
    )
    return RuntimeConfigSyncResponse(**response.model_dump())


@config_router.post("/config")
async def config(payload: AwairElement, request: Request) -> RuntimeConfigApplyResponse:
    _sync_runtime_auth_from_request(request, payload)
    return RuntimeConfigApplyResponse.model_validate(await apply_device_config(payload))


@config_router.post("/configs/sync", response_model=RuntimeConfigSyncResponse)
async def sync_configs(payload: RuntimeConfigSnapshot, request: Request):
    _sync_runtime_auth_from_request(request, payload)
    return await apply_runtime_config_snapshot(payload)


@config_router.post("/config/sync", response_model=RuntimeConfigSyncResponse)
async def sync_config(payload: RuntimeConfigSnapshot, request: Request):
    _sync_runtime_auth_from_request(request, payload)
    return await apply_runtime_config_snapshot(payload)


@config_router.post("/deconfigure")
async def deconfigure_device(payload: DeconfigureConfig) -> RuntimeConfigRemoveResponse:
    device_id = payload.config.get("id")
    if device_id is None:
        raise HTTPException(status_code=400, detail="Missing config id")

    removed = await remove_device_config(device_id)
    if not removed:
        log_event("deconfigure_noop", device_id=device_id)

    return build_config_remove_response(
        config_id=str(device_id),
        removed=removed,
    )


@config_router.post("/refresh")
async def refresh_device(payload: DeconfigureConfig):
    return await trigger_refresh(payload.config["id"])
