# NC MX-5 SH7058S ECU Unbrick

A target-specific Python recovery flasher for the Renesas
`R4F70580S`/SH7058S used in the NC MX-5 ECU. It implements Renesas Flash
Development Toolkit Protocol C generic boot mode over SCI1; it is not an OBD,
KWP, or UDS flashing tool.

> [!CAUTION]
> A cold connection's mandatory programming-mode transition erases the complete
> 1 MiB User MAT and the separate 12 KiB User Boot MAT. The input image restores
> only the User MAT, so User Boot MAT remains blank. The tool makes no backup and
> cannot safely resume an interrupted destructive command.

Read the [complete safety, electrical, protocol, baud-rate, verification, and
recovery guide](SH7058S_BOOT_FLASHER.md) before connecting hardware. This
repository does not establish the ECU diagnostic-pin mapping or electrical
levels. Do not assume that the serial interface is 3.3 V; use a measured,
protected interface and stable fused bench power.

## Requirements

- Python 3.10 or newer
- `pyserial` for live serial operation
- A legally obtained raw 1 MiB User-MAT image for the exact ECU/ROM variant

No Mazda firmware image is included. The tool validates image shape, target
silicon identity, and flash geometry before erasure. It does not establish ROM
provenance, ECU variant compatibility, calibration correctness, internal
checksum validity, or bootability.

## Optional classic Arduino Nano hardware

The optional [`pulse_train` sketch](arduino/pulse_train/pulse_train.ino) targets
the classic 5 V, 16 MHz ATmega328P Arduino Nano layout. It uses Timer1 to toggle
`D12` and `D13` together at approximately 150.24 Hz with nominal 50% duty. It
configures `D0` and `D1` as inputs after startup, but does not implement or relay
the Renesas protocol: the Python tool communicates directly through the board's
separate USB-UART bridge. This input configuration does not eliminate the brief
bootloader interval after an ATmega reset.

