from __future__ import annotations

import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import flash_sh7058s_boot as flasher


TEST_IMAGE: Path
_test_image_directory: tempfile.TemporaryDirectory[str] | None = None


def setUpModule() -> None:
    global TEST_IMAGE, _test_image_directory
    _test_image_directory = tempfile.TemporaryDirectory()
    TEST_IMAGE = Path(_test_image_directory.name) / "synthetic-test-image.bin"
    data = bytearray(b"\xFF" * flasher.ROM_SIZE)
    data[0x2000:0x2080] = bytes(range(flasher.PROGRAM_UNIT))
    data[0x40000:0x40080] = b"\x00" * flasher.PROGRAM_UNIT
    TEST_IMAGE.write_bytes(data)


def tearDownModule() -> None:
    if _test_image_directory is not None:
        _test_image_directory.cleanup()


class ScriptedSerial:
    def __init__(
        self,
        steps: list[tuple[int, bytes, bytes]],
        *,
        read_limit: int | None = None,
        write_limit: int | None = None,
    ) -> None:
        self.steps = list(steps)
        self.received = bytearray()
        self.baudrate = flasher.INITIAL_BAUD
        self.timeout: float | None = 0
        self.read_limit = read_limit
        self.write_limit = write_limit
        self._write_offset = 0

    def write(self, data: bytes) -> int:
        if not self.steps:
            raise AssertionError(f"unexpected write: {data.hex(' ')}")
        expected_baud, expected_data, response = self.steps[0]
        if self.baudrate != expected_baud:
            raise AssertionError(
                f"write baud {self.baudrate}, expected {expected_baud}"
            )
        remaining = expected_data[self._write_offset :]
        accepted = min(len(data), self.write_limit or len(data))
        if data[:accepted] != remaining[:accepted] or accepted > len(remaining):
            raise AssertionError(
                f"write {data.hex(' ')}, expected remaining {remaining.hex(' ')}"
            )
        self._write_offset += accepted
        if self._write_offset == len(expected_data):
            self.steps.pop(0)
            self._write_offset = 0
            self.received.extend(response)
        return accepted

    def read(self, size: int) -> bytes:
        size = min(size, self.read_limit or size)
        result = bytes(self.received[:size])
        del self.received[:size]
        return result

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self.received.clear()

    def reset_output_buffer(self) -> None:
        pass


def _response(command: int, payload: bytes, *, size_width: int = 1) -> bytes:
    return flasher.sized_frame(command, payload, size_width=size_width)


def _memory_response(data: bytes) -> bytes:
    body = b"\x52" + len(data).to_bytes(4, "big") + data
    return body + bytes([flasher.checksum8(body)])


def _cold_boot_steps(
    program_baud: int, adapter_baud: int | None = None
) -> list[tuple[int, bytes, bytes]]:
    host_baud = program_baud if adapter_baud is None else adapter_baud
    device_payload = b"\x01\x0D" + flasher.DEVICE_CODE + flasher.PRODUCT_NAME.encode()
    steps = [
        (9600, b"\x00", b"\x00"),
        (9600, b"\x55", b"\xE6"),
        (9600, b"\x20", _response(0x30, device_payload)),
        (9600, flasher.sized_frame(0x10, flasher.DEVICE_CODE), b"\x06"),
        (9600, b"\x21", _response(0x31, b"\x00")),
        (9600, flasher.sized_frame(0x11, b"\x00"), b"\x06"),
        (
            9600,
            b"\x22",
            _response(0x32, flasher.EXPECTED_MULTIPLIER_PAYLOAD),
        ),
        (
            9600,
            b"\x23",
            _response(0x33, flasher.EXPECTED_FREQUENCY_PAYLOAD),
        ),
        (9600, b"\x25", _response(0x35, flasher.EXPECTED_USER_MAT_PAYLOAD)),
        (
            9600,
            b"\x24",
            _response(0x34, flasher.EXPECTED_USER_BOOT_MAT_PAYLOAD),
        ),
        (
            9600,
            b"\x26",
            _response(0x36, flasher.EXPECTED_ERASE_PAYLOAD, size_width=2),
        ),
        (9600, b"\x27", _response(0x37, b"\x00\x80")),
        (9600, flasher.baud_selection_frame(program_baud), b"\x06"),
        (host_baud, b"\x06", b"\x06"),
    ]
    steps.extend(
        (
            host_baud,
            b"\x27",
            _response(0x37, b"\x00\x80"),
        )
        for _ in range(flasher.SELECTED_RATE_PROBE_COUNT)
    )
    return steps


