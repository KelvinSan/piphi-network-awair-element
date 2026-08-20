from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
import httpx
from piphi_runtime_kit_python import (
    AutomationActionRequest,
    AutomationActionResult,
    AutomationRegistry,
    SQLiteAutomationIdempotencyStore,
)
from piphi_runtime_kit_python.fastapi import dispatch_automation_action_from_fastapi

from com_piphi_await_element.contract.config.routes import trigger_refresh
from com_piphi_await_element.lib.manifest import load_manifest
from com_piphi_await_element.lib.schemas import CommandRequest
from com_piphi_await_element.lib.store import get_primary_device, registry


router = APIRouter(tags=['command'])
_ledger_path = Path(
    os.getenv(
        'PIPHI_AUTOMATION_LEDGER_PATH',
        '/.piphinetwork/automation-actions.sqlite3',
    )
)
automation_registry = AutomationRegistry(
    idempotency_store=SQLiteAutomationIdempotencyStore(_ledger_path)
)

COMMAND_ALIASES = {
    'refresh_readings': 'refresh',
    'device.refresh': 'refresh',
}
IMPLEMENTED_COMMANDS = {'refresh', 'notify', 'discord_webhook'}
DISCORD_WEBHOOK_PREFIXES = (
    'https://discord.com/api/webhooks/',
    'https://discordapp.com/api/webhooks/',
)


def _command_name(payload: CommandRequest) -> str:
    return COMMAND_ALIASES.get(payload.command.strip(), payload.command.strip())


def _structured_error(status_code: int, code: str, message: str):
    raise HTTPException(
        status_code=status_code,
        detail={
            'ok': False,
            'error': code,
            'message': message,
        },
    )


def _target_value(payload: CommandRequest, key: str) -> str | None:
    value = payload.target.get(key)
    return str(value).strip() if value is not None and str(value).strip() else None


def _resolve_device_id(payload: CommandRequest) -> str:
    device_id = payload.device_id or _target_value(payload, 'device_id')
    if device_id:
        return device_id
    config_id = payload.config_id or _target_value(payload, 'config_id')
    if config_id:
        configured = registry.get(config_id)
        if configured is not None:
            return configured['device_id']
    primary_device = get_primary_device()
    if primary_device is None:
        _structured_error(404, 'missing_target', 'No configured Awair Element device found')
    return primary_device['device_id']


def _validate_capability(payload: CommandRequest) -> None:
    requested = {item for item in [payload.capability, *payload.capability_requirements] if item}
    unsupported = requested - {
        'device.refresh',
        'refresh',
        'air_quality.readings',
        'notification.send',
        'notification.discord_webhook',
    }
    if unsupported:
        unsupported_text = sorted(unsupported)[0]
        _structured_error(
            400,
            'unsupported_capability',
            f"Awair Element does not support capability '{unsupported_text}'",
        )


def _command_params(payload: CommandRequest) -> dict:
    return payload.params or payload.args or {}


def _discord_webhook_url(params: dict) -> str:
    webhook_url = str(params.get('webhook_url') or params.get('webhookUrl') or '').strip()
    if not webhook_url:
        _structured_error(400, 'missing_webhook_url', 'Discord webhook URL is required')
    if not webhook_url.startswith(DISCORD_WEBHOOK_PREFIXES):
        _structured_error(400, 'invalid_webhook_url', 'Discord webhook URL must be a Discord webhook URL')
    return webhook_url


async def _send_discord_webhook(params: dict) -> dict:
    webhook_url = _discord_webhook_url(params)
    message = str(params.get('message') or 'Awair Element alert').strip() or 'Awair Element alert'
    username = str(params.get('username') or 'PiPhi Network').strip() or 'PiPhi Network'
    payload = {'content': message, 'username': username}
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(webhook_url, json=payload)
    if response.status_code >= 400:
        _structured_error(
            502,
            'discord_webhook_failed',
            f'Discord webhook returned HTTP {response.status_code}',
        )
    return {
        'delivered': True,
        'channel': 'discord',
        'message': message,
    }


async def _execute_registered_command(
    action_request: AutomationActionRequest,
) -> AutomationActionResult:
    extras = action_request.model_extra or {}
    target = extras.get('target') if isinstance(extras.get('target'), dict) else {}
    common_result: dict[str, Any] = {
        'ok': True,
        'status': 'ok',
        'command': action_request.command,
        'contract_version': extras.get('contract_version'),
        'device_id': action_request.device_id,
        'target': target,
        'params': action_request.args,
    }
    if action_request.command == 'discord_webhook':
        try:
            delivered = await _send_discord_webhook(action_request.args)
        except HTTPException as exc:
            return AutomationActionResult.failure(
                str(exc.detail),
                retryable=exc.status_code >= 500,
                metadata={'status_code': exc.status_code},
            )
        except Exception as exc:
            return AutomationActionResult.failure(
                'Discord delivery outcome is unknown; review before retrying',
                retryable=False,
                metadata={
                    'status_code': 503,
                    'delivery_status': 'ambiguous',
                    'error_type': type(exc).__name__,
                },
            )
        return AutomationActionResult.success({**common_result, 'result': delivered})
    if action_request.command == 'notify':
        return AutomationActionResult.success(
            {
                **common_result,
                'result': {
                    'delivered': True,
                    'channel': action_request.args.get('channel', 'in_app'),
                    'message': action_request.args.get('message', ''),
                },
            }
        )
    try:
        refreshed_state = await trigger_refresh(str(action_request.device_id or ''))
    except HTTPException as exc:
        return AutomationActionResult.failure(
            str(exc.detail),
            retryable=exc.status_code >= 500,
            metadata={'status_code': exc.status_code},
        )
    return AutomationActionResult.success(
        {**common_result, 'result': refreshed_state}
    )


for _registered_command in sorted(IMPLEMENTED_COMMANDS):
    automation_registry.action(_registered_command)(_execute_registered_command)


@router.post('/command')
async def execute_command(payload: CommandRequest, request: Request):
    manifest = load_manifest()
    commands = manifest.get('commands', {})
    command = _command_name(payload)
    if command not in commands and command not in IMPLEMENTED_COMMANDS:
        _structured_error(400, 'unsupported_command', f"Unsupported command: {payload.command}")
    _validate_capability(payload)
    params = _command_params(payload)
    if command == 'discord_webhook':
        _discord_webhook_url(params)
    device_id = (
        payload.device_id or _target_value(payload, 'device_id')
        if command in {'discord_webhook', 'notify'}
        else _resolve_device_id(payload)
    )
    result = await dispatch_automation_action_from_fastapi(
        automation_registry,
        request,
        {
            **payload.model_dump(mode='python'),
            'command': command,
            'device_id': device_id,
            'args': params,
        },
    )
    if not result.ok:
        detail: Any = result.error
        if result.metadata.get('delivery_status') == 'ambiguous':
            detail = {
                'ok': False,
                'error': 'delivery_ambiguous',
                'message': result.error,
            }
        raise HTTPException(
            status_code=int(result.metadata.get('status_code') or 503),
            detail=detail,
        )
    return {**result.result, 'replayed': result.replayed}
