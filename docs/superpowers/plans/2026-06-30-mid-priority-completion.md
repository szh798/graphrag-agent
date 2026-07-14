# Mid Priority Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the remaining medium-priority product gaps: full KG loading, safe answer rendering, batch QA management, precise path entity selection, and graph neighbor/location interactions.

**Architecture:** Keep existing FastAPI + JSON storage for this prototype, adding batch list/cancel APIs that work with the current file store. On the frontend, avoid HTML injection by rendering lightweight markdown as React nodes, fetch all paginated KG data in the app store, and add focused UI state for path selections and graph focus.

**Tech Stack:** FastAPI, Python unittest, React/Vite, TypeScript, D3, Playwright verification.

---

### Task 1: Batch Management Backend

**Files:**
- Modify: `backend/services/qa_service.py`
- Modify: `backend/routers/query.py`
- Test: `backend/tests/test_qa_batch.py`

- [ ] Add tests for listing batch history and cancelling a running batch.
- [ ] Verify tests fail on the current implementation.
- [ ] Implement `list_batches()` and `cancel_batch()` in `qa_service.py`.
- [ ] Add `GET /query/batch` and `DELETE /query/batch/{batch_id}`.
- [ ] Verify focused backend tests pass.

### Task 2: Frontend Data And Rendering

**Files:**
- Modify: `frontend/src/app/api.ts`
- Modify: `frontend/src/app/store.tsx`
- Modify: `frontend/src/app/components/pages/QAChat.tsx`

- [ ] Add API methods for batch list/cancel.
- [ ] Fetch all KG nodes and edges across pages.
- [ ] Replace `dangerouslySetInnerHTML` with React-rendered lightweight markdown.
- [ ] Add batch history, cancel, refresh, and export controls.

### Task 3: Search And Graph Interactions

**Files:**
- Modify: `frontend/src/app/components/pages/SearchPage.tsx`
- Modify: `frontend/src/app/components/pages/KGExplorer.tsx`

- [ ] Replace path search substring resolution with selected entity IDs.
- [ ] Add suggestion dropdowns for path start/end.
- [ ] Make “查看全部邻居” expand/collapse actual neighbor list.
- [ ] Focus and highlight URL-selected graph nodes from search.

### Task 4: Verification

**Files:**
- No production file edits unless verification exposes an issue.

- [ ] Run backend unittest discovery.
- [ ] Run frontend production build.
- [ ] Use Playwright to inspect chat batch panel, search path selector, and graph node focus.
