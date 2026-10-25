"""
Out-of-process Native API device-info fetch.

Run as ``python -m esphome_device_builder.helpers.api_device_info``: reads
one JSON request from stdin, connects to the device over the ESPHome Native
API, and writes its name + MAC address + ESPHome version as JSON to stdout.
Running ``aioesphomeapi`` in a short-lived child keeps the heavy client out
of the long-running dashboard process. Any failure exits non-zero with no
stdout.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from .json import JSONDecodeError, dumps_str, loads

# Connect + device_info round-trip budget; the parent enforces its own
# (larger) subprocess timeout as a backstop.
_CONNECT_TIMEOUT = 10.0
_CHIP_REVISION_ACTION = "chip_revision"


async def _fetch(request: dict[str, Any]) -> dict[str, Any]:
    # Import inside the try-wrapped path (not module top) so an import-time
    # failure of aioesphomeapi / a transitive dep is caught by main() and
    # reported via the ``{"error": ...}`` channel rather than dying silently
    # before main() with stderr discarded by the parent.
    from aioesphomeapi import APIClient  # noqa: PLC0415

    client = APIClient(
        request["address"],
        request["port"],
        None,
        client_info="esphome-device-builder",
        noise_psk=request.get("noise_psk") or None,
        addresses=request.get("addresses") or None,
    )
    revision = None
    try:
        await client.connect(login=False)
        info = await client.device_info()
        if request.get("chip_revision"):
            revision = await _chip_revision(client)
    finally:
        # ``disconnect`` on an unconnected client is a no-op, so it's safe even
        # when ``connect`` itself raised.
        await client.disconnect(force=True)
    result: dict[str, Any] = {
        "name": info.name or "",
        "mac_address": info.mac_address or "",
        "esphome_version": info.esphome_version or "",
    }
    if revision is not None:
        result["chip_revision"] = revision
    return result


async def _chip_revision(client: Any) -> int | None:
    try:
        _, services = await client.list_entities_services()
        service = next((s for s in services if s.name == _CHIP_REVISION_ACTION), None)
        if service is None:
            return None
        response = await client.execute_service(service, {}, return_response=True)
        if response is None or not response.success or not response.response_data:
            return None
        revision = loads(response.response_data).get("revision")
    except Exception:
        return None
    return revision if isinstance(revision, int) and not isinstance(revision, bool) else None


def main() -> int:
    try:
        request = loads(sys.stdin.read())
    except (JSONDecodeError, ValueError) as exc:
        # Mirror the connect-failure channel: report why the request was
        # rejected on stdout so the parent's reason-logging path sees it.
        sys.stdout.write(dumps_str({"error": f"bad request: {exc!r}"}))
        return 2
    try:
        result = asyncio.run(asyncio.wait_for(_fetch(request), _CONNECT_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 — surface the reason, then exit non-zero
        # stderr is discarded by the parent (merge_stderr=False); carry the
        # failure reason on stdout so the parent can log why the probe failed.
        sys.stdout.write(dumps_str({"error": repr(exc)}))
        return 1
    sys.stdout.write(dumps_str(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
