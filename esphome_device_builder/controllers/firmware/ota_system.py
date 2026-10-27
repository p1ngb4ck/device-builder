from __future__ import annotations

import asyncio
import binascii
import logging
import os
import struct
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome.storage_json import StorageJSON
from esphome.upload_targets import PortType, get_port_type

from ...helpers.async_ import run_in_executor
from ...helpers.device_yaml._tolerant_yaml import load_yaml_tolerant
from ...helpers.json import JSONDecodeError, dumps, loads
from ...helpers.sibling_cli import find_esptool_cmd
from ...helpers.storage_path import resolve_storage_path
from ...helpers.subprocess import run_subprocess_capture
from ...models import FirmwareJob, JobType
from ..devices import encryption_key_lookup
from . import factories
from .helpers import _ingest_output_line

if TYPE_CHECKING:
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)

_CONF_OTA = "ota"
_CONF_OTA_SYSTEM = "ota_system"
_SYSTEM_FILENAME = "ota_system.yaml"
_FACTORY_FILENAME = "firmware.factory.bin"
_MERGED_FILENAME = "firmware.ota_system.factory.bin"
_PARTITION_TABLE_OFFSET = 0x8000
_PARTITION_ENTRY = struct.Struct("<HBBII16sI")
_PARTITION_MAGIC = 0x50AA
_TYPE_APP = 0x00
_TYPE_DATA = 0x01
_SUBTYPE_OTA_0 = 0x10
_SUBTYPE_OTADATA = 0x00
_SUBTYPE_NVS = 0x02
_NVS_LABEL = "nvs"
_NVS_IMAGE_FILENAME = "nvs.bin"
_OTADATA_SEQ = struct.Struct("<I")
_OTADATA_CRC_OFFSET = 28
_OTA_0_SEQ = 1
_REBOOT_WORKER = "esphome_device_builder.helpers.api_reboot_to_system"
_REBOOT_TIMEOUT = 300.0
_FINGERPRINT_FILENAME = "ota_system.fingerprint"
_MAIN_FINGERPRINT_FILENAME = "main.fingerprint"
_APP_DESC_OFFSET = 32
_APP_DESC_MAGIC = 0xABCD5432
_APP_DESC_ELF_SHA256 = 144
_SHA256_SIZE = 32
_SYSTEM_CHANGED_WARNINGS = (
    "WARNING The OTA system changed, and the OTA system on the device cannot update itself.",
    "WARNING Flash the device via serial once: the full image with both systems.",
)
_UNCHANGED_FLASH_MAIN = "INFO Both systems are unchanged on the device: the main firmware is flashed"
_SYSTEM_PART_FILENAME = "firmware.ota_system.part.bin"
_OTADATA_PART_FILENAME = "firmware.otadata.bin"
_UPLOAD_SPEED_ENV = "ESPHOME_UPLOAD_SPEED"
_UPLOAD_SPEED = "460800"
_SYSTEM_NOT_UP_ERROR = "ERROR The device did not come up in the OTA system after the restart"
_MIGRATE_WARNINGS = (
    "WARNING Migrating an official ESPHome install to the OTA system via OTA.",
    "WARNING ESPHome does not consider the partition table update via OTA stable.",
    "WARNING Back up the device before; if this fails, flash the device via serial.",
)

RunStep = Callable[..., Awaitable[tuple[int, bool, bool]]]


@dataclass(frozen=True)
class OtaSystem:
    configuration: str


def system_configuration(configuration: str) -> str:
    path = Path(configuration)
    return (path.parent / path.stem / "ota" / _SYSTEM_FILENAME).as_posix()


def parse_ota_system(yaml_content: str, configuration: str) -> OtaSystem | None:
    config = load_yaml_tolerant(yaml_content)
    entries = config.get(_CONF_OTA) if isinstance(config, dict) else None
    for entry in entries if isinstance(entries, list) else ():
        system = entry.get(_CONF_OTA_SYSTEM) if isinstance(entry, dict) else None
        if isinstance(system, dict):
            return OtaSystem(configuration=system_configuration(configuration))
    return None


async def detect(controller: FirmwareController, configuration: str) -> OtaSystem | None:
    path = await run_in_executor(controller._db.settings.rel_path, configuration)
    try:
        content = await run_in_executor(partial(path.read_text, encoding="utf-8"))
    except OSError:
        return None
    return parse_ota_system(content, configuration)


