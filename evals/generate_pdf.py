#!/usr/bin/env python3
"""Write a deterministic, project-authored two-page text PDF to one requested path."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

PAGE_LINES = (
    (
        "GPTgrep generated fixture - recovery",
        "PAGE_ONE_ANCHOR",
        "Atlas backup interval is 17 minutes.",
        "Origin: project-authored synthetic test content.",
    ),
    (
        "GPTgrep generated fixture - retention",
        "PAGE_TWO_ANCHOR",
        "Harbor retention window is 37 days.",
        "Restore authorization requires token ZEPHYR-218.",
    ),
)


def pdf_bytes() -> bytes:
    def stream(lines: tuple[str, ...]) -> bytes:
        commands = ["BT", "/F1 12 Tf", "72 720 Td"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.extend([f"({escaped}) Tj", "0 -22 Td"])
        commands.append("ET")
        content = ("\n".join(commands) + "\n").encode("ascii")
        return b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream"

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>",
        stream(PAGE_LINES[0]),
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>",
        stream(PAGE_LINES[1]),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def write_pdf(destination: Path) -> dict[str, object]:
    data = pdf_bytes()
    destination.write_bytes(data)
    return {
        "path": str(destination),
        "pages": 2,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "origin": "project_authored_synthetic_fixture",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="PDF destination; its parent directory must already exist")
    args = parser.parse_args()
    print(json.dumps(write_pdf(args.output), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
