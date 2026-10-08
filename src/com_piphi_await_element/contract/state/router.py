from fastapi import APIRouter, HTTPException, Query
from piphi_runtime_kit_python import RuntimeStateService
from com_piphi_await_element.contract.config.routes import trigger_refresh
from com_piphi_await_element.lib.store import get_primary_device, registry


router = APIRouter(tags=['state'])
state_service = RuntimeStateService(registry)


async def _refresh_all_devices() -> None:
    for configured_device_id in registry.ids():
        await trigger_refresh(configured_device_id)


state_service.provide(_refresh_all_devices, source="Awair local API")


@router.get('/state')
async def get_state(
    device_id: str | None = Query(default=None),
    refresh: bool = Query(default=False),
    refresh_request_id: str | None = Query(default=None),
):
    if refresh:
        try:
            return await state_service.response(
                refresh=True,
                refresh_request_id=refresh_request_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    resolved_device_id = device_id
    if resolved_device_id is None:
        primary_device = get_primary_device()
        if primary_device is None:
            raise HTTPException(status_code=404, detail='No configured device found')
        resolved_device_id = primary_device['device_id']
    if resolved_device_id not in registry.state_snapshots and registry.get(resolved_device_id) is not None:
        await trigger_refresh(resolved_device_id)
    latest_state = registry.state_snapshots.get(resolved_device_id)
    if latest_state is None:
        raise HTTPException(status_code=404, detail=f"No state available for device '{resolved_device_id}'")
    return latest_state