async def enqueue_compile_chain(
    controller: FirmwareController,
    *,
    configuration: str,
    system: OtaSystem,
    force_local: bool = False,
) -> FirmwareJob:
    compile_job = _create_compile(
        controller, configuration, system, skip_bootloader=False, force_local=force_local
    )
    await factories.commit_chain(controller, compile_job, supersede_configuration=configuration)
    return compile_job


async def enqueue_install_chain(
    controller: FirmwareController,
    *,
    configuration: str,
    port: str,
    system: OtaSystem,
    force_local: bool = False,
) -> FirmwareJob:
    compile_job = _create_compile(
        controller,
        configuration,
        system,
        skip_bootloader=factories._is_ota_app_flash(port, flash_bootloader=False),
        force_local=force_local,
    )
    factories.mark_chip_probe(compile_job, port)
    upload = factories.create_job(
        controller, configuration, JobType.UPLOAD, port=port, depends_on=compile_job.job_id
    )
    upload.ota_system = system.configuration
    await factories.commit_chain(
        controller, compile_job, upload, supersede_configuration=configuration
    )
    return compile_job


def _create_compile(
    controller: FirmwareController,
    configuration: str,
    system: OtaSystem,
    *,
    skip_bootloader: bool,
    force_local: bool,
) -> FirmwareJob:
    compile_job = factories.create_job(
        controller,
        configuration,
        JobType.COMPILE,
        build_source=controller._resolve_install_source(force_local=force_local),
        skip_bootloader=skip_bootloader,
    )
    compile_job.ota_system = system.configuration
    return compile_job


async def enqueue_queued_upload(controller: FirmwareController, job: FirmwareJob) -> None:
    if (system := await detect(controller, job.configuration)) is None:
        await controller._enqueue(job)
        return
    await enqueue_upload_chain(controller, job, system)


async def enqueue_upload_chain(
    controller: FirmwareController, job: FirmwareJob, system: OtaSystem
) -> FirmwareJob:
    compile_job = _create_compile(
        controller,
        job.configuration,
        system,
        skip_bootloader=factories._is_ota_app_flash(job.port, flash_bootloader=False),
        force_local=False,
    )
    factories.mark_chip_probe(compile_job, job.port)
    job.ota_system = system.configuration
    job.depends_on = compile_job.job_id
    await factories.commit_chain(
        controller, compile_job, job, supersede_configuration=job.configuration
    )
    return job


async def run_compile(
    controller: FirmwareController, job: FirmwareJob, run: RunStep
) -> tuple[int, bool, bool]:
    await generate_system(controller, job, job.ota_system)
    fingerprint = await run_in_executor(_written_fingerprint, controller, job.ota_system)
    if fingerprint and fingerprint == await run_in_executor(built_fingerprint, job.ota_system):
        _emit(controller, job, "INFO OTA system unchanged: its build is kept")
    else:
        result = await run(job.ota_system, job.port, full_image=True)
        if result[0] != 0 or result[1]:
            return result
        if fingerprint:
            await run_in_executor(_store_built_fingerprint, job.ota_system, fingerprint)
        job.progress = 0
        job.ninja_total = 0
    main_fingerprint = await run_in_executor(
        _written_fingerprint, controller, job.ota_system, _MAIN_FINGERPRINT_FILENAME
    )
    if main_fingerprint and main_fingerprint == await run_in_executor(
        built_main_fingerprint, job.configuration
    ):
        _emit(controller, job, "INFO Main firmware unchanged: its build is kept")
        return 0, False, False
    result = await run(job.configuration, job.port)
    if result[0] == 0 and not result[1] and main_fingerprint:
        await run_in_executor(_store_built_main_fingerprint, job.configuration, main_fingerprint)
    return result


async def run_upload(
    controller: FirmwareController, job: FirmwareJob, run: RunStep
) -> tuple[int, bool, bool]:
    system = await detect(controller, job.configuration)
    if system is None:
        return await run(job.configuration, job.port)
    if get_port_type(job.port) is PortType.SERIAL:
        return await _serial_upload(controller, job, system, run)
    return await _ota_upload(controller, job, system, run)


