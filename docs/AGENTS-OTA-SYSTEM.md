# Agent notes: OTA system support (branch `p4_minimal_ota_support`)

For agents working on this branch. User-facing description: `README-OTA-SYSTEM.md`. The repo's own `AGENTS.md` /
`CLAUDE.md` apply on top. Device side: ESPHome fork `p1ngb4ck/esphome@testing_dev`, `esphome/components/ota`
(`ota_system.py`, `ota_system_esp_idf.*`) and `esphome/components/esp32` (chip revision, flash, NVS, coredump actions).
Branch base: upstream `main` at 1adedf417; every change of the branch is listed below.

## Model
- Partitions: `otadata` + `factory` app = OTA system + `ota_0` app = main firmware (+ nvs, coredump). The main
  firmware cannot OTA itself (no next update partition); every flash goes through the OTA system.
- A config "has an OTA system" when its YAML has `ota: [ {ota_system: {...}} ]` (`ota_system.parse_ota_system`, tolerant
  YAML load, no validation). The derived config is `<config dir>/<config stem>/ota/ota_system.yaml`
  (`system_configuration()`); `main_configuration()` maps it back.
- The derived config is written by ESPHome during config validation of the main config (fork); this branch runs
  `esphome --dashboard config <main>` (`generate_system`) with the job's esphome and environment before building.
- `FirmwareJob.ota_system` (str): the derived config's path; set on COMPILE / UPLOAD jobs of such a config. Empty =
  upstream behaviour everywhere.

## Code map
| file | what |
|---|---|
| `controllers/firmware/ota_system.py` | detection, chain enqueueing, `run_compile`, `run_upload` (serial / OTA / migration), fingerprints, merged factory image, partial serial parts, flash check |
| `controllers/firmware/runner.py` | `execute_job` builds `_run_step(configuration, port, *, partition_table, upload_file, full_image, command)` and hands it to `ota_system.run_upload` / `run_compile`; `_run_command` = the former inline subprocess + output pump; `cli.probe_chip_revision` before the run; `schedule_flash_check` after a successful UPLOAD / INSTALL without bootloader |
| `controllers/firmware/factories.py` | `create_job` fills `chip_revision`, `flash_id`, `flash_max_frequency` from the device metadata; `mark_chip_probe` (serial `/dev` port while revision or flash ID unknown); `enqueue_install_or_defer` / `enqueue_compile` route configs with an OTA system to `ota_system.*`; `commit_chain(head, *dependents)` |
| `controllers/firmware/controller.py` | `firmware/upload` with a port and without bootloader -> `ota_system.enqueue_upload_chain`; queued (deferred) updates -> `ota_system.enqueue_queued_upload`; `_build_command` passes `partition_table` / `upload_file` |
| `controllers/firmware/cli.py` | `compose_subprocess_env`: `ESPHOME_CHIP_REVISION`, `ESPHOME_FLASH_ID`, `ESPHOME_FLASH_MAX_FREQUENCY`, `ESPHOME_DATA_DIR` for OTA system jobs; `build_command`: `--partition-table`, `--file`; `verify_chip` uses `esptool flash-id` (was `chip-id`) and stores revision + flash ID; `probe_chip_revision` |
| `controllers/firmware/download.py` | `firmware.factory.bin` of a config with an OTA system = `ota_system.build_factory_image` |
| `controllers/firmware/remote_runner.py` | probe before dispatch; submit carries `ota_system`, `chip_revision`, `flash_id`, `flash_max_frequency`; artifacts split (`split_ota_system_artifacts`) and both builds materialised |
| `controllers/remote_build/*` | receiver: `_coerce_chip_revision` (0..9999), `_coerce_flash_field` (alnum, <= 16), `_PendingSubmit` fields, the job's `ota_system` from `system_configuration(configuration)`; sender: `pack_build_artifacts(configuration, ota_system)` adds `main.fingerprint` and the OTA system's build under `ota_system/` |
| `helpers/remote_artifacts_materialise.py` | `split_ota_system_artifacts`; the build dir name taken from the receiver's build path when it is a safe name (the OTA system's `-ota-system` dir) |
| `helpers/artifacts_tarball_members.py` | `OTA_SYSTEM_MEMBER_PREFIX`, fingerprint member names |
| `helpers/api_reboot_to_system.py` | subprocess API worker (aioesphomeapi), JSON on stdin / stdout; modes `query`, `reboot` (default), `wait`, `reboot_main`, `flash_check`; the legacy `query: true` flag maps to `query` |
| `helpers/api_device_info.py` | with `chip_revision: true` in the request, also runs the device's `chip_revision` action |
| `controllers/devices/controller.py`, `_metadata_store.py` | per-device fields `chip_revision`, `flash_id`, `flash_checked_frequency`, `flash_failed_frequency`, `ota_system_fingerprint`, `main_fingerprint`; command `devices/set_chip_revision` |
| `controllers/_device_state_monitor/*` | the API probe asks for the revision while none is stored (`chip_revision_wanted` / `on_chip_revision`) |
| `models/firmware.py`, `models/remote_build/wire_frames.py` | new job and frame fields |

