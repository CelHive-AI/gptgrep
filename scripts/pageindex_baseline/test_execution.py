"""Offline qualification and durable accounting tests; no model is invoked."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from bridge import AdapterError, LocalCodex, json_bytes
from capability import scoring_summary
from qualification import qualify
from run import checkpoint_attempt, may_attempt


class ExecutionTests(unittest.TestCase):
    def test_interrupted_attempt_remains_in_global_cap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = LocalCodex(root / "missing", "missing", root, root, 1)
            request = root / "calls/00001.request.json"
            request.write_bytes(json_bytes({"instructions": "original", "state": {}, "schema": {}}))
            host.start_attempt({"ordinal": 1, "phase": "index:full:fixture", "host_invoked": True,
                                "request_sha256": hashlib.sha256(request.read_bytes()).hexdigest()})
            resumed = LocalCodex(root / "missing", "missing", root, root, 1)
            self.assertEqual(len(resumed.calls), 1)
            self.assertEqual(resumed.calls[0]["status"], "interrupted")
            self.assertIsNone(resumed.calls[0]["usage"])
            with self.assertRaisesRegex(AdapterError, "budget"):
                resumed.complete("No invocation", {}, {"type": "object"})
            self.assertEqual(len(LocalCodex(root / "missing", "missing", root, root, 2).calls), 1)

    def test_attempt_checkpoints_preserve_failure_on_explicit_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "document.index.json"
            checkpoint_attempt(target, {"status": "failed", "host_invocations": 4})
            first = (target.parent / "attempts/document.index/0001.json").read_bytes()
            checkpoint_attempt(target, {"status": "completed", "host_invocations": 2})
            self.assertEqual((target.parent / "attempts/document.index/0001.json").read_bytes(), first)
            self.assertFalse(may_attempt("failed", False))
            self.assertTrue(may_attempt("failed", True))
            self.assertTrue(may_attempt("budget_blocked", False))
            self.assertFalse(may_attempt("completed", True))

    def test_benchmark_roundtrip_requires_real_bound_decision_and_consumed_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calls").mkdir()
            call = {"type": "function_call", "call_id": "actual-call", "name": "get_page_content",
                    "arguments": '{"doc_name":"source.pdf","pages":"3"}'}
            output = {"type": "function_call_output", "call_id": "actual-call", "output": "actual SDK result"}
            envelope = {"items": [call, output]}
            receipts = []
            for ordinal in (1, 2):
                request = {"state": {"input": [] if ordinal == 1 else [call, output]}}
                response = {"status": "completed", "auth_mode": "chatgpt", "model_provider": "openai", "model": "gpt-5.6-luna",
                            "requested_reasoning_effort": "max", "thread_id": f"test-thread-{ordinal}", "turn_id": "test-turn",
                            "value": {"tool_calls": [{"name": call["name"], "arguments": call["arguments"]}] if ordinal == 1 else []}}
                req_bytes, res_bytes = json_bytes(request), json_bytes(response)
                (root / f"calls/{ordinal:05d}.request.json").write_bytes(req_bytes)
                (root / f"calls/{ordinal:05d}.response.json").write_bytes(res_bytes)
                receipts.append({"ordinal": ordinal, "status": "completed", "thread_id": response["thread_id"], "turn_id": response["turn_id"],
                                 "request_sha256": hashlib.sha256(req_bytes).hexdigest(), "response_sha256": hashlib.sha256(res_bytes).hexdigest()})
            profile = {"model": "gpt-5.6-luna", "reasoning_effort": "max"}
            proof = qualify(envelope, "source.pdf", {"actual-call": {3}}, root, [1, 2], receipts, profile, {"source_sha256": "fixture-only"})
            self.assertTrue(proof["verified"])
            self.assertFalse(proof["requires_correct_answer"])
            self.assertFalse(qualify(envelope, "source.pdf", {}, root, [1, 2], receipts, profile, {})["verified"])
            self.assertFalse(qualify(envelope, "source.pdf", {"actual-call": {3}}, root, [1], receipts, profile, {})["verified"])
            self.assertFalse(qualify(envelope, "source.pdf", {"different-call": {3}}, root, [1, 2], receipts, profile, {})["verified"])
            first_failure = {"status": "completed", "accessed_physical_pages": [], "judge": {"status": "completed", "equivalent": False, "abstained": True}}
            later = {"status": "completed", "accessed_physical_pages": [3], "judge": {"status": "completed", "equivalent": True, "abstained": False}}
            summary = scoring_summary([first_failure, later], 2, adapter_verified=proof["verified"])
            self.assertEqual(summary["answer_equivalence_accuracy"], .5)
            self.assertEqual(summary["question_denominator"], 2)


if __name__ == "__main__":
    unittest.main()