class FrameTests(unittest.TestCase):
    def test_control_frame_vectors(self) -> None:
        self.assertEqual(
            flasher.sized_frame(0x10, b"1n01"),
            bytes.fromhex("10 04 31 6E 30 31 EC"),
        )
        self.assertEqual(
            flasher.sized_frame(0x11, b"\x00"),
            bytes.fromhex("11 01 00 EE"),
        )
        self.assertEqual(
            flasher.sized_frame(0x3F, bytes.fromhex("01 80 03 E8 02 08 02")),
            bytes.fromhex("3F 07 01 80 03 E8 02 08 02 42"),
        )
        self.assertEqual(flasher.erase_frame(2), bytes.fromhex("58 01 02 A5"))
        self.assertEqual(flasher.erase_frame(0x0F), bytes.fromhex("58 01 0F 98"))
        self.assertEqual(flasher.erase_frame(0xFF), bytes.fromhex("58 01 FF A8"))
        self.assertEqual(
            flasher.program_end_frame(), bytes.fromhex("50 FF FF FF FF B4")
        )
        self.assertEqual(
            flasher.sized_frame(
                0x52,
                bytes.fromhex("01 00 00 00 00 00 00 10 00"),
            ),
            bytes.fromhex("52 09 01 00 00 00 00 00 00 10 00 94"),
        )
        self.assertEqual(
            flasher.sized_frame(
                0x52,
                bytes.fromhex("01 00 0F F0 00 00 00 10 00"),
            ),
            bytes.fromhex("52 09 01 00 0F F0 00 00 00 10 00 95"),
        )
        self.assertEqual(
            _memory_response(bytes.fromhex("12 34 56 78")),
            bytes.fromhex("52 00 00 00 04 12 34 56 78 96"),
        )

    def test_supported_baud_selection_vectors(self) -> None:
        vectors = {
            38_400: "3F 07 01 80 03 E8 02 08 02 42",
            62_500: "3F 07 02 71 03 E8 02 08 02 50",
            125_000: "3F 07 04 E2 03 E8 02 08 02 DD",
            206_900: "3F 07 08 15 03 E8 02 08 02 A6",
            312_500: "3F 07 0C 35 03 E8 02 08 02 82",
            625_000: "3F 07 18 6A 03 E8 02 08 02 41",
        }
        self.assertEqual(tuple(vectors), flasher.PROGRAM_BAUD_OPTIONS)
        for baud, expected in vectors.items():
            with self.subTest(baud=baud):
                self.assertEqual(
                    flasher.baud_selection_frame(baud), bytes.fromhex(expected)
                )
        with self.assertRaisesRegex(ValueError, "unsupported second-stage baud"):
            flasher.baud_selection_frame(115_200)

    def test_baud_aware_time_estimates(self) -> None:
        record_count = 7_844
        self.assertAlmostEqual(
            flasher._estimated_program_seconds(
                record_count, 38_400, fdt_two_stop_bits=True
            ),
            record_count
            * (
                (flasher.PROGRAM_FRAME_BYTES * 11 + 10) / 38_400
                + flasher.TYPICAL_PROGRAM_SECONDS_PER_RECORD
            ),
        )
        program_estimates = [
            flasher._estimated_program_seconds(
                record_count, baud, fdt_two_stop_bits=False
            )
            for baud in flasher.PROGRAM_BAUD_OPTIONS
        ]
        readback_estimates = [
            flasher._estimated_readback_wire_seconds(baud, fdt_two_stop_bits=False)
            for baud in flasher.PROGRAM_BAUD_OPTIONS
        ]
        self.assertEqual(program_estimates, sorted(program_estimates, reverse=True))
        self.assertEqual(readback_estimates, sorted(readback_estimates, reverse=True))

        target_baud = 312_500
        adapter_baud = 315_800
        same_rate_program = flasher._estimated_program_seconds(
            7_844,
            target_baud,
            fdt_two_stop_bits=False,
        )
        overridden_program = flasher._estimated_program_seconds(
            7_844,
            target_baud,
            adapter_baud=adapter_baud,
            fdt_two_stop_bits=False,
        )
        self.assertLess(overridden_program, same_rate_program)

        overridden_readback = flasher._estimated_readback_wire_seconds(
            target_baud,
            adapter_baud=adapter_baud,
            fdt_two_stop_bits=False,
        )
        chunk_count = flasher.ROM_SIZE // flasher.READBACK_UNIT
        request_bits = chunk_count * 12 * 10
        response_bits = chunk_count * (1 + 4 + flasher.READBACK_UNIT + 1) * 10
        self.assertAlmostEqual(
            overridden_readback,
            request_bits / adapter_baud + response_bits / target_baud,
        )

    def test_program_frame_validates_alignment_and_size(self) -> None:
        with self.assertRaises(ValueError):
            flasher.program_frame(flasher.ProgramChunk(1, b"\x00" * 128))
        with self.assertRaises(ValueError):
            flasher.program_frame(flasher.ProgramChunk(0, b"\x00" * 127))

    def test_generic_command_error_labels_echoed_opcode(self) -> None:
        error = flasher.DeviceRejected(0x27, 0x80, 0x27)
        self.assertIn("target echoed rejected opcode 0x27", str(error))
        self.assertNotIn("operating-frequency error", str(error))


