from __future__ import annotations

import asyncio
import binascii
import logging
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
_CONF_METHOD = "method"
_CONF_MIGRATE_VIA_OTA = "migrate_via_ota"
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
_OTADATA_SEQ = struct.Struct("<I")
_OTADATA_CRC_OFFSET = 28
_OTA_0_SEQ = 1
_REBOOT_WORKER = "esphome_device_builder.helpers.api_reboot_to_system"
_REBOOT_TIMEOUT = 300.0
_FINGERPRINT_FILENAME = "ota_system.fingerprint"
_APP_DESC_OFFSET = 32
_APP_DESC_MAGIC = 0xABCD5432
_APP_DESC_ELF_SHA256 = 144
_SHA256_SIZE = 32
_SYSTEM_CHANGED_WARNINGS = (
    "WARNING The OTA system changed since it was flashed onto this device.",
    "WARNING Flash the device via serial: the full image with both systems.",
)
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
    method: str
    migrate_via_ota: bool


def system_configuration(configuration: str) -> str:
    path = Path(configuration)
    return (path.parent / path.stem / "ota" / _SYSTEM_FILENAME).as_posix()


def is_system_configuration(configuration: str) -> bool:
    path = Path(configuration)
    return path.name == _SYSTEM_FILENAME and path.parent.name == "ota"


def bundle_configuration(configuration: str) -> str:
    if not is_system_configuration(configuration):
        return configuration
    device = Path(configuration).parent.parent
    return (device.parent / f".{device.name}.{_SYSTEM_FILENAME}").as_posix()


def stage_bundle_copy(controller: FirmwareController, configuration: str) -> str:
    staged = bundle_configuration(configuration)
    if staged != configuration:
        source = controller._db.settings.rel_path(configuration)
        controller._db.settings.rel_path(staged).write_bytes(source.read_bytes())
    return staged


def parse_ota_system(yaml_content: str, configuration: str) -> OtaSystem | None:
    config = load_yaml_tolerant(yaml_content)
    entries = config.get(_CONF_OTA) if isinstance(config, dict) else None
    for entry in entries if isinstance(entries, list) else ():
        system = entry.get(_CONF_OTA_SYSTEM) if isinstance(entry, dict) else None
        if isinstance(system, dict):
            return OtaSystem(
                configuration=system_configuration(configuration),
                method=str(system.get(_CONF_METHOD, "")).lower(),
                migrate_via_ota=system.get(_CONF_MIGRATE_VIA_OTA) is True,
            )
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
    system = await detect(controller, job.configuration)
    if system is None:
        await controller._enqueue(job)
        return
    system_compile = factories.create_job(
        controller,
        system.configuration,
        JobType.COMPILE,
        build_source=controller._resolve_install_source(),
        skip_bootloader=True,
    )
    job.depends_on = system_compile.job_id
    job.ota_system = system.configuration
    await factories.commit_chain(
        controller, system_compile, job, supersede_configuration=job.configuration
    )


async def run_compile(
    controller: FirmwareController, job: FirmwareJob, run: RunStep
) -> tuple[int, bool, bool]:
    await generate_system(controller, job, job.ota_system)
    fingerprint = await run_in_executor(_written_fingerprint, controller, job.ota_system)
    if fingerprint and fingerprint == await run_in_executor(built_fingerprint, job.ota_system):
        _emit(controller, job, "INFO OTA system unchanged: its build is kept")
    else:
        result = await run(job.ota_system, job.port)
        if result[0] != 0 or result[1]:
            return result
        if fingerprint:
            await run_in_executor(_store_built_fingerprint, job.ota_system, fingerprint)
        job.progress = 0
        job.ninja_total = 0
    return await run(job.configuration, job.port)


async def run_upload(
    controller: FirmwareController, job: FirmwareJob, run: RunStep
) -> tuple[int, bool, bool]:
    system = await detect(controller, job.configuration)
    if system is None:
        return await run(job.configuration, job.port)
    if get_port_type(job.port) is PortType.SERIAL:
        image = await run_in_executor(build_factory_image, job.configuration, system)
        _emit(controller, job, f"INFO Factory image with the OTA system: {image}")
        result = await run(job.configuration, job.port, upload_file=str(image))
        await _remember_flashed_system(controller, job, system, result)
        return result
    state = await _system_worker(controller, job, query=True)
    if state.get("in_system") or state.get("reboot_action"):
        device_elf = str(state.get("elf_sha256") or "")
        if not await run_in_executor(_system_unchanged, controller, job, system, device_elf):
            for line in _SYSTEM_CHANGED_WARNINGS:
                _emit(controller, job, line)
            return 1, False, False
    outcome = await _reboot_to_system(controller, job)
    _emit(controller, job, f"INFO OTA system: {outcome}")
    if outcome == "system_not_up":
        _emit(controller, job, _SYSTEM_NOT_UP_ERROR)
        return 1, False, False
    if outcome == "no_action":
        for line in _MIGRATE_WARNINGS:
            _emit(controller, job, line)
        result = await _sequence(
            partial(run, system.configuration, job.port),
            partial(run, system.configuration, job.port, partition_table=True),
            partial(run, job.configuration, job.port),
        )
        await _remember_flashed_system(controller, job, system, result)
        return result
    return await run(job.configuration, job.port)


