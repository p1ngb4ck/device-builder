# OTA system support (branch `p4_minimal_ota_support`)

ESPHome Device Builder plus the build and flash steps for a firmware that has an **OTA system**: a second, small
firmware in the `factory` partition that is the recovery and safe mode and does all flashing, while the main
firmware runs from `ota_0`. The device side (the `ota_system` option and the API actions it uses) is in the ESPHome
fork `p1ngb4ck/esphome@testing_dev`.

## Why

- **One honest partition table, a big main firmware, a recovery that cannot be overwritten.** With `factory` (OTA
  system) + a single `ota_0` (main firmware), an OTA from the OTA system writes `ota_0`; the main firmware has no
  "next" update partition, so it can overwrite neither itself nor the OTA system. No second app slot: the flash it
  would take goes to the main firmware. The older trick (two `ota_N` slots and two CSVs with swapped sizes) is not
  needed.
- **The OTA system is a firmware of its own, derived from the device config.** It keeps the network, the API, the
  storages and whatever is listed, so it can flash the main firmware from the network or from a storage, update the
  ESP32-C6 coprocessor, update itself, back up and restore settings, and run as the safe mode.
- **The device builder has to know about both.** A compile has to build both firmwares, an install has to put each
  where it belongs (serial: the merged image; OTA: restart into the OTA system first, then upload), and nothing
  unchanged should be built or flashed again.
- **Chip and flash options from the device, not guessed.** ESP32-P4 engineering samples (< v3.0) and production
  chips need different settings (minimum chip revision, PSRAM speeds, JPEG / RAM handling); the flash chip decides
  the driver and the safe speed. The device builder records each device's chip revision and flash chip and hands
  them to its builds; an explicit YAML setting still wins (with a warning when it does not match).

## Install

The device builder runs in the ESPHome venv of its host. Install the branch there (as the user that runs the
service), then restart the service yourself when nothing is running:

```bash
uv pip install --upgrade 'esphome-device-builder[esphome] @ git+https://github.com/p1ngb4ck/device-builder.git@p4_minimal_ota_support'
```

- With a paired build server (remote builds), install it there **first**, then on the host that sends the builds:
  the submit frames carry new fields (`ota_system`, `chip_revision`, `flash_id`, `flash_max_frequency`) and the
  returned artifacts hold both builds.
- ESPHome itself stays upstream `dev`. The device configs take the components from the fork through
  `external_components` (at least `esp32`, `psram`, `ota`, `api`; `store_yaml`, `firmware_update`, storages and UI as
  the OTA system needs them).

## Device config

```yaml
esp32:
  partitions: <device>/partitions.csv

external_components:
  - source: github://p1ngb4ck/esphome@testing_dev
    components: [esp32, psram, ota, api, store_yaml, firmware_update, ...]

api:

ota:
  - platform: esphome
    allow_partition_access: true
    ota_system:
      keep_components: [sd_storage, usb_storage, nfs_client, binary_storage]
      keep_ids: [display_backlight, sd_power, sntp_time]
      includes: [<device>/ota/ui.yaml]
```

`partitions.csv` needs `otadata`, a `factory` app (the OTA system) and an `ota_0` app (the main firmware), e.g.:

```
nvs,       data, nvs,      0x9000,   0x5000
otadata,   data, ota,      0xE000,   0x2000
ota_sys,   app,  factory,  0x10000,  0x280000
app,       app,  ota_0,    0x290000, 0xD60000
coredump,  data, coredump, 0xFF0000, 0x10000
```

### `ota_system` options (ESPHome fork)

| option | what |
|---|---|
| `keep_components` | components the OTA system keeps beside its base set (esp32, psram, logger, network, wifi, ethernet, esp32_hosted, api, ota, http_request, preferences, storage, store_yaml, display, touchscreen, lvgl's display settings); their dependencies and the IDs they reference come along |
| `keep_ids` | single blocks kept by ID (a backlight, a power LDO, a time source) |
| `includes` | YAML files merged into the OTA system (its own UI, update entities, scripts) |

`ota_system` may be set on one `ota` platform only. The build writes the derived config to
`<device>/ota/ota_system.yaml` beside the device config, with `ota_system.fingerprint`, `main.fingerprint` (ESPHome
version, chip / flash variables, config text, component sources, referenced files) and a `secrets.yaml` that includes
the device's own; these are generated files. The OTA system builds in `<build path>-ota-system`.

## What it does

| action | with an OTA system |
|---|---|
| Compile | one job: `esphome config` writes the OTA system config and the fingerprints; the OTA system is built (full image) unless its fingerprint is unchanged; then the main firmware unless its fingerprint is unchanged |
| Install / upload, serial | only what changed: both -> the merged image at 0x0; only the OTA system -> its part of the image; only the main firmware -> otadata (boot `ota_0`) + the main firmware at the `ota_0` offset; nothing -> no flash |
| Install / upload, OTA | asks the device (API): restarts the main firmware into the OTA system (`reboot_to_ota_system`) and waits until it answers as the OTA system; a changed OTA system updates itself first; then the main firmware; unchanged -> back to the main firmware (`reboot_to_main`) |
| OTA, official ESPHome install (no OTA system, no restart action) | migration with warnings: the OTA system via OTA, then via OTA with the partition table, then the main firmware. ESPHome does not consider the partition-table OTA stable: back up first, flash via serial if it fails |
| Factory download | the merged image (`firmware.ota_system.factory.bin`: OTA system, main firmware in `ota_0`, otadata selecting `ota_0`) |
| After an install | flash check: `flash_info` stores the flash ID; a speed not checked before is verified once (`flash_verify`); a failed check limits the device's next builds to 40 MHz |

What is compared: the ELF SHA256 the device reports (`ota_system_elf`) against the built image, else the fingerprint
recorded at the last flash.

### Chip revision and flash per device

Recorded in the device's metadata (`chip_revision` = major * 100 + minor, `flash_id`, checked / failed flash
frequency, the flashed fingerprints) from:
- a serial job (`esptool flash-id` on the port: revision and flash manufacturer / device),
- the device's `chip_revision` API action (asked while none is stored),
- the WebSocket command `devices/set_chip_revision` (`configuration`, `revision`) - for a browser flash, whose
  frontend knows the revision.

Each compile gets them as environment variables, read by the fork's `esp32` / `psram` components:

| variable | value |
|---|---|
| `ESPHOME_CHIP_REVISION` | e.g. `100` for v1.0 |
| `ESPHOME_FLASH_ID` | manufacturer + device, hex |
| `ESPHOME_FLASH_MAX_FREQUENCY` | `40MHZ` after a failed flash check |

`ESPHOME_UPLOAD_SPEED` sets the baud rate of the partial serial writes (default 460800).

## Limits

- ESP-IDF builds only (the partition table, otadata and the app description are IDF's).
- The OTA system can update itself only when `enable_ota_downgrade_protection` is off (the fork warns).
- Serial chip probing runs on `/dev/...` ports (Linux hosts).
- The frontend (`esphome/device-builder-frontend`) is unchanged; a browser flash hands the revision over only when a
  frontend sends `devices/set_chip_revision`.
