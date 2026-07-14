from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _make_text_pdf_bytes(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 24 Tf 100 700 Td ({escaped}) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /MediaBox [0 0 612 792] /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    pdf.extend(
        f"trailer\n<< /Root 1 0 R /Size {len(objects) + 1} >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("latin-1")
    )
    return bytes(pdf)


class LocalParserTests(unittest.TestCase):
    def test_parse_markdown_writes_mineru_compatible_content_list(self):
        from services.local_parser import parse_local_file

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "demo.md"
            source.write_text("# Offline Demo\n\nGraphRAG local parsing works.", encoding="utf-8")

            content_list_path = parse_local_file(source, root / "out", data_id="job_local")
            content = json.loads(content_list_path.read_text(encoding="utf-8"))

        self.assertEqual(content_list_path.name, "job_local_content_list.json")
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[0]["page_idx"], 0)
        self.assertIn("GraphRAG local parsing works.", content[0]["text"])
        self.assertEqual(content[0]["source"], "local_parser")

    def test_parse_html_strips_markup_into_text_block(self):
        from services.local_parser import parse_local_file

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "demo.html"
            source.write_text("<html><body><h1>Alpha</h1><p>Beta paragraph.</p></body></html>", encoding="utf-8")

            content_list_path = parse_local_file(source, root / "out", data_id="job_html")
            text = json.loads(content_list_path.read_text(encoding="utf-8"))[0]["text"]

        self.assertIn("Alpha", text)
        self.assertIn("Beta paragraph.", text)
        self.assertNotIn("<h1>", text)

    def test_parse_text_pdf_extracts_selectable_text(self):
        from services.local_parser import parse_local_file

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "demo.pdf"
            source.write_bytes(_make_text_pdf_bytes("GraphRAG Offline PDF"))

            content_list_path = parse_local_file(source, root / "out", data_id="job_pdf")
            content = json.loads(content_list_path.read_text(encoding="utf-8"))

        self.assertEqual(content[0]["page_idx"], 0)
        self.assertIn("GraphRAG Offline PDF", content[0]["text"])

    def test_parse_empty_pdf_reports_ocr_required(self):
        from services.local_parser import LocalParserError, parse_local_file

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "empty.pdf"
            source.write_bytes(_make_text_pdf_bytes(""))

            with self.assertRaisesRegex(LocalParserError, "OCR"):
                parse_local_file(source, root / "out", data_id="job_empty")


if __name__ == "__main__":
    unittest.main()
