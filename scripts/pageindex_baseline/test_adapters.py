"""Adapter/oracle controls. These tests never invoke a real model."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge import AdapterError, LocalCodex, owned_process
from capability import answer_evidence, index_context_observation, scoring_summary, verify_receipt
from run import gptgrep_tree, returned_pages
from transports import INDEX_ALIAS, PROVIDER, PROTOCOL_PREFIX, PROTOCOL_SUFFIX, ResponsesTransport, chat_backend, decision_request, register_index_provider, response_body


class AdapterUnitTests(unittest.TestCase):
    def test_protocol_preserves_original_instruction_bytes_and_separate_hashes(self):
        original = "Do not expose tool names.\r\nRead source evidence. 雪"
        body = {"instructions": original, "input": [{"role": "user", "content": "lookup"}], "tools": []}
        instructions, state, _, provenance = decision_request(body)
        self.assertEqual(instructions, PROTOCOL_PREFIX + original + PROTOCOL_SUFFIX)
        self.assertEqual(state["input"], body["input"])
        self.assertEqual(provenance["upstream_instructions_sha256"], hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(provenance["combined_instructions_sha256"], hashlib.sha256(instructions.encode()).hexdigest())
        self.assertNotEqual(provenance["adapter_instructions_sha256"], provenance["upstream_instructions_sha256"])

    def test_unverified_adapter_does_not_yield_an_eligible_baseline(self):
        row = {"status": "completed", "accessed_physical_pages": [],
               "judge": {"status": "completed", "equivalent": True, "abstained": False}}
        summary = scoring_summary([row], 1, adapter_verified=False)
        self.assertFalse(summary["baseline_eligible"])
        self.assertIsNone(summary["answer_equivalence_accuracy"])
        self.assertEqual(summary["observed_judge_accuracy"], 1.0)

    def test_verified_adapter_no_page_abstention_stays_in_qa_denominator(self):
        records = [
            {"status": "completed", "accessed_physical_pages": [], "judge": {"status": "completed", "equivalent": False, "abstained": True}},
            {"status": "completed", "accessed_physical_pages": [2], "judge": {"status": "completed", "equivalent": True, "abstained": False}},
        ]
        summary = scoring_summary(records, 2, adapter_verified=True)
        self.assertEqual(summary["question_denominator"], 2)
        self.assertEqual(summary["observed_judge_accuracy"], 0.5)
        self.assertEqual(summary["answer_equivalence_accuracy"], 0.5)
        self.assertTrue(summary["baseline_eligible"])
        self.assertEqual(summary["zero_page_responses"], 1)

    def test_tool_capable_but_wrong_answer_remains_a_measured_failure(self):
        row = {"status": "completed", "accessed_physical_pages": [1],
               "judge": {"status": "completed", "equivalent": False, "abstained": False}}
        summary = scoring_summary([row], 1, adapter_verified=True)
        self.assertTrue(summary["baseline_eligible"])
        self.assertEqual(summary["answer_equivalence_accuracy"], 0.0)

    def test_summary_answer_keeps_correctness_without_page_or_citation_credit(self):
        row = {"status": "completed", "accessed_physical_pages": [], "page_access_recall": 0.0,
               "index_metadata_supplied": True, "index_summary_supplied": True,
               "judge": {"status": "completed", "equivalent": True, "abstained": False}}
        summary = scoring_summary([row], 1, adapter_verified=True)
        self.assertEqual(summary["answer_equivalence_accuracy"], 1.0)
        self.assertEqual(summary["mean_page_access_recall"], 0.0)
        self.assertEqual(summary["summary_evidence_responses"], 1)
        self.assertEqual(answer_evidence(row)["evidence_access"], "index_summary_or_metadata")
        self.assertIsNone(summary["citation_fidelity_verified"])

    def test_judge_unavailable_remains_unknown_after_capability_pass(self):
        row = {"status": "completed", "accessed_physical_pages": [2], "judge": {"status": "unavailable"}}
        summary = scoring_summary([row], 1, adapter_verified=True)
        self.assertFalse(summary["baseline_eligible"])
        self.assertIsNone(summary["answer_equivalence_accuracy"])
        self.assertEqual(summary["judge_unavailable"], 1)

    def test_supplied_target_description_is_observed_as_index_evidence(self):
        metadata = {"name": "fixture.pdf", "description": "A source-derived description."}
        observation = index_context_observation([{"role": "user", "content": "Document metadata: " + json.dumps(metadata)}])
        self.assertTrue(observation["index_metadata_supplied"])
        self.assertTrue(observation["index_summary_supplied"])

    def test_tool_next_step_summary_is_not_a_document_summary(self):
        items = [{"type": "function_call", "call_id": "a", "name": "get_document_structure"},
                 {"type": "function_call_output", "call_id": "a", "output": json.dumps({
                     "success": True, "structure": [], "next_steps": {"summary": "Tool succeeded."}})}]
        observation = index_context_observation(items)
        self.assertTrue(observation["index_metadata_supplied"])
        self.assertFalse(observation["index_summary_supplied"])

    def test_a_boolean_probe_claim_without_native_receipts_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="baseline-capability-receipt-") as temporary:
            receipt = Path(temporary) / "capability.json"
            value = {"schema_version": "gptgrep.pageindex.capability.v1", "status": "capability_verified",
                     "binding": {}, "real_host_invocations": 2, "completed_host_turns": 2,
                     "accessed_physical_pages": [2], "answer_correct": True, "answer_withheld_from_initial_request": True}
            receipt.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "native-session provenance"):
                verify_receipt(receipt, {})

    def test_index_provider_reaches_custom_handler_without_api_key(self):
        import litellm
        class FakeHost:
            model = "gpt-5.6-luna"
            def complete(self, instructions, state, schema):
                self.seen = state
                return {"text": "controlled summary"}, {}
        host = FakeHost()
        register_index_provider(host)
        reply = litellm.completion(model=PROVIDER + "/" + INDEX_ALIAS,
                                   messages=[{"role": "user", "content": "original prompt bytes"}], max_retries=0)
        self.assertEqual(reply.choices[0].message.content, "controlled summary")
        self.assertEqual(host.seen["messages"][0]["content"], "original prompt bytes")
        self.assertIsNone(reply.usage)

    def test_undeclared_tool_or_wrong_arguments_are_rejected(self):
        request = {"tools": [{"type": "function", "name": "read_page", "parameters": {
            "type": "object", "properties": {"page": {"type": "integer"}}, "required": ["page"], "additionalProperties": False}}]}
        for call in [{"name": "write_file", "arguments": "{}"},
                     {"name": "read_page", "arguments": '{"page":"wrong"}'}]:
            with self.assertRaises(Exception):
                response_body(request, {"text": "", "tool_calls": [call]}, "gpt-5.6-luna")

    def test_no_completion_budget_never_starts_a_host(self):
        with tempfile.TemporaryDirectory(prefix="baseline-budget-") as temporary:
            root = Path(temporary)
            host = LocalCodex(root / "does-not-exist", "also-absent", root, root, 0)
            with self.assertRaisesRegex(AdapterError, "budget"):
                host.complete("instruction", {}, {"type": "object"})
            self.assertEqual(host.calls, [])

    def test_oversize_is_rejected_without_truncation(self):
        with tempfile.TemporaryDirectory(prefix="baseline-input-") as temporary:
            root = Path(temporary)
            host = LocalCodex(root / "missing", "missing", root, root, 1, max_input_bytes=50)
            with self.assertRaisesRegex(AdapterError, "byte cap"):
                host.complete("x" * 100, {}, {"type": "object"})
            self.assertEqual(host.rejections[0]["code"], "input_limit")
            self.assertFalse(host.rejections[0]["host_invoked"])

    def test_timeout_reaps_the_owned_process(self):
        with tempfile.TemporaryDirectory(prefix="baseline-process-") as temporary:
            with self.assertRaises(AdapterError) as raised:
                owned_process([sys.executable, "-c", "import time;time.sleep(5)"], Path(temporary), 0.05)
            self.assertTrue(raised.exception.cleanup["reaped"])
            self.assertTrue(raised.exception.cleanup["term_sent"])

    def test_only_returned_pages_receive_credit(self):
        envelope = {"items": [
            {"type": "function_call", "call_id": "a", "name": "get_page_content",
             "arguments": '{"doc_name":"manual.pdf","pages":"1-4"}'},
            {"type": "function_call_output", "call_id": "a",
             "output": json.dumps({"success": True, "doc_name": "manual.pdf", "returned_pages": "1-2",
                                   "content": [{"text": json.dumps({"success": True, "doc_name": "manual.pdf", "returned_pages": "3-4"})}]})},
        ]}
        self.assertEqual(returned_pages(envelope, "manual.pdf", 4), {1, 2})
        self.assertEqual(returned_pages(envelope, "different.pdf", 4), set())

    def test_sdk_page_text_integrity_is_checked_against_stored_source(self):
        envelope = {"items": [
            {"type": "function_call", "call_id": "x", "name": "get_page_content", "arguments": '{"doc_name":"sample.pdf","pages":"1"}'},
            {"type": "function_call_output", "call_id": "x", "output": json.dumps({
                "success": True, "doc_name": "sample.pdf", "returned_pages": "1", "content": [{"page": 1, "text": "forged"}]})},
        ]}
        with self.assertRaisesRegex(ValueError, "source-bound stored extraction"):
            returned_pages(envelope, "sample.pdf", 1, {1: "actual source"})

    def test_tree_control_preserves_pages_and_rejects_unreachable_nodes(self):
        parsed = {"pages": [{}, {}], "nodes": [
            {"id": "root", "parent_id": None, "title": "Root", "page_start": 1, "page_end": 2},
            {"id": "child", "parent_id": "root", "title": "Child", "page_start": 2, "page_end": 2},
        ]}
        tree = gptgrep_tree(parsed)
        self.assertEqual(tree[0]["nodes"][0]["start_index"], 2)
        with self.assertRaises(ValueError):
            gptgrep_tree({**parsed, "nodes": [{**parsed["nodes"][1], "parent_id": "absent"}]})


class ActualSdkTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_agents_runner_executes_a_tool_roundtrip_with_fake_model(self):
        from agents import Agent, RunConfig, Runner, function_tool
        from agents.models.openai_responses import OpenAIResponsesModel
        from openai import AsyncOpenAI
        called = []
        @function_tool
        def read_fixture(page: int) -> str:
            called.append(page)
            return "ORACLE_PAGE_TWO" if page == 2 else "WRONG_PAGE"

        class FakeHost:
            model = "gpt-5.6-luna"
            phase = "controlled_sdk_test"
            calls = 0
            def complete(self, instructions, state, schema):
                self.calls += 1
                self.tools = state["tools"]
                if any(item.get("type") == "function_call_output" for item in state["input"]):
                    assert "ORACLE_PAGE_TWO" in json.dumps(state["input"])
                    return {"text": "Read ORACLE_PAGE_TWO.", "tool_calls": []}, {}
                return {"text": "", "tool_calls": [{"name": "read_fixture", "arguments": '{"page":2}'}]}, {}

        host = FakeHost()
        transport = ResponsesTransport(host)
        backend = chat_backend(transport)
        client = AsyncOpenAI(**backend)
        agent = Agent(name="Controlled reader", instructions="Use the supplied tool to read page2.",
                      tools=[read_fixture], model=OpenAIResponsesModel(host.model, openai_client=client))
        try:
            result = await Runner.run(agent, input="Read the second page.", max_turns=4,
                                      run_config=RunConfig(tracing_disabled=True))
            self.assertEqual(result.final_output, "Read ORACLE_PAGE_TWO.")
            self.assertEqual(called, [2])
            self.assertEqual(host.calls, 2)
            self.assertEqual(len(transport.wire_receipts), 2)
            self.assertEqual(host.tools[0]["name"], "read_fixture")
        finally:
            await client.close()
            await backend["http_client"].aclose()


if __name__ == "__main__":
    unittest.main()