async def _serial_upload(
    controller: FirmwareController, job: FirmwareJob, system: OtaSystem, run: RunStep
) -> tuple[int, bool, bool]:
    built = await run_in_executor(_built_state, job.configuration, system)
    flashed = _flashed_state(controller, job)
    write_system = not built.system_fingerprint or flashed.system != built.system_fingerprint
    write_main = not built.main_fingerprint or flashed.main != built.main_fingerprint
    if not write_system and not write_main:
        _emit(controller, job, _UNCHANGED_FLASH_MAIN)
        write_main = True
    parts = await run_in_executor(_serial_parts, job.configuration, system, write_system, write_main)
    for offset, path in parts:
        _emit(controller, job, f"INFO Serial flash: {path.name} at 0x{offset:X}")
    if write_system and write_main:
        result = await run(job.configuration, job.port, upload_file=str(parts[0][1]))
    else:
        command = await run_in_executor(_write_flash_command, job, parts)
        result = await run(job.configuration, job.port, command=command)
    if result[0] == 0 and not result[1]:
        _remember_flashed(controller, job, built, system=write_system, main=write_main)
    return result


async def _ota_upload(
    controller: FirmwareController, job: FirmwareJob, system: OtaSystem, run: RunStep
) -> tuple[int, bool, bool]:
    state = await _system_worker(controller, job, mode="query")
    if not state.get("in_system") and not state.get("reboot_action"):
        outcome = await _reboot_to_system(controller, job)
        _emit(controller, job, f"INFO OTA system: {outcome}")
        if outcome != "no_action":
            _emit(controller, job, f"ERROR The device could not be asked: {outcome}")
            return 1, False, False
        for line in _MIGRATE_WARNINGS:
            _emit(controller, job, line)
        result = await _sequence(
            partial(run, system.configuration, job.port),
            partial(run, system.configuration, job.port, partition_table=True),
            partial(run, job.configuration, job.port),
        )
        if result[0] == 0 and not result[1]:
            built = await run_in_executor(_built_state, job.configuration, system)
            _remember_flashed(controller, job, built, system=True, main=True)
        return result
    built = await run_in_executor(_built_state, job.configuration, system)
    flashed = _flashed_state(controller, job)
    system_changed = _changed(
        str(state.get("elf_sha256") or ""), built.system_elf, flashed.system, built.system_fingerprint
    )
    main_changed = _changed(
        str(state.get("main_elf_sha256") or ""), built.main_elf, flashed.main, built.main_fingerprint
    )
    if not system_changed and not main_changed:
        _emit(controller, job, _UNCHANGED_FLASH_MAIN)
        main_changed = True
    if not state.get("in_system"):
        outcome = await _reboot_to_system(controller, job)
        _emit(controller, job, f"INFO OTA system: {outcome}")
        if outcome != "in_system":
            _emit(controller, job, _SYSTEM_NOT_UP_ERROR)
            return 1, False, False
        state = await _system_worker(controller, job, mode="query")
    if system_changed:
        if not state.get("self_update"):
            for line in _SYSTEM_CHANGED_WARNINGS:
                _emit(controller, job, line)
            return 1, False, False
        _emit(controller, job, "INFO The OTA system changed: it updates itself first")
        result = await run(system.configuration, job.port)
        if result[0] != 0 or result[1]:
            return result
        _remember_flashed(controller, job, built, system=True, main=False)
        waited = await _system_worker(controller, job, mode="wait", expect_elf=built.system_elf)
        if waited.get("outcome") != "in_system":
            _emit(controller, job, _SYSTEM_NOT_UP_ERROR)
            return 1, False, False
    if main_changed:
        result = await run(job.configuration, job.port)
        if result[0] == 0 and not result[1]:
            _remember_flashed(controller, job, built, system=False, main=True)
        return result
    outcome = await _system_worker(controller, job, mode="reboot_main")
    _emit(controller, job, f"INFO Back to the main firmware: {outcome.get('outcome', 'no answer')}")
    return 0, False, False


def schedule_flash_check(controller: FirmwareController, job: FirmwareJob) -> None:
    controller._db.create_background_task(_flash_check(controller, job))


