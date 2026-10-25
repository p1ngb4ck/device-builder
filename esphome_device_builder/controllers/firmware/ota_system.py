from __future__ import annotations

import binascii
import logging
import struct
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from esphome.storage_json import StorageJSON
from esphome.upload_targets import PortType, get_port_type

from ...helpers.async_ import run_in_executor
from ...helpers.device_yaml import EsphomeConfigUnavailableError, run_esphome_config
from ...helpers.device_yaml._tolerant_yaml import load_yaml_tolerant
from ...helpers.json import JSONDecodeError, dumps, loads
from ...helpers.storage_path import resolve_storage_path
from ...helpers.subprocess import run_subprocess_capture
from ...models import OTA_PORT, FirmwareJob, JobType
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
_METHOD_SERIAL_THEN_OTA = "serial_then_ota"
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
_REBOOT_TIMEOUT = 45.0
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
    main_compile, system_compile = await _create_compiles(
        controller, configuration, system, skip_bootloader=False, force_local=force_local
    )
    await factories.commit_chain(
        controller, main_compile, system_compile, supersede_configuration=configuration
    )
    return main_compile


async def enqueue_install_chain(
    controller: FirmwareController,
    *,
    configuration: str,
    port: str,
    system: OtaSystem,
    force_local: bool = False,
) -> FirmwareJob:
    main_compile, system_compile = await _create_compiles(
        controller,
        configuration,
        system,
        skip_bootloader=factories._is_ota_app_flash(port, flash_bootloader=False),
        force_local=force_local,
    )
    upload = factories.create_job(
        controller, configuration, JobType.UPLOAD, port=port, depends_on=system_compile.job_id
    )
    upload.ota_system = system.configuration
    await factories.commit_chain(
        controller, main_compile, system_compile, upload, supersede_configuration=configuration
    )
    return main_compile


async def _create_compiles(
    controller: FirmwareController,
    configuration: str,
    system: OtaSystem,
    *,
    skip_bootloader: bool,
    force_local: bool,
) -> tuple[FirmwareJob, FirmwareJob]:
    await _generate_system(controller, configuration)
    main_compile = factories.create_job(
        controller,
        configuration,
        JobType.COMPILE,
        build_source=controller._resolve_install_source(force_local=force_local),
        skip_bootloader=skip_bootloader,
    )
    system_compile = factories.create_job(
        controller,
        system.configuration,
        JobType.COMPILE,
        build_source=controller._resolve_install_source(force_local=force_local),
        depends_on=main_compile.job_id,
        skip_bootloader=skip_bootloader,
    )
    return main_compile, system_compile


async def enqueue_queued_upload(controller: FirmwareController, job: FirmwareJob) -> None:
    system = await detect(controller, job.configuration)
    if system is None:
        await controller._enqueue(job)
        return
    await _generate_system(controller, job.configuration)
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


async def run_upload(
    controller: FirmwareController, job: FirmwareJob, run: RunStep
) -> tuple[int, bool, bool]:
    system = await detect(controller, job.configuration)
    if system is None:
        return await run(job.configuration, job.port)
    if get_port_type(job.port) is PortType.SERIAL:
        if system.method == _METHOD_SERIAL_THEN_OTA:
            return await _sequence(
                partial(run, system.configuration, job.port),
                partial(run, job.configuration, OTA_PORT),
            )
        image = await run_in_executor(build_factory_image, controller, job.configuration, system)
        _emit(controller, job, f"INFO Factory image with the OTA system: {image}")
        return await run(job.configuration, job.port, upload_file=str(image))
    outcome = await _reboot_to_system(controller, job)
    _emit(controller, job, f"INFO Reboot into the OTA system: {outcome}")
    if outcome == "no_action" and system.migrate_via_ota:
        for line in _MIGRATE_WARNINGS:
            _emit(controller, job, line)
        return await _sequence(
            partial(run, system.configuration, job.port),
            partial(run, system.configuration, job.port, partition_table=True),
            partial(run, job.configuration, job.port),
        )
    return await run(job.configuration, job.port)


def build_factory_image(
    controller: FirmwareController, configuration: str, system: OtaSystem
) -> Path:
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


async def _generate_system(controller: FirmwareController, configuration: str) -> None:
    path = await run_in_executor(controller._db.settings.rel_path, configuration)
    try:
        await run_esphome_config(controller.state.esphome_cmd, path)
    except EsphomeConfigUnavailableError as exc:
        _LOGGER.warning("esphome config for %s did not run: %s", configuration, exc)


async def _reboot_to_system(controller: FirmwareController, job: FirmwareJob) -> str:
    devices = controller._db.devices
    device = devices.get_by_configuration(job.configuration) if devices is not None else None
    if devices is None or device is None:
        return "no_device"
    addresses = list(device.runtime_state.ip_addresses) or [
        a for a in (device.ip, device.address) if a
    ]
    if not addresses:
        return "no_address"
    try:
        noise_psk, port = await encryption_key_lookup.get_api_connection(devices, job.configuration)
    except Exception as exc:
        _LOGGER.warning("Native API key lookup for %s failed: %s", job.configuration, exc)
        return "no_api_key"
    request = dumps(
        {"address": addresses[0], "port": port, "noise_psk": noise_psk, "addresses": addresses}
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
        return f"worker failed: {exc}"
    if result.timed_out:
        return "timed out"
    try:
        parsed = loads(result.stdout) if result.stdout else None
    except (JSONDecodeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        return "no answer"
    return str(parsed.get("outcome") or parsed.get("error") or "no answer")


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
