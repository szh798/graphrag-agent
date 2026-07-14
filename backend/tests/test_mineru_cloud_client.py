from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from requests.exceptions import SSLError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = str(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, zip_bytes: bytes):
        self.zip_bytes = zip_bytes
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("post", url, headers, json, timeout))
        return FakeResponse(payload={
            "code": 0,
            "data": {
                "batch_id": "batch-1",
                "file_urls": ["https://upload.example/presigned"],
            },
        })

    def put(self, url, data=None, timeout=None):
        self.calls.append(("put", url, data.read(), timeout))
        return FakeResponse(status_code=200)

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("get", url, headers, timeout))
        if url.endswith("/extract-results/batch/batch-1"):
            return FakeResponse(payload={
                "code": 0,
                "data": {
                    "extract_result": [{
                        "state": "done",
                        "full_zip_url": "https://download.example/result.zip",
                        "extract_progress": {"extracted_pages": 1, "total_pages": 1},
                    }],
                },
            })
        return FakeResponse(status_code=200, content=self.zip_bytes)


class FlakyDownloadSession(FakeSession):
    def __init__(self, zip_bytes: bytes):
        super().__init__(zip_bytes)
        self.download_attempts = 0

    def get(self, url, headers=None, timeout=None):
        if url == "https://download.example/result.zip":
            self.download_attempts += 1
            if self.download_attempts == 1:
                raise SSLError("temporary ssl eof")
        return super().get(url, headers=headers, timeout=timeout)


class MinerUCloudClientTests(unittest.TestCase):
    def test_parse_local_file_uploads_without_headers_and_extracts_content_list(self):
        from services.mineru_cloud_client import MinerUCloudClient

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("result/full.md", "# Parsed")
            zf.writestr("result/abc_content_list.json", '[{"type":"text","text":"hello","page_idx":0}]')

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            input_pdf = tmp_path / "demo.pdf"
            input_pdf.write_bytes(b"%PDF-1.4")
            output_dir = tmp_path / "out"
            fake_session = FakeSession(zip_buffer.getvalue())

            client = MinerUCloudClient(
                token="token-123",
                api_base="https://mineru.example/api/v4",
                session=fake_session,
                poll_interval=0,
                max_polls=1,
            )

            content_list_path = client.parse_local_file(
                input_pdf,
                output_dir,
                data_id="job-1",
                language="ch",
                enable_formula=True,
                enable_table=True,
            )

            self.assertTrue(content_list_path.name.endswith("_content_list.json"))
            self.assertTrue(content_list_path.exists())

            post_call = fake_session.calls[0]
            self.assertEqual(post_call[0], "post")
            self.assertEqual(post_call[2]["Authorization"], "Bearer token-123")
            self.assertEqual(post_call[3]["files"][0]["name"], "demo.pdf")
            self.assertEqual(post_call[3]["model_version"], "pipeline")

            put_call = fake_session.calls[1]
            self.assertEqual(put_call[0], "put")
            self.assertEqual(put_call[1], "https://upload.example/presigned")
            self.assertEqual(put_call[2], b"%PDF-1.4")
            self.assertEqual(len(put_call), 4)

    def test_parse_local_file_retries_transient_zip_download_errors(self):
        from services.mineru_cloud_client import MinerUCloudClient

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("abc_content_list.json", '[{"type":"text","text":"hello","page_idx":0}]')

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            input_pdf = tmp_path / "demo.pdf"
            input_pdf.write_bytes(b"%PDF-1.4")
            fake_session = FlakyDownloadSession(zip_buffer.getvalue())

            client = MinerUCloudClient(
                token="token-123",
                api_base="https://mineru.example/api/v4",
                session=fake_session,
                poll_interval=0,
                max_polls=1,
                download_retries=2,
            )

            content_list_path = client.parse_local_file(
                input_pdf,
                tmp_path / "out",
                data_id="job-1",
                language="ch",
                enable_formula=True,
                enable_table=True,
            )

            self.assertTrue(content_list_path.exists())
            self.assertEqual(fake_session.download_attempts, 2)


if __name__ == "__main__":
    unittest.main()