async def _flash_check(controller: FirmwareController, job: FirmwareJob) -> None:
    devices = controller._db.devices
    if devices is None or not await run_in_executor(_is_esp32, job.configuration):
        return
    expect = await run_in_executor(_image_elf_sha256, job.configuration)
    state = await _system_worker(
        controller,
        job,
        mode="flash_check",
        expect_elf=expect,
        checked=devices.flash_checked_frequencies(job.configuration),
    )
    if state.get("outcome") != "ok":
        return
    if isinstance(flash_id := state.get("flash_id"), str) and flash_id:
        devices.set_flash_id(job.configuration, flash_id)
    frequency = state.get("frequency")
    verified = state.get("verified")
    if not isinstance(frequency, str) or not isinstance(verified, bool):
        return
    devices.set_flash_check(job.configuration, frequency, ok=verified)
    if not verified:
        _LOGGER.warning(
            "Flash check of %s failed at %s: its next builds limit the flash to 40MHZ",
            job.configuration,
            frequency,
        )


def _is_esp32(configuration: str) -> bool:
    storage = StorageJSON.load(resolve_storage_path(configuration))
    return storage is not None and (storage.target_platform or "").upper().startswith("ESP32")


def built_fingerprint(system_configuration: str) -> str:
    return _read_marker(_built_fingerprint_path(system_configuration))


def built_main_fingerprint(configuration: str) -> str:
    return _read_marker(_built_marker_path(configuration, _MAIN_FINGERPRINT_FILENAME, factory=False))


def build_factory_image(configuration: str, system: OtaSystem) -> Path:
    main_bin = _firmware_bin(resolve_storage_path(configuration))
    system_bin = _firmware_bin(resolve_storage_path(system.configuration))
    factory = (system_bin.parent / _FACTORY_FILENAME).read_bytes()
    ota_0, otadata, nvs = _partitions(factory)
    if len(factory) > ota_0[0]:
        msg = f"OTA system image ({len(factory)} bytes) overlaps ota_0 at 0x{ota_0[0]:X}"
        raise ValueError(msg)
    app = main_bin.read_bytes()
    if len(app) > ota_0[1]:
        msg = f"Main firmware ({len(app)} bytes) does not fit ota_0 (0x{ota_0[1]:X})"
        raise ValueError(msg)
    image = bytearray(factory)
    image.extend(b"\xff" * (ota_0[0] - len(image)))
    image.extend(app)
    start, size = otadata
    image[start : start + size] = b"\xff" * size
    seq = _OTADATA_SEQ.pack(_OTA_0_SEQ)
    image[start : start + 4] = seq
    crc = binascii.crc32(seq, 0xFFFFFFFF) & 0xFFFFFFFF
    image[start + _OTADATA_CRC_OFFSET : start + _OTADATA_CRC_OFFSET + 4] = _OTADATA_SEQ.pack(crc)
    nvs_image = main_bin.with_name(_NVS_IMAGE_FILENAME)
    if nvs is not None and nvs_image.is_file():
        defaults = nvs_image.read_bytes()
        if len(defaults) > nvs[1]:
            msg = f"NVS defaults ({len(defaults)} bytes) do not fit the nvs partition (0x{nvs[1]:X})"
            raise ValueError(msg)
        image[nvs[0] : nvs[0] + len(defaults)] = defaults
    target = system_bin.parent / _MERGED_FILENAME
    target.write_bytes(bytes(image))
    return target


def _partitions(
    factory: bytes,
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int] | None]:
    ota_0: tuple[int, int] | None = None
    otadata: tuple[int, int] | None = None
    nvs: tuple[int, int] | None = None
    offset = _PARTITION_TABLE_OFFSET
    while offset + _PARTITION_ENTRY.size <= len(factory):
        magic, ptype, subtype, start, size, label, _ = _PARTITION_ENTRY.unpack_from(factory, offset)
        if magic != _PARTITION_MAGIC:
            break
        if ptype == _TYPE_APP and subtype == _SUBTYPE_OTA_0:
            ota_0 = (start, size)
        elif ptype == _TYPE_DATA and subtype == _SUBTYPE_OTADATA:
            otadata = (start, size)
        elif (
            ptype == _TYPE_DATA
            and subtype == _SUBTYPE_NVS
            and label.rstrip(b"\0").decode(errors="replace") == _NVS_LABEL
        ):
            nvs = (start, size)
        offset += _PARTITION_ENTRY.size
    if ota_0 is None or otadata is None:
        msg = "The OTA system's partition table has no ota_0 or no otadata partition"
        raise ValueError(msg)
    return ota_0, otadata, nvs


