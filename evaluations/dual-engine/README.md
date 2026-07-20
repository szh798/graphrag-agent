# Dual-engine evaluation set

This directory contains a committed, fully synthetic regression corpus:

- `documents/`: 10 sanitized, synthetic fixtures with deterministic logical IDs.
  The upload set contains Markdown, PDF, Office DOCX, PNG images, HTML and TXT;
  the matching Markdown files remain the reviewable source of truth.
- `questions.jsonl`: 50 fixed questions covering facts, procedures, conditions,
  multi-fact and cross-document retrieval.
- `documents.manifest.json`: fixture-to-file mapping.
- `baseline.pending.json`: an explicit marker that live classic-engine answers
  and latency still have to be captured from the target preview environment.

The fixture IDs must be replaced with the real document IDs returned when the
corpus is uploaded to the preview tenant. Do not commit upload tokens, user
documents, live answers, or production query results.

Regenerate and verify the committed multi-format fixtures with the bundled
document runtime (no production service or credentials are contacted):

```bash
python evaluations/dual-engine/generate_fixtures.py
python evaluations/dual-engine/generate_fixtures.py --check
```

Validate and print the call plan without sending queries:

```bash
cd backend
python -m scripts.evaluate_dual_engine ../evaluations/dual-engine/questions.jsonl
```

Run both engines in `mix` mode and write the sensitive result with owner-only
file permissions:

```bash
LIGHTRAG_OPS_AUTH_TOKEN='set-in-shell-only' \
python -m scripts.evaluate_dual_engine \
  ../evaluations/dual-engine/questions.jsonl \
  --run \
  --output /secure/evaluations/dual-engine.jsonl
```

Use `--modes all` to exercise all five LightRAG modes. Evaluation calls carry
the stateless batch header, but still count toward model usage and rate limits.
The runner records page matches, exact expected-fact matches, token metadata
and latency. These deterministic checks are a regression signal; the required
80% blind comparison still needs human scoring of the captured live results.
