"""Checks for the independently consumable synthetic PDF artifact."""

import importlib.util
from pathlib import Path
import re
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("fixture_pdf", Path(__file__).with_name("generate_pdf.py"))
assert SPEC and SPEC.loader
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


class PdfArtifactTests(unittest.TestCase):
    def test_pdf_cross_references_point_to_objects(self):
        data = generator.pdf_bytes()
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        xref = int(data.rsplit(b"startxref\n", 1)[1].splitlines()[0])
        self.assertEqual(data[xref:xref + 4], b"xref")
        entries = re.findall(rb"(\d{10}) 00000 n ", data[xref:])
        self.assertEqual(len(entries), 7)
        for number, offset in enumerate(entries, 1):
            self.assertTrue(data[int(offset):].startswith(f"{number} 0 obj\n".encode()))
        self.assertEqual(len(re.findall(rb"/Type /Page\b", data)), 2)
        self.assertIn(b"/Count 2", data)

    def test_requested_file_contains_both_page_facts(self):
        with tempfile.TemporaryDirectory(prefix="gptgrep-pdf-") as directory:
            destination = Path(directory) / "fixture.pdf"
            receipt = generator.write_pdf(destination)
            data = destination.read_bytes()
            self.assertIn(b"PAGE_ONE_ANCHOR", data)
            self.assertIn(b"PAGE_TWO_ANCHOR", data)
            self.assertIn(b"ZEPHYR-218", data)
            self.assertEqual(receipt["pages"], 2)
            self.assertEqual(list(Path(directory).iterdir()), [destination])


if __name__ == "__main__":
    unittest.main()