def _firmware_bin(storage_path: Path) -> Path:
    storage = StorageJSON.load(storage_path)
    if storage is None or not storage.firmware_bin_path:
        msg = f"No build found ({storage_path})"
        raise ValueError(msg)
    return Path(storage.firmware_bin_path)


def main_configuration(configuration: str) -> str:
    device = Path(configuration).parent.parent
    return (device.parent / f"{device.name}.yaml").as_posix()


async def generate_system(
    controller: FirmwareController, job: FirmwareJob, configuration: str
) -> None:
    _emit(controller, job, "INFO Writing the OTA system configuration (esphome config)")
    main = main_configuration(configuration)
    path = await run_in_executor(controller._db.settings.rel_path, main)
    cmd = [*await controller._resolve_esphome_cmd(job), "--dashboard", "config", str(path)]
    try:
        async with controller._tracked_subprocess(
            job,
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=controller._compose_subprocess_env(job),
            start_new_session=True,
        ) as proc:
            assert proc.stdout is not None
            output = await proc.stdout.read()
            code = await proc.wait()
    except OSError as exc:
        _emit(controller, job, f"ERROR esphome config did not run: {exc}")
        return
    if code != 0:
        for line in output.decode("utf-8", errors="replace").splitlines():
            _emit(controller, job, line)


async def _reboot_to_system(controller: FirmwareController, job: FirmwareJob) -> str:
    parsed = await _system_worker(controller, job, mode="reboot")
    return str(parsed.get("outcome") or parsed.get("error") or "no answer")


