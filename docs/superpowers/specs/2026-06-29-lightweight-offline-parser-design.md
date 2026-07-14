# Lightweight Offline Parser Design

## Goal

Make the offline interview package capable of indexing new documents without MinerU cloud when the file contains selectable text. The demo should support local parsing for text-based PDF, HTML, TXT, Markdown, and Markdown-like files while keeping the existing MinerU cloud parser available for richer OCR/table parsing.

## Scope

In scope:

- Add a local parser that emits the same MinerU-style `content_list.json` shape consumed by the existing `text_assembler`.
- Route indexing by `PARSER_MODE`:
  - `auto`: use MinerU when `MINERU_API_TOKEN` is configured, otherwise use local parser.
  - `local`: always use local parser.
  - `mineru` or `cloud`: always use MinerU cloud.
- Support local parsing for `pdf`, `html`, `htm`, `txt`, `md`, and `markdown`.
- Fail clearly for scanned PDFs, image files, Office files, or other formats when no OCR/cloud parser is available.
- Update docs, env templates, health info, tests, and offline package output.

Out of scope:

- Full OCR engine bundling.
- Local table/formula extraction parity with MinerU.
- Local Office document parsing.

## Architecture

The current indexing pipeline already has clean phases:

```text
document upload -> document parsing -> assemble pages -> entity extraction -> KG build
```

The new design replaces only the parsing phase. `services.local_parser` writes a `*_content_list.json` file with text blocks:

```json
[
  {
    "type": "text",
    "text": "extracted page text",
    "page_idx": 0,
    "bbox": [0, 0, 0, 0],
    "source": "local_parser"
  }
]
```

Because `pipeline.text_assembler` already understands text blocks, the extraction and KG builder stages remain unchanged.

## Runtime Behavior

`PARSER_MODE=auto` is the default. In auto mode:

- If `MINERU_API_TOKEN` exists, use MinerU cloud.
- If no token exists, use local parser.

`PARSER_MODE=local` is best for the interview package because it proves new indexing can work without MinerU. It still requires a working LLM endpoint for entity extraction. Full offline new indexing therefore means:

- local parser for text extraction
- local OpenAI-compatible LLM for entity extraction

## Error Handling

The local parser raises a clear `LocalParserError` when it cannot extract usable text:

- unsupported file format
- PDF has no selectable text
- HTML/TXT/Markdown file is empty after parsing
- `pypdf` dependency is missing

The indexing job catches the error through the existing pipeline exception path, marks the job failed, and updates the document status to `failed`.

## Testing

Tests cover:

- local TXT/Markdown/HTML parsing emits content-list blocks
- local text PDF parsing works using a generated minimal PDF
- scanned/empty PDF reports a clear OCR-required error
- indexing pipeline routes to local parser when `PARSER_MODE=local`
- indexing pipeline routes to local parser in `auto` mode without `MINERU_API_TOKEN`
- indexing pipeline still uses MinerU in `auto` mode when `MINERU_API_TOKEN` exists
- supported upload formats include TXT and Markdown

## Interview Demo Story

The interview package can now say:

> The offline package can start without network and can index new selectable-text documents locally. For scanned PDFs or image OCR, the system can switch to MinerU cloud or a future local OCR engine, but the stable interview path uses lightweight local parsing to avoid heavy model and system-library risk.
