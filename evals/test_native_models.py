"""Synthetic checks for nested model accounting; no provider calls or task data."""
import copy
import contextlib
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/pageindex_baseline"))
from native_models import PLANNER_PROFILE, TOKEN_FIELDS, attempts_from_report, usage_summary

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("planned_system_eval", ROOT / "scripts/gptgrep_system_eval.py")
system = importlib.util.module_from_spec(spec)
spec.loader.exec_module(system)


def attempt(role, amount=10):
    return {"attempt_id": role, "role": role, "status": "completed",
            "requested_model": "gpt-5.6-luna", "requested_reasoning_effort": "max",
            "requested_service_tier": "fast", "model": "gpt-5.6-luna", "model_provider": "openai",
            "effective_reasoning_effort": "max", "effective_service_tier": "priority",
            "thread_id": "thread-" + role, "turn_id": "turn-" + role,
            "usage": {"total": {key: amount for key in TOKEN_FIELDS.values()},
                      "last": {key: 99999 for key in TOKEN_FIELDS.values()}, "modelContextWindow": 1000000},
            "elapsed_ms": 12, "server_retry_notifications": 0, "accounting_complete": True}


class NativeModelAccountingTests(unittest.TestCase):
    def budget_failure(self):
        budget = {"max_tool_calls": 1, "admitted_tool_calls": 1,
                  "denied_tool_calls": 1, "max_denied_tool_calls": 1}
        records = [attempt("query_planner", 14), attempt("final_reader", 40)]
        records[1].update(status="failed", accounting_complete=False)
        denial = {"tool": "gptgrep_search", "success": False, "required_initial": False,
                  "evidence": [], "search": None, "tool_budget": budget}
        report = {"status": "failed", "code": "host_tool_budget_exhausted", "host_retrieval": {
            "cause": {"kind": "tool_budget_exhausted", "tool_budget": budget,
                      "usage": records[1]["usage"], "accounting_complete": False},
            "model_attempts": records, "receipts": [denial]}}
        return report, budget, denial

    def test_native_budget_failure_retains_sanitized_diagnostics_and_two_actual_turns(self):
        report, budget, _ = self.budget_failure()
        with tempfile.TemporaryDirectory() as directory:
            class Shared:
                run_dir = Path(directory)
                receipt = {"ordinal": 1}
                starts = 0

                @contextlib.contextmanager
                def external_attempt(self, *args, **kwargs):
                    def run(*args, **kwargs):
                        self.starts += 1
                        return subprocess.CompletedProcess([], 1, json.dumps(report).encode(), b"")
                    yield types.SimpleNamespace(receipt=self.receipt, run=run)

                def call_by_ordinal(self, ordinal):
                    return self.receipt

            shared = Shared()
            (shared.run_dir / "calls").mkdir()
            payload = {**PLANNER_PROFILE, "phase": "synthetic_reader", "experimental_query_plan": True}
            _, receipt = system.invoke_native(shared, ["synthetic-native"], payload, 5)
            self.assertEqual(shared.starts, 1)
            self.assertEqual(receipt["tool_budget"], budget)
            self.assertEqual(receipt["usage"]["total"]["totalTokens"], 40)
            self.assertEqual(receipt["model_turn_accounting"]["observed_turns"], 2)
            self.assertEqual(receipt["model_turn_accounting"]["token_totals"]["total_tokens"]["known_subtotal"], 54)
            self.assertIsNone(receipt["model_turn_accounting"]["token_totals"]["total_tokens"]["total"])

    def test_bound_ledger_recovery_keeps_budget_and_nested_usage_separate(self):
        report, budget, denial = self.budget_failure()
        query = hashlib.sha256(b"an invented request").hexdigest()
        event = {"schema_version": "gptgrep.jev-attempt.v1", "event": "failed",
                 "generation": "g-synthetic", "workflow": {"query_sha256": query,
                 "document_scope": "invented.md", "generation": "g-synthetic"},
                 "model_attempts": report["host_retrieval"]["model_attempts"],
                 "receipt": denial, "attempted_calls": 4, "requests": 4,
                 "unobserved_attempts": 0, "accounting_complete": False}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempt.jsonl"
            path.write_text(json.dumps(event) + "\n")
            recovered = system.ledger_recovery(path, generation="g-synthetic", query_sha256=query,
                                               document="invented.md", reader_profile=PLANNER_PROFILE, planned=True)
            self.assertEqual(recovered["tool_budget"], budget)
            self.assertEqual(recovered["model_turn_accounting"]["observed_turns"], 2)
            self.assertEqual(recovered["model_turn_accounting"]["token_totals"]["total_tokens"]["known_subtotal"], 54)
            self.assertFalse(recovered["jev"]["accounting_complete"])

    def report(self):
        return {"status": "completed", "usage_scope": "final_reader",
                "usage": attempt("final_reader", 20)["usage"],
                "model_attempts": [attempt("query_planner", 10), attempt("final_reader", 20)]}

    def test_planner_and_reader_are_counted_without_last_or_context_or_legacy_duplication(self):
        records = attempts_from_report(self.report(), PLANNER_PROFILE, required=True)
        total = usage_summary(records)
        self.assertEqual(total["attempted_calls"], 2)
        self.assertEqual(total["observed_turns"], 2)
        self.assertEqual(total["token_totals"]["total_tokens"]["total"], 30)
        self.assertEqual(total["roles"]["query_planner"]["attempted_calls"], 1)
        self.assertEqual(total["roles"]["final_reader"]["attempted_calls"], 1)
        self.assertTrue(total["accounting_complete"])
        self.assertNotIn("last", records[0]["usage"])
        self.assertNotIn("modelContextWindow", records[0]["usage"])

    def test_partial_failed_observations_remain_known_subtotals(self):
        report = self.report()
        report["status"] = "failed"
        report["model_attempts"][1].update(status="interrupted", accounting_complete=False)
        records = attempts_from_report(report, PLANNER_PROFILE, required=True)
        total = usage_summary(records)
        self.assertEqual(total["token_totals"]["total_tokens"]["known_subtotal"], 30)
        self.assertIsNone(total["token_totals"]["total_tokens"]["total"])
        self.assertFalse(total["accounting_complete"])
        report["model_attempts"][1]["usage"] = None
        total = usage_summary(attempts_from_report(report, PLANNER_PROFILE, required=True))
        self.assertEqual(total["token_totals"]["total_tokens"]["known_subtotal"], 10)
        self.assertEqual(total["token_totals"]["total_tokens"]["missing_contributions"], 1)

    def test_failed_planner_does_not_invent_a_reader_or_usage(self):
        step = attempt("query_planner")
        step.update(status="failed", model=None, model_provider=None, thread_id=None,
                    turn_id=None, usage=None, accounting_complete=False)
        report = {"host_retrieval": {"model_attempts": [step]}}
        total = usage_summary(attempts_from_report(report, PLANNER_PROFILE, required=True))
        self.assertEqual(total["attempted_calls"], 1)
        self.assertEqual(total["observed_turns"], 0)
        self.assertEqual(total["roles"]["final_reader"]["attempted_calls"], 0)
        self.assertIsNone(total["token_totals"]["total_tokens"]["total"])
        self.assertIsNone(attempts_from_report({}, PLANNER_PROFILE))
        self.assertFalse(usage_summary(None)["available"])
        with self.assertRaises(ValueError):
            attempts_from_report({}, PLANNER_PROFILE, required=True)

    def test_last_only_or_empty_total_does_not_count_as_observed_total_usage(self):
        for observed in ({"last": {"totalTokens": 500}}, {"total": {}}, {"total": None}, {}):
            step = attempt("query_planner")
            step.update(status="failed", usage=observed, accounting_complete=False)
            records = attempts_from_report({"host_retrieval": {"model_attempts": [step]}},
                                           PLANNER_PROFILE, required=True)
            total = usage_summary(records)
            self.assertEqual(total["missing_usage_attempts"], 1)
            self.assertIsNone(total["token_totals"]["total_tokens"]["total"])
            self.assertEqual(total["token_totals"]["total_tokens"]["known_subtotal"], 0)

    def test_conflicting_or_duplicate_role_and_turn_identity_is_rejected(self):
        for mutate in (
            lambda r: r["model_attempts"].append(attempt("query_planner")),
            lambda r: r["model_attempts"][1].update(role="query_planner"),
            lambda r: r["model_attempts"][1].update(attempt_id="query_planner"),
            lambda r: r["model_attempts"][1].update(thread_id="thread-query_planner", turn_id="turn-query_planner"),
        ):
            report = self.report()
            mutate(report)
            with self.assertRaises(ValueError):
                attempts_from_report(report, PLANNER_PROFILE, required=True)
        records = attempts_from_report(self.report(), PLANNER_PROFILE, required=True)
        duplicate = usage_summary([*records, copy.deepcopy(records[0])])
        self.assertEqual(duplicate["duplicate_turn_observations"], 1)
        self.assertEqual(duplicate["token_totals"]["total_tokens"]["total"], 30)
        changed = copy.deepcopy(records[0])
        changed["usage"]["total"]["inputTokens"] += 1
        with self.assertRaises(ValueError):
            usage_summary([*records, changed])

    def test_invalid_numbers_profiles_and_incomplete_success_do_not_pass(self):
        mutations = [
            lambda r: r["model_attempts"][0].update(requested_model="another-model"),
            lambda r: r["model_attempts"][0].update(effective_service_tier="flex"),
            lambda r: r["model_attempts"][0].update(thread_id=None),
            lambda r: r["model_attempts"][0].update(status="failed"),
            lambda r: r["model_attempts"][0].update(elapsed_ms=float("nan")),
            lambda r: r["model_attempts"].pop(),
        ]
        for value in (True, -1, 1.5, 2**64):
            mutations.append(lambda r, value=value: r["model_attempts"][0]["usage"]["total"].update(totalTokens=value))
        for mutate in mutations:
            report = self.report()
            mutate(report)
            with self.assertRaises(ValueError):
                attempts_from_report(report, PLANNER_PROFILE, required=True)

    def test_query_planning_keeps_original_reader_inputs_and_excludes_gold(self):
        args = argparse.Namespace(jev_model="typesafe/jev-1.13", codex_bin="codex",
                                  codex_home=Path("/synthetic/runtime"), model="gpt-5.6-luna",
                                  reasoning_effort="max", service_tier="fast", timeout=180,
                                  max_tool_calls=12, experimental_query_plan=True)
        row = {"source_row": 1, "question": "What  does the invented rule cover?",
               "doc_id": "invented.md", "answer": "PRIVATE_GOLD", "evidence_pages": "[999]"}
        altered = {**row, "answer": "DIFFERENT_GOLD", "evidence_pages": "[123]", "task_type": "secret"}
        first = system.ask_arguments(Path("tool"), Path("/synthetic/corpus"), row, args)
        self.assertEqual(first, system.ask_arguments(Path("tool"), Path("/synthetic/corpus"), altered, args))
        self.assertLess(first.index("--experimental-query-plan"), first.index("--"))
        self.assertIn(row["question"], first)
        self.assertEqual(system.reader_payload(row, args), system.reader_payload(altered, args))
        self.assertNotIn("PRIVATE_GOLD", repr(first))
        self.assertTrue(system.reader_payload(row, args)["experimental_query_plan"])
        args.experimental_query_plan = False
        self.assertNotIn("experimental_query_plan", system.reader_payload(row, args))
        self.assertNotIn("--experimental-query-plan", system.ask_arguments(Path("tool"), Path("/synthetic/corpus"), row, args))

    def test_evidence_roles_are_explicit_bound_reader_inputs_without_gold(self):
        args = argparse.Namespace(jev_model="typesafe/jev-1.13", codex_bin="codex",
                                  codex_home=Path("/synthetic/runtime"), model="gpt-5.6-luna",
                                  reasoning_effort="max", service_tier="fast", timeout=180,
                                  max_tool_calls=12, experimental_query_plan=True,
                                  experimental_evidence_roles=False)
        row = {"source_row": 2, "question": "Which invented component owns the relation?",
               "doc_id": "invented.md", "answer": "PRIVATE_GOLD", "evidence_pages": "[999]"}
        altered = {**row, "answer": "CHANGED_GOLD", "evidence_pages": "[1]", "task_type": "private"}
        ordinary = system.reader_payload(row, args)
        self.assertNotIn("experimental_evidence_roles", ordinary)
        self.assertNotIn("--experimental-evidence-roles", system.ask_arguments(Path("tool"), Path("/synthetic/corpus"), row, args))
        args.experimental_evidence_roles = True
        selected = system.reader_payload(row, args)
        self.assertTrue(selected["experimental_evidence_roles"])
        self.assertNotEqual(system.fingerprint(selected), system.fingerprint(ordinary))
        self.assertEqual(selected, system.reader_payload(altered, args))
        command = system.ask_arguments(Path("tool"), Path("/synthetic/corpus"), row, args)
        self.assertEqual(command, system.ask_arguments(Path("tool"), Path("/synthetic/corpus"), altered, args))
        self.assertLess(command.index("--experimental-evidence-roles"), command.index("--"))
        self.assertNotIn("PRIVATE_GOLD", repr(selected))
        args.experimental_query_plan = False
        with self.assertRaisesRegex(ValueError, "requires"):
            system.reader_payload(row, args)
        with self.assertRaisesRegex(ValueError, "requires"):
            system.ask_arguments(Path("tool"), Path("/synthetic/corpus"), row, args)

    def test_requested_role_policy_requires_observed_bound_decisions(self):
        question = "Which invented component owns the relation?"
        payload = {"question": question, "experimental_evidence_roles": True}
        role = {
            "strategy": "jev-evidence-role-v1",
            "original_question_sha256": hashlib.sha256(question.encode()).hexdigest(),
            "decision_contract_sha256": "a" * 64, "request_sha256": "b" * 64,
            "request_bytes": 2048, "question_count": 4,
            "score_question_count": 2, "choice_question_count": 2,
            "delivered_set_sufficiency": "unassessed",
        }
        def report(block):
            return {"jev": {"searches": [{"required_initial": True, "plan": {
                "coverage": {"union_candidates": 2}, "evidence_roles": block,
            }}]}}
        self.assertEqual(system.evidence_role_policy(report(role), payload), role)
        self.assertIsNone(system.evidence_role_policy(report(None), {"question": question}))
        with self.assertRaisesRegex(ValueError, "did not execute"):
            system.evidence_role_policy(report(None), payload)
        with self.assertRaisesRegex(ValueError, "unrequested"):
            system.evidence_role_policy(report(role), {"question": question})
        for changed in (
            {"original_question_sha256": "c" * 64},
            {"choice_question_count": 1}, {"question_count": 5},
            {"request_bytes": 65537}, {"request_bytes": True},
            {"decision_contract_sha256": None},
            {"delivered_set_sufficiency": "sufficient"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                system.evidence_role_policy(report({**role, **changed}), payload)

    def test_planner_usage_is_recovered_before_any_jev_request_with_original_workflow_binding(self):
        question = hashlib.sha256(b"an original synthetic request").hexdigest()
        workflow = {"query_sha256": question, "document_scope": "invented.md", "generation": "g-synthetic"}
        event = {"schema_version": "gptgrep.jev-attempt.v1", "event": "failed",
                 "generation": "g-synthetic", "workflow": workflow,
                 "model_attempts": [attempt("query_planner")], "attempted_calls": 0,
                 "requests": 0, "unobserved_attempts": 0, "accounting_complete": False}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempt.jsonl"
            path.write_text(json.dumps(event) + "\n")
            recovered = system.ledger_recovery(path, generation="g-synthetic", query_sha256=question,
                                               document="invented.md", reader_profile=PLANNER_PROFILE, planned=True)
            self.assertTrue(recovered["case_identity_verified"])
            self.assertEqual(recovered["jev"]["requests"], 0)
            self.assertFalse(recovered["jev"]["accounting_complete"])
            self.assertEqual(recovered["model_turn_accounting"]["attempted_calls"], 1)
            self.assertEqual(recovered["model_turn_accounting"]["token_totals"]["total_tokens"]["total"], 10)
            for broken in ({**event, "workflow": {**workflow, "query_sha256": "foreign"}},
                           {key: value for key, value in event.items() if key != "workflow"}):
                path.write_text(json.dumps(broken) + "\n")
                with self.assertRaises(ValueError):
                    system.ledger_recovery(path, generation="g-synthetic", query_sha256=question,
                                           document="invented.md", reader_profile=PLANNER_PROFILE, planned=True)


if __name__ == "__main__":
    unittest.main()
