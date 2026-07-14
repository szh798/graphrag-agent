# Offline Demo Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn GraphRAGAgent into an interview-ready offline demo package with one-command startup, verification, packaging, and troubleshooting docs.

**Architecture:** Keep the current FastAPI backend and Vite frontend intact. Add a delivery layer around the existing app: portable health checks, shell scripts, a package builder that excludes secrets, and docs that explain offline capability boundaries.

**Tech Stack:** FastAPI, Uvicorn, Python virtualenv, Vite static build, shell scripts, unittest.

---

### Task 1: Portable Health Check

**Files:**
- Modify: `backend/routers/system.py`
- Test: `backend/tests/test_system_health.py`

- [x] Write a failing test that expects backend Python candidates to include Unix virtualenv, Windows virtualenv, and the current runtime.
- [x] Run `backend/.venv/bin/python -m unittest backend.tests.test_system_health` and confirm it fails because `_backend_python_candidates` does not exist.
- [x] Implement `_backend_python_candidates()` and `_check_python_import()`.
- [x] Run the health test and confirm it passes.

### Task 2: Offline Demo Contract

**Files:**
- Create: `backend/tests/test_offline_demo_contract.py`
- Create: `scripts/start-demo.sh`
- Create: `scripts/stop-demo.sh`
- Create: `scripts/verify-demo.sh`
- Create: `scripts/package-offline-demo.sh`
- Create: `README-interview-demo.md`
- Create: `docs/offline-deployment-guide.md`
- Create: `docs/troubleshooting-offline-demo.md`

- [x] Write a failing test that checks required scripts and docs exist.
- [ ] Add executable shell scripts for start, stop, verify, and package.
- [ ] Add interview-facing README, deployment guide, and troubleshooting guide.
- [ ] Run `backend/.venv/bin/python -m unittest backend.tests.test_offline_demo_contract` and confirm it passes.

### Task 3: End-to-End Verification

**Files:**
- Use: `scripts/start-demo.sh`
- Use: `scripts/verify-demo.sh`
- Use: `scripts/stop-demo.sh`

- [ ] Stop stale demo processes.
- [ ] Start the backend and frontend with `./scripts/start-demo.sh`.
- [ ] Run `./scripts/verify-demo.sh`.
- [ ] Stop services with `./scripts/stop-demo.sh`.

### Task 4: Package Artifact

**Files:**
- Use: `scripts/package-offline-demo.sh`

- [ ] Run the package script with `OUTPUT_DIR=/Users/dundun/Documents/Codex/2026-06-29/w/outputs`.
- [ ] Confirm the archive exists and excludes `backend/.env`.
- [ ] Report the package path and verification result to the user.
