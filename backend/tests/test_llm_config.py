from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class LLMConfigTests(unittest.TestCase):
    def setUp(self):
        for module_name in ("pipeline.entity_extractor", "pipeline.llm_config"):
            sys.modules.pop(module_name, None)

    def tearDown(self):
        for module_name in ("pipeline.entity_extractor", "pipeline.llm_config"):
            sys.modules.pop(module_name, None)

    def test_entity_extraction_uses_index_model_separate_from_qa_model(self):
        env = {
            "LLM_PROVIDER": "zhipu",
            "LLM_API_KEY": "test-key",
            "LLM_BASE_URL": "https://open.bigmodel.cn/api/paas/v4/",
            "LLM_MODEL": "glm-5.2",
            "LLM_INDEX_MODEL": "glm-4-flash-250414",
        }
        with patch.dict(os.environ, env, clear=True), patch("dotenv.load_dotenv", return_value=True):
            llm_config = importlib.import_module("pipeline.llm_config")
            entity_extractor = importlib.import_module("pipeline.entity_extractor")

        self.assertEqual(llm_config.LLM_MODEL, "glm-5.2")
        self.assertEqual(llm_config.LLM_INDEX_MODEL, "glm-4-flash-250414")
        self.assertEqual(entity_extractor.MODEL_ID, "glm-4-flash-250414")

    def test_offline_demo_provider_does_not_fall_back_to_host_api_keys(self):
        env = {
            "LLM_PROVIDER": "offline-demo",
            "LLM_API_KEY": "",
            "ZAI_API_KEY": "host-zai-key",
            "ZHIPUAI_API_KEY": "host-zhipu-key",
            "DEEPSEEK_API_KEY": "host-deepseek-key",
        }
        with patch.dict(os.environ, env, clear=True), patch("dotenv.load_dotenv", return_value=True):
            llm_config = importlib.import_module("pipeline.llm_config")

        self.assertEqual(llm_config.LLM_PROVIDER, "offline-demo")
        self.assertEqual(llm_config.LLM_API_KEY, "")


if __name__ == "__main__":
    unittest.main()
