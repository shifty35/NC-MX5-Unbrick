#!/usr/bin/env python3
"""Extract and decode ROMDrop's internally named NC1/NC2 bootloaders."""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path


ROMDROP_SHA256 = "4572ca298595104c6b6ac51c459492be3896e64bb5615bcd4982a87ab8ec5c89"
CHOMP_KEY = b"!5XMCECN"


@dataclass(frozen=True)
class SblSpec:
    name: str
    offset: int
    length: int
    raw_sha256: str
    decoded_sha256: str
    footer: bytes
    filename: str


SBL_SPECS = (
    SblSpec(
        name="NC1",
        offset=0x28E26,
        length=0x154C,
        raw_sha256="343709b5d121addb5bc71b1f1d03ffaff9949158cf4150133df203b327ff9d09",
        decoded_sha256="b36ff927ef5f5eeb46d77b973e476ef03e40df3ecf5ac067ad586058f3536287",
        footer=b"0200*SBL_END",
        filename="romdrop_nc1_sbl.bin",
    ),
    SblSpec(
        name="NC2",
        offset=0x2A372,
        length=0x16F4,
        raw_sha256="2411e702ac9b05da78c000ced6008dcb55eb99b0b4d392cd65026075c9960490",
        decoded_sha256="731311951e5761c017ac57ec1ab5e66c36ee91ab993fcf571fb352edf3671f53",
        footer=b"0300*SBL_END",
        filename="romdrop_nc2_sbl.bin",
    ),
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def chomp(data: bytes) -> bytes:
    """Apply ROMDrop's symmetric reverse-order XOR transformation."""
    result = bytearray(data)
    for index in range(len(result)):
        destination = len(result) - 1 - index
        result[destination] ^= (CHOMP_KEY[index % len(CHOMP_KEY)] + index) & 0xFF
    return bytes(result)


def extract(binary_path: Path, output_dir: Path) -> None:
    binary = binary_path.read_bytes()
    actual_binary_hash = sha256(binary)
    if actual_binary_hash != ROMDROP_SHA256:
        raise SystemExit(
            f"refusing unknown ROMDrop binary: expected {ROMDROP_SHA256}, "
            f"got {actual_binary_hash}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    for spec in SBL_SPECS:
        end = spec.offset + spec.length
        raw = binary[spec.offset : end]
        if len(raw) != spec.length:
            raise SystemExit(f"{spec.name}: embedded range extends beyond the binary")
        if sha256(raw) != spec.raw_sha256:
            raise SystemExit(f"{spec.name}: embedded-byte hash mismatch")

        decoded = chomp(raw)
        if sha256(decoded) != spec.decoded_sha256:
            raise SystemExit(f"{spec.name}: decoded-byte hash mismatch")
        if not decoded.endswith(spec.footer):
            raise SystemExit(f"{spec.name}: decoded SBL footer mismatch")

        output_path = output_dir / spec.filename
        output_path.write_bytes(decoded)
        print(
            f"{spec.name}: wrote {len(decoded):#x} bytes to {output_path} "
            f"(sha256 {spec.decoded_sha256})"
        )


def main() -> None:
    project_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "binary",
        type=Path,
        help="operator-supplied ROMDrop Mach-O to extract",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=project_dir / "bootloaders",
        help="decoded SBL destination directory (default: ../bootloaders)",
    )
    args = parser.parse_args()
    extract(args.binary, args.output_dir)


if __name__ == "__main__":
    main()
