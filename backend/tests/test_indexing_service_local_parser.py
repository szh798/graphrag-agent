from __future__ import annotations

import tempfile
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class FakeAnnotatedDocument:
    extractions = []


def _run_pipeline_with_doc(env: dict[str, str]):
    from services import indexing_service as idx

    calls: list[tuple[Path, Path, dict]] = []

    def fake_local_parse(file_path, output_dir, **kwargs):
        calls.append((file_path, output_dir, kwargs))
        output_dir.mkdir(parents=True, exist_ok=True)
        content_list = output_dir / "fake_content_list.json"
        content_list.write_text('[{"type":"text","text":"Offline GraphRAG","page_idx":0}]', encoding="utf-8")
        return content_list

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdf_path = root / "input.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        job_id = "job_local_test"
        meta = {
            "job_id": job_id,
            "doc_id": "doc_1",
            "status": "submitted",
            "stage": "Job submitted",
            "progress": {"parsed_pages": 0, "total_pages": 0, "extracted_entities": 0},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": 0.0,
            "error": None,
            "pdf_name": "input.pdf",
            "pdf_path": str(pdf_path),
        }
        saved_meta = meta.copy()

        def load_job_meta(_job_id):
            return saved_meta.copy()

        def save_job_meta(_job_id, next_meta):
            saved_meta.clear()
            saved_meta.update(next_meta)

        class UnexpectedMinerUClient:
            def parse_local_file(self, *_args, **_kwargs):
                raise AssertionError("MinerU should not be used for this test")

        with (
            patch.dict("os.environ", env, clear=False),
            patch("dotenv.load_dotenv", return_value=True),
            patch.object(idx.fs, "load_job_meta", side_effect=load_job_meta),
            patch.object(idx.fs, "save_job_meta", side_effect=save_job_meta),
            patch.object(idx.fs, "job_dir", return_value=root / job_id),
            patch.object(idx.fs, "write_json"),
            patch.object(idx, "update_doc_status"),
            patch.object(idx, "MinerUCloudClient", return_value=UnexpectedMinerUClient()),
            patch.object(idx.local_parser, "parse_local_file", side_effect=fake_local_parse),
            patch("pipeline.entity_extractor.create_model", return_value=object()),
            patch("pipeline.entity_extractor.extract_entities", return_value=FakeAnnotatedDocument()),
            patch("pipeline.kg_builder.extractions_to_records", return_value=[]),
            patch("pipeline.kg_builder.build_kg", return_value=([], [])),
            patch.object(idx.fs, "merge_kg", return_value=(0, 0)),
            patch.object(idx.fs, "get_doc", return_value={
                "doc_id": "doc_1",
                "format": "pdf",
                "language": "ch",
                "enable_formula": True,
                "enable_table": True,
            }),
        ):
            idx._cancel_flags[job_id] = False
            idx._run_pipeline(job_id)

    return saved_meta, calls


class IndexingServiceLocalParserTests(unittest.TestCase):
    def test_forced_local_parser_mode_indexes_without_mineru(self):
        saved_meta, calls = _run_pipeline_with_doc({
            "PARSER_MODE": "local",
            "MINERU_API_TOKEN": "",
        })

        self.assertEqual(saved_meta["status"], "done")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]["data_id"], "job_local_test")

    def test_auto_mode_without_mineru_token_uses_local_parser(self):
        saved_meta, calls = _run_pipeline_with_doc({
            "PARSER_MODE": "auto",
            "MINERU_API_TOKEN": "",
        })

        self.assertEqual(saved_meta["status"], "done")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
