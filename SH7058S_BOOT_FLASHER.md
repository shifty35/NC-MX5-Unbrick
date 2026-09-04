# NC ECU SH7058S boot-mode flasher

`tools/flash_sh7058s_boot.py` is a target-specific recovery tool for the
Renesas `R4F70580S`/SH7058S. It implements Renesas Flash Development Toolkit
(FDT) **Protocol C** generic boot mode over SCI1; it is not a Mazda diagnostic,
KWP, or UDS reflashing tool.

Before it enables a destructive command, the tool requires the exact device
code and product name, clock information, 1 MiB User MAT, 12 KiB User Boot MAT,
16-block erase geometry, and 128-byte programming unit expected by this target.
The input must be a raw 1 MiB User-MAT image whose `0x0000-0x1FFF` prefix is
erased (`0xFF`).

No Mazda firmware is distributed in this repository.

## Safety and electrical prerequisites

Erasing or programming flash is destructive. Loss of ECU power, reset, a
serial connection, or the host process can leave flash incomplete. The tool
never automatically retries an erase or program record: after a lost ACK, the
result of repeating a destructive operation would be ambiguous.

This repository does not provide a verified ECU connector/test-point map,
measured signal levels, or a complete boot-control circuit. Do not connect an
adapter until this table has been completed for the actual ECU and interface:

| Logical connection | ECU connector/test point | Adapter side | Verify before connecting |
| --- | --- | --- | --- |
| ECU battery feed(s) | **fill in** | fused bench supply positive | required voltage, current limit, fuse, and polarity |
| ECU ignition/wake feed(s) | **fill in** | fused/controlled supply positive | required feeds and energizing order |
| ECU power ground(s) | **fill in all required pins** | bench supply return | complete ground map and current capacity |
| Serial common reference | **fill in** | adapter GND | correct signal ground and no unintended current path |
| Host to ECU | **fill in: verified SCI1 receive path** | adapter TX | idle polarity, voltage, inversion, and required buffer |
| ECU to host | **fill in: verified SCI1 transmit path** | adapter RX | idle/high voltage and adapter input tolerance |
| Boot/reset controls | **fill in** | external verified circuit | exact boot-mode sequence and levels |
| Power/reset order | **fill in and approve** | supply/reset controls | assert/reset, strap, release, and shutdown sequence |

Leave adapter VCC, RTS, DTR, and flow-control leads physically disconnected
unless the interface has been independently verified to use them. The tool asks
pyserial to keep RTS/DTR inactive, but software configuration is not a substitute
for disconnecting unproven wiring. Never connect a positive/negative-voltage
RS-232 interface to logic-level signals.

The Renesas manual distinguishes the Mode-4 `PVCC1 = 3.3 V` condition from the
`PVCC2` domain used by `PC0/TxD1` and `PC1/RxD1`; `PVCC2` is specified at 5 V.
Consequently, “3.3 V boot mode” does not establish a safe UART or diagnostic-pin
voltage. Measure the actual interface and use direction-appropriate protection
or level shifting.

At the bare-MCU level, Mode 4 uses `FWE=1, MD2=1, MD1=0, MD0=0`. With the ECU
unpowered or reset asserted, establish the verified boot straps, then power on
or release reset. Keep reset deasserted and FWE/mode signals stable throughout
the operation. A real ECU may add inversion, pulls, protection, or other signal
conditioning, so use an ECU-specific circuit and sequence.

Cold boot is the only supported live mode. Its mandatory `0x40` transition
erases the complete 1 MiB User MAT and the separate 12 KiB User Boot MAT before
returning ACK. The input restores only User MAT; User Boot MAT remains blank,
and no backup is made.

Before starting:

- Use a stable, current-limited, fused bench supply and prevent USB/ECU
  backfeed.
- Close FDT, IDE serial monitors, and every other process that could access the
  serial device.
- Disable host sleep and USB power suspension.
- Keep the boot setup unchanged until the tool finishes or prints recovery
  instructions.
- On successful completion, assert reset or remove ECU power before restoring
  normal straps/FWE and disconnecting the programming interface.

The optional classic Arduino Nano arrangement is described in the repository
[README](README.md). Holding the ATmega reset low is appropriate only when the
board is used as a passive USB-UART path; it also stops the pulse sketch.

## Installation and basic use

