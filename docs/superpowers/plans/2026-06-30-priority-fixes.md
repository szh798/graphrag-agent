# Priority Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the highest-priority product gaps: document dates, settings page, extraction result closure, dashboard index actions, and visible production storage status.

**Architecture:** Keep the JSON filesystem backend for local/demo mode, but make storage persistence explicit through health metadata and settings UI. Add a document-level index-result endpoint backed by existing job artifacts, then wire frontend pages to the existing API client and app state patterns.

**Tech Stack:** FastAPI, Python unittest, React/Vite, TypeScript, Playwright verification.

---

### Task 1: Backend Document Result And Storage Metadata

**Files:**
- Modify: `backend/services/document_service.py`
- Modify: `backend/routers/documents.py`
- Modify: `backend/storage/file_store.py`
- Modify: `backend/routers/system.py`
- Test: `backend/tests/test_document_results.py`
- Test: `backend/tests/test_system_health.py`

- [ ] Write failing tests for upload date compatibility, document index result lookup, extraction lookup, and storage profile.
- [ ] Run the focused tests and verify the new tests fail.
- [ ] Implement service helpers and routes for `/documents/{doc_id}/index-result` and `/documents/{doc_id}/extractions`.
- [ ] Add storage profile metadata to health responses.
- [ ] Run focused backend tests and verify they pass.

### Task 2: Frontend Wiring

**Files:**
- Modify: `frontend/src/app/api.ts`
- Modify: `frontend/src/app/store.tsx`
- Modify: `frontend/src/app/routes.tsx`
- Modify: `frontend/src/app/components/layout/Sidebar.tsx`
- Modify: `frontend/src/app/components/pages/Documents.tsx`
- Modify: `frontend/src/app/components/pages/Dashboard.tsx`
- Create: `frontend/src/app/components/pages/SettingsPage.tsx`

- [ ] Map both `upload_date` and `uploaded_at` into frontend documents.
- [ ] Add API client methods and types for document index result and extraction records.
- [ ] Add `/settings` route and sidebar navigation.
- [ ] Replace the disabled extraction button with a modal that fetches records.
- [ ] Bind dashboard index/retry actions to `api.startIndexing` and app state updates.
- [ ] Build the frontend.

### Task 3: Verification

**Files:**
- No production file edits unless verification reveals a bug.

- [ ] Run focused backend tests.
- [ ] Run frontend build.
- [ ] Use Playwright to verify `/dashboard`, `/documents`, and `/settings`.
- [ ] Confirm `Invalid Date` no longer appears.
