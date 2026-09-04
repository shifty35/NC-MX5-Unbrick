#!/usr/bin/env python3
"""Flash an NC ECU's R4F70580S through the Renesas SCI boot protocol.

This implementation is intentionally target-specific. It validates the device
identity and supported flash geometry before allowing any destructive command.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence


ROM_SIZE = 0x100000
PROGRAM_UNIT = 0x80
READBACK_UNIT = 0x1000
RESERVED_PREFIX_SIZE = 0x2000
INITIAL_BAUD = 9600
DEFAULT_PROGRAM_BAUD = 38_400
PROGRAM_BAUD_OPTIONS = (38_400, 62_500, 125_000, 206_900, 312_500, 625_000)
DEVICE_CODE = b"1n01"
PRODUCT_NAME = "R4F70580S"
TYPICAL_PROGRAM_SECONDS_PER_RECORD = 0.001
PROGRAM_FRAME_BYTES = 1 + 4 + PROGRAM_UNIT + 1
SELECTED_RATE_PROBE_COUNT = 3

ERASE_BLOCKS: tuple[tuple[int, int], ...] = (
    (0x000000, 0x000FFF),
    (0x001000, 0x001FFF),
    (0x002000, 0x002FFF),
    (0x003000, 0x003FFF),
    (0x004000, 0x004FFF),
    (0x005000, 0x005FFF),
    (0x006000, 0x006FFF),
    (0x007000, 0x007FFF),
    (0x008000, 0x01FFFF),
    (0x020000, 0x03FFFF),
    (0x040000, 0x05FFFF),
    (0x060000, 0x07FFFF),
    (0x080000, 0x09FFFF),
    (0x0A0000, 0x0BFFFF),
    (0x0C0000, 0x0DFFFF),
    (0x0E0000, 0x0FFFFF),
)

EXPECTED_MULTIPLIER_PAYLOAD = bytes.fromhex("02 01 08 01 02")
EXPECTED_FREQUENCY_PAYLOAD = bytes.fromhex("02 0F A0 1F 40 03 E8 07 D0")
EXPECTED_USER_MAT_PAYLOAD = bytes.fromhex("01 00 00 00 00 00 0F FF FF")
EXPECTED_USER_BOOT_MAT_PAYLOAD = bytes.fromhex("01 00 00 00 00 00 00 2F FF")
EXPECTED_ERASE_PAYLOAD = bytes([len(ERASE_BLOCKS)]) + b"".join(
    struct.pack(">II", start, end) for start, end in ERASE_BLOCKS
)

ERROR_NAMES = {
    0x00: "no error",
    0x11: "checksum error",
    0x21: "device-code mismatch",
    0x22: "clock-mode mismatch",
    0x24: "bit-rate selection error",
    0x25: "input-frequency error",
    0x26: "multiplication-ratio error",
    0x27: "operating-frequency error",
    0x29: "erase-block number error",
    0x2A: "address error",
    0x2B: "data-length error",
    0x51: "erase error",
    0x52: "erase incomplete / MAT is not blank",
    0x53: "programming error",
    0x54: "selection/program-transfer error",
    0x80: "command or command-order error",
    0xFF: "bit-rate-adjustment confirmation error",
}


class FlashToolError(RuntimeError):
    """Base error for image, transport, and protocol failures."""


class ProtocolTimeout(FlashToolError):
    """The target did not provide a complete response by the deadline."""


class ProtocolError(FlashToolError):
    """The target response was malformed or contradicted the expected target."""


class DeviceRejected(FlashToolError):
    """The target explicitly rejected a command."""

    def __init__(self, command: int, response: int, detail: int) -> None:
        self.command = command
        self.response = response
        self.detail = detail
        if response == 0x80:
            description = (
                "command/order error; target echoed rejected opcode " f"0x{detail:02X}"
            )
        else:
            description = ERROR_NAMES.get(detail, "unknown target error")
        super().__init__(
            f"command 0x{command:02X} rejected with 0x{response:02X} "
            f"0x{detail:02X} ({description})"
        )


def _emit(*values: object, **kwargs: Any) -> None:
    """Best-effort console output that can never interrupt a live flash."""
    try:
        print(*values, **kwargs)
    except (BrokenPipeError, OSError, ValueError):
        pass


@dataclass(frozen=True)
class ProgramChunk:
    address: int
    data: bytes


@dataclass(frozen=True)
class ImagePlan:
    path: Path
    data: bytes
    sha256: str
    chunks: tuple[ProgramChunk, ...]
    ranges: tuple[tuple[int, int], ...]
    erase_blocks: tuple[int, ...]

    @property
    def programmed_bytes(self) -> int:
        return len(self.chunks) * PROGRAM_UNIT

    @property
    def user_mat_sum(self) -> int:
        return sum(self.data) & 0xFFFFFFFF


def checksum8(data: bytes) -> int:
    """Return the Renesas SUM byte which makes the frame sum zero."""
    return (-sum(data)) & 0xFF


def sized_frame(command: int, payload: bytes, *, size_width: int = 1) -> bytes:
    if size_width not in (1, 2):
        raise ValueError("size width must be one or two bytes")
    maximum = (1 << (size_width * 8)) - 1
    if len(payload) > maximum:
        raise ValueError("payload is too large for its size field")
    body = bytes([command]) + len(payload).to_bytes(size_width, "big") + payload
    return body + bytes([checksum8(body)])


def baud_selection_frame(baud: int) -> bytes:
    """Build H3F for one of the supported second-stage rates."""
    if baud not in PROGRAM_BAUD_OPTIONS:
        choices = ", ".join(f"{choice:,}" for choice in PROGRAM_BAUD_OPTIONS)
        raise ValueError(f"unsupported second-stage baud {baud:,}; choose {choices}")
    payload = struct.pack(">HHBBB", baud // 100, 1000, 2, 8, 2)
    return sized_frame(0x3F, payload)


def program_frame(chunk: ProgramChunk) -> bytes:
    if len(chunk.data) != PROGRAM_UNIT:
        raise ValueError("SH7058S program records must contain exactly 128 bytes")
    if chunk.address % PROGRAM_UNIT:
        raise ValueError("SH7058S program addresses must be 128-byte aligned")
    body = b"\x50" + struct.pack(">I", chunk.address) + chunk.data
    return body + bytes([checksum8(body)])


def program_end_frame() -> bytes:
    body = b"\x50\xFF\xFF\xFF\xFF"
    return body + bytes([checksum8(body)])


def erase_frame(block: int) -> bytes:
    return sized_frame(0x58, bytes([block]))


def _contiguous_ranges(chunks: Sequence[ProgramChunk]) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for chunk in chunks:
        end = chunk.address + PROGRAM_UNIT - 1
        if ranges and chunk.address == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((chunk.address, end))
    return tuple(ranges)


def _blocks_for_chunks(chunks: Sequence[ProgramChunk]) -> tuple[int, ...]:
    result: list[int] = []
    for index, (start, end) in enumerate(ERASE_BLOCKS):
        if any(start <= chunk.address <= end for chunk in chunks):
            result.append(index)
    return tuple(result)


def build_image_plan(path: Path) -> ImagePlan:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise FlashToolError(f"cannot read image {path}: {error}") from error

    if len(data) != ROM_SIZE:
        raise FlashToolError(
            f"expected a 1 MiB R4F70580S user-MAT image, got {len(data):,} bytes"
        )
    if data[:RESERVED_PREFIX_SIZE] != b"\xFF" * RESERVED_PREFIX_SIZE:
        raise FlashToolError(
            "the first 0x2000 bytes are not all 0xFF; this does not match the "
            "supported NC ECU image layout"
        )

    chunks = tuple(
        ProgramChunk(address, data[address : address + PROGRAM_UNIT])
        for address in range(0, len(data), PROGRAM_UNIT)
        if data[address : address + PROGRAM_UNIT] != b"\xFF" * PROGRAM_UNIT
    )
    if not chunks:
        raise FlashToolError("refusing to flash an entirely blank image")

    return ImagePlan(
        path=path,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        chunks=chunks,
        ranges=_contiguous_ranges(chunks),
        erase_blocks=_blocks_for_chunks(chunks),
    )


class RenesasBootProtocol:
    """Target-specific SH7058S SCI boot-protocol client."""

    def __init__(
        self,
        port: Any,
        *,
        program_baud: int = DEFAULT_PROGRAM_BAUD,
        adapter_baud: int | None = None,
        verbose: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if program_baud not in PROGRAM_BAUD_OPTIONS:
            choices = ", ".join(f"{choice:,}" for choice in PROGRAM_BAUD_OPTIONS)
            raise ValueError(
                f"unsupported second-stage baud {program_baud:,}; choose {choices}"
            )
        if adapter_baud is not None and adapter_baud <= 0:
            raise ValueError("adapter baud must be positive")
        self.port = port
        self.program_baud = program_baud
        self.adapter_baud = program_baud if adapter_baud is None else adapter_baud
        self.verbose = verbose
        self._sleep = sleep
        self._monotonic = monotonic

    def _log(self, message: str) -> None:
        if self.verbose:
            _emit(message)

    def _write(self, data: bytes, description: str) -> None:
        self._log(
            f"TX {description}: {data.hex(' ')}"
            if len(data) <= 32
            else f"TX {description}: {len(data)} bytes"
        )
        try:
            offset = 0
            while offset < len(data):
                written = self.port.write(data[offset:])
                if written is None:
                    written = len(data) - offset
                if written <= 0:
                    raise FlashToolError("serial write made no progress")
                offset += written
            self.port.flush()
        except FlashToolError:
            raise
        except Exception as error:
            raise FlashToolError(
                f"serial write failed during {description}: {error}"
            ) from error

    def _read_exact(self, size: int, timeout: float, description: str) -> bytes:
        deadline = self._monotonic() + timeout
        result = bytearray()
        # The live port is opened nonblocking and this loop enforces its own
        # deadline. Never assign port.timeout here: pyserial reconfigures an open
        # port on every assignment. Darwin's custom-baud path uses a B38400
        # termios placeholder and unconditionally reissues IOSSIOSPEED; doing
        # that after TX races immediate replies.
        while len(result) < size:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ProtocolTimeout(
                    f"timed out waiting for {description} ({len(result)}/{size} bytes)"
                )
            try:
                piece = self.port.read(size - len(result))
            except Exception as error:
                raise FlashToolError(
                    f"serial read failed while waiting for {description}: {error}"
                ) from error
            if piece:
                result.extend(piece)
            else:
                self._sleep(min(0.005, remaining))
        data = bytes(result)
        self._log(
            f"RX {description}: {data.hex(' ')}"
            if len(data) <= 32
            else f"RX {description}: {len(data)} bytes"
        )
        return data

    def _set_baudrate(self, baudrate: int, description: str) -> None:
        try:
            self.port.baudrate = baudrate
        except Exception as error:
            raise FlashToolError(
                f"cannot set {baudrate} baud during {description}: {error}"
            ) from error

    def _clear_serial_buffers(self, description: str) -> None:
        try:
            self.port.reset_input_buffer()
            self.port.reset_output_buffer()
        except Exception as error:
            raise FlashToolError(
                f"cannot clear serial buffers during {description}: {error}"
            ) from error

    def _expect_ack(self, command: int, *, timeout: float, description: str) -> None:
        first = self._read_exact(1, timeout, description)[0]
        if first == 0x06:
            return
        if first not in (0x80, command | 0x80):
            raise ProtocolError(
                f"{description}: expected ACK 0x06 or an error response, "
                f"got isolated 0x{first:02X}"
            )
        try:
            error = self._read_exact(1, 1.0, f"error detail for 0x{command:02X}")[0]
        except ProtocolTimeout as timeout_error:
            raise ProtocolError(
                f"{description}: expected ACK 0x06, got isolated 0x{first:02X}; "
                "no error-detail byte followed"
            ) from timeout_error
        raise DeviceRejected(command, first, error)

    def _read_frame(
        self,
        expected_response: int,
        *,
        size_width: int = 1,
        timeout: float = 5.0,
        description: str,
    ) -> bytes:
        response = self._read_exact(1, timeout, description)[0]
        if response != expected_response:
            command = expected_response - 0x10
            if response not in (0x80, command | 0x80):
                raise ProtocolError(
                    f"{description}: expected response 0x{expected_response:02X} "
                    f"or an error response, got isolated 0x{response:02X}"
                )
            try:
                error = self._read_exact(
                    1, 1.0, f"error detail after 0x{response:02X}"
                )[0]
            except ProtocolTimeout as timeout_error:
                raise ProtocolError(
                    f"{description}: expected response 0x{expected_response:02X}, "
                    f"got isolated 0x{response:02X}; no error-detail byte followed"
                ) from timeout_error
            raise DeviceRejected(command, response, error)
        size_bytes = self._read_exact(size_width, 1.0, f"{description} size")
        size = int.from_bytes(size_bytes, "big")
        tail = self._read_exact(size + 1, 5.0, f"{description} payload/checksum")
        complete = bytes([response]) + size_bytes + tail
        if sum(complete) & 0xFF:
            raise ProtocolError(f"bad checksum in {description}: {complete.hex(' ')}")
        return tail[:-1]

    def _request_frame(
        self,
        command: int,
        expected_response: int,
        *,
        size_width: int = 1,
        timeout: float = 5.0,
        description: str,
    ) -> bytes:
        self._write(bytes([command]), description)
        return self._read_frame(
            expected_response,
            size_width=size_width,
            timeout=timeout,
            description=description,
        )

    def _select(self, command: int, payload: bytes, description: str) -> None:
        self._write(sized_frame(command, payload), description)
        self._expect_ack(command, timeout=5.0, description=f"{description} ACK")

    def connect_cold_boot(self) -> None:
        """Autobaud, identify the exact target, and enter inquiry state."""
        # Check that the local driver accepts the requested rate before asking
        # the target to switch. This cannot prove the adapter's actual wire rate.
        self._set_baudrate(self.adapter_baud, "selected-rate adapter preflight")
        self._set_baudrate(INITIAL_BAUD, "cold-boot setup")
        self._clear_serial_buffers("cold-boot setup")
        _emit("Waiting for SH7058S boot autobaud response at 9600 baud...")

        for attempt in range(1, 31):
            self._write(b"\x00", f"autobaud attempt {attempt}/30")
            try:
                response = self._read_exact(1, 0.02, "autobaud completion")
            except ProtocolTimeout:
                continue
            if response == b"\x00":
                break
            if response == b"\xFF":
                raise ProtocolError("target rejected autobaud adjustment")
            raise ProtocolError(f"unexpected autobaud response 0x{response[0]:02X}")
        else:
            raise ProtocolTimeout(
                "no boot autobaud response after 30 attempts; verify the boot-mode "
                "straps, ECU reset/power sequence, RX/TX crossing, and signal levels"
            )

        self._write(b"\x55", "autobaud confirmation")
        boot_response = self._read_exact(1, 1.0, "boot response")
        if boot_response != b"\xE6":
            raise ProtocolError(
                f"expected boot response 0xE6, got 0x{boot_response[0]:02X}"
            )

        devices = self._request_frame(
            0x20, 0x30, description="supported-device inquiry"
        )
        self._validate_device_list(devices)
        self._select(0x10, DEVICE_CODE, "device selection")

        clock_modes = self._request_frame(0x21, 0x31, description="clock-mode inquiry")
        if clock_modes != b"\x00":
            raise ProtocolError(
                f"unexpected clock-mode response: {clock_modes.hex(' ')}"
            )
        self._select(0x11, b"\x00", "clock-mode 0 selection")

        multipliers = self._request_frame(
            0x22, 0x32, description="multiplication-ratio inquiry"
        )
        if multipliers != EXPECTED_MULTIPLIER_PAYLOAD:
            raise ProtocolError(
                f"unexpected multiplier response: {multipliers.hex(' ')}"
            )

        frequencies = self._request_frame(
            0x23, 0x33, description="operating-frequency inquiry"
        )
        if frequencies != EXPECTED_FREQUENCY_PAYLOAD:
            raise ProtocolError(
                f"unexpected frequency response: {frequencies.hex(' ')}"
            )

        user_mat = self._request_frame(0x25, 0x35, description="user-MAT inquiry")
        if user_mat != EXPECTED_USER_MAT_PAYLOAD:
            raise ProtocolError(f"unexpected user-MAT geometry: {user_mat.hex(' ')}")

        user_boot_mat = self._request_frame(
            0x24, 0x34, description="user-boot-MAT inquiry"
        )
        if user_boot_mat != EXPECTED_USER_BOOT_MAT_PAYLOAD:
            raise ProtocolError(
                f"unexpected user-boot-MAT geometry: {user_boot_mat.hex(' ')}"
            )

        erase_geometry = self._request_frame(
            0x26,
            0x36,
            size_width=2,
            description="erase-block inquiry",
        )
        if erase_geometry != EXPECTED_ERASE_PAYLOAD:
            raise ProtocolError(
                f"unexpected erase-block geometry: {erase_geometry.hex(' ')}"
            )

        programming_unit = self._request_frame(
            0x27, 0x37, description="programming-unit inquiry"
        )
        if programming_unit != struct.pack(">H", PROGRAM_UNIT):
            raise ProtocolError(
                f"unexpected programming unit: {programming_unit.hex(' ')}"
            )

        self._write(
            baud_selection_frame(self.program_baud),
            f"{self.program_baud}-baud selection",
        )
        self._expect_ack(0x3F, timeout=5.0, description="old-rate baud-selection ACK")
        self._set_baudrate(self.adapter_baud, "new-rate confirmation")
        self._sleep(0.01)
        self._write(b"\x06", "new-rate confirmation")
        self._expect_ack(0x3F, timeout=1.0, description="new-rate confirmation ACK")

        for attempt in range(1, SELECTED_RATE_PROBE_COUNT + 1):
            selected_rate_unit = self._request_frame(
                0x27,
                0x37,
                description=(
                    "selected-rate programming-unit probe "
                    f"{attempt}/{SELECTED_RATE_PROBE_COUNT}"
                ),
            )
            if selected_rate_unit != struct.pack(">H", PROGRAM_UNIT):
                raise ProtocolError(
                    "unexpected programming unit during selected-rate probe: "
                    f"{selected_rate_unit.hex(' ')}"
                )
        if self.adapter_baud == self.program_baud:
            rate_description = f"switched to {self.program_baud:,} baud"
        else:
            rate_description = (
                f"target H3F selection {self.program_baud:,} baud, adapter "
                f"requested {self.adapter_baud:,} baud"
            )
        _emit(
            f"Connected to validated {PRODUCT_NAME}; {rate_description} and passed "
            f"{SELECTED_RATE_PROBE_COUNT} framed link probes."
        )

    def _validate_device_list(self, payload: bytes) -> None:
        if not payload:
            raise ProtocolError("empty supported-device response")
        count = payload[0]
        offset = 1
        devices: list[tuple[bytes, str]] = []
        for _ in range(count):
            if offset >= len(payload):
                raise ProtocolError("truncated supported-device response")
            character_count = payload[offset]
            offset += 1
            record = payload[offset : offset + character_count]
            if len(record) != character_count or character_count < 4:
                raise ProtocolError("malformed supported-device record")
            offset += character_count
            devices.append((record[:4], record[4:].decode("ascii", errors="replace")))
        if offset != len(payload):
            raise ProtocolError("trailing bytes in supported-device response")
        if (DEVICE_CODE, PRODUCT_NAME) not in devices:
            names = ", ".join(
                f"{code.decode('ascii', errors='replace')}/{name}"
                for code, name in devices
            )
            raise ProtocolError(
                f"target is not the supported {DEVICE_CODE.decode()}/{PRODUCT_NAME}; "
                f"reported: {names or 'no devices'}"
            )

    def transition_and_auto_erase(self) -> None:
        _emit(
            "Transitioning to programming mode; the target is now automatically "
            "erasing the entire User MAT and User Boot MAT..."
        )
        self._write(b"\x40", "program/erase transition")
        # The manual specifies that ACK is emitted only after both MATs are erased.
        # Do not retry or send a blank-check while the destructive operation is active.
        self._expect_ack(
            0x40,
            timeout=180.0,
            description="automatic-erasure completion ACK",
        )
        _emit("Automatic erase complete.")

    def require_program_erase_ready(self) -> None:
        payload = self._request_frame(
            0x4F,
            0x5F,
            description="post-erase boot-program status",
        )
        if payload != b"\x3F\x00":
            raise ProtocolError(
                "unexpected post-erase boot-program status/error: "
                f"{payload.hex(' ')}"
            )

    def _mat_is_blank(
        self, command: int, error_response: int, description: str
    ) -> bool:
        self._write(bytes([command]), f"{description} blank check")
        first = self._read_exact(1, 30.0, f"{description} blank-check response")[0]
        if first == 0x06:
            return True
        if first not in (0x80, error_response):
            raise ProtocolError(
                f"{description} blank check expected 0x06 or "
                f"0x{error_response:02X} 0x52, got isolated 0x{first:02X}"
            )
        try:
            error = self._read_exact(1, 1.0, f"{description} blank-check error")[0]
        except ProtocolTimeout as timeout_error:
            raise ProtocolError(
                f"{description} blank check expected 0x06 or "
                f"0x{error_response:02X} 0x52, got isolated 0x{first:02X}"
            ) from timeout_error
        if first == error_response and error == 0x52:
            return False
        raise DeviceRejected(command, first, error)

    def user_boot_mat_is_blank(self) -> bool:
        return self._mat_is_blank(0x4C, 0xCC, "user-boot MAT")

    def user_mat_is_blank(self) -> bool:
        return self._mat_is_blank(0x4D, 0xCD, "user MAT")

    def program(
        self,
        chunks: Sequence[ProgramChunk],
        progress: Callable[[int, int, int], None] | None = None,
    ) -> None:
        self._write(b"\x43", "user-MAT programming selection")
        self._expect_ack(0x43, timeout=10.0, description="programming-selection ACK")
        total = len(chunks)
        for position, chunk in enumerate(chunks, start=1):
            self._write(program_frame(chunk), f"program 0x{chunk.address:06X}")
            # Never retry: if an ACK was lost, the page may already be programmed.
            self._expect_ack(
                0x50,
                timeout=5.0,
                description=f"program ACK at 0x{chunk.address:06X}",
            )
            if progress is not None:
                progress(position, total, chunk.address)
        self._write(program_end_frame(), "programming termination")
        self._expect_ack(0x50, timeout=5.0, description="program-termination ACK")

    def user_mat_checksum(self) -> int:
        payload = self._request_frame(
            0x4B,
            0x5B,
            timeout=60.0,
            description="user-MAT additive checksum",
        )
        if len(payload) != 4:
            raise ProtocolError(
                f"unexpected user-MAT checksum payload: {payload.hex(' ')}"
            )
        return int.from_bytes(payload, "big")

    def read_user_mat(self, address: int, size: int) -> bytes:
        if address < 0 or size <= 0 or address + size > ROM_SIZE:
            raise ValueError("User-MAT read is outside the 1 MiB address range")
        request = b"\x01" + struct.pack(">II", address, size)
        self._write(sized_frame(0x52, request), f"read User MAT at 0x{address:06X}")

        response = self._read_exact(1, 5.0, "memory-read response")[0]
        if response != 0x52:
            if response not in (0x80, 0xD2):
                raise ProtocolError(
                    "memory read expected response 0x52 or an error response, "
                    f"got isolated 0x{response:02X}"
                )
            try:
                detail = self._read_exact(1, 1.0, "memory-read error detail")[0]
            except ProtocolTimeout as timeout_error:
                raise ProtocolError(
                    "memory read expected response 0x52, got isolated "
                    f"0x{response:02X}; no error-detail byte followed"
                ) from timeout_error
            raise DeviceRejected(0x52, response, detail)

        size_bytes = self._read_exact(4, 1.0, "memory-read response size")
        returned_size = int.from_bytes(size_bytes, "big")
        if returned_size != size:
            raise ProtocolError(
                f"memory read at 0x{address:06X} returned size {returned_size}, "
                f"expected {size}"
            )

        wire_seconds = (returned_size + 1) * 10 / self.program_baud
        tail = self._read_exact(
            returned_size + 1,
            max(5.0, wire_seconds * 2 + 1.0),
            f"memory-read data at 0x{address:06X}",
        )
        complete = bytes([response]) + size_bytes + tail
        if sum(complete) & 0xFF:
            raise ProtocolError(
                f"bad checksum in memory-read response at 0x{address:06X}"
            )
        return tail[:-1]

    def verify_user_mat(
        self,
        expected: bytes,
        progress: Callable[[int, int, int], None] | None = None,
    ) -> str:
        if len(expected) != ROM_SIZE:
            raise ValueError("byte-for-byte verification requires a 1 MiB image")
        digest = hashlib.sha256()
        total = ROM_SIZE // READBACK_UNIT
        for position, address in enumerate(range(0, ROM_SIZE, READBACK_UNIT), start=1):
            wanted = expected[address : address + READBACK_UNIT]
            received = self.read_user_mat(address, len(wanted))
            if received != wanted:
                mismatch = next(
                    index
                    for index, (actual, intended) in enumerate(zip(received, wanted))
                    if actual != intended
                )
                failing_address = address + mismatch
                raise ProtocolError(
                    f"byte-for-byte readback mismatch at 0x{failing_address:06X}: "
                    f"image 0x{wanted[mismatch]:02X}, target 0x{received[mismatch]:02X}"
                )
            digest.update(received)
            if progress is not None:
                progress(position, total, address)
        return digest.hexdigest()


def _open_serial(device: str, baud: int, *, fdt_two_stop_bits: bool) -> Any:
    try:
        import serial  # type: ignore[import-not-found]
    except ImportError as error:
        raise FlashToolError(
            "pyserial is required for live flashing; install it with "
            "'python3 -m pip install pyserial'"
        ) from error
    port: Any | None = None
    try:
        port = serial.Serial(
            port=None,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            # Renesas specifies 8N1.  The optional two-stop setting reproduces
            # FDT's extra host-side mark bit, but USB-UART framing is bidirectional.
            stopbits=(
                serial.STOPBITS_TWO if fdt_two_stop_bits else serial.STOPBITS_ONE
            ),
            # All reads use the protocol client's monotonic deadline loop. A
            # fixed nonblocking mode avoids live port reconfiguration after TX.
            timeout=0,
            write_timeout=5.0,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        # Keep modem-control outputs inactive before opening.  They are not part
        # of this protocol and should remain physically disconnected.
        port.dtr = False
        port.rts = False
        port.port = device
        port.open()
        return port
    except Exception as error:
        if port is not None:
            try:
                port.close()
            except Exception:
                pass
        raise FlashToolError(f"cannot open serial device {device}: {error}") from error


def _print_plan(
    plan: ImagePlan,
    port: str | None,
    *,
    program_baud: int,
    adapter_baud: int,
    verification: str,
    fdt_two_stop_bits: bool,
) -> None:
    verification_names = {
        "readback": "byte-for-byte 1 MiB readback (default)",
        "sum": "32-bit additive User-MAT sum (weaker)",
        "none": "ACKs only (unverified)",
    }
    _emit(f"Image:              {plan.path}")
    _emit(f"SHA-256:            {plan.sha256}")
    _emit(f"Image size:         {len(plan.data):,} bytes")
    _emit(
        f"Program records:    {len(plan.chunks):,} x {PROGRAM_UNIT} bytes "
        f"({plan.programmed_bytes:,} bytes sent)"
    )
    _emit(f"Serial device:      {port or '(not specified for dry run)'}")
    if program_baud == DEFAULT_PROGRAM_BAUD:
        baud_status = "(default)"
    elif program_baud == 125_000:
        baud_status = "(manual-derived; user-reported hardware success)"
    elif program_baud == 206_900 and adapter_baud == 206_900:
        baud_status = "(user-validated on operator's Nano-clone/CH340 setup)"
    elif program_baud == 312_500 and adapter_baud == 315_800:
        baud_status = "(user-validated split-baud on operator's Nano-clone/CH340 setup)"
    else:
        baud_status = "(manual-derived; not hardware-proven)"
    _emit(f"Target H3F baud:     {program_baud:,} {baud_status}")
    _emit(
        f"Adapter baud:        {adapter_baud:,} "
        + (
            "(same as target H3F selection)"
            if adapter_baud == program_baud
            else "(explicit override; target H3F selection is unchanged)"
        )
    )
    if program_baud != DEFAULT_PROGRAM_BAUD:
        _emit(
            "Baud warning:       verify the USB-UART driver's actual wire rate; "
            "no automatic fallback is possible after H3F"
        )
    if adapter_baud != program_baud:
        _emit(
            f"Override warning:    adapter request {adapter_baud:,} is "
            "driver-specific; measure the actual wire rate"
        )
    elif program_baud == 206_900:
        _emit(
            "Rate estimate:       Renesas ~208,333.33; inspected Apple CH340 "
            "~206,896.55 (-0.690%)"
        )
    elif program_baud in (312_500, 625_000):
        _emit(
            f"CH340 warning:      {program_baud:,} may be driver-quantized; "
            "measure the actual wire rate"
        )
    if program_baud == 625_000:
        _emit(
            "CH340 rating:       625,000 is above the CH340G/C continuous-rate "
            "rating"
        )
    _emit(
        "Serial framing:     "
        + (
            "8N2 FDT-like host pacing (adapter must tolerate ECU 8N1 RX)"
            if fdt_two_stop_bits
            else "8N1 (Renesas-documented default)"
        )
    )
    _emit("Connection mode:    planned cold boot; identity checked before erase")
    _emit(f"Verification:       {verification_names[verification]}")
    _emit("Programmed ranges:")
    for start, end in plan.ranges:
        _emit(f"  0x{start:08X}-0x{end:08X}  ({end - start + 1:,} bytes)")
    _emit("Cold transition erase: entire User MAT and separate User Boot MAT")
    _emit("Restore scope:       User MAT only; User Boot MAT remains blank")
    _emit(
        "Validation scope:  size/layout and target silicon/geometry only; "
        "not ROM provenance or bootability"
    )


def _progress_printer(
    label: str = "Programming", unit: int = PROGRAM_UNIT
) -> Callable[[int, int, int], None]:
    last_percent = -1

    def report(position: int, total: int, address: int) -> None:
        nonlocal last_percent
        percent = position * 100 // total
        if percent != last_percent or position == total:
            _emit(
                f"{label}: {percent:3d}% "
                f"({position:,}/{total:,}, through 0x{address + unit - 1:06X})"
            )
            last_percent = percent

    return report


def _parse_baud(value: str) -> int:
    try:
        return int(value.replace(",", "").replace("_", ""))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid baud rate: {value!r}") from error


def _parse_positive_baud(value: str) -> int:
    baud = _parse_baud(value)
    if baud <= 0:
        raise argparse.ArgumentTypeError("adapter baud must be positive")
    return baud


def _estimated_program_seconds(
    record_count: int,
    baud: int,
    *,
    adapter_baud: int | None = None,
    fdt_two_stop_bits: bool,
) -> float:
    host_baud = baud if adapter_baud is None else adapter_baud
    host_bits_per_byte = 11 if fdt_two_stop_bits else 10
    selected_wire_seconds = (
        PROGRAM_FRAME_BYTES * host_bits_per_byte / host_baud + 10 / baud
    )
    return record_count * (selected_wire_seconds + TYPICAL_PROGRAM_SECONDS_PER_RECORD)


def _estimated_readback_wire_seconds(
    baud: int,
    *,
    adapter_baud: int | None = None,
    fdt_two_stop_bits: bool,
) -> float:
    host_baud = baud if adapter_baud is None else adapter_baud
    chunk_count = ROM_SIZE // READBACK_UNIT
    request_bits = chunk_count * 12 * (11 if fdt_two_stop_bits else 10)
    response_bits = chunk_count * (1 + 4 + READBACK_UNIT + 1) * 10
    return request_bits / host_baud + response_bits / baud


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Program a 1 MiB image intended for NC ECU recovery to an "
            "identity/geometry-checked R4F70580S over Renesas Protocol-C SCI."
        )
    )
    parser.add_argument("image", type=Path, help="raw 1 MiB user-MAT binary")
    parser.add_argument(
        "--port",
        help="USB serial device, for example /dev/cu.usbserial-0001 or COM3",
    )
    parser.add_argument(
        "--baud",
        "--program-baud",
        dest="program_baud",
        type=_parse_baud,
        choices=PROGRAM_BAUD_OPTIONS,
        default=DEFAULT_PROGRAM_BAUD,
        help=(
            "post-handshake programming/readback baud; 38400 is the default; "
            "validation status for faster choices is shown in the plan"
        ),
    )
    parser.add_argument(
        "--adapter-baud",
        type=_parse_positive_baud,
        help=(
            "advanced host USB-UART baud override after H3F; defaults to --baud "
            "and does not change the target H3F selection"
        ),
    )
    parser.add_argument(
        "--verify",
        choices=("readback", "sum", "none"),
        default="readback",
        help=(
            "post-write verification: full byte readback (default), weaker 32-bit "
            "additive sum, or none"
        ),
    )
    parser.add_argument(
        "--fdt-two-stop-bits",
        action="store_true",
        help=(
            "reproduce FDT's extra host mark bit using 8N2; only for an adapter "
            "validated to receive the target's 8N1 replies"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the image and print the operation plan without opening serial",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive destructive-operation confirmation",
    )
    parser.add_argument("--verbose", action="store_true", help="log protocol traffic")
    args = parser.parse_args(argv)
    if not args.dry_run and args.port is None:
        parser.error("--port is required unless --dry-run is used")
    return args


def _print_post_destructive_recovery_warning(phase: str) -> None:
    _emit(
        f"RECOVERY REQUIRED after {phase}: do not normal-boot or use the ECU; "
        "its flash state is not proven complete.",
        file=sys.stderr,
    )
    if phase == "verification":
        _emit(
            "The read/check command is non-destructive and no erase/program command "
            "is expected to remain active, but the programmed contents are unverified. "
            "Keep the ECU in the controlled boot setup until it is independently "
            "verified or reflashed from a fresh cold session.",
            file=sys.stderr,
        )
    elif phase.startswith("post-erase"):
        _emit(
            "Automatic erase was ACKed, but target readiness and/or the MAT blank "
            "state was not proved. No erase/program command is expected to remain "
            "active; keep the ECU in the controlled boot setup and recover from a "
            "fresh cold session.",
            file=sys.stderr,
        )
    else:
        _emit(
            "If power is still stable, leave power/reset/mode wiring untouched while "
            "an in-flight erase or program command settles. Then reset or power down "
            "and recover with a fresh, validated cold-boot session; do not blindly "
            "retry a record.",
            file=sys.stderr,
        )


def _print_pre_destructive_reset_notice() -> None:
    _emit(
        "No erase/program command was sent, but the cold boot handshake was "
        "started. Reset or power-cycle back into the verified boot setup before "
        "retrying.",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    adapter_baud = args.program_baud if args.adapter_baud is None else args.adapter_baud
    failure_phase: str | None = None
    boot_session_started = False
    port: Any | None = None
    try:
        plan = build_image_plan(args.image)
        _print_plan(
            plan,
            args.port,
            program_baud=args.program_baud,
            adapter_baud=adapter_baud,
            verification=args.verify,
            fdt_two_stop_bits=args.fdt_two_stop_bits,
        )
        if args.dry_run:
            _emit("Dry run complete; no serial device was opened.")
            return 0

        if args.port is None:  # Guard for programmatic callers bypassing argparse.
            raise FlashToolError("--port is required for a live operation")
        port = _open_serial(
            args.port,
            INITIAL_BAUD,
            fdt_two_stop_bits=args.fdt_two_stop_bits,
        )
        boot_session_started = True
        protocol = RenesasBootProtocol(
            port,
            program_baud=args.program_baud,
            adapter_baud=adapter_baud,
            verbose=args.verbose,
        )
        protocol.connect_cold_boot()

        if not args.yes:
            _emit("\nTARGET VALIDATED; NEXT COMMAND IS DESTRUCTIVE:")
            _emit("  - No backup is made.")
            _emit(
                "  - The full 1 MiB User MAT and separate 12 KiB User Boot MAT "
                "are erased."
            )
            _emit("  - Only the User MAT is restored; the User Boot MAT remains blank.")
            _emit(
                "  - The input is not proven to be the correct ECU/ROM variant "
                "or bootable."
            )
            _emit("  - Stable ECU power, wiring, USB, and an awake host are required.")
            _emit("Type ERASE_AND_FLASH to continue: ", end="", flush=True)
            try:
                answer = input()
            except EOFError:
                answer = ""
            if answer != "ERASE_AND_FLASH":
                _emit(
                    "Cancelled before erase/programming. Reset or power down the "
                    "target before another cold session."
                )
                return 1

        # Set this before H40: a transport failure can make command acceptance
        # unknowable even when no response reaches the host.
        failure_phase = "automatic erase"
        protocol.transition_and_auto_erase()
        failure_phase = "post-erase status check"
        protocol.require_program_erase_ready()
        failure_phase = "post-erase blank checks"
        if not protocol.user_boot_mat_is_blank():
            raise ProtocolError(
                "target acknowledged automatic erase but User Boot MAT is not blank"
            )
        if not protocol.user_mat_is_blank():
            raise ProtocolError(
                "target acknowledged automatic erase but User MAT is not blank"
            )

        estimated_seconds = _estimated_program_seconds(
            len(plan.chunks),
            args.program_baud,
            adapter_baud=adapter_baud,
            fdt_two_stop_bits=args.fdt_two_stop_bits,
        )
        _emit(
            f"Programming {plan.programmed_bytes / 1024:.2f} KiB in "
            f"{len(plan.chunks):,} records (wire time plus typical flash time: "
            f"{estimated_seconds:.0f} seconds)..."
        )
        failure_phase = "programming"
        protocol.program(plan.chunks, _progress_printer())
        failure_phase = "verification"

        if args.verify == "readback":
            readback_seconds = _estimated_readback_wire_seconds(
                args.program_baud,
                adapter_baud=adapter_baud,
                fdt_two_stop_bits=args.fdt_two_stop_bits,
            )
            _emit(
                "Reading back and comparing the complete 1 MiB User MAT "
                f"byte-for-byte (about {readback_seconds:.0f} seconds of serial "
                "wire time)..."
            )
            target_sha256 = protocol.verify_user_mat(
                plan.data,
                _progress_printer("Readback", READBACK_UNIT),
            )
            _emit(f"Byte-for-byte readback verified; SHA-256 {target_sha256}.")
        elif args.verify == "sum":
            _emit("Checking the complete User-MAT additive sum...")
            target_sum = protocol.user_mat_checksum()
            if target_sum != plan.user_mat_sum:
                raise ProtocolError(
                    f"verification failed: image sum 0x{plan.user_mat_sum:08X}, "
                    f"target returned 0x{target_sum:08X}"
                )
            _emit(f"User-MAT additive checksum verified: 0x{target_sum:08X}.")
        else:
            _emit(
                "Programming sequence ACKed, but verification was skipped; the "
                "result is UNVERIFIED."
            )
            _emit("External verification is required before attempting a normal boot.")

        if args.verify != "none":
            _emit(
                "Before normal boot, hold reset or remove power, restore the normal "
                "boot/FWE wiring, and disconnect the programming interface."
            )
        failure_phase = None
        return 0
    except KeyboardInterrupt:
        _emit("\nInterrupted.", file=sys.stderr)
        if failure_phase is not None:
            _print_post_destructive_recovery_warning(failure_phase)
        elif boot_session_started:
            _print_pre_destructive_reset_notice()
        return 130
    except FlashToolError as error:
        _emit(f"error: {error}", file=sys.stderr)
        if failure_phase is not None:
            _print_post_destructive_recovery_warning(failure_phase)
        elif boot_session_started:
            _print_pre_destructive_reset_notice()
        return 2
    except Exception as error:
        _emit(f"unexpected error: {type(error).__name__}: {error}", file=sys.stderr)
        if failure_phase is not None:
            _print_post_destructive_recovery_warning(failure_phase)
        elif boot_session_started:
            _print_pre_destructive_reset_notice()
        return 3
    finally:
        if port is not None:
            try:
                port.close()
            except Exception as error:
                _emit(
                    f"warning: could not close serial device cleanly: {error}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    raise SystemExit(main())
