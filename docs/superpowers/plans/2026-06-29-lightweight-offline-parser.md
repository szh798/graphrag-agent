# Lightweight Offline Parser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add local text-based document parsing so new indexing can run without MinerU cloud for selectable-text files.

**Architecture:** Create a `services.local_parser` module that writes MinerU-compatible `content_list.json`; update `services.indexing_service` to choose MinerU or local parser by `PARSER_MODE`; keep extraction and KG building unchanged. Update dependencies, docs, health reporting, tests, and packaging.

**Tech Stack:** Python 3.12, FastAPI service modules, `pypdf`, BeautifulSoup, unittest, shell packaging scripts.

---

### Task 1: Local Parser Module

**Files:**
- Create: `backend/services/local_parser.py`
- Test: `backend/tests/test_local_parser.py`
- Modify: `backend/requirements.txt`
- Modify: `backend/pyproject.toml`

- [ ] Write tests for TXT, Markdown, HTML, text PDF, and empty/scanned PDF behavior.
- [ ] Run `backend/.venv/bin/python -m unittest backend.tests.test_local_parser` and confirm it fails because `services.local_parser` does not exist.
- [ ] Implement `LocalParserError`, `SUPPORTED_LOCAL_EXTENSIONS`, and `parse_local_file()`.
- [ ] Add `pypdf>=4.0.0` to dependency files.
- [ ] Install `pypdf` into the local venv.
- [ ] Run the local parser tests and confirm they pass.

### Task 2: Parser Routing In Indexing

**Files:**
- Modify: `backend/services/indexing_service.py`
- Test: `backend/tests/test_indexing_service_cloud.py`
- Create: `backend/tests/test_indexing_service_local_parser.py`

- [ ] Write tests proving `PARSER_MODE=local` and `PARSER_MODE=auto` without token route to local parser.
- [ ] Add a regression test proving `PARSER_MODE=auto` with token still routes to MinerU.
- [ ] Run targeted indexing tests and confirm local-routing tests fail before implementation.
- [ ] Implement parser selection helpers and integrate them into `_run_pipeline`.
- [ ] Run targeted indexing tests and confirm they pass.

### Task 3: Upload Formats And Health

**Files:**
- Modify: `backend/services/document_service.py`
- Modify: `backend/routers/system.py`
- Test: `backend/tests/test_document_service.py`
- Test: `backend/tests/test_system_health.py`

- [ ] Add tests that TXT/Markdown uploads are accepted.
- [ ] Add tests that health reports parser mode and local parser supported formats.
- [ ] Update upload extension allowlist and `/system/formats`.
- [ ] Update health response with `document_parser` component.
- [ ] Run targeted tests and confirm they pass.

### Task 4: Docs And Offline Package

**Files:**
- Modify: `backend/.env.example`
- Modify: `backend/.env.offline.example`
- Modify: `README-interview-demo.md`
- Modify: `docs/offline-deployment-guide.md`
- Modify: `docs/troubleshooting-offline-demo.md`

- [ ] Document `PARSER_MODE=auto|local|mineru`.
- [ ] Document full offline new indexing requirements: local parser plus local OpenAI-compatible LLM.
- [ ] Document scanned PDF/OCR limitation.
- [ ] Run all backend unit tests.
- [ ] Rebuild the offline package to `outputs`.
- [ ] Start and verify the package from the output directory.