## Device API contract (names must match the fork)
| action | firmware | answer |
|---|---|---|
| `reboot_to_ota_system` | main | restarts into the factory app |
| `reboot_to_main` | OTA system | restarts into `ota_0` |
| `ota_system_running` | OTA system | marker only (its presence = "in the OTA system") |
| `ota_system_elf` | both | JSON `elf_sha256`, `main_elf_sha256`, `self_update` |
| `chip_revision` | any with `api` | JSON `revision` (major * 100 + minor) |
| `flash_info`, `flash_verify` | any with `api` | JSON `id`, `frequency`; `ok` |

## Flows (what `ota_system.py` decides)
- Compile: `generate_system`; OTA system built with `full_image=True` (bootloader + factory image) unless the written
  fingerprint equals the one stored in its build dir; then the main firmware unless `main.fingerprint` equals the
  stored one. Built fingerprints are stored in the build dirs only after a successful build.
- Serial upload: built fingerprints vs. the device's flashed fingerprints (metadata) -> `_serial_parts`: merged image
  at 0x0 via `esphome upload --file`, or esptool `write-flash` of the parts (`_write_flash_command`, baud
  `ESPHOME_UPLOAD_SPEED`, default 460800).
- OTA upload: `query`; neither marker nor restart action -> migration (3 uploads, warnings); else compare ELF SHA256
  (device vs. built app description at offset 32 + 144), fall back to fingerprints; restart into the OTA system and
  `wait` for the marker (180 s, retry every 3 s); OTA system changed without `self_update` -> error "flash via serial
  once"; with it: upload the OTA system, `wait` for the new ELF; then the main firmware, or `reboot_main`.
- Successful upload / install: `flash_check` in the background (waits for the main firmware with the expected ELF,
  `flash_info`, `flash_verify` once per frequency); a failure stores `flash_failed_frequency` and the next builds get
  `ESPHOME_FLASH_MAX_FREQUENCY=40MHZ`.
- `build_factory_image`: reads the OTA system's partition table at 0x8000 from its `firmware.factory.bin`, requires
  `ota_0` and `otadata`, pads to `ota_0`, appends the main `firmware.bin`, writes otadata sequence 1 + CRC (boots
  `ota_0`); refuses an overlap or a main image larger than `ota_0`.

## Rules for changes here
- Owner: Olli (p1ngb4ck). Commits with his identity (`git -c user.name=p1ngb4ck -c
  user.email=kleinecke.oliver@googlemail.com commit --no-verify`), no trailers, LF only, push to
  `origin p4_minimal_ota_support`. No code comments added. No other branch without his order.
- Never flash, erase or probe a board yourself (esptool, OTA, serial): Olli flashes.
- Never restart the esphome service on the Pi or the build server: installs go into the venv, the restart is his.
- Keep upstream behaviour unchanged for configs without `ota_system` (every new path is gated on it or on new,
  optional fields; remote frames use `NotRequired` keys and the receiver coerces them strictly).
- New fields that cross the peer link: add to `wire_frames.py`, the sender (`_submit.py`, `client.py`,
  `remote_runner.py`), the receiver (`controller.py` coercion, `_PendingSubmit`, `_post_ack.py`). Install order:
  build server first.
- Device-side names (actions, `ota_system`, file names, `-ota-system` build suffix, fingerprint file names) are a
  contract with the fork: change both together.