def built_fingerprint(system_configuration: str) -> str:
    path = _built_fingerprint_path(system_configuration)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_factory_image(configuration: str, system: OtaSystem) -> Path:
    main_bin = _firmware_bin(resolve_storage_path(configuration))
    system_bin = _firmware_bin(resolve_storage_path(system.configuration))
    factory = (system_bin.parent / _FACTORY_FILENAME).read_bytes()
    ota_0, otadata = _partitions(factory)
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
    target = system_bin.parent / _MERGED_FILENAME
    target.write_bytes(bytes(image))
    return target


def _partitions(factory: bytes) -> tuple[tuple[int, int], tuple[int, int]]:
    ota_0: tuple[int, int] | None = None
    otadata: tuple[int, int] | None = None
    offset = _PARTITION_TABLE_OFFSET
    while offset + _PARTITION_ENTRY.size <= len(factory):
        magic, ptype, subtype, start, size, _, _ = _PARTITION_ENTRY.unpack_from(factory, offset)
        if magic != _PARTITION_MAGIC:
            break
        if ptype == _TYPE_APP and subtype == _SUBTYPE_OTA_0:
            ota_0 = (start, size)
        elif ptype == _TYPE_DATA and subtype == _SUBTYPE_OTADATA:
            otadata = (start, size)
        offset += _PARTITION_ENTRY.size
    if ota_0 is None or otadata is None:
        msg = "The OTA system's partition table has no ota_0 or no otadata partition"
        raise ValueError(msg)
    return ota_0, otadata


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
    parsed = await _system_worker(controller, job, query=False)
    return str(parsed.get("outcome") or parsed.get("error") or "no answer")


async def _system_worker(
    controller: FirmwareController, job: FirmwareJob, *, query: bool
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
            "query": query,
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


def _written_fingerprint(controller: FirmwareController, system_configuration: str) -> str:
    path = controller._db.settings.rel_path(system_configuration).parent / _FINGERPRINT_FILENAME
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _built_fingerprint_path(system_configuration: str) -> Path | None:
    storage = StorageJSON.load(resolve_storage_path(system_configuration))
    if storage is None or not storage.build_path or not storage.firmware_bin_path:
        return None
    if not Path(storage.firmware_bin_path).is_file():
        return None
    return Path(storage.build_path) / _FINGERPRINT_FILENAME


def _store_built_fingerprint(system_configuration: str, fingerprint: str) -> None:
    path = _built_fingerprint_path(system_configuration)
    if path is not None:
        path.write_text(f"{fingerprint}\n", encoding="utf-8")


def _built_elf_sha256(system_configuration: str) -> str:
    end = _APP_DESC_OFFSET + _APP_DESC_ELF_SHA256 + _SHA256_SIZE
    with _firmware_bin(resolve_storage_path(system_configuration)).open("rb") as image:
        head = image.read(end)
    if len(head) < end or int.from_bytes(head[_APP_DESC_OFFSET : _APP_DESC_OFFSET + 4], "little") != (
        _APP_DESC_MAGIC
    ):
        return ""
    return head[_APP_DESC_OFFSET + _APP_DESC_ELF_SHA256 : end].hex()


def _system_unchanged(
    controller: FirmwareController, job: FirmwareJob, system: OtaSystem, device_elf: str
) -> bool:
    if device_elf:
        try:
            return device_elf == _built_elf_sha256(system.configuration)
        except (OSError, ValueError):
            return False
    built = built_fingerprint(system.configuration)
    devices = controller._db.devices
    flashed = devices.ota_system_fingerprint(job.configuration) if devices is not None else None
    return bool(built) and flashed == built


async def _remember_flashed_system(
    controller: FirmwareController,
    job: FirmwareJob,
    system: OtaSystem,
    result: tuple[int, bool, bool],
) -> None:
    if result[0] != 0 or result[1]:
        return
    built = await run_in_executor(built_fingerprint, system.configuration)
    devices = controller._db.devices
    if built and devices is not None:
        devices.set_ota_system_fingerprint(job.configuration, built)


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