async def _system_worker(
    controller: FirmwareController,
    job: FirmwareJob,
    *,
    mode: str,
    expect_elf: str = "",
    checked: list[str] | None = None,
) -> dict[str, Any]:
    devices = controller._db.devices
    device = devices.get_by_configuration(job.configuration) if devices is not None else None
    if devices is None or device is None:
        return {"outcome": "no_device"}
    addresses = list(device.runtime_state.ip_addresses) or [
        a for a in (device.ip, device.address) if a
    ]
    if not addresses:
        return {"outcome": "no_address"}
    try:
        noise_psk, port = await encryption_key_lookup.get_api_connection(devices, job.configuration)
    except Exception as exc:
        _LOGGER.warning("Native API key lookup for %s failed: %s", job.configuration, exc)
        return {"outcome": "no_api_key"}
    request = dumps(
        {
            "address": addresses[0],
            "port": port,
            "noise_psk": noise_psk,
            "addresses": addresses,
            "mode": mode,
            "expect_elf": expect_elf,
            "checked": checked or [],
        }
    )
    try:
        result = await run_subprocess_capture(
            sys.executable,
            "-m",
            _REBOOT_WORKER,
            timeout=_REBOOT_TIMEOUT,
            stdin_data=request,
            merge_stderr=False,
        )
    except OSError as exc:
        return {"outcome": f"worker failed: {exc}"}
    if result.timed_out:
        return {"outcome": "timed out"}
    try:
        parsed = loads(result.stdout) if result.stdout else None
    except (JSONDecodeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        return {"outcome": "no answer"}
    return parsed


def _written_fingerprint(
    controller: FirmwareController,
    system_configuration: str,
    filename: str = _FINGERPRINT_FILENAME,
) -> str:
    return _read_marker(controller._db.settings.rel_path(system_configuration).parent / filename)


def _read_marker(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _built_marker_path(configuration: str, filename: str, *, factory: bool) -> Path | None:
    storage = StorageJSON.load(resolve_storage_path(configuration))
    if storage is None or not storage.build_path or not storage.firmware_bin_path:
        return None
    firmware = Path(storage.firmware_bin_path)
    if not firmware.is_file() or (factory and not firmware.with_name(_FACTORY_FILENAME).is_file()):
        return None
    return Path(storage.build_path) / filename


def _built_fingerprint_path(system_configuration: str) -> Path | None:
    return _built_marker_path(system_configuration, _FINGERPRINT_FILENAME, factory=True)


def _store_built_fingerprint(system_configuration: str, fingerprint: str) -> None:
    path = _built_fingerprint_path(system_configuration)
    if path is not None:
        path.write_text(f"{fingerprint}\n", encoding="utf-8")


def _store_built_main_fingerprint(configuration: str, fingerprint: str) -> None:
    path = _built_marker_path(configuration, _MAIN_FINGERPRINT_FILENAME, factory=False)
    if path is not None:
        path.write_text(f"{fingerprint}\n", encoding="utf-8")


@dataclass(frozen=True)
class _BuiltState:
    system_fingerprint: str
    main_fingerprint: str
    system_elf: str
    main_elf: str


@dataclass(frozen=True)
class _FlashedState:
    system: str | None
    main: str | None


def _built_state(configuration: str, system: OtaSystem) -> _BuiltState:
    return _BuiltState(
        system_fingerprint=built_fingerprint(system.configuration),
        main_fingerprint=built_main_fingerprint(configuration),
        system_elf=_image_elf_sha256(system.configuration),
        main_elf=_image_elf_sha256(configuration),
    )


def _flashed_state(controller: FirmwareController, job: FirmwareJob) -> _FlashedState:
    devices = controller._db.devices
    if devices is None:
        return _FlashedState(system=None, main=None)
    return _FlashedState(
        system=devices.ota_system_fingerprint(job.configuration),
        main=devices.main_fingerprint(job.configuration),
    )


def _changed(device_elf: str, built_elf: str, flashed: str | None, built_fingerprint: str) -> bool:
    if device_elf and built_elf:
        return device_elf != built_elf
    return not built_fingerprint or flashed != built_fingerprint


def _remember_flashed(
    controller: FirmwareController, job: FirmwareJob, built: _BuiltState, *, system: bool, main: bool
) -> None:
    devices = controller._db.devices
    if devices is None:
        return
    if system and built.system_fingerprint:
        devices.set_ota_system_fingerprint(job.configuration, built.system_fingerprint)
    if main and built.main_fingerprint:
        devices.set_main_fingerprint(job.configuration, built.main_fingerprint)


def _image_elf_sha256(configuration: str) -> str:
    end = _APP_DESC_OFFSET + _APP_DESC_ELF_SHA256 + _SHA256_SIZE
    try:
        with _firmware_bin(resolve_storage_path(configuration)).open("rb") as image:
            head = image.read(end)
    except (OSError, ValueError):
        return ""
    if len(head) < end or int.from_bytes(head[_APP_DESC_OFFSET : _APP_DESC_OFFSET + 4], "little") != (
        _APP_DESC_MAGIC
    ):
        return ""
    return head[_APP_DESC_OFFSET + _APP_DESC_ELF_SHA256 : end].hex()


def _serial_parts(
    configuration: str, system: OtaSystem, write_system: bool, write_main: bool
) -> list[tuple[int, Path]]:
    merged = build_factory_image(configuration, system)
    if write_system and write_main:
        return [(0, merged)]
    image = merged.read_bytes()
    system_bin = _firmware_bin(resolve_storage_path(system.configuration))
    ota_0, otadata, _ = _partitions((system_bin.parent / _FACTORY_FILENAME).read_bytes())
    if write_system:
        part = merged.with_name(_SYSTEM_PART_FILENAME)
        part.write_bytes(image[: ota_0[0]])
        return [(0, part)]
    start, size = otadata
    otadata_part = merged.with_name(_OTADATA_PART_FILENAME)
    otadata_part.write_bytes(image[start : start + size])
    return [(start, otadata_part), (ota_0[0], _firmware_bin(resolve_storage_path(configuration)))]


def _write_flash_command(job: FirmwareJob, parts: list[tuple[int, Path]]) -> list[str]:
    storage = StorageJSON.load(resolve_storage_path(job.configuration))
    chip = (storage.target_platform if storage is not None else "") or "auto"
    command = [
        *find_esptool_cmd(),
        "--before",
        "default-reset",
        "--after",
        "hard-reset",
        "--baud",
        os.getenv(_UPLOAD_SPEED_ENV, _UPLOAD_SPEED),
        "--port",
        job.port,
        "--chip",
        chip.lower(),
        "write-flash",
        "-z",
        "--flash-size",
        "detect",
    ]
    for offset, path in parts:
        command += [f"0x{offset:X}", str(path)]
    return command


async def _sequence(
    *steps: Callable[[], Awaitable[tuple[int, bool, bool]]],
) -> tuple[int, bool, bool]:
    result = (0, False, False)
    for step in steps:
        result = await step()
        if result[0] != 0 or result[1]:
            return result
    return result


def _emit(controller: FirmwareController, job: FirmwareJob, line: str) -> None:
    _ingest_output_line(job, controller._db.bus, f"{line}\n")
