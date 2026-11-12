from fastapi import APIRouter

from com_piphi_await_element.lib.manifest import load_manifest
from com_piphi_await_element.lib.store import registry

router = APIRouter(tags=["entities"])


def _manifest_entity_capabilities(manifest: dict) -> list[str]:
    capabilities: set[str] = set()
    entities = manifest.get("entities") if isinstance(manifest.get("entities"), list) else []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_capabilities = entity.get("capabilities") if isinstance(entity.get("capabilities"), list) else []
        for capability_id in entity_capabilities:
            token = str(capability_id or "").strip()
            if token:
                capabilities.add(token)
    return sorted(capabilities)


@router.get('/entities')
async def get_entities():
    manifest = load_manifest()
    entity_capabilities = _manifest_entity_capabilities(manifest)
    entities: list[dict] = []

    for device_id in registry.ids():
        entry = registry.get(device_id) or {}
        latest_state = entry.get("latest_state") if isinstance(entry.get("latest_state"), dict) else {}
        name = (
            str(latest_state.get("device_name") or "").strip()
            or str(latest_state.get("friendly_name") or "").strip()
            or str(entry.get("device_ip") or "").strip()
            or str(device_id)
        )
        entities.append(
            {
                "id": str(entry.get("device_id") or device_id),
                "name": name,
                "config_id": str(entry.get("config_id") or "").strip() or None,
                "device_id": str(entry.get("device_id") or device_id),
                "device_class": "air-quality-sensor",
                "entity_type": "sensor",
                "capabilities": entity_capabilities,
                "dashboard": {
                    "default_widget": "stat",
                    "recommended_widgets": ["stat", "gauge", "line-chart"],
                },
                "device_ip": entry.get("device_ip"),
                "device_mac": entry.get("device_mac"),
                "latest_state": latest_state,
            }
        )

    return {
        "entities": entities,
        "capabilities": manifest.get("capabilities", {}),
        "commands": manifest.get("commands", {}),
    }