The live serial path requires Python 3.10 or newer and pyserial:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m serial.tools.list_ports
```

First validate the image and inspect the sparse programming plan. A dry run
does not need `--port`, import pyserial, or open a serial device:

```sh
python tools/flash_sh7058s_boot.py ECU.bin --dry-run
```

After independently verifying the wiring and entering cold boot mode:

```sh
python tools/flash_sh7058s_boot.py ECU.bin \
  --port /dev/cu.usbserial-0001
```

On Windows:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m serial.tools.list_ports
.venv\Scripts\python.exe tools\flash_sh7058s_boot.py ECU.bin --port COM3
```

The tool prints the image hash, sparse ranges, erase/restore scope, baud plan,
and validation limits. It opens the port and validates target identity and
geometry without erasing, then requires the literal confirmation
`ERASE_AND_FLASH` immediately before `0x40`. `--yes` is intended only for an
already controlled, non-interactive setup.

## Baud-rate selection

The initial boot/autobaud exchange always runs at 9,600 baud. The default
second-stage rate is 38,400 baud. Choose another supported H3F request with
`--baud` (also spelled `--program-baud`):

```sh
python tools/flash_sh7058s_boot.py ECU.bin \
  --port /dev/cu.usbserial-0001 \
  --baud 125000
```

Commas and underscores are accepted, such as `--baud 62,500`. At the validated
20 MHz peripheral clock, the available selections produce:

| `--baud` H3F request | Target-calculated line rate | Status |
| ---: | ---: | --- |
| 38,400 | 39,062.5 | default |
| 62,500 | 62,500 exact | experimental |
| 125,000 | 125,000 exact | one complete erase/program/readback run user-reported |
| 206,900 | approximately 208,333.33 | user-validated on an Arduino Nano clone with onboard CH340 |
| 312,500 | 312,500 exact | user-validated on that Nano clone only with `--adapter-baud 315800` |
| 625,000 | 625,000 exact | experimental BRG maximum |

These target rates follow the manual's asynchronous SCI divider formula. The
boot ROM accepts an H3F request only when its calculated divider error is less
than 4%; the SCI chapter recommends tighter system error margins. A USB-UART
driver accepting a numeric baud does not prove the physical line rate.

The tool sends H3F at 9,600, receives its old-rate ACK, switches the local
adapter, and exchanges a confirmation byte. It then performs three complete,
checksummed `0x27` programming-unit inquiries at the selected rate before it
prints the destructive prompt. A failed confirmation or probe has no automatic
fallback: reset or power-cycle into a fresh cold boot before retrying.

The 206,900-baud request has been successfully validated by the operator using
an Arduino Nano clone with its onboard CH340 bridge:

```sh
python tools/flash_sh7058s_boot.py ECU.bin \
  --port /dev/cu.usbserial-0001 \
  --baud 206900
```

H3F requests 206,900 baud. The target selects approximately 208,333.33 baud;
the inspected Apple driver maps the same request to approximately 206,896.55
baud, about 0.6897% below the target. The successful live run validates that
complete configuration, but the calculated mapping remains adapter-, driver-,
and host-specific and does not establish behavior on another setup.

The inspected macOS 26.4 AppleUSBCHCOM/CH340 `1A86:7523` mappings are:

| Adapter request | Calculated CH340 line rate |
| ---: | ---: |
| 62,500 | 62,500 exact |
| 125,000 | 125,000 exact |
| 206,900 | approximately 206,896.55 |
| 312,500 | 300,000 |
| 625,000 | 600,000 |

Those mappings are not portable to every OS, driver, or CH340 revision. In
particular, uncompensated 312,500 and 625,000 requests leave this setup at a
marginal 4% difference from the target, and 625,000 exceeds WCH's stated
460,800-baud continuous-rate rating for CH340G/CH340C.

`--adapter-baud` changes only the local serial-port request while leaving the
target H3F request selected by `--baud`. The following pairing has also been
successfully validated on the operator's Arduino Nano clone: it requests
312,500 on the target and 315,800 locally.

```sh
python tools/flash_sh7058s_boot.py ECU.bin \
  --port /dev/cu.usbserial-0001 \
  --baud 312500 \
  --adapter-baud 315800
```

The inspected driver is expected to produce approximately 315,789.47 baud,
about 1.0526% above the target. The successful result applies to this exact
target/adapter pairing; plain `--baud 312500`, which requests 312,500 from the
same CH340 driver, is not the validated configuration. No equivalent
high-margin 625,000 pairing is available through that driver. Treat other
`--adapter-baud` overrides as advanced and qualify them before authorizing
erase.

