from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import operations


class OperationsWebhookTests(unittest.TestCase):
    def test_feishu_webhook_uses_supported_text_payload_and_keyword(self):
        repository = Mock()
        repository.record_ops_event.return_value = "event-123"
        response = Mock()
        response.json.return_value = {"code": 0, "msg": "success"}

        with (
            patch.dict(
                "os.environ",
                {
                    "OPS_ALERT_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/test",
                    "OPS_ALERT_WEBHOOK_PROVIDER": "auto",
                },
                clear=False,
            ),
            patch.object(operations, "get_account_repository", return_value=repository),
            patch.object(operations.requests, "post", return_value=response) as post,
        ):
            event_id = operations.report_event(
                "index_failed",
                "Index worker timed out",
                request_id="request-123",
                context={"doc_id": "doc-123", "token": "must-not-leak", "question": "private"},
            )

        self.assertEqual(event_id, "event-123")
        response.raise_for_status.assert_called_once_with()
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["msg_type"], "text")
        text = payload["content"]["text"]
        self.assertIn("GraphRAG 生产告警", text)
        self.assertIn("index_failed", text)
        self.assertIn("request-123", text)
        self.assertIn("doc-123", text)
        self.assertNotIn("must-not-leak", text)
        self.assertNotIn("private", text)

    def test_feishu_business_error_is_treated_as_delivery_failure(self):
        response = Mock()
        response.json.return_value = {"code": 19024, "msg": "Key Words Not Found"}

        with self.assertRaisesRegex(RuntimeError, "delivery was rejected"):
            operations._validate_alert_response(response, "feishu")

    def test_generic_webhook_keeps_privacy_safe_event_shape(self):
        provider, payload = operations._alert_webhook_payload(
            "https://alerts.example.test/ingest",
            {
                "level": "error",
                "event": "test_event",
                "source": "backend",
                "request_id": "request-456",
                "tenant_id": None,
                "actor_id": None,
                "message": "RuntimeError",
                "context": {},
            },
            "event-456",
        )

        self.assertEqual(provider, "generic")
        self.assertEqual(payload["event_id"], "event-456")
        self.assertNotIn("msg_type", payload)


if __name__ == "__main__":
    unittest.main()
