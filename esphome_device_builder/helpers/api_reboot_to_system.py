from __future__ import annotations

import asyncio
import sys
from typing import Any

from aioesphomeapi import APIClient

from .json import JSONDecodeError, dumps_str, loads

_ACTION = "reboot_to_ota_system"
_SYSTEM_MARKER = "ota_system_running"
_ELF_ACTION = "ota_system_elf"
_MAIN_ACTION = "reboot_to_main"
_CONNECT_TIMEOUT = 10.0
_REBOOT_TIMEOUT = 20.0
_SYSTEM_TIMEOUT = 180.0
_RETRY_DELAY = 3.0


def _client(request: dict[str, Any]) -> APIClient:
    return APIClient(
        request["address"],
        request["port"],
        None,
        client_info="esphome-device-builder",
        noise_psk=request.get("noise_psk") or None,
        addresses=request.get("addresses") or None,
    )


async def _system_running(request: dict[str, Any]) -> bool:
    client = _client(request)
    try:
        await asyncio.wait_for(client.connect(login=False), _CONNECT_TIMEOUT)
        _, services = await client.list_entities_services()
        return any(s.name == _SYSTEM_MARKER for s in services)
    finally:
        await client.disconnect(force=True)


async def _wait_for_system(request: dict[str, Any]) -> bool:
    deadline = asyncio.get_running_loop().time() + _SYSTEM_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await _system_running(request):
                return True
        except Exception:
            pass
        await asyncio.sleep(_RETRY_DELAY)
    return False


async def _query(request: dict[str, Any]) -> dict[str, Any]:
    client = _client(request)
    try:
        await asyncio.wait_for(client.connect(login=False), _CONNECT_TIMEOUT)
        _, services = await client.list_entities_services()
        names = {s.name for s in services}
        result: dict[str, Any] = {
            "in_system": _SYSTEM_MARKER in names,
            "reboot_action": _ACTION in names,
        }
        elf = next((s for s in services if s.name == _ELF_ACTION), None)
        if elf is not None:
            response = await client.execute_service(elf, {}, return_response=True)
            if response is not None and response.success and response.response_data:
                answer = loads(response.response_data)
                for key in ("elf_sha256", "main_elf_sha256"):
                    if isinstance(answer.get(key), str):
                        result[key] = answer[key]
                result["self_update"] = answer.get("self_update") is True
        return result
    finally:
        await client.disconnect(force=True)


async def _wait(request: dict[str, Any]) -> dict[str, str]:
    expect = request.get("expect_elf") or ""
    deadline = asyncio.get_running_loop().time() + _SYSTEM_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        try:
            state = await _query(request)
            if state.get("in_system") and (not expect or state.get("elf_sha256") == expect):
                return {"outcome": "in_system"}
        except Exception:
            pass
        await asyncio.sleep(_RETRY_DELAY)
    return {"outcome": "system_not_up"}


async def _reboot_main(request: dict[str, Any]) -> dict[str, str]:
    client = _client(request)
    try:
        await asyncio.wait_for(client.connect(login=False), _CONNECT_TIMEOUT)
        _, services = await client.list_entities_services()
        service = next((s for s in services if s.name == _MAIN_ACTION), None)
        if service is None:
            return {"outcome": "no_action"}
        await client.execute_service(service, {})
        return {"outcome": "rebooting"}
    finally:
        await client.disconnect(force=True)


async def _reboot(request: dict[str, Any]) -> dict[str, str]:
    client = _client(request)
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
    finally:
        await client.disconnect(force=True)
    if await _wait_for_system(request):
        return {"outcome": "in_system"}
    return {"outcome": "system_not_up"}


def main() -> int:
    try:
        request = loads(sys.stdin.read())
    except (JSONDecodeError, ValueError) as exc:
        sys.stdout.write(dumps_str({"error": f"bad request: {exc!r}"}))
        return 2
    try:
        modes = {"query": _query, "wait": _wait, "reboot_main": _reboot_main}
        mode = request.get("mode") or ("query" if request.get("query") else "reboot")
        result = asyncio.run(modes.get(mode, _reboot)(request))
    except Exception as exc:
        sys.stdout.write(dumps_str({"error": repr(exc)}))
        return 1
    sys.stdout.write(dumps_str(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
