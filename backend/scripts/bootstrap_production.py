"""Bootstrap production repositories and print dependency readiness."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from storage import app_repository as app_store
from storage import account_repository as account_store
from storage import blob_repository as blob_store
from storage import graph_repository as graph_store
from storage import queue_repository as queue_store

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def _apply_schema(repo: Any) -> str:
    ensure_schema = getattr(repo, "ensure_schema", None)
    if not callable(ensure_schema):
        return "not_supported"
    ensure_schema()
    return "applied"


def bootstrap_production(apply_schema: bool = True) -> dict:
    graph_repo = graph_store.get_graph_repository()
    app_repo = app_store.get_app_repository()
    account_repo = account_store.get_account_repository()
    blob_repo = blob_store.get_blob_repository()
    queue_repo = queue_store.get_queue_repository()

    schema = {
        "graph_database": _apply_schema(graph_repo) if apply_schema else "skipped",
        "app_database": _apply_schema(app_repo) if apply_schema else "skipped",
        "account_database": _apply_schema(account_repo) if apply_schema else "skipped",
    }
    components = {
        "graph_database": graph_repo.health(),
        "app_database": app_repo.health(),
        "blob_storage": blob_repo.health(),
        "task_queue": queue_repo.health(),
    }
    return {
        "ready": all(component.get("status") == "ok" for component in components.values()),
        "schema": schema,
        "components": components,
    }


def main() -> None:
    # The Vercel CLI writes linked production variables at the repository
    # root. Load them only for the executable command so importing this module
    # in tests does not mutate the process-wide repository backend selection.
    load_dotenv(Path(__file__).resolve().parents[2] / ".env.local", override=True)
    parser = argparse.ArgumentParser(description="Bootstrap GraphRAG Studio production repositories.")
    parser.add_argument("--check-only", action="store_true", help="skip schema creation and only print dependency health")
    args = parser.parse_args()
    print(json.dumps(bootstrap_production(apply_schema=not args.check_only), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