The manual specifies 8 data bits, no parity, and one stop bit. The tool uses
8N1 by default. `--fdt-two-stop-bits` configures two stop bits to add host-side
mark time, but USB-UART framing is normally bidirectional while target replies
remain 8N1. Use it only with an adapter whose receive behavior has been tested.

The flasher keeps pyserial reads nonblocking and enforces deadlines with a
monotonic clock. It does not change serial timeout settings after each command,
which is important because some custom-baud drivers reprogram the UART when a
port attribute is reassigned.

## Image planning and verification

The image gate requires:

- exactly 1,048,576 bytes;
- `0x0000-0x1FFF` entirely `0xFF`; and
- at least one nonblank 128-byte programming page.

It does **not** establish Mazda/NC ECU identity, ROM variant compatibility,
calibration correctness, internal checksum validity, provenance, or
bootability. Printing SHA-256 is identification, not an allowlist.

The flasher programs every 128-byte-aligned page that is not entirely `0xFF`.
It retains `0xFF` bytes inside a used page. This sparse write is safe only after
the tool has received the documented automatic-erase completion ACK and proved
both MATs blank. Non-`0xFF` pages must not be omitted merely because their data
looks repetitive or zero-filled.

Default verification uses the documented `0x52` memory-read command to compare
the complete 1 MiB User MAT byte-for-byte and reports its SHA-256. A faster,
weaker mode asks the target for its 32-bit additive User-MAT sum:

```sh
python tools/flash_sh7058s_boot.py ECU.bin \
  --port /dev/cu.usbserial-0001 \
  --verify sum
```

Addition is commutative and collision-prone, so this cannot prove
address-correct contents. To skip verification entirely:

```sh
python tools/flash_sh7058s_boot.py ECU.bin \
  --port /dev/cu.usbserial-0001 \
  --verify none
```

That mode trusts only per-command ACKs and deliberately reports an
**UNVERIFIED** result. External verification is required before attempting a
normal boot.

The tool's programming-time display combines serial wire time with the
manual's typical 1 ms programming time per 128-byte record. It is only a rough
estimate; USB scheduling, software overhead, automatic erase, blank checks,
and verification add time. The manual specifies up to 20 ms per programming
record under its stated test conditions.

## Serial protocol

Both UART lines idle high. Bytes are least-significant bit first, with 8 data
bits, no parity, and one stop bit unless the explicit two-stop-bit option is
used. Initial traffic is 9,600 baud; traffic after H3F uses the selected rates.

Normal multibyte messages have an opcode, a one-byte payload size, payload, and
checksum:

```text
OPCODE SIZE PAYLOAD... SUM
```

The erase-geometry response `0x36` alone has a two-byte size. Integers and
addresses are big-endian. `SUM` makes the unsigned byte sum of the complete
frame zero modulo 256:

```python
sum_byte = (-sum(frame_without_sum)) & 0xff
```

Special forms include:

```text
06                              ACK
80 REJECTED_OPCODE              generic command/order error
50 ADDRESS_BE32 DATA[128] SUM   one program record
50 FF FF FF FF B4               end programming
52 09 01 ADDRESS SIZE SUM       read User MAT
52 SIZE_BE32 DATA... SUM        memory-read response
```

### Cold connection and target validation

Renesas allows up to 30 `0x00` attempts during initial bit-rate adjustment, so
the tool loops until it receives the adjustment-complete byte:

```text
Host  00 ...
ECU   00                 bit-rate adjustment complete
Host  55
ECU   E6                 boot response
```

It then performs and validates this 9,600-baud inquiry/selection sequence:

```text
Host  20
ECU   30 0F 01 0D 31 6E 30 31 52 34 46 37 30 35 38 30 53 90
      one device: code "1n01", product "R4F70580S"

Host  10 04 31 6E 30 31 EC       select device
ECU   06

Host  21
ECU   31 01 00 CE                 zero enumerated modes; selection 0 permitted
Host  11 01 00 EE                 select the permitted clock mode
ECU   06

Host  22
ECU   32 05 02 01 08 01 02 BB    main x8, peripheral x2

Host  23
ECU   33 09 02 0F A0 1F 40 03 E8 07 D0 F2
      40-80 MHz and 10-20 MHz operating ranges

Host  25
ECU   35 09 01 00 00 00 00 00 0F FF FF B4
      User MAT 0x00000000-0x000FFFFF

Host  24
ECU   34 09 01 00 00 00 00 00 00 2F FF 94
      User Boot MAT 0x00000000-0x00002FFF

Host  26
ECU   36 00 81 10 ... 16 start/end address pairs ... 61

Host  27
ECU   37 02 00 80 47             128-byte programming unit
```

