from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import langextract as lx


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from pipeline.kg_builder import build_kg, extractions_to_records
from pipeline import kg_builder
from pipeline.text_assembler import PageText


def _page(text: str) -> PageText:
    return PageText(page_idx=0, text=text, block_spans=[])


def _document(*extractions: lx.data.Extraction) -> lx.data.AnnotatedDocument:
    return lx.data.AnnotatedDocument(extractions=list(extractions))


def _unaligned(text: str, entity_type: str = "CONCEPT") -> lx.data.Extraction:
    return lx.data.Extraction(extraction_class=entity_type, extraction_text=text)


class GroundingRecoveryTests(unittest.TestCase):
    def test_recovers_unique_null_aligned_ocr_entities_and_builds_edges(self):
        text = "运维排班\n恢复窗口：每周日 02:00\n值班组织：北辰小组"
        page = _page(text)
        annotated = _document(
            _unaligned("每周日", "CONCEPT"),
            _unaligned("北辰小组", "ORGANIZATION"),
        )

        records = extractions_to_records([page], [annotated], "image-doc")
        nodes, edges = build_kg([page], [annotated], "image-doc")

        self.assertEqual([record["alignment"] for record in records], ["match_fuzzy"] * 2)
        for record in records:
            self.assertEqual(
                text[record["char_start"] : record["char_end"]],
                record["text"],
            )
        self.assertEqual({node["name"] for node in nodes}, {"每周日", "北辰小组"})
        self.assertEqual(len(edges), 1)

    def test_recovers_markdown_entity_with_safe_case_and_whitespace_normalization(self):
        text = "# 北辰质量门禁\n\n验收代码：**BC - 95**\n"
        page = _page(text)
        annotated = _document(_unaligned("bc-95", "CONCEPT"))

        records = extractions_to_records([page], [annotated], "markdown-doc")
        nodes, _ = build_kg([page], [annotated], "markdown-doc")

        self.assertEqual(records[0]["alignment"], "match_fuzzy")
        self.assertEqual(
            text[records[0]["char_start"] : records[0]["char_end"]],
            "BC - 95",
        )
        self.assertEqual([node["name"] for node in nodes], ["bc-95"])

    def test_rejects_null_aligned_hallucination_absent_from_source(self):
        page = _page("验收代码：BC-95")
        annotated = _document(_unaligned("BC-99", "CONCEPT"))

        records = extractions_to_records([page], [annotated], "markdown-doc")
        nodes, edges = build_kg([page], [annotated], "markdown-doc")

        self.assertIsNone(records[0]["alignment"])
        self.assertIsNone(records[0]["char_start"])
        self.assertIsNone(records[0]["char_end"])
        self.assertEqual(nodes, [])
        self.assertEqual(edges, [])

    def test_rejects_ambiguous_repeated_null_alignment(self):
        page = _page("BC-95 是旧门禁；BC-95 也是新门禁。")
        annotated = _document(_unaligned("BC-95", "CONCEPT"))

        records = extractions_to_records([page], [annotated], "markdown-doc")
        nodes, _ = build_kg([page], [annotated], "markdown-doc")

        self.assertIsNone(records[0]["alignment"])
        self.assertEqual(nodes, [])

    def test_normalizes_large_page_once_for_multiple_unaligned_extractions(self):
        text = ("大段正文 " * 50_000) + "唯一甲 唯一乙 唯一丙"
        page = _page(text)
        annotated = _document(
            _unaligned("唯一甲"),
            _unaligned("唯一乙"),
            _unaligned("唯一丙"),
        )

        original_normalize = kg_builder._normalize_for_grounding
        page_normalizations = 0

        def counting_normalize(value: str):
            nonlocal page_normalizations
            if value is text:
                page_normalizations += 1
            return original_normalize(value)

        with patch.object(
            kg_builder,
            "_normalize_for_grounding",
            side_effect=counting_normalize,
        ):
            records = extractions_to_records([page], [annotated], "large-doc")

        self.assertEqual(page_normalizations, 1)
        self.assertEqual(
            [record["alignment"] for record in records],
            ["match_fuzzy"] * 3,
        )

    def test_skips_null_alignment_recovery_for_over_limit_page(self):
        text = "超限页面包含唯一实体"
        page = _page(text)
        annotated = _document(_unaligned("唯一实体"))

        with (
            patch.object(
                kg_builder,
                "MAX_NULL_ALIGNMENT_RECOVERY_PAGE_CHARS",
                len(text) - 1,
            ),
            patch.object(kg_builder, "_normalize_for_grounding") as normalize,
        ):
            records = extractions_to_records([page], [annotated], "over-limit-doc")
            nodes, edges = build_kg([page], [annotated], "over-limit-doc")

        normalize.assert_not_called()
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["alignment"])
        self.assertIsNone(records[0]["char_start"])
        self.assertIsNone(records[0]["char_end"])
        self.assertEqual(nodes, [])
        self.assertEqual(edges, [])


if __name__ == "__main__":
    unittest.main()
