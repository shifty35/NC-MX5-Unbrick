# ROMDrop CAN flashing protocol

This document records the CAN-based NC MX-5 PCM flashing protocol recovered
from an analyzed local ROMDrop executable. The executable is not distributed
in this repository. This document covers the host-side J2534 implementation,
the stock primary-loader handoff, the embedded secondary bootloaders (SBLs),
erase and programming behavior, security access, address mapping, timing, and
known evidence limits.

This is not the protocol implemented by
[tools/flash_sh7058s_boot.py](tools/flash_sh7058s_boot.py). That recovery tool
uses Renesas Protocol C over SCI1/UART. The two paths happen to target the same
SH7058S User-MAT addresses, but their transports, session setup, loaders, and
erase behavior are different.

Flashing an ECU is destructive. In particular, a timed-out TransferData
request must not be automatically replayed: ROMDrop's TransferData packets
contain neither an address nor a block sequence counter, so the ECU may already
have advanced its internal write pointer.

## Analysis target and evidence

The analyzed local executable is:

| Property | Value |
| --- | --- |
| Source filename | <code>romdrop</code> (operator-supplied; not tracked) |
| Format | Mach-O 64-bit, ARM64 |
| Size | 219,512 bytes |
| Symbols | Present |
| Minimum macOS | 14.0 |
| SDK | 14.4 |
| Mach-O UUID | <code>7421F389-5B3D-37B7-AC3B-DE7E6A5B3CBA</code> |
| SHA-256 | <code>4572ca298595104c6b6ac51c459492be3896e64bb5615bcd4982a87ab8ec5c89</code> |

