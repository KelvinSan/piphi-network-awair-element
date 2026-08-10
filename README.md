# com.piphi.awair_element

PiPhi integration for Awair Element devices over the local network.

This runtime uses the published Python runtime kit and exposes the standard
PiPhi integration contract for discovery, config, state, telemetry, events, and
UI config.

## What this integration does

- discovers Awair Element devices on the local network
- configures devices by host identity
- exposes PiPhi contract routes for health, discovery, state, entities, commands, and events
- publishes telemetry back to PiPhi Core through `piphi-runtime-kit-python`
- supports config snapshot rehydrate through `/configs/sync`

## Runtime SDK and testkit

- runtime SDK: `piphi-runtime-kit-python==0.6.0`
- test helper: `piphi-runtime-testkit-python==0.1.1`

Both the runtime SDK and the Python testkit are installed from PyPI, which
keeps local development and GitHub release CI aligned on the same published
dependency set.

## Local development

Install dependencies:

```bash
pdm install -G dev
```

Run the runtime:

```bash
pdm run python -m com_piphi_await_element.app
```

Run tests:

```bash
pdm run pytest -q
```

The integration API defaults to `http://127.0.0.1:3665`.

## Important routes

- `GET /health`
- `POST /discover`
- `GET /entities`
- `GET /state`
- `POST /command`
- `POST /config`
- `POST /configs/sync`
- `POST /deconfigure`
- `GET /ui-config`
- `GET /events`

## Device model

This integration is focused on Awair Element devices reachable through the
local API / local network path. The configuration identity field in the
manifest is `host`, which keeps config matching straightforward when the device
is rediscovered later.

## Project layout

- `src/manifest.json` integration manifest
- `src/com_piphi_await_element/app.py` FastAPI entrypoint
- `src/com_piphi_await_element/contract/` PiPhi contract routes
- `src/com_piphi_await_element/lib/` Awair-specific helpers and schemas
- `tests/` integration tests using the Python testkit

## Notes

- The runtime follows the standard PiPhi Python integration contract.
- Config, state, and event flows are covered by testkit-backed tests in this repo.
- Example IDs, hosts, and tokens in development logs or tests should be treated as placeholders.
