"""Generate a reproducible legacy-versus-LightRAG evaluation artifact."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from scripts.lightrag_ops_common import ApiError, JsonHttpClient, auth_headers_from_env, unwrap_data


ENGINES = ("legacy", "lightrag")
LIGHTRAG_MODES = ("local", "global", "hybrid", "mix", "naive")


def load_dataset(path: Path, *, min_questions: int = 50, min_documents: int = 10) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at line {line_number}") from exc
            question = str(row.get("question") or "").strip()
            document_ids = row.get("document_ids")
            if not question or not isinstance(document_ids, list) or not all(isinstance(item, str) for item in document_ids):
                raise ValueError(f"Line {line_number} requires question and document_ids[]")
            rows.append({
                "id": str(row.get("id") or f"q{line_number}"),
                "question": question,
                "document_ids": document_ids,
                "expected_reference_pages": row.get("expected_reference_pages") or [],
                "expected_facts": row.get("expected_facts") or [],
                "category": str(row.get("category") or "unspecified"),
                "tags": row.get("tags") or [],
            })
    unique_documents = {doc_id for row in rows for doc_id in row["document_ids"]}
    if len(rows) < min_questions:
        raise ValueError(f"Evaluation set has {len(rows)} questions; requires at least {min_questions}")
    if len(unique_documents) < min_documents:
        raise ValueError(f"Evaluation set has {len(unique_documents)} documents; requires at least {min_documents}")
    return rows


def build_cases(
    dataset: list[dict[str, Any]],
    *,
    engines: tuple[str, ...] = ENGINES,
    modes: tuple[str, ...] = ("mix",),
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for row in dataset:
        if "legacy" in engines:
            cases.append({**row, "engine": "legacy", "retrieval_mode": None})
        if "lightrag" in engines:
            for mode in modes:
                cases.append({**row, "engine": "lightrag", "retrieval_mode": mode})
    return cases


def _reference_pages(result: dict[str, Any]) -> list[int]:
    pages: set[int] = set()
    for reference in result.get("references") or []:
        if not isinstance(reference, dict):
            continue
        try:
            pages.add(int(reference.get("page")))
        except (TypeError, ValueError):
            continue
    return sorted(pages)


def _fact_match(answer: str, expected_facts: list[str]) -> bool | None:
    if not expected_facts:
        return None
    normalized = " ".join(str(answer or "").casefold().split())
    return all(" ".join(str(fact).casefold().split()) in normalized for fact in expected_facts)


def run_cases(
    client: JsonHttpClient,
    cases: list[dict[str, Any]],
    *,
    query_path: str = "/query",
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for case in cases:
        payload: dict[str, Any] = {
            "question": case["question"],
            "document_ids": case["document_ids"],
            "engine": case["engine"],
        }
        if case["retrieval_mode"]:
            payload["retrieval_mode"] = case["retrieval_mode"]
        started = time.perf_counter()
        try:
            response = unwrap_data(client.request("POST", query_path, payload=payload))
            result = response if isinstance(response, dict) else {"answer": str(response or "")}
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            expected = {int(page) for page in case["expected_reference_pages"] if str(page).isdigit()}
            actual = set(_reference_pages(result))
            results.append({
                "id": case["id"],
                "engine": case["engine"],
                "retrieval_mode": case["retrieval_mode"],
                "latency_ms": elapsed_ms,
                "ok": True,
                "answer": result.get("answer") or "",
                "references": result.get("references") or [],
                "cited_entities": result.get("cited_entities") or [],
                "token_usage": result.get("token_usage") or result.get("usage") or {},
                "expected_reference_pages": sorted(expected),
                "reference_pages": sorted(actual),
                "reference_page_match": None if not expected else expected.issubset(actual),
                "expected_facts": case.get("expected_facts") or [],
                "fact_match": _fact_match(
                    str(result.get("answer") or ""), case.get("expected_facts") or []
                ),
                "category": case.get("category") or "unspecified",
                "tags": case["tags"],
            })
        except ApiError as exc:
            results.append({
                "id": case["id"],
                "engine": case["engine"],
                "retrieval_mode": case["retrieval_mode"],
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "ok": False,
                "error": str(exc),
                "tags": case["tags"],
            })
    return results


def write_results(path: Path, results: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan or run dual-engine evaluation queries.")
    parser.add_argument("dataset", type=Path, help="JSONL evaluation dataset")
    parser.add_argument("--run", action="store_true", help="send queries; omitted means validation/dry-run only")
    parser.add_argument("--output", type=Path, help="required with --run; receives sensitive answer JSONL")
    parser.add_argument("--engines", default="legacy,lightrag")
    parser.add_argument("--modes", default="mix", help="comma list or 'all'")
    parser.add_argument("--min-questions", type=int, default=50)
    parser.add_argument("--min-documents", type=int, default=10)
    parser.add_argument("--gateway-url", default=os.getenv("GRAPHRAG_GATEWAY_URL", "http://localhost:8000/api/v1"))
    parser.add_argument("--query-path", default=os.getenv("LIGHTRAG_EVAL_QUERY_PATH", "/query"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("LIGHTRAG_OPS_TIMEOUT_SECONDS", "300")))
    args = parser.parse_args()

    engines = tuple(item.strip() for item in args.engines.split(",") if item.strip())
    invalid_engines = set(engines) - set(ENGINES)
    if invalid_engines:
        parser.error(f"unsupported engines: {', '.join(sorted(invalid_engines))}")
    modes = LIGHTRAG_MODES if args.modes == "all" else tuple(item.strip() for item in args.modes.split(",") if item.strip())
    invalid_modes = set(modes) - set(LIGHTRAG_MODES)
    if invalid_modes:
        parser.error(f"unsupported LightRAG modes: {', '.join(sorted(invalid_modes))}")
    if args.run and args.output is None:
        parser.error("--output is required with --run")

    dataset = load_dataset(args.dataset, min_questions=args.min_questions, min_documents=args.min_documents)
    cases = build_cases(dataset, engines=engines, modes=modes)
    if not args.run:
        print(json.dumps({
            "dry_run": True,
            "questions": len(dataset),
            "cases": len(cases),
            "engines": engines,
            "lightrag_modes": modes if "lightrag" in engines else [],
        }, ensure_ascii=False, indent=2))
        return

    headers = {**auth_headers_from_env(), "X-GraphRAG-Stateless-Batch": "1"}
    client = JsonHttpClient(args.gateway_url, timeout_seconds=args.timeout, headers=headers)
    results = run_cases(client, cases, query_path=args.query_path)
    assert args.output is not None
    write_results(args.output, results)
    matched = [item for item in results if item.get("reference_page_match") is not None]
    fact_scored = [item for item in results if item.get("fact_match") is not None]
    summary = {
        "dry_run": False,
        "questions": len(dataset),
        "cases": len(cases),
        "succeeded": sum(1 for item in results if item.get("ok")),
        "failed": sum(1 for item in results if not item.get("ok")),
        "reference_page_accuracy": (
            sum(1 for item in matched if item["reference_page_match"]) / len(matched) if matched else None
        ),
        "exact_fact_accuracy": (
            sum(1 for item in fact_scored if item["fact_match"]) / len(fact_scored) if fact_scored else None
        ),
        "output": str(args.output),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["failed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
