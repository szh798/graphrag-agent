"""Import local JSON business data into the configured AppRepository."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from storage import app_repository as app_store
from storage import file_store as fs

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def import_file_store_app_data(dry_run: bool = True, documents_only: bool = False) -> dict:
    docs = fs.load_docs_index()
    jobs = fs.list_all_jobs()
    sessions = fs.list_chat_sessions()
    queries = fs.load_query_history()
    batches = fs.list_batch_metas()
    result = {
        "dry_run": dry_run,
        "documents": len(docs),
        "jobs": len(jobs),
        "sessions": len(sessions),
        "queries": len(queries),
        "batches": len(batches),
        "imported": {"documents": 0, "jobs": 0, "sessions": 0, "queries": 0, "batches": 0},
    }
    if dry_run:
        return result

    repo = app_store.get_app_repository()
    for doc in docs.values():
        repo.save_document(doc)
        result["imported"]["documents"] += 1
    if documents_only:
        return result
    for job in jobs:
        repo.save_job_meta(job["job_id"], job)
        result["imported"]["jobs"] += 1
    for session in sessions:
        repo.save_chat_session(session)
        result["imported"]["sessions"] += 1
    for query in queries:
        repo.append_query_history(query)
        result["imported"]["queries"] += 1
    for batch in batches:
        repo.save_batch_meta(batch["batch_id"], batch)
        result["imported"]["batches"] += 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Import local JSON app data into Postgres/AppRepository.")
    parser.add_argument("--apply", action="store_true", help="write records instead of only printing a dry-run summary")
    parser.add_argument("--documents-only", action="store_true", help="import document metadata without local sessions, queries, batches, or jobs")
    args = parser.parse_args()
    print(json.dumps(import_file_store_app_data(dry_run=not args.apply, documents_only=args.documents_only), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
