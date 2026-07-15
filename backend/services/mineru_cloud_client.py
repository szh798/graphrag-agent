"""MinerU cloud API client for local file parsing."""
from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import requests
from requests import exceptions as requests_exceptions
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=False)

ProgressCallback = Callable[[str, dict[str, Any]], None]


class MinerUCloudError(RuntimeError):
    pass


class MinerUCloudClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        api_base: str | None = None,
        model_version: str | None = None,
        session=requests,
        poll_interval: float | None = None,
        max_polls: int | None = None,
        request_timeout: float | None = None,
        upload_timeout: float | None = None,
        download_timeout: float | None = None,
        download_retries: int | None = None,
    ) -> None:
        self.token = (token if token is not None else os.getenv("MINERU_API_TOKEN", "")).strip()
        self.api_base = (api_base or os.getenv("MINERU_BASE_URL", "https://mineru.net/api/v4")).rstrip("/")
        self.model_version = model_version or os.getenv("MINERU_MODEL_VERSION", "pipeline")
        self.session = session
        self.poll_interval = poll_interval if poll_interval is not None else float(os.getenv("MINERU_POLL_INTERVAL_SECONDS", "5"))
        self.max_polls = max_polls if max_polls is not None else int(os.getenv("MINERU_MAX_POLLS", "120"))
        self.request_timeout = request_timeout if request_timeout is not None else float(os.getenv("MINERU_REQUEST_TIMEOUT_SECONDS", "30"))
        self.upload_timeout = upload_timeout if upload_timeout is not None else float(os.getenv("MINERU_UPLOAD_TIMEOUT_SECONDS", "300"))
        self.download_timeout = download_timeout if download_timeout is not None else float(os.getenv("MINERU_DOWNLOAD_TIMEOUT_SECONDS", "300"))
        self.download_retries = download_retries if download_retries is not None else int(os.getenv("MINERU_DOWNLOAD_RETRIES", "3"))

    @property
    def auth_headers(self) -> dict[str, str]:
        self._require_token()
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def parse_local_file(
        self,
        file_path: Path,
        output_dir: Path,
        *,
        data_id: str,
        language: str,
        enable_formula: bool,
        enable_table: bool,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        self._require_token()
        file_path = Path(file_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        batch_id, upload_url = self._create_upload_batch(
            file_path.name,
            data_id=data_id,
            language=language,
            enable_formula=enable_formula,
            enable_table=enable_table,
        )
        self._upload_file(upload_url, file_path)
        zip_url = self._poll_result(batch_id, progress_callback=progress_callback)
        self._download_and_extract(zip_url, output_dir)
        return self._find_content_list(output_dir)

    def _create_upload_batch(
        self,
        filename: str,
        *,
        data_id: str,
        language: str,
        enable_formula: bool,
        enable_table: bool,
    ) -> tuple[str, str]:
        payload = {
            "files": [{"name": filename, "data_id": data_id}],
            "model_version": self.model_version,
            "enable_formula": enable_formula,
            "enable_table": enable_table,
            "language": language,
        }
        data = self._post_json("/file-urls/batch", payload)
        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls") or []
        if not batch_id or not file_urls:
            raise MinerUCloudError("MinerU did not return batch_id/file_urls")
        return str(batch_id), str(file_urls[0])

    def _upload_file(self, upload_url: str, file_path: Path) -> None:
        with open(file_path, "rb") as f:
            response = self.session.put(upload_url, data=f, timeout=self.upload_timeout)
        if response.status_code >= 400:
            raise MinerUCloudError(f"MinerU upload failed: HTTP {response.status_code} {response.text[:300]}")

    def _poll_result(self, batch_id: str, *, progress_callback: ProgressCallback | None = None) -> str:
        url = f"{self.api_base}/extract-results/batch/{batch_id}"
        headers = {"Authorization": f"Bearer {self.token}"}
        last_state = "unknown"
        for _ in range(self.max_polls):
            response = self.session.get(url, headers=headers, timeout=self.request_timeout)
            payload = self._decode_response(response, "poll MinerU result")
            results = ((payload.get("data") or {}).get("extract_result") or [])
            if not results:
                raise MinerUCloudError("MinerU returned no extract_result entries")
            result = results[0]
            state = str(result.get("state") or "unknown")
            last_state = state
            progress = result.get("extract_progress") or {}
            if result.get("full_zip_url"):
                progress = {**progress, "full_zip_url": result["full_zip_url"]}
            if progress_callback:
                progress_callback(state, progress)
            if state == "done":
                zip_url = result.get("full_zip_url")
                if not zip_url:
                    raise MinerUCloudError("MinerU finished without full_zip_url")
                return str(zip_url)
            if state == "failed":
                raise MinerUCloudError(f"MinerU parsing failed: {result.get('err_msg') or 'unknown error'}")
            time.sleep(self.poll_interval)
        raise MinerUCloudError(f"MinerU parsing timed out after {self.max_polls} polls (last state={last_state})")

    def _download_and_extract(self, zip_url: str, output_dir: Path) -> None:
        last_error: Exception | None = None
        response = None
        for attempt in range(max(1, self.download_retries)):
            try:
                response = self.session.get(zip_url, timeout=self.download_timeout)
                break
            except requests_exceptions.RequestException as exc:
                last_error = exc
                if attempt + 1 < self.download_retries:
                    time.sleep(min(2 ** attempt, 10))
        if response is None:
            raise MinerUCloudError(f"MinerU result download failed after {self.download_retries} attempts: {last_error}") from last_error
        if response.status_code >= 400:
            raise MinerUCloudError(f"MinerU result download failed: HTTP {response.status_code}")
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                self._safe_extract(zf, output_dir)
        except zipfile.BadZipFile as exc:
            raise MinerUCloudError("MinerU result is not a valid ZIP archive") from exc

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            f"{self.api_base}{path}",
            headers=self.auth_headers,
            json=payload,
            timeout=self.request_timeout,
        )
        result = self._decode_response(response, f"POST {path}")
        return result.get("data") or {}

    def _decode_response(self, response, action: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise MinerUCloudError(f"{action} failed: HTTP {response.status_code} {response.text[:300]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MinerUCloudError(f"{action} returned non-JSON response") from exc
        if payload.get("code") != 0:
            raise MinerUCloudError(f"{action} failed: {payload.get('msg') or payload}")
        return payload

    def _find_content_list(self, output_dir: Path) -> Path:
        matches = sorted(output_dir.rglob("*_content_list.json"))
        if not matches:
            matches = sorted(output_dir.rglob("*content_list.json"))
        if not matches:
            raise MinerUCloudError(f"MinerU output content_list.json not found in {output_dir}")
        return matches[0]

    def _safe_extract(self, zf: zipfile.ZipFile, output_dir: Path) -> None:
        output_root = output_dir.resolve()
        for member in zf.infolist():
            target = (output_dir / member.filename).resolve()
            if output_root not in (target, *target.parents):
                raise MinerUCloudError(f"Refusing unsafe ZIP member path: {member.filename}")
            zf.extract(member, output_dir)

    def _require_token(self) -> None:
        if not self.token:
            raise MinerUCloudError("MINERU_API_TOKEN not set in backend/.env")