The official upstream Windows executable inspected during this analysis was a
PE32 i386 image with SHA-256
<code>4b919a5f8f892eaf8f9e81edfa8453856587914ef5197f27994fdaa3858e20b5</code>.
Its two embedded obfuscated SBL byte strings match the local Mach-O. The
upstream project is at
[github.com/speepsio/romdrop](https://github.com/speepsio/romdrop).

The primary-loader handoff was traced through the first <code>0x2000</code>
bytes of the local image
<code>LFLMEA 2.5 FAB STG1 HEADER 93 OCTANE CATFIX V7.bin</code>, whose complete
SHA-256 is
<code>7bbb081812e31c5019248b7f206818e5856a062af4664e2789175918c70c4352</code>.
The first <code>0x2000</code> bytes have SHA-256
<code>e9b8b1d9b6ff2e782764a51e435aa247e22be3473ee1fd1e6d13afaca1ead769</code>.
Its byte at <code>0x2030</code> is ASCII <code>7</code>, so ROMDrop selects its
internally named <code>_sbl_nc2</code> image for this ROM. The
<code>nc1</code>/<code>nc2</code> labels in this document follow ROMDrop's
symbols and identify loader families; they must not be interpreted as a direct
facelift/model-year mapping. No second primary-loader variant was available in
the local evidence bundle, but both embedded SBLs were extracted and
disassembled directly.

Evidence is classified as follows:

- **Direct host proof:** ARM64 disassembly and data from the local executable.
- **Direct ECU proof:** big-endian SH-2 disassembly of the analyzed LFLMEA primary
  loader and both decoded embedded SBLs.
- **Field corroboration:** an archived ROMDrop log confirms the outer requests,
  successful responses through RequestDownload, and one seed/key pair.
- **Derived wire format:** ISO-TP PCI frames follow deterministically from the
  logical payload lengths, but the repository contains no successful physical
  ROMDrop CAN capture.
- **Still capture-dependent:** actual flow-control bytes, padding value,
  effective inter-frame separation, and adapter/ECU timing.

The archived field log is available in the
[DIY Tuning Toolkit Development thread](https://forum.miata.net/vb/archive/index.php/t-630805-p-3.html).
It records a failed old SBL transfer; its initial handshake is useful evidence,
but its SBL ciphertext must not be replayed.

## Quick protocol reference

ROMDrop configures:

| Setting | Value |
| --- | --- |
| CAN bit rate | 500,000 bit/s |
| Identifier format | 11-bit |
| J2534 ProtocolID | 6, ISO15765 |
| Tester request identifier | <code>0x7E0</code> |
| PCM response identifier | <code>0x7E8</code> |
| Transmit flag | <code>ISO15765_FRAME_PAD</code>, value <code>0x40</code> |
| J2534 ISO15765_BS | 0 |
| J2534 ISO15765_STMIN | 0 |

The normal successful flash exchange, expressed as logical ISO-TP payloads, is:

| Phase | Request on <code>0x7E0</code> | Response on <code>0x7E8</code> |
| --- | --- | --- |
| Tester present | <code>3E 01</code> | <code>7E</code> |
| Programming session | <code>10 85</code> | <code>50 85</code> |
| Security seed | <code>27 01</code> | <code>67 01 S0 S1 S2</code> |
| Security key | <code>27 02 K0 K1 K2</code> | <code>67 02</code> |
| OEM flash gate | <code>B1 00 B2 00</code> | <code>F1 00 B2</code> |
| RequestDownload | <code>34 00 00 80 00 00 0F F8 00</code> | <code>74 04 01</code> |
| SBL transfer | six times <code>36 + 1024 bytes</code> | described below |
| ROM transfer | N times <code>36 + 1024 bytes</code> | pending response(s), then <code>76</code> |
| TransferExit | <code>37</code> | <code>77</code> |
| ECU reset | <code>11 01</code> | <code>51</code> |

No application-level request precedes <code>3E 01</code>. Device setup before
that point consists only of J2534 open/connect/configuration/filter calls.
Full and dynamic flashing use the same session sequence.

The protocol does **not** use:

- a host-side erase request;
- service <code>31</code>;
- an address or block sequence counter in TransferData;
- a second RequestDownload after loading the SBL;
- an explicit execute-loader request;
- periodic TesterPresent messages during erase/programming; or
- a post-write readback/verification request.

## J2534 transport construction

The connection structure passed to PassThruConnect contains ProtocolID
<code>6</code>, a bit rate of <code>0x7A120</code> (500,000), and zero connect
flags. This selects normal 11-bit ISO-TP.

ROMDrop calls SET_CONFIG with these parameter/value pairs:

~~~text
0x1E, 0    ISO15765_BS
0x1F, 0    ISO15765_STMIN
~~~

It installs two FLOW_CONTROL_FILTER filters with an exact four-byte
<code>FF FF FF FF</code> mask:

~~~text
pattern 00 00 07 E0  -> flow-control ID 00 00 07 E8
pattern 00 00 07 E8  -> flow-control ID 00 00 07 E0
~~~

Every application request is prepended with the four-byte big-endian J2534
identifier <code>00 00 07 E0</code>. A ROMDrop/J2534 log line such as:

~~~text
00 00 07 E0 3E 01
~~~

means arbitration ID <code>0x7E0</code> with logical ISO-TP payload
<code>3E 01</code>. The four identifier bytes are not CAN data. The ID-only
<code>00 00 07 E0</code> lines seen after transmissions are adapter TX
indications/echo metadata, not additional commands to reproduce.

## Session and download sequence

### Tester present and programming session

The first request is exactly:

~~~text
3E 01
~~~

ROMDrop then requests its Mazda-specific programming session:

~~~text
10 85
~~~

This is Mazda/KWP-flavored behavior and should not be replaced with the
modern-UDS programming-session subfunction <code>02</code>.

### Security access

The tester requests seed level 1:

~~~text
27 01
~~~

ROMDrop takes exactly three seed bytes from:

~~~text
67 01 S0 S1 S2
~~~

It calculates a three-byte key and sends:

~~~text
27 02 K0 K1 K2
~~~

The field log contains this confirmed vector:

~~~text
27 01              -> 67 01 05 A8 8F
27 02 5A D6 62     -> 67 02
~~~

### OEM flash gate

ROMDrop sends:

~~~text
B1 00 B2 00
~~~

The archived field response is:

~~~text
F1 00 B2
~~~

The binary labels this operation as checking the flash counter/limit. It does
not parse a returned numeric counter; it only treats a non-negative response
as success.

### RequestDownload

The exact request is:

~~~text
34 00 00 80 00 00 0F F8 00
~~~

The bytes after SID <code>34</code> are an OEM eight-byte layout, not a normal
UDS DFI/ALFI tuple. The analyzed LFLMEA primary loader:

1. requires the programming/security state flags;
2. requires exactly eight request parameters;
3. parses parameters 0-3 as big-endian <code>0x00008000</code>;
4. adds its <code>0xFFFF0000</code> RAM base, producing
   <code>0xFFFF8000</code>;
5. requires parameter 4 to be zero;
6. parses parameters 5-7 as big-endian <code>0x0FF800</code>;
7. derives an SBL receive length of
   <code>0x0FF800 - 0x0FE000 = 0x1800</code>; and
8. requires that receive length to be a multiple of <code>0x400</code>.

The positive response is:

~~~text
74 04 01
~~~

The final two bytes are consistent with a maximum logical TransferData length
of <code>0x401</code> bytes (SID plus 1024 data bytes), but ROMDrop itself does
not parse or enforce that interpretation.

The <code>0x00008000</code> value is therefore the SBL's RAM destination—not a
User-MAT write address.

This same RequestDownload value remains constant in dynamic mode even when
ROMDrop will send fewer User-MAT bytes. The primary loader uses it to establish
the fixed six-block, <code>0x1800</code>-byte SBL transfer. The patched SBL's
start address and its eventual <code>0x100000</code> end pointer govern the
shortened flash suffix.

## Security-key algorithm

The algorithm consumes the three seed bytes followed by ASCII
<code>MazdA</code>. Both the input and state are shifted as little-endian byte
arrays.

~~~python
def shift_right_little_endian(value):
    for index in range(len(value) - 1):
        value[index] = (
            (value[index] >> 1)
            | ((value[index + 1] & 1) << 7)
        )
    value[-1] >>= 1


def calculate_key(seed):
    value = bytearray(seed + b"MazdA")
    state = bytearray([0xA9, 0x41, 0xC5])

    for _ in range(64):
        input_bit = value[0] & 1
        state_bit = state[0] & 1
        shift_right_little_endian(value)
        shift_right_little_endian(state)

        if input_bit != state_bit:
            state[0] ^= 0x28
            state[1] ^= 0x90
            state[2] ^= 0x90

    return bytes([
        (state[0] >> 4) | ((state[1] & 0x0F) << 4),
        (state[1] & 0xF0) | (state[2] >> 4),
        ((state[0] & 0x0F) << 4) | (state[2] & 0x0F),
    ])
~~~

Verified vectors:

| Seed | Key |
| --- | --- |
| <code>00 00 00</code> | <code>41 CE 7B</code> |
| <code>12 34 56</code> | <code>86 CA 06</code> |
| <code>FF FF FF</code> | <code>2A 33 A2</code> |
| <code>05 A8 8F</code> | <code>5A D6 62</code> |

## Embedded secondary bootloaders

The decoded, unpatched SBLs can be generated locally from the matching
operator-supplied executable:

| ROMDrop family symbol | Generated output | Embedded offset | Length | Raw SHA-256 | Decoded SHA-256 |
| --- | --- | ---: | ---: | --- | --- |
| <code>_sbl_nc1</code> | <code>bootloaders/romdrop_nc1_sbl.bin</code> | <code>0x28E26</code> | <code>0x154C</code> (5,452) | <code>343709b5d121addb5bc71b1f1d03ffaff9949158cf4150133df203b327ff9d09</code> | <code>b36ff927ef5f5eeb46d77b973e476ef03e40df3ecf5ac067ad586058f3536287</code> |
| <code>_sbl_nc2</code> | <code>bootloaders/romdrop_nc2_sbl.bin</code> | <code>0x2A372</code> | <code>0x16F4</code> (5,876) | <code>2411e702ac9b05da78c000ced6008dcb55eb99b0b4d392cd65026075c9960490</code> | <code>731311951e5761c017ac57ec1ab5e66c36ee91ab993fcf571fb352edf3671f53</code> |

Both are big-endian Renesas SH-2 code linked at <code>0xFFFF8000</code>.
Their terminal bytes are:

~~~text
_sbl_nc1 offset 0x1540: 0200*SBL_END
_sbl_nc2 offset 0x16E8: 0300*SBL_END
~~~

The marker ends exactly at EOF. The generated files are canonical decoded
blobs before ROMDrop applies run-specific patches. They are ignored by Git and
are not by themselves the complete six-packet SBL upload stream.

The extraction is reproducible with
[tools/extract_romdrop_sbl.py](tools/extract_romdrop_sbl.py). The script refuses
an executable whose SHA-256 does not match the analyzed binary, verifies both
raw embedded ranges, applies the transformation, checks decoded hashes and
footers, and then writes the files.

~~~sh
python3 tools/extract_romdrop_sbl.py /path/to/romdrop
~~~

### Embedded transformation

ROMDrop calls this transformation <code>chomp</code>. It is symmetric:

~~~python
key = b"!5XMCECN"

for index in range(length):
    buffer[length - 1 - index] ^= (
        key[index % 8] + index
    ) & 0xFF
~~~

Applying it twice restores the original bytes. The key is stored at executable
file offset <code>0x23AC0</code>.

The stock primary loader does **not** apply this transformation. It copies the
received bytes verbatim and searches for plaintext <code>SBL_END</code>.

### Building the transmitted SBL stream

ROMDrop:

1. examines ROM byte <code>0x2030</code>;
2. selects <code>_sbl_nc1</code> for ASCII <code>5</code> or
   <code>6</code>, and <code>_sbl_nc2</code> for ASCII <code>7</code>;
3. decodes the matching embedded SBL;
4. overlays the true SBL length at offset zero of a working ROM copy;
5. applies the run-specific erase-index/address patches below;
6. corrects the working ROM's checksums; and
7. sends the first fixed <code>0x1800</code> bytes in six
   <code>0x400</code>-byte TransferData payloads.

Because the actual SBLs are shorter than <code>0x1800</code>, the transmitted
tail comes from the working input ROM:

| ROMDrop family | SBL length | Tail in six-block stream |
| --- | ---: | ---: |
| <code>_sbl_nc1</code> | <code>0x154C</code> | <code>0x2B4</code> bytes |
| <code>_sbl_nc2</code> | <code>0x16F4</code> | <code>0x10C</code> bytes |

### Working-image checksum correction

After overlaying and patching the SBL, and before starting the CAN flash
session, ROMDrop runs <code>_correctChecksums</code> over the working image. It
walks 35 consecutive 12-byte, big-endian descriptors at ROM offsets
<code>0xFF650-0xFF7F3</code>. Each descriptor contains a start address, an end
address, and a stored checksum word.

For each descriptor, ROMDrop sums big-endian 32-bit words over the inclusive
byte range <code>[start, end]</code>, visiting word starts from
<code>start</code> through <code>end - 3</code>. Equivalently, the byte range is
half-open <code>[start, end + 1)</code>. It then computes:

~~~text
checksum = (0x5AA5A55A - sum) mod 2^32
~~~

It overwrites the descriptor's stored checksum word when it differs. The
descriptor table is above every legal dynamic start address, so all repaired
words are included in the transmitted suffix. As described below, the archive
copy is made before the temporary SBL overlay, patches, and checksum repair.

## SBL runtime patches

The following offsets are relative to decoded <code>_sbl_nc2</code>. Subtract
<code>0xB0</code> for the corresponding <code>_sbl_nc1</code> offsets.

| Purpose | <code>_sbl_nc2</code> offset | <code>_sbl_nc1</code> offset | Patched bytes |
| --- | ---: | ---: | --- |
| Initial erase-table index | <code>0xD4A</code> | <code>0xC9A</code> | <code>ED index</code> |
| Load selected 32-bit start | <code>0xDF4</code> | <code>0xD44</code> | <code>D2 17</code> |
| Relocate response-word load | <code>0xE0E</code> | <code>0xD5E</code> | <code>93 20</code> |
| Preserved response literal | <code>0xE52</code> | <code>0xDA2</code> | <code>00 C5</code> |
| Initial flash address | <code>0xE54</code> | <code>0xDA4</code> | <code>00, (start&gt;&gt;16)&amp;FF, (start&gt;&gt;8)&amp;FF, 00</code> |

The legal start addresses are aligned such that the final address encoding is
their normal four-byte big-endian representation. The <code>ED index</code>
instruction is SH-2 <code>mov #index,r13</code>, selecting the first entry of
the shortened erase table. The other patches change a 16-bit start-address
load into a 32-bit PC-relative load while preserving an SBL response literal.

Start address to ROMDrop SBL erase index:

| Start | Index | Start | Index |
| ---: | ---: | ---: | ---: |
| <code>0x002000</code> | 0 | <code>0x003000</code> | 1 |
| <code>0x004000</code> | 2 | <code>0x005000</code> | 3 |
| <code>0x006000</code> | 4 | <code>0x007000</code> | 5 |
| <code>0x008000</code> | 6 | <code>0x020000</code> | 7 |
| <code>0x040000</code> | 8 | <code>0x060000</code> | 9 |
| <code>0x080000</code> | 10 | <code>0x0A0000</code> | 11 |
| <code>0x0C0000</code> | 12 | <code>0x0E0000</code> | 13 |

## Primary-loader handoff

The analyzed LFLMEA primary loader occupies ROM
<code>0x0000-0x1FFF</code>.
Its relevant dispatch and handler addresses are:

| Service | Dispatch | Handler |
| --- | ---: | ---: |
| RequestDownload <code>34</code> | <code>0x14D6 / 0x154C</code> | <code>0x1A84</code> |
| TransferData <code>36</code> | <code>0x14DA / 0x1570</code> | <code>0x1BB6</code> |
| TransferExit <code>37</code> | <code>0x14DE / 0x157C</code> | <code>0x1CCE</code> |

The primary-loader request structure uses SID at offset 0, parameter count at
offset 2, and parameters beginning at offset 4.

RequestDownload initializes:

~~~text
SBL destination/base: 0xFFFF8000
SBL current pointer:  0xFFFF8000
SBL bytes remaining:  0x00001800
download active:      true
~~~

Each TransferData request must contain exactly <code>0x400</code> data bytes.
There is no block sequence counter. The primary copies the bytes to its current
RAM pointer, increments the pointer, decrements the remaining count, and scans
the incoming bytes against its literal <code>SBL_END</code> marker.

The exact successful response/handoff sequence is:

~~~text
SBL blocks 1-5:
    primary loader -> 76

SBL block 6:
    primary loader -> 7F 36 78
    primary sets SP = 0xFFFFBFA0
    primary jumps to 0xFFFF8000
    SBL may send additional 7F 36 78 responses during erase
    SBL -> 76 after successful erase
~~~

If the marker appears in the wrong transfer or is absent when the
<code>0x1800</code> bytes are complete, the primary returns
<code>7F 36 72</code>.

This handoff is the answer to the apparent missing execute/erase requests: the
sixth SBL TransferData request never receives its final success from the
primary loader. The primary sends ResponsePending and transfers execution; the
new SBL erases flash and eventually completes that same request with
<code>76</code>.

## SBL erase and programming state machine

The decoded SBL entry immediately initializes its state and calls its erase
routine before entering the normal receive loop:

| ROMDrop family | Erase routine |
| --- | ---: |
| <code>_sbl_nc1</code> | <code>0xFFFF8C74</code> |
| <code>_sbl_nc2</code> | <code>0xFFFF8D24</code> |

The erase address table is identical in both decoded loaders. It is at runtime
address <code>0xFFFF9508</code> in <code>_sbl_nc1</code> and
<code>0xFFFF96B0</code> in <code>_sbl_nc2</code>:

~~~text
00002000
00003000
00004000
00005000
00006000
00007000
00008000
00020000
00040000
00060000
00080000
000A0000
000C0000
000E0000
~~~

The patched initial index selects the first entry to erase. The SBL then erases
that entry and every following entry through the end of User MAT. It can emit
<code>7F 36 78</code> whenever its timer crosses literal threshold
<code>0x0131</code>. Successful startup/erase ends with <code>76</code>;
failure ends with <code>7F 36 72</code>.

Once active, the SBL handles these relevant requests:

### RequestDownload inside the SBL

The host must not issue a second RequestDownload. The active SBL rejects
service <code>34</code> with:

~~~text
7F 34 22
~~~

### TransferData

The SBL requires:

- active programming state; and
- exactly <code>0x400</code> data bytes after SID <code>36</code>.

For every valid request it:

1. sends <code>7F 36 78</code>;
2. divides the 1024 bytes into eight <code>0x80</code>-byte writes;
3. calls the flash driver for each write;
4. advances its implicit flash address by <code>0x80</code> each time;
5. may emit more ResponsePending messages; and
6. sends <code>76</code> on completion.

Programming or length failure returns:

~~~text
7F 36 72
~~~

Both a non-<code>0x400</code> length and a flash-driver/program failure clear
the SBL's transfer-active state before returning that NRC. The failure is
session-terminal: a later <code>36</code> cannot resume the same transfer.

The handler stops programming at address <code>0x00100000</code>.

### TransferExit

TransferExit must have zero parameters. Its outcomes are:

| Condition | Response |
| --- | --- |
| Active state and current address exactly <code>0x00100000</code> | <code>77</code> |
| Wrong final address | <code>7F 37 79</code> |
| Invalid state | <code>7F 37 22</code> |
| Unexpected parameter | <code>7F 37 12</code> |

For both the successful and wrong-final-address paths, the SBL clears its
transfer-active state before comparing the current address with
<code>0x00100000</code>. A premature <code>37</code> that returns
<code>7F 37 79</code> is therefore terminal for that SBL transfer session;
subsequent <code>36</code> requests fail the state check. The SBL returns to its
receive dispatcher after sending the response.

### Reset

The subsequent <code>11 01</code> reaches the SBL's own reset handler:

| ROMDrop family | Handler | Final spin |
| --- | ---: | ---: |
| <code>_sbl_nc1</code> | <code>0xFFFF8F88</code> | <code>0xFFFF8FFA</code> |
| <code>_sbl_nc2</code> | <code>0xFFFF9038</code> | <code>0xFFFF90AA</code> |

Both handlers require the secured/session flags, exactly one parameter, and
subfunction <code>01</code>. They send the bare positive response:

~~~text
51
~~~

Each then masks interrupts and spins at the address shown above. No direct
reset-register write or jump was identified. An eventual watchdog/failsafe
reset is the likely mechanism, but that last mechanism is an inference rather
than directly proved by either handler.

## Standard and dynamic address selection

Standard flashing starts at User-MAT address <code>0x2000</code>. ROMDrop
preserves the resident primary-loader area at <code>0x0000-0x1FFF</code>.

Dynamic flashing compares the new image with the local
<code>romdrop.rda</code> archive, not with live ECU contents. It searches for
the first difference starting at <code>0x2000</code>, then aligns downward:

| First differing address | Selected start |
| --- | --- |
| <code>0x2000-0x7FFF</code> | <code>difference &amp; 0xF000</code> |
| <code>0x8000-0x1FFFF</code> | <code>0x8000</code> |
| <code>0x20000-0xFFFFF</code> | <code>difference &amp; 0xE0000</code> |

If the archive cannot be opened, ROMDrop falls back to standard
<code>0x2000</code> flashing. If the new image is identical to the archive,
<code>compareRoms</code> returns <code>0x100000</code> and ROMDrop aborts with
<code>ROM identical to archive [flash aborted]</code> before beginning the
flash session.

Before applying the temporary SBL overlay and checksum corrections, ROMDrop
copies the supplied input image into the archive buffer. After a successful
flash it writes that pre-overlay input copy to <code>romdrop.rda</code>; the
archive therefore contains neither the transient RAM SBL image nor any
checksum-word repairs made only in the transmitted working copy. It is not
necessarily a byte-exact copy of what was sent.

The file loader allocates exactly 1 MiB and requires a full 1 MiB read, so a
short file is rejected. It does not check for data after that first 1 MiB;
larger inputs are silently truncated.

Dynamic flashing is only a prefix skip. From the selected erase boundary
onward, the SBL erases every remaining block and ROMDrop transmits every byte
through <code>0xFFFFF</code>. It is not sparse arbitrary-block programming.

## Transfer counts and data coverage

For a standard flash:

~~~text
SBL bytes:       0x1800
SBL transfers:   0x1800 / 0x400 = 6

ROM bytes:       0x100000 - 0x2000 = 0xFE000
ROM transfers:   0xFE000 / 0x400 = 1016

Total 0x36 requests: 1022
Total data bytes:    0xFF800
~~~

For a dynamic start address <code>start</code>:

~~~text
SBL transfers = 6
ROM transfers = (0x100000 - start) / 0x400
~~~

All ROM transfers are sequential and stop-and-wait.
The host computes the number of requests with integer
<code>length / 0x400</code> division and has no remainder-packet path; every
supported SBL and ROM range is therefore an exact multiple of 1024 bytes.

## Correlation with SCI/FDT unbrick writes

The CAN and SCI paths use the same User-MAT byte addresses from
<code>0x2000</code> upward, but package them differently.

For a ROMDrop ROM-data block beginning at implicit flash address
<code>A</code>, the SBL's eight physical writes correspond exactly to these
SCI/FDT Protocol-C records:

~~~text
A + 0x000
A + 0x080
A + 0x100
A + 0x180
A + 0x200
A + 0x280
A + 0x300
A + 0x380
~~~

For example:

~~~text
ROMDrop block: 0x002000-0x0023FF

Equivalent FDT record addresses:
0x002000  0x002080  0x002100  0x002180
0x002200  0x002280  0x002300  0x002380
~~~

The final ROMDrop block is <code>0x0FFC00-0x0FFFFF</code>; its last equivalent
FDT record begins at <code>0x0FFF80</code>.

### Erase-block numbering

ROMDrop's SBL omits the two primary-loader blocks from its table, so its
internal index is two less than the Renesas/FDT erase-block number:

| ROMDrop index | FDT block | User-MAT range |
| ---: | ---: | --- |
| 0 | EB2 | <code>0x002000-0x002FFF</code> |
| 1 | EB3 | <code>0x003000-0x003FFF</code> |
| 2 | EB4 | <code>0x004000-0x004FFF</code> |
| 3 | EB5 | <code>0x005000-0x005FFF</code> |
| 4 | EB6 | <code>0x006000-0x006FFF</code> |
| 5 | EB7 | <code>0x007000-0x007FFF</code> |
| 6 | EB8 | <code>0x008000-0x01FFFF</code> |
| 7 | EB9 | <code>0x020000-0x03FFFF</code> |
| 8 | EB10 | <code>0x040000-0x05FFFF</code> |
| 9 | EB11 | <code>0x060000-0x07FFFF</code> |
| 10 | EB12 | <code>0x080000-0x09FFFF</code> |
| 11 | EB13 | <code>0x0A0000-0x0BFFFF</code> |
| 12 | EB14 | <code>0x0C0000-0x0DFFFF</code> |
| 13 | EB15 | <code>0x0E0000-0x0FFFFF</code> |

The resident FDT capture explicitly erases EB2-EB15, exactly matching a
standard ROMDrop flash's User-MAT erase coverage.

The cold SCI tool's <code>0x40</code> path is materially broader: it
automatically erases the complete User MAT, including EB0/EB1, and the separate
User Boot MAT. Its accepted image must contain a nonblank
<code>0x0000-0x1FFF</code> primary-loader region, which it restores with normal
128-byte User-MAT programming records. Therefore the write addresses correlate
with ROMDrop from <code>0x2000</code> upward, but the cold tool restores rather
than preserves the primary-loader region.

### Blank-page handling

A standard ROMDrop flash covers all 8,128 128-byte pages from
<code>0x2000</code> through <code>0xFFFFF</code>, including all-FF pages.

The historical captured FDT program stream for
<code>zeal-red-stock-2.0-v1.bin</code>
with SHA-256
<code>79ba6aee9d2cf0d93adf68b3892e1e025f2564937880a0271e2c678a417d3daa</code>
contains 7,844 explicitly addressed nonblank pages. It omits 348 all-FF pages
across the complete 1 MiB image:

- 64 pages in its omitted <code>0x0000-0x1FFF</code> primary-loader region; and
- 284 all-FF pages within <code>0x2000-0xFFFFF</code>.

That historical partial image is valid evidence for the resident FDT stream but
is rejected by the cold tool because it cannot restore EB0/EB1. With equivalent
already checksum-valid application data and a successful erase, both approaches
produce the same bytes from <code>0x2000</code> upward. If ROMDrop's
<code>_correctChecksums</code> changes any checksum-table words, its transmitted
working copy differs at those words from the unmodified image sent by the
SCI/FDT tool. Packet ordinal positions also diverge because the cold tool sends
the nonblank EB0/EB1 records, but the explicit addresses continue to correlate.

See [SH7058S_BOOT_FLASHER.md](SH7058S_BOOT_FLASHER.md) and
[../UNBRICK_CAPTURE_ANALYSIS.md](../UNBRICK_CAPTURE_ANALYSIS.md) for the SCI
protocol and capture evidence.

## Derived physical ISO-TP frames

ROMDrop delegates ISO-TP segmentation and flow control to J2534. The following
PCI bytes are derived from the exact logical payload lengths; they are not a
physical capture of the particular adapter/ECU combination.

Let <code>PP</code> mean an adapter-selected pad byte in the tester-originated
frames below. ROMDrop requests tester-frame padding through its J2534 transmit
flag; it does not control whether or how the ECU pads responses.

All tester requests that fit in one classic-CAN ISO-TP Single Frame are:

~~~text
7E0  02 3E 01 PP PP PP PP PP
7E0  02 10 85 PP PP PP PP PP
7E0  02 27 01 PP PP PP PP PP
7E0  05 27 02 K0 K1 K2 PP PP
7E0  04 B1 00 B2 00 PP PP PP
7E0  01 37 PP PP PP PP PP PP
7E0  02 11 01 PP PP PP PP PP
~~~

Representative successful and pending Single Frame responses are:

~~~text
7E8  01 7E
7E8  02 50 85
7E8  05 67 01 S0 S1 S2
7E8  02 67 02
7E8  03 F1 00 B2
7E8  03 74 04 01
7E8  03 7F 36 78
7E8  01 76
7E8  01 77
7E8  01 51
~~~

Only the required PCI and payload bytes are shown for ECU responses. Their
actual DLC, any trailing bytes, and any ECU-selected padding value require a
physical capture.

RequestDownload, logical length 9:

~~~text
7E0  10 09 34 00 00 80 00 00
7E8  <ECU flow-control frame>
7E0  21 0F F8 00 PP PP PP PP
~~~

Every TransferData payload has logical length <code>0x401</code>:

~~~text
7E0  14 01 36 D0 D1 D2 D3 D4
7E8  <ECU flow-control frame>
7E0  21 D5 D6 D7 D8 D9 D10 D11
...
7E0  22 D1020 D1021 D1022 D1023 PP PP PP
~~~

After the First Frame, 146 Consecutive Frames carry the remaining bytes. Their
sequence numbers cycle through <code>1 ... F, 0</code>; after wrapping, the
final Consecutive Frame has sequence number <code>2</code>.

The physical capture still needed for an exact wire transcript must establish:

- ECU FlowControl status/BS/STmin bytes;
- whether more FlowControl frames appear within each 1025-byte request;
- the adapter's actual tester-frame padding value;
- ECU response DLC and any ECU-selected padding;
- inter-frame and inter-message timing; and
- any adapter-specific status indications not present on the CAN bus.

## Host timeout and response behavior

ROMDrop's request wrapper:

- submits each J2534 write once with a 100 ms write timeout;
- calls PassThruReadMsgs with a hard-coded 5,000 ms timeout per read;
- passes an apparent 250 ms response value that is stored but never used;
- discards messages when <code>RxStatus &amp; 0x0B</code> is nonzero;
- discards a transmit echo when its first application byte equals the request
  SID;
- waits again for every <code>7F xx 78</code>, without checking that
  <code>xx</code> matches the request SID;
- has no overall deadline across repeated ResponsePending replies;
- returns any other negative-response NRC as an error;
- aborts the transfer loop on its first error;
- does not validate that a positive response SID matches the request;
- does not compare the four-byte source identifier at the start of the J2534
  message; and
- does not validate the received application-payload length before inspecting
  response bytes.

ROMDrop does not retransmit a request after a timeout. This is essential for
TransferData. Once the final ISO-TP Consecutive Frame has reached the SBL, the
SBL may write the block and advance its implicit address even if the positive
response is lost. Replaying that same <code>36</code> can place duplicate data
at the following address and corrupt the image.

After a successful TransferExit, ROMDrop first writes its local archive file.
Only if both flashing and archive writing succeed does it synchronously send
<code>11 01</code>. It waits through the normal response path and logs the
result, but neither retries reset nor branches to another CAN command. There
are no later application-level CAN requests in the flash workflow.

## Historical failed-log discrepancy

The archived r064d-era log sends an SBL payload beginning:

~~~text
0B D4 90 69 B6 6B 18 F3 ...
~~~

That byte sequence is exactly the raw obfuscated <code>_sbl_nc1</code> blob
stored in both inspected ROMDrop executables. It does not contain plaintext
<code>SBL_END</code>. The stock primary loader copies incoming bytes without
decoding, so it acknowledges the first five transfers and rejects the sixth
with:

~~~text
7F 36 72
~~~

The current analyzed host applies <code>chomp</code> before transmission; the
decoded <code>_sbl_nc1</code> image begins <code>93 76 EE 00 ...</code> and ends
with <code>0200*SBL_END</code>. Consequently, the old field dump documents a
failed transfer, not a valid bootloader payload to replay. The precise
software-history reason that old build transmitted ciphertext was not
independently established.

## Reverse-engineering symbol map

Useful ARM64 symbols in the analyzed Mach-O:

| Symbol | Address |
| --- | ---: |
| <code>_processRequest</code> | <code>0x100005978</code> |
| <code>_processResponse</code> | <code>0x1000059C4</code> |
| <code>_newRequest</code> | <code>0x100005B84</code> |
| <code>_processPayload</code> | <code>0x100005C00</code> |
| <code>_requestProgrammingSession</code> | <code>0x100006644</code> |
| <code>_requestEcuReset</code> | <code>0x1000066F4</code> |
| <code>_sendSecurityAccessKey</code> | <code>0x100006DC0</code> |
| <code>_requestSecurityAccess</code> | <code>0x100006E8C</code> |
| <code>_requestDownload</code> | <code>0x100006FC4</code> |
| <code>_transferData</code> | <code>0x10000707C</code> |
| <code>_requestTransferExit</code> | <code>0x1000072D8</code> |
| <code>_sendTesterPresent</code> | <code>0x100007384</code> |
| <code>_checkFlashLimit</code> | <code>0x100007434</code> |
| <code>_setupFlash</code> | <code>0x100007C98</code> |
| <code>_applySblPatchToROM</code> | <code>0x100008048</code> |
| <code>_flashRom</code> | <code>0x1000084C0</code> |
| <code>_startSecuredSession</code> | <code>0x100008628</code> |
| <code>_setupDevice</code> | <code>0x100008CC4</code> |
| <code>_flash</code> | <code>0x100008F78</code> |
| <code>_chomp</code> | <code>0x100009968</code> |
| <code>_calculateKey</code> | <code>0x100009B50</code> |

<code>_startSecuredSession</code> is not part of the flash call graph. Its only
caller is the separate read-ROM workflow. Flashing enters
<code>_flashRom</code>, which immediately calls <code>_setupFlash</code>; that
function begins with <code>3E 01</code>.

Relevant constant-pool file offsets:

| Offset | Bytes/meaning |
| ---: | --- |
| <code>0x2398C</code> | <code>00 00 07 E0</code>, request identifier |
| <code>0x23994</code> | <code>B1 00 B2 00</code> |
| <code>0x2399C</code> | <code>10 85</code> |
| <code>0x2399E</code> | <code>11 01</code> |
| <code>0x239A0</code> | <code>27 01</code> |
| <code>0x239A2</code> | <code>34 00 00 80 00 00 0F F8 00</code> |
| <code>0x239AB</code> | <code>36</code> |
| <code>0x239AC</code> | <code>37</code> |
| <code>0x239AD</code> | <code>3E 01</code> |
| <code>0x23AB9</code> | ASCII <code>MazdA</code> |
| <code>0x23AC0</code> | ASCII <code>!5XMCECN</code> |
| <code>0x23AC8</code> | key state <code>A9 41 C5</code> |

## Remaining verification boundary

The logical application sequence, identifiers, security algorithm, SBL
transformation, primary-loader transition, SBL state machine, address
progression, erase selection, and host retry behavior are all statically
recovered.

A successful physical CAN capture is still required before claiming exact
wire-level:

- FlowControl bytes;
- adapter tester-frame padding;
- ECU response DLC and padding behavior;
- message timing and pacing;
- power/ignition transition behavior; or
- ECU behavior after the SBL's reset-handler spin.

The files under <code>unbrick_logs/</code> cannot fill that gap. They contain
two-channel SCI/UART transition captures from Renesas FDT, with no arbitration
IDs, DLCs, ISO-TP PCI, or CAN frames. Their valid use here is limited to
cross-checking User-MAT addresses, flash geometry, and the 128-byte physical
programming unit.

Any locally generated SBLs are derived from the operator-supplied executable
and are not included in this repository. Review the applicable ROMDrop license
before redistributing them.
