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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/pageindex_baseline"))
from native_models import (
    DEFAULT_PLANNER_MODEL, PLANNER_PROFILE, TOKEN_FIELDS, attempts_from_report,
    planner_profile, planner_profile_from_payload, usage_summary,
)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("planned_system_eval", ROOT / "scripts/gptgrep_system_eval.py")
system = importlib.util.module_from_spec(spec)
spec.loader.exec_module(system)


def attempt(role, amount=10, model="gpt-5.6-luna"):
    return {"attempt_id": role, "role": role, "status": "completed",
            "requested_model": model, "requested_reasoning_effort": "max",
            "requested_service_tier": "fast", "model": model, "model_provider": "openai",
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


class PlannerModelBindingTests(unittest.TestCase):
    reader = dict(PLANNER_PROFILE)

    def args(self, **overrides):
        values = dict(jev_model="typesafe/jev-1.13", codex_bin="codex",
                      codex_home=Path("/synthetic/runtime"), model=self.reader["model"],
                      reasoning_effort="max", service_tier="fast", timeout=180,
                      max_tool_calls=12, max_input_bytes=65536,
                      experimental_query_plan=True, experimental_evidence_roles=False)
        return argparse.Namespace(**{**values, **overrides})

    def report(self, planner_model, *, failed=False):
        steps = [attempt("query_planner", 10, planner_model), attempt("final_reader", 20)]
        reader = steps[-1]
        if failed:
            reader.update(status="failed", accounting_complete=False)
            return {"status": "failed", "code": "host_citation_validation_failed",
                    "host_retrieval": {"model_attempts": steps}}
        return {"status": "completed", "usage_scope": "final_reader", "model_attempts": steps,
                "usage": reader["usage"], "thread_id": reader["thread_id"], "turn_id": reader["turn_id"],
                "model": reader["model"], "model_provider": "openai", "auth_mode": "chatgpt",
                "requested_reasoning_effort": "max", "effective_reasoning_effort": "max",
                "requested_service_tier": "fast", "effective_service_tier": "priority"}

    def test_default_and_explicit_planner_models_bind_argv_payload_and_preserve_reader(self):
        row = {"source_row": 0, "question": "Which invented latch opens?", "doc_id": "invented.pdf",
               "answer": "unused reference", "evidence_pages": "[99]"}
        payloads = []
        for chosen, expected in [(None, "gpt-6-luna"), ("gpt-5.6-luna", "gpt-5.6-luna")]:
            args = self.args(planner_model=chosen)
            argv = system.ask_arguments(Path("tool"), Path("corpus"), row, args)
            payload = system.reader_payload(row, args)
            self.assertEqual(argv[argv.index("--planner-model") + 1], expected)
            self.assertEqual(payload["planner_model"], expected)
            self.assertEqual(payload["model"], self.reader["model"])
            self.assertEqual(payload["question"], row["question"])
            self.assertEqual(planner_profile_from_payload(payload), planner_profile(expected))
            self.assertNotIn("unused reference", repr(argv) + repr(payload))
            payloads.append(payload)
        self.assertNotEqual(system.fingerprint(payloads[0]), system.fingerprint(payloads[1]))
        self.assertEqual({k: v for k, v in payloads[0].items() if k != "planner_model"},
                         {k: v for k, v in payloads[1].items() if k != "planner_model"})
        for invalid in ("", " gpt-6-luna", "gpt-6-luna ", "bad\nmodel", "x" * 257):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                system.reader_payload(row, self.args(planner_model=invalid))
        with self.assertRaises(ValueError):
            system.ask_arguments(Path("tool"), Path("corpus"), row,
                                 self.args(experimental_query_plan=False, planner_model="gpt-6-luna"))
        ordinary = self.args(experimental_query_plan=False)
        self.assertNotIn("planner_model", system.reader_payload(row, ordinary))
        self.assertNotIn("--planner-model", system.ask_arguments(Path("tool"), Path("corpus"), row, ordinary))

    def test_selected_profile_validates_both_completed_and_failed_steps(self):
        for model in (DEFAULT_PLANNER_MODEL, "gpt-5.6-luna"):
            for failed in (False, True):
                report = self.report(model, failed=failed)
                records = attempts_from_report(report, self.reader, required=True,
                                               planner_profile=planner_profile(model))
                self.assertEqual(records[0]["model"], model)
                self.assertEqual(records[1]["model"], self.reader["model"])
                totals = usage_summary(records)
                self.assertEqual(totals["token_totals"]["total_tokens"]["known_subtotal"], 30)
                self.assertEqual(totals["token_totals"]["total_tokens"]["total"], None if failed else 30)
                other = "gpt-5.6-luna" if model == DEFAULT_PLANNER_MODEL else DEFAULT_PLANNER_MODEL
                with self.assertRaises(ValueError):
                    attempts_from_report(report, self.reader, required=True,
                                         planner_profile=planner_profile(other))
        wrong_reader = self.report(DEFAULT_PLANNER_MODEL)
        wrong_reader["model_attempts"][1].update(model=DEFAULT_PLANNER_MODEL, requested_model=DEFAULT_PLANNER_MODEL)
        with self.assertRaises(ValueError):
            attempts_from_report(wrong_reader, self.reader, required=True,
                                 planner_profile=planner_profile())

    def test_native_invocation_qualifies_the_bound_planner_on_success_and_failure(self):
        for model in (DEFAULT_PLANNER_MODEL, "gpt-5.6-luna"):
            for failed in (False, True):
                report = self.report(model, failed=failed)
                with tempfile.TemporaryDirectory() as directory:
                    class Shared:
                        run_dir = Path(directory)
                        receipt = {"ordinal": 1}

                        @contextlib.contextmanager
                        def external_attempt(self, *args, **kwargs):
                            def run(*args, **kwargs):
                                return subprocess.CompletedProcess([], int(failed), json.dumps(report).encode(), b"")
                            yield types.SimpleNamespace(receipt=self.receipt, run=run)

                        def call_by_ordinal(self, ordinal):
                            return self.receipt

                    shared = Shared()
                    (shared.run_dir / "calls").mkdir()
                    payload = {**self.reader, "phase": "synthetic-reader", "experimental_query_plan": True,
                               "planner_model": model}
                    _, receipt = system.invoke_native(shared, ["synthetic-native"], payload, 5)
                    self.assertEqual(receipt["status"], "failed" if failed else "completed")
                    self.assertEqual(receipt["model_attempts"][0]["model"], model)
                    self.assertEqual(receipt["model_turn_accounting"]["observed_turns"], 2)
                    totals = receipt["model_turn_accounting"]["token_totals"]["total_tokens"]
                    self.assertEqual(totals["known_subtotal"], 30)
                    self.assertEqual(totals["total"], None if failed else 30)

    def test_legacy_absent_binding_keeps_historical_planner_contract(self):
        self.assertEqual(planner_profile()["model"], DEFAULT_PLANNER_MODEL)
        legacy = {"experimental_query_plan": True}
        self.assertEqual(planner_profile_from_payload(legacy), PLANNER_PROFILE)
        self.assertEqual(len(attempts_from_report(self.report("gpt-5.6-luna"), self.reader, required=True)), 2)
        with self.assertRaises(ValueError):
            attempts_from_report(self.report(DEFAULT_PLANNER_MODEL), self.reader, required=True)
        with self.assertRaises(ValueError):
            planner_profile_from_payload({"planner_model": DEFAULT_PLANNER_MODEL})

    def test_ledger_recovery_checks_declared_planner_on_partial_failure(self):
        query = hashlib.sha256(b"synthetic question").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            for model in (DEFAULT_PLANNER_MODEL, "gpt-5.6-luna"):
                event = {"schema_version": "gptgrep.jev-attempt.v1", "event": "failed",
                         "generation": "g-synthetic", "workflow": {"query_sha256": query,
                         "document_scope": "invented.pdf", "generation": "g-synthetic"},
                         "model_attempts": self.report(model, failed=True)["host_retrieval"]["model_attempts"],
                         "attempted_calls": 4, "requests": 4, "unobserved_attempts": 0,
                         "accounting_complete": False}
                path.write_text(json.dumps(event) + "\n")
                kwargs = dict(generation="g-synthetic", query_sha256=query, document="invented.pdf",
                              reader_profile=self.reader, planned=True)
                recovered = system.ledger_recovery(path, **kwargs, planner_profile=planner_profile(model))
                self.assertEqual(recovered["model_attempts"][0]["model"], model)
                self.assertIsNone(recovered["model_turn_accounting"]["token_totals"]["total_tokens"]["total"])
                other = "gpt-5.6-luna" if model == DEFAULT_PLANNER_MODEL else DEFAULT_PLANNER_MODEL
                with self.assertRaises(ValueError):
                    system.ledger_recovery(path, **kwargs, planner_profile=planner_profile(other))

    def test_cumulative_bound_response_uses_payload_planner_for_failed_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "calls").mkdir()
            request = {**self.reader, "operation": "ask", "phase": "answer:native:synthetic",
                       "experimental_query_plan": True, "planner_model": DEFAULT_PLANNER_MODEL}
            response = self.report(DEFAULT_PLANNER_MODEL, failed=True)
            rp, sp = run / "calls/00001.request.json", run / "calls/00001.response.json"
            rp.write_bytes(system.json_bytes(request))
            sp.write_text(json.dumps(response))
            call = {"ordinal": 1, "operation": "native_ask", "phase": request["phase"],
                    "status": "failed", "elapsed_ms": 20,
                    "request_sha256": system.locks.digest(rp), "response_sha256": system.locks.digest(sp)}
            shared = types.SimpleNamespace(calls=[call])
            actual = system.cumulative_accounting(shared, run)["native_model_turn_accounting"]
            self.assertEqual(actual["attempted_calls"], 2)
            self.assertEqual(actual["token_totals"]["total_tokens"]["known_subtotal"], 30)
            self.assertIsNone(actual["token_totals"]["total_tokens"]["total"])
            request["planner_model"] = "gpt-5.6-luna"
            rp.write_bytes(system.json_bytes(request))
            call["request_sha256"] = system.locks.digest(rp)
            rejected = system.cumulative_accounting(shared, run)["native_model_turn_accounting"]
            self.assertEqual(rejected["unavailable_native_ask_accounting"], 1)
            self.assertIsNone(rejected["attempted_calls"])
            self.assertIsNone(rejected["token_totals"]["total_tokens"]["total"])

    def test_retained_complete_response_cannot_cross_planner_arms(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "calls").mkdir()
            request = {**self.reader, "operation": "ask", "phase": "answer:native:synthetic",
                       "experimental_query_plan": True, "planner_model": DEFAULT_PLANNER_MODEL}
            response = self.report(DEFAULT_PLANNER_MODEL)
            records = attempts_from_report(response, self.reader, required=True, planner_profile=planner_profile())
            rp, sp = run / "calls/00001.request.json", run / "calls/00001.response.json"
            rp.write_bytes(system.json_bytes(request))
            sp.write_text(json.dumps(response))
            call = {"ordinal": 1, "status": "completed", "phase": request["phase"],
                    "requested_model": self.reader["model"], "requested_effort": "max",
                    "requested_service_tier": "fast", "thread_id": response["thread_id"],
                    "turn_id": response["turn_id"], "model_attempts": records,
                    "request_sha256": system.locks.digest(rp), "response_sha256": system.locks.digest(sp)}
            shared = types.SimpleNamespace(run_dir=run, call_by_ordinal=lambda _: call)
            arguments = (1, request, self.reader["model"], "max", request["phase"], shared, "fast")
            self.assertIsNotNone(system.completed_host_response(*arguments))
            # Even mutually consistent request/receipt hashes cannot relabel the actual model.
            request["planner_model"] = "gpt-5.6-luna"
            rp.write_bytes(system.json_bytes(request))
            call["request_sha256"] = system.locks.digest(rp)
            with self.assertRaises(ValueError):
                system.completed_host_response(*arguments)

    def test_plan_manifest_binds_selected_planner_and_keeps_judge_fixed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            benchmark, upstream, judge = (root / name for name in ("benchmark", "upstream", "judge"))
            (benchmark / "documents").mkdir(parents=True)
            upstream.mkdir()
            (judge / "eval").mkdir(parents=True)
            (judge / "eval/judge.py").write_text("# synthetic judge source\n")
            (benchmark / "questions.json").write_text(json.dumps([{
                "question": "Where is the invented latch?", "doc_id": "invented.pdf",
                "answer": "unused synthetic reference", "evidence_pages": "[1]"}]))
            (benchmark / "documents/invented.pdf").write_bytes(b"synthetic source; plan stage never parses it")
            binary = root / "synthetic-binary"
            binary.write_bytes(b"not executable; plan stage cannot launch")
            args = self.args(planner_model=None, binary=binary, judge_binary=binary, benchmark=benchmark,
                             upstream=upstream, judge_source=judge, run_dir=root / "new-run", rows="all",
                             profile="matched-luna-max", index_model="gpt-5.6-luna", index_reasoning_effort=None,
                             judge_model=None, judge_reasoning_effort=None, model=None, reasoning_effort=None,
                             reader_concurrency=5, judge_concurrency=5, max_model_calls=0, build_timeout=300,
                             optimize_merge=False, retry_failed=False, stage="plan", baseline_summary=None)
            with mock.patch.object(system.locks, "verify", return_value={}), \
                 mock.patch.object(system.cohorts, "load", return_value=({"development": {0}, "heldout": set()}, "fixture-cohort")), \
                 mock.patch.object(system.cohorts, "select", return_value=[0]):
                new = system.execute(args)
                self.assertEqual(new["profile"]["roles"]["query_planner"], planner_profile())
                self.assertEqual(new["query_strategy"]["planner"], planner_profile())
                self.assertEqual(new["profile"]["roles"]["judge"], {**self.reader, "reasoning_effort": "high"})
                self.assertEqual(new["new_host_invocations"], 0)
                args.planner_model = "gpt-5.6-luna"
                with self.assertRaises(ValueError):
                    system.execute(args)
                args.run_dir = root / "control-run"
                control = system.execute(args)
                self.assertEqual(control["query_strategy"]["planner"], PLANNER_PROFILE)
                self.assertEqual(control["profile"]["roles"]["judge"], new["profile"]["roles"]["judge"])
                self.assertNotEqual(system.run_binding(control), system.run_binding(new))
                self.assertFalse(list(root.rglob("*.response.json")))


if __name__ == "__main__":
    unittest.main()
