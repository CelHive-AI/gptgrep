"""Synthetic protocol failures retain observations without replaying inference."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bridge import AdapterError, LocalCodex, json_bytes


def host_at(root):
    return LocalCodex(root / "never-executed", "never-executed", root, root, 1)


def process_report(report, returncode=2):
    def invoke(arguments, cwd, timeout, *, cancelled=None, on_start=None):
        if on_start:
            on_start(900001)
        return subprocess.CompletedProcess(arguments, returncode, json_bytes(report), b"")
    return invoke


def failure_report():
    return {"schema_version": "gptgrep.error.v1", "status": "completed",
            "code": "host_codex_terminal_error", "error": "synthetic-private-marker",
            "host_protocol": {"kind": "terminal_error", "codex_error_info": "usageLimitExceeded",
                              "will_retry": False, "http_status_code": 429, "server_retry_notifications": 3,
                              "usage": {"total": {"inputTokens": 17, "cachedInputTokens": 0,
                                                  "outputTokens": 4, "reasoningOutputTokens": True,
                                                  "message": "synthetic-private-marker"},
                                        "last": {"totalTokens": 21, "inputTokens": -1},
                                        "modelContextWindow": 262144, "details": "synthetic-private-marker"},
                              "accounting_complete": True, "message": "synthetic-private-marker"}}


class ProtocolAccountingTests(unittest.TestCase):
    def test_terminal_failure_keeps_partial_usage_and_consumes_budget_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = host_at(root)
            report = failure_report()
            try:
                with patch("bridge.owned_process", side_effect=process_report(report)) as process:
                    with self.assertRaises(AdapterError) as raised:
                        host.complete("synthetic", {}, {})
                    self.assertEqual(raised.exception.code, "host_codex_terminal_error")
                    with self.assertRaisesRegex(AdapterError, "budget"):
                        host.complete("never replay", {}, {})
                    self.assertEqual(process.call_count, 1)
                self.assertEqual(len(host.calls), 1)
                receipt = host.calls[0]
                self.assertEqual(receipt["status"], "failed")
                self.assertFalse(receipt["accounting_complete"])
                self.assertEqual(receipt["host_protocol"], {
                    "kind": "terminal_error", "codex_error_info": "usageLimitExceeded", "will_retry": False,
                    "http_status_code": 429, "server_retry_notifications": 3, "accounting_complete": False,
                    "usage": {"total": {"inputTokens": 17, "cachedInputTokens": 0, "outputTokens": 4},
                              "last": {"totalTokens": 21}, "modelContextWindow": 262144}})
                self.assertEqual(receipt["usage"], receipt["host_protocol"]["usage"])
                self.assertNotIn("synthetic-private-marker", host.ledger.read_text())
                self.assertEqual(receipt["response_sha256"], hashlib.sha256(json_bytes(report)).hexdigest())
            finally:
                host.close(cancel=True)
            resumed = host_at(root)
            try:
                with patch("bridge.owned_process") as process:
                    with self.assertRaisesRegex(AdapterError, "budget"):
                        resumed.complete("never replay after resume", {}, {})
                    process.assert_not_called()
                self.assertEqual(resumed.calls[0]["usage"], receipt["usage"])
                self.assertEqual(resumed.calls[0]["status"], "failed")
            finally:
                resumed.close(cancel=True)

    def test_response_before_final_receipt_recovers_partial_usage_as_interrupted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = host_at(root)
            receipt = {"ordinal": 1, "phase": "index:synthetic", "host_invoked": True}
            try:
                (root / "calls/00001.request.json").write_bytes(b"{}")
                host.start_attempt(receipt)
                host._save_response(receipt, json_bytes(failure_report()))
            finally:
                host.close(cancel=True)
            resumed = host_at(root)
            try:
                self.assertEqual(resumed.calls[0]["status"], "interrupted")
                self.assertFalse(resumed.calls[0]["accounting_complete"])
                self.assertEqual(resumed.calls[0]["host_protocol"], receipt["host_protocol"])
                self.assertEqual(resumed.calls[0]["usage"], receipt["usage"])
                self.assertNotIn("synthetic-private-marker", resumed.ledger.read_text())
            finally:
                resumed.close(cancel=True)

    def test_invalid_fields_are_not_exported_or_reported_as_zero_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = host_at(Path(temporary))
            report = {"code": "host_codex_malformed_error", "server_retry_notifications": True,
                      "usage": {"total_tokens": False, "total": {"inputTokens": -1}},
                      "host_protocol": {"kind": ["terminal_error"], "codex_error_info": "synthetic-private-marker",
                                        "will_retry": "false", "http_status_code": 999,
                                        "server_retry_notifications": -1,
                                        "usage": {"last": {"outputTokens": 1.5}}, "accounting_complete": True}}
            try:
                with patch("bridge.owned_process", side_effect=process_report(report)):
                    with self.assertRaises(AdapterError):
                        host.complete("synthetic", {}, {})
                receipt = host.calls[0]
                self.assertIsNone(receipt["usage"])
                self.assertNotIn("server_retry_notifications", receipt)
                self.assertEqual(receipt["host_protocol"], {"usage": None, "accounting_complete": False,
                                                          "will_retry": None, "http_status_code": None})
                self.assertNotIn("synthetic-private-marker", json.dumps(receipt))
            finally:
                host.close(cancel=True)

    def test_completed_report_retains_observed_server_retry_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = host_at(Path(temporary))
            report = {"status": "completed", "model": "gpt-5.6-luna", "requested_reasoning_effort": "max",
                      "effective_reasoning_effort": "max", "auth_mode": "chatgpt", "model_provider": "openai",
                      "thread_id": "synthetic-thread", "turn_id": "synthetic-turn", "value": {},
                      "server_retry_notifications": 2, "usage": {"total": {"totalTokens": 21}}}
            try:
                with patch("bridge.owned_process", side_effect=process_report(report, 0)):
                    value, _ = host.complete("synthetic", {}, {})
                self.assertEqual(value, {})
                self.assertEqual(host.calls[0]["status"], "completed")
                self.assertEqual(host.calls[0]["server_retry_notifications"], 2)
                self.assertEqual(host.calls[0]["usage"], report["usage"])
                self.assertNotIn("host_protocol", host.calls[0])
            finally:
                host.close(cancel=True)


if __name__ == "__main__":
    unittest.main()
