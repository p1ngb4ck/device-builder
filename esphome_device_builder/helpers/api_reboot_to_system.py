from __future__ import annotations

import asyncio
import sys
from typing import Any

from aioesphomeapi import APIClient

from .json import JSONDecodeError, dumps_str, loads

_ACTION = "reboot_to_ota_system"
_SYSTEM_MARKER = "ota_system_running"
_CONNECT_TIMEOUT = 10.0
_REBOOT_TIMEOUT = 20.0


async def _reboot(request: dict[str, Any]) -> dict[str, str]:
    client = APIClient(
        request["address"],
        request["port"],
        None,
        client_info="esphome-device-builder",
        noise_psk=request.get("noise_psk") or None,
        addresses=request.get("addresses") or None,
    )
    stopped = asyncio.Event()

    async def _on_stop(expected_disconnect: bool) -> None:
        stopped.set()

    try:
        await asyncio.wait_for(client.connect(on_stop=_on_stop, login=False), _CONNECT_TIMEOUT)
        _, services = await client.list_entities_services()
        names = {s.name for s in services}
        if _SYSTEM_MARKER in names:
            return {"outcome": "in_system"}
        service = next((s for s in services if s.name == _ACTION), None)
        if service is None:
            return {"outcome": "no_action"}
        await client.execute_service(service, {})
        await asyncio.wait_for(stopped.wait(), _REBOOT_TIMEOUT)
        return {"outcome": "rebooted"}
    finally:
        await client.disconnect(force=True)


def main() -> int:
    try:
        request = loads(sys.stdin.read())
    except (JSONDecodeError, ValueError) as exc:
        sys.stdout.write(dumps_str({"error": f"bad request: {exc!r}"}))
        return 2
    try:
        result = asyncio.run(_reboot(request))
    except Exception as exc:
        sys.stdout.write(dumps_str({"error": repr(exc)}))
        return 1
    sys.stdout.write(dumps_str(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