The default rate-selection exchange is:

```text
Host  3F 07 01 80 03 E8 02 08 02 42   at 9,600
ECU   06                               at 9,600
Host  06                               after local rate switch
ECU   06                               at the selected target rate
```

The H3F payload contains the requested baud in units of 100 bps, a 10.00 MHz
input clock, and the validated x8/x2 ratios. The tool computes the frame and
checksum for each supported selection, then validates three framed `0x27`
responses at the selected rate.

### Automatic erase and programming

`0x40` transfers the target into programming/erasing selection and
automatically erases both User MAT and User Boot MAT. The manual states that
the target returns `0x06` only after both erases finish. The tool allows up to
180 seconds for that ACK and never polls or sends a blank check while the
operation may still be active.

After ACK, it sends `0x4F` and requires the checksum-valid ready response:

```text
5F 02 3F 00 60
```

It then requires `0x4C` and `0x4D` to report User Boot MAT and User MAT blank
before selecting User-MAT programming with `0x43`.

Programming uses strict stop-and-wait records:

```text
Host  43                         select User-MAT programming
ECU   06
Host  50 ADDRESS DATA[128] SUM
ECU   06                         after each completed record
      ...
Host  50 FF FF FF FF B4
ECU   06
```

The target's relevant status values are:

| Status | Meaning |
| --- | --- |
| `0x31` | automatic User/User-Boot MAT erase active |
| `0x3F` | ready for program/erase selection |
| `0x4F` | waiting for programming data |
| `0x5F` | waiting for erase-block number |

## Failure handling

After `0x40` may have been accepted, every timeout, target rejection, serial
exception, verification mismatch, or keyboard interrupt leaves the ECU state
unproven. The tool prints a phase-specific recovery warning and never retries a
destructive record.

During erase/programming failures, leave stable power, reset, and mode wiring
untouched while a possible in-flight operation settles. A readback/checksum
failure is itself non-destructive, but the programmed contents remain
unverified. Do not normal-boot the ECU or guess at resuming an old session;
re-establish the verified boot setup and start a fresh cold run.

If a response begins with an unexpected byte but no valid error frame follows,
the error reports the byte explicitly, such as `got isolated 0xF1`. This is a
malformed transport response, not a valid target rejection, and the tool does
not ignore or retry it.

## Validation boundary

The self-contained test suite covers frame/checksum vectors, all supported baud
frames, image constraints and sparse planning, a scripted cold-boot transcript,
fragmented and failed serial I/O, malformed responses, adapter-rate overrides,
selected-rate probes, destructive-stage recovery messages, and full 1 MiB
readback comparison:

```sh
python -m unittest discover -s tests -p 'test_flash_sh7058s_boot.py' -v
```

The protocol and target geometry are documented by these primary sources:

- [SH7058S/SH7059 hardware manual, boot SCI protocol sections 24.5 and 24.10](https://www.renesas.com/en/document/mah/sh-2e-sh7059-f-ztattm-sh7058s-f-ztattm-hardware-manual?r=1054756)
- [FDT v4.09 manual, Protocol C description](https://www.renesas.com/en/document/mat/flash-development-toolkit-v409-release-03-windows-7-windows-81-windows-10-users-manual-rev1300)
- [SH7058F on-board programming application note](https://www.renesas.com/en/document/apn/sh7058f-group-018-m-process-f-ztat-microcomputer-board-programming)
- [WCH CH340G/CH340C datasheet](https://www.wch-ic.com/download/file?id=79)
- [WCH USB-to-serial product selector](https://www.wch-ic.com/products/productsCenter/otherChip?categoryId=68)

Offline tests do not prove the pyserial/USB path, electrical interface,
diagnostic-pin mapping, boot-control wiring, default `0x52` readback on a
physical ECU, or a successful normal boot. First use remains hardware
validation and requires stable bench power plus a recoverable setup.