An [official classic Nano](https://store.arduino.cc/products/arduino-nano) uses
an FT232RL bridge; many compatible clones substitute a CH340G or CH340C. Verify
the exact board, bridge, schematic, 5 V operation, and bootloader before using
it. Nano Every, Nano 33, Nano ESP32, Nano R4, and other Nano-form-factor boards
are not compatible with this AVR-specific sketch merely because their headers
look similar.

| Nano connection | Role in this setup |
| --- | --- |
| USB | Powers the Nano and provides the host serial device |
| `D0` / `RX` | Normally driven by the onboard bridge's TX; host-to-ECU SCI1 RX path through the verified interface |
| `D1` / `TX` | Normally feeds the onboard bridge's RX; ECU SCI1 TX-to-host path through the verified interface |
| `D12` / `PB4` | Preferred external approximately 150.24 Hz pulse output |
| `D13` / `PB5` | In-phase copy of the pulse; also drives the onboard LED and is normally left disconnected externally |
| `GND` | Common signal reference through the verified interface |
| `RESET` | Deasserted when the pulse sketch must run; held low only for bridge-only operation |
| `5V`, `3V3`, `VIN` | Leave disconnected from the ECU unless a separately verified circuit explicitly requires one |

The `RX` and `TX` header labels are from the ATmega's point of view. On a
typical Nano-compatible board, USB host transmission emerges on `D0/RX`, while
ECU data intended for the host enters `D1/TX`. Confirm this against the exact
clone schematic instead of relying on its silkscreen.

There are two straightforward ways to use a Nano-compatible board:

1. **Pulse source only:** run the sketch and use a separate, protected USB-UART
   adapter for SCI1. This avoids sharing the ATmega bootloader with the serial
   path.
2. **USB-UART bridge only:** hold the ATmega `RESET` pin low so its pins cannot
   contend with `D0`/`D1`. Timer1 is stopped in reset, so a separate pulse
   source is required.

A combined one-board pulse-source and USB-UART arrangement is possible only
after proving its startup behavior. Nano-style boards normally couple the
bridge's DTR signal to ATmega reset. Opening the serial port can therefore run
the bootloader, briefly interrupt the pulse, and let the bootloader drive the
UART pins before the sketch starts. The flasher requests inactive DTR/RTS before
opening the port, but software cannot guarantee that every bridge and driver is
glitch-free. For combined use, disable or isolate auto-reset using a method
verified against that exact board schematic, and confirm with an oscilloscope
or logic analyzer that opening the port neither resets the AVR nor causes
`D0`/`D1` contention.

Upload and validate the sketch with every ECU connection physically
disconnected. With Arduino CLI and the AVR board package installed:

```sh
arduino-cli compile --fqbn arduino:avr:nano arduino/pulse_train
arduino-cli upload \
  --fqbn arduino:avr:nano \
  --port /dev/cu.usbserial-0001 \
  arduino/pulse_train
```

Many clones use the old Nano bootloader; for those boards, use
`arduino:avr:nano:cpu=atmega328old` for both commands. After upload, measure
`D12`, verify its frequency, duty cycle, polarity, and voltage, and test serial
port opening before connecting the ECU. Close Arduino IDE, Serial Monitor, and
every other serial client before running the flasher.

> [!WARNING]
> This repository does not identify the ECU connector/test point that should
> receive the pulse, nor does it prove the electrical characteristics of any
> diagnostic line. Do not connect Nano pins directly from this table. Use the
> independently verified ECU-specific circuit, correct direction-specific
> buffering or level shifting, a verified common reference, and stable fused
> ECU power. Never connect an RS-232-level port or backfeed the ECU from a Nano
> supply pin.

## Quick start

Create a virtual environment and install the live serial dependency:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m serial.tools.list_ports
```

Validate an image and inspect the exact sparse programming plan without opening
a serial device:

```sh
python tools/flash_sh7058s_boot.py ECU.bin --dry-run
```

After completing and independently verifying the wiring and boot-mode setup:

```sh
python tools/flash_sh7058s_boot.py ECU.bin \
  --port /dev/cu.usbserial-0001
```

The default second-stage rate is 38,400 baud. A corrected 125,000-baud run has
been user-reported to complete erase, programming, and byte-for-byte
verification:

```sh
python tools/flash_sh7058s_boot.py ECU.bin \
  --port /dev/cu.usbserial-0001 \
  --baud 125000
```

The operator has also successfully validated these faster configurations using
an Arduino Nano clone and its onboard CH340 USB-UART bridge:

```sh
python tools/flash_sh7058s_boot.py ECU.bin \
  --port /dev/cu.usbserial-0001 \
  --baud 206900

python tools/flash_sh7058s_boot.py ECU.bin \
  --port /dev/cu.usbserial-0001 \
  --baud 312500 \
  --adapter-baud 315800
```

The available `--baud` selections are 38,400, 62,500, 125,000, 206,900,
312,500, and 625,000. The successful 312,500-baud target run specifically used
the 315,800-baud adapter override; plain `--baud 312500` is not the validated
configuration. USB-UART driver quantization remains board-, driver-, and
host-specific. Always inspect the dry-run output before authorizing an erase.
The detailed guide explains the CH340 mappings and target-calculated rates.

Default verification reads and compares the complete 1 MiB User MAT. The
weaker `--verify sum` mode uses the target's additive sum. `--verify none`
skips the verification stage and deliberately reports an **UNVERIFIED** result;
external verification is then required before attempting a normal boot.

## Repository contents

- `tools/flash_sh7058s_boot.py` — live flasher and dry-run image planner
- `tests/test_flash_sh7058s_boot.py` — protocol, safety, framing, and CLI tests
- `arduino/pulse_train/pulse_train.ino` — optional classic Nano pulse generator
- `SH7058S_BOOT_FLASHER.md` — complete operating and protocol documentation

The flasher programs every 128-byte image page that is not entirely `0xFF`.
Interior `0xFF` bytes in a used page are retained. The cold transition erases
the entire User MAT before sparse programming begins.

## Tests

The suite is self-contained and uses a generated synthetic image:

```sh
python -m unittest discover -s tests -p 'test_flash_sh7058s_boot.py' -v
```

## License

The original code and documentation in this repository are licensed under the
[MIT License](LICENSE). Vehicle firmware, Renesas documentation, and other
third-party material are not covered by that license.