class ImagePlanTests(unittest.TestCase):
    def test_synthetic_image_plan(self) -> None:
        plan = flasher.build_image_plan(TEST_IMAGE)
        self.assertEqual(plan.sha256, hashlib.sha256(plan.data).hexdigest())
        self.assertEqual(len(plan.chunks), 2)
        self.assertEqual(plan.programmed_bytes, 256)
        self.assertEqual(plan.chunks[0].address, 0x2000)
        self.assertEqual(plan.chunks[-1].address, 0x40000)
        self.assertEqual(plan.erase_blocks, (2, 10))
        self.assertEqual(plan.user_mat_sum, sum(plan.data) & 0xFFFFFFFF)

    def test_rejects_wrong_size_reserved_data_and_blank_image(self) -> None:
        cases = (
            b"\xFF" * 128,
            b"\x00" + b"\xFF" * (flasher.ROM_SIZE - 1),
            b"\xFF" * flasher.ROM_SIZE,
        )
        for data in cases:
            with self.subTest(size=len(data), first=data[0]):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "input.bin"
                    path.write_bytes(data)
                    with self.assertRaises(flasher.FlashToolError):
                        flasher.build_image_plan(path)


class ProtocolTranscriptTests(unittest.TestCase):
    def test_cold_boot_program_and_sum_transcript(self) -> None:
        data = bytes(range(128))
        chunk = flasher.ProgramChunk(0x2000, data)
        image = bytearray(b"\xFF" * flasher.ROM_SIZE)
        image[chunk.address : chunk.address + len(data)] = data
        expected_sum = sum(image) & 0xFFFFFFFF

        steps = [
            *_cold_boot_steps(flasher.DEFAULT_PROGRAM_BAUD),
            (38400, b"\x40", b"\x06"),
            (38400, b"\x4F", _response(0x5F, b"\x3F\x00")),
            (38400, b"\x4C", b"\x06"),
            (38400, b"\x4D", b"\x06"),
            (38400, b"\x43", b"\x06"),
            (38400, flasher.program_frame(chunk), b"\x06"),
            (38400, flasher.program_end_frame(), b"\x06"),
            (
                38400,
                b"\x4B",
                _response(0x5B, expected_sum.to_bytes(4, "big")),
            ),
        ]
        serial = ScriptedSerial(steps)
        protocol = flasher.RenesasBootProtocol(serial, sleep=lambda _: None)
        with contextlib.redirect_stdout(io.StringIO()):
            protocol.connect_cold_boot()
            protocol.transition_and_auto_erase()
            protocol.require_program_erase_ready()
            self.assertTrue(protocol.user_boot_mat_is_blank())
            self.assertTrue(protocol.user_mat_is_blank())
            protocol.program((chunk,))
            self.assertEqual(protocol.user_mat_checksum(), expected_sum)
        self.assertEqual(serial.steps, [])
        self.assertEqual(serial.received, b"")

    def test_configurable_second_stage_baud_transcripts(self) -> None:
        for baud in flasher.PROGRAM_BAUD_OPTIONS[1:]:
            with self.subTest(baud=baud):
                serial = ScriptedSerial(_cold_boot_steps(baud))
                protocol = flasher.RenesasBootProtocol(
                    serial,
                    program_baud=baud,
                    sleep=lambda _: None,
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    protocol.connect_cold_boot()
                self.assertEqual(serial.baudrate, baud)
                self.assertEqual(serial.steps, [])

    def test_adapter_baud_override_keeps_target_h3f_selection(self) -> None:
        target_baud = 312_500
        adapter_baud = 315_800
        serial = ScriptedSerial(_cold_boot_steps(target_baud, adapter_baud))
        protocol = flasher.RenesasBootProtocol(
            serial,
            program_baud=target_baud,
            adapter_baud=adapter_baud,
            sleep=lambda _: None,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            protocol.connect_cold_boot()

        self.assertEqual(protocol.program_baud, target_baud)
        self.assertEqual(protocol.adapter_baud, adapter_baud)
        self.assertEqual(serial.baudrate, adapter_baud)
        self.assertEqual(serial.steps, [])
        self.assertIn(
            "target H3F selection 312,500 baud, adapter requested 315,800 baud",
            output.getvalue(),
        )

    def test_full_user_mat_readback_with_fragmented_transport(self) -> None:
        image = bytes(range(256)) * (flasher.ROM_SIZE // 256)
        steps = []
        for address in range(0, flasher.ROM_SIZE, flasher.READBACK_UNIT):
            data = image[address : address + flasher.READBACK_UNIT]
            request = (
                b"\x01" + address.to_bytes(4, "big") + len(data).to_bytes(4, "big")
            )
            steps.append(
                (
                    38400,
                    flasher.sized_frame(0x52, request),
                    _memory_response(data),
                )
            )
        serial = ScriptedSerial(steps, read_limit=37, write_limit=3)
        serial.baudrate = 38400
        protocol = flasher.RenesasBootProtocol(serial, sleep=lambda _: None)
        digest = protocol.verify_user_mat(image)
        self.assertEqual(digest, hashlib.sha256(image).hexdigest())
        self.assertEqual(serial.steps, [])

    def test_serial_read_error_is_contextual(self) -> None:
        class BrokenSerial(ScriptedSerial):
            def read(self, size: int) -> bytes:
                raise OSError("adapter disconnected")

        serial = BrokenSerial([(9600, b"\x00", b"\x00")])
        protocol = flasher.RenesasBootProtocol(serial, sleep=lambda _: None)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(
                flasher.FlashToolError,
                "serial read failed while waiting for autobaud completion",
            ):
                protocol.connect_cold_boot()

    def test_memory_read_rejection_and_bad_checksum_fail_closed(self) -> None:
        request = flasher.sized_frame(
            0x52,
            bytes.fromhex("01 00 00 00 00 00 00 00 04"),
        )
        rejected_serial = ScriptedSerial([(38400, request, b"\xD2\x2A")])
        rejected_serial.baudrate = 38400
        rejected = flasher.RenesasBootProtocol(rejected_serial)
        with self.assertRaises(flasher.DeviceRejected):
            rejected.read_user_mat(0, 4)

        isolated_serial = ScriptedSerial([(38400, request, b"\xFC")])
        isolated_serial.baudrate = 38400
        isolated = flasher.RenesasBootProtocol(isolated_serial)
        with self.assertRaisesRegex(flasher.ProtocolError, "isolated 0xFC"):
            isolated.read_user_mat(0, 4)

        bad_response = bytearray(bytes.fromhex("52 00 00 00 04 12 34 56 78 96"))
        bad_response[-1] ^= 1
        corrupt_serial = ScriptedSerial([(38400, request, bytes(bad_response))])
        corrupt_serial.baudrate = 38400
        corrupt = flasher.RenesasBootProtocol(corrupt_serial)
        with self.assertRaisesRegex(flasher.ProtocolError, "bad checksum"):
            corrupt.read_user_mat(0, 4)

        wrong_size_serial = ScriptedSerial(
            [(38400, request, bytes.fromhex("52 00 00 00 05"))]
        )
        wrong_size_serial.baudrate = 38400
        wrong_size = flasher.RenesasBootProtocol(wrong_size_serial)
        with self.assertRaisesRegex(flasher.ProtocolError, "returned size 5"):
            wrong_size.read_user_mat(0, 4)

        class Clock:
            now = 0.0

            def monotonic(self) -> float:
                return self.now

            def sleep(self, duration: float) -> None:
                self.now += duration

        clock = Clock()
        truncated_serial = ScriptedSerial(
            [(38400, request, bytes.fromhex("52 00 00 00 04 12"))]
        )
        truncated_serial.baudrate = 38400
        truncated = flasher.RenesasBootProtocol(
            truncated_serial,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        with self.assertRaises(flasher.ProtocolTimeout):
            truncated.read_user_mat(0, 4)

    def test_isolated_unexpected_blank_check_byte_is_preserved(self) -> None:
        class Clock:
            now = 0.0

            def monotonic(self) -> float:
                return self.now

            def sleep(self, duration: float) -> None:
                self.now += duration

        clock = Clock()
        serial = ScriptedSerial([(38_400, b"\x4C", b"\xF1")])
        serial.baudrate = 38_400
        protocol = flasher.RenesasBootProtocol(
            serial,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        with self.assertRaisesRegex(flasher.ProtocolError, "got isolated 0xF1"):
            protocol.user_boot_mat_is_blank()

    def test_read_does_not_reconfigure_timeout_after_transmit(self) -> None:
        class TimeoutGuardSerial(ScriptedSerial):
            def __init__(self, steps: list[tuple[int, bytes, bytes]]) -> None:
                self._timeout: float | None = None
                self.timeout_assignments = 0
                self.reject_timeout_assignment = False
                super().__init__(steps)
                self.reject_timeout_assignment = True

            @property
            def timeout(self) -> float | None:
                return self._timeout

            @timeout.setter
            def timeout(self, value: float | None) -> None:
                if self.reject_timeout_assignment:
                    raise AssertionError("read path reconfigured the live serial port")
                self.timeout_assignments += 1
                self._timeout = value

        serial = TimeoutGuardSerial([(62_500, b"\x4C", b"\x06")])
        serial.baudrate = 62_500
        protocol = flasher.RenesasBootProtocol(
            serial,
            program_baud=62_500,
            sleep=lambda _: None,
        )

        self.assertTrue(protocol.user_boot_mat_is_blank())
        self.assertEqual(serial.timeout_assignments, 1)


class CliSafetyTests(unittest.TestCase):
    class DummyPort:
        def close(self) -> None:
            pass

    def test_dry_run_needs_no_port(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = flasher.main([str(TEST_IMAGE), "--dry-run"])
        self.assertEqual(result, 0)
        self.assertIn("no serial device was opened", output.getvalue())

    def test_dry_run_accepts_all_configurable_bauds(self) -> None:
        spellings = (
            "38,400",
            "62,500",
            "125000",
            "206,900",
            "312_500",
            "625000",
        )
        for spelling, expected in zip(
            spellings, flasher.PROGRAM_BAUD_OPTIONS, strict=True
        ):
            with self.subTest(baud=spelling):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = flasher.main(
                        [str(TEST_IMAGE), "--dry-run", "--baud", spelling]
                    )
                self.assertEqual(result, 0)
                self.assertIn(f"Target H3F baud:     {expected:,}", output.getvalue())
                self.assertIn(f"Adapter baud:        {expected:,}", output.getvalue())
                if expected == 206_900:
                    self.assertIn(
                        "Renesas ~208,333.33; inspected Apple CH340 ~206,896.55",
                        output.getvalue(),
                    )
                    self.assertIn(
                        "user-validated on operator's Nano-clone/CH340 setup",
                        output.getvalue(),
                    )
                if expected in (312_500, 625_000):
                    self.assertIn("CH340 warning:", output.getvalue())
                if expected == 312_500:
                    self.assertIn("not hardware-proven", output.getvalue())

    def test_dry_run_displays_explicit_adapter_baud_override(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = flasher.main(
                [
                    str(TEST_IMAGE),
                    "--dry-run",
                    "--baud",
                    "312500",
                    "--adapter-baud",
                    "315,800",
                ]
            )

        plan = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("Target H3F baud:     312,500", plan)
        self.assertIn("Adapter baud:        315,800", plan)
        self.assertIn("explicit override; target H3F selection is unchanged", plan)
        self.assertIn("Override warning:    adapter request 315,800", plan)
        self.assertIn(
            "user-validated split-baud on operator's Nano-clone/CH340 setup",
            plan,
        )
        self.assertNotIn("CH340 warning:", plan)

    def test_cli_rejects_nonpositive_adapter_baud(self) -> None:
        for value in ("0", "-1"):
            with (
                self.subTest(value=value),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                flasher._parse_args(
                    [
                        str(TEST_IMAGE),
                        "--dry-run",
                        "--adapter-baud",
                        value,
                    ]
                )

    def test_cli_rejects_an_unsupported_baud(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            flasher._parse_args([str(TEST_IMAGE), "--dry-run", "--baud", "115200"])

    def test_pre_erase_failure_has_no_partial_flash_warning(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(
                flasher,
                "_open_serial",
                side_effect=flasher.FlashToolError("cannot open"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            result = flasher.main([str(TEST_IMAGE), "--port", "dummy", "--yes"])
        self.assertEqual(result, 2)
        self.assertNotIn("RECOVERY REQUIRED", stderr.getvalue())

    def test_h40_ack_loss_has_phase_specific_recovery_warning(self) -> None:
        protocol = mock.Mock()
        protocol.transition_and_auto_erase.side_effect = flasher.FlashToolError(
            "lost ACK"
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(
                flasher,
                "_open_serial",
                return_value=self.DummyPort(),
            ),
            mock.patch.object(
                flasher,
                "RenesasBootProtocol",
                return_value=protocol,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            result = flasher.main([str(TEST_IMAGE), "--port", "dummy", "--yes"])
        self.assertEqual(result, 2)
        self.assertIn("RECOVERY REQUIRED after automatic erase", stderr.getvalue())

    def test_post_h40_status_failure_says_erase_was_acked(self) -> None:
        protocol = mock.Mock()
        protocol.require_program_erase_ready.side_effect = flasher.FlashToolError(
            "bad status"
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(
                flasher,
                "_open_serial",
                return_value=self.DummyPort(),
            ),
            mock.patch.object(
                flasher,
                "RenesasBootProtocol",
                return_value=protocol,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            result = flasher.main([str(TEST_IMAGE), "--port", "dummy", "--yes"])

        warning = stderr.getvalue()
        self.assertEqual(result, 2)
        self.assertIn(
            "RECOVERY REQUIRED after post-erase status check",
            warning,
        )
        self.assertIn("Automatic erase was ACKed", warning)
        self.assertNotIn("in-flight erase or program command settles", warning)

    def test_started_handshake_failure_requires_reset_but_not_flash_recovery(
        self,
    ) -> None:
        protocol = mock.Mock()
        protocol.connect_cold_boot.side_effect = flasher.FlashToolError("bad reply")
        stderr = io.StringIO()
        with (
            mock.patch.object(
                flasher,
                "_open_serial",
                return_value=self.DummyPort(),
            ),
            mock.patch.object(
                flasher,
                "RenesasBootProtocol",
                return_value=protocol,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            result = flasher.main([str(TEST_IMAGE), "--port", "dummy", "--yes"])
        self.assertEqual(result, 2)
        self.assertIn("cold boot handshake was started", stderr.getvalue())
        self.assertNotIn("RECOVERY REQUIRED", stderr.getvalue())

    def test_no_verification_never_recommends_normal_boot(self) -> None:
        protocol = mock.Mock()
        protocol.user_boot_mat_is_blank.return_value = True
        protocol.user_mat_is_blank.return_value = True
        stdout = io.StringIO()
        protocol_class = mock.Mock(return_value=protocol)
        with (
            mock.patch.object(
                flasher,
                "_open_serial",
                return_value=self.DummyPort(),
            ),
            mock.patch.object(
                flasher,
                "RenesasBootProtocol",
                protocol_class,
            ),
            contextlib.redirect_stdout(stdout),
        ):
            result = flasher.main(
                [
                    str(TEST_IMAGE),
                    "--port",
                    "dummy",
                    "--baud",
                    "125000",
                    "--verify",
                    "none",
                    "--yes",
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("result is UNVERIFIED", stdout.getvalue())
        self.assertIn("External verification is required", stdout.getvalue())
        self.assertNotIn("Before normal boot", stdout.getvalue())
        protocol_class.assert_called_once_with(
            mock.ANY,
            program_baud=125_000,
            adapter_baud=125_000,
            verbose=False,
        )


if __name__ == "__main__":
    unittest.main()
