"""Synthetic evaluator controls; no provider calls or benchmark task special cases."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts/pageindex_baseline"))
from role_hosts import RoleHost
import profiles
from run import reader_request

spec = importlib.util.spec_from_file_location("system_eval", REPO / "scripts/gptgrep_system_eval.py")
assert spec and spec.loader
system = importlib.util.module_from_spec(spec)
spec.loader.exec_module(system)


class SystemEvalTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(jev_model="typesafe/jev-1.13", codex_bin="codex",
                                  codex_home=Path("/synthetic/account"), model="gpt-5.6-luna",
                                  reasoning_effort="max", timeout=180, max_tool_calls=12)

    def test_gold_and_annotation_changes_do_not_change_reader_inputs(self):
        first = {"question": "What is the retained duration?", "doc_id": "unseen-name.pdf",
                 "answer": "SECRET_GOLD_A", "evidence_pages": "[991]", "answer_format": "Str",
                 "doc_type": "category-A", "task_type": "lookup", "source_row": 1}
        second = {**first, "answer": "SECRET_GOLD_B", "evidence_pages": "[992]",
                  "answer_format": "List", "doc_type": "category-B", "task_type": "derive", "source_row": 987}
        a = system.ask_arguments(Path("/bin/tool"), Path("/private/corpus"), first, self.args())
        b = system.ask_arguments(Path("/bin/tool"), Path("/private/corpus"), second, self.args())
        self.assertEqual(a, b)
        self.assertNotIn("SECRET_GOLD_A", repr(a))
        self.assertNotIn("991", repr(a))
        self.assertEqual(reader_request(first, "runtime-document-id", "max", 6),
                         reader_request(second, "runtime-document-id", "max", 6))
        changed_name = {**first, "doc_id": "another-unseen-name.pdf"}
        self.assertNotEqual(a, system.ask_arguments(Path("/bin/tool"), Path("/private/corpus"), changed_name, self.args()))

    def test_build_materialization_contains_only_raw_selected_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            (source / "arbitrary.pdf").write_bytes(b"original synthetic PDF bytes")
            (source / "questions.json").write_text('{"answer":"NEVER_INDEX"}')
            hashes = {"arbitrary.pdf": hashlib.sha256((source / "arbitrary.pdf").read_bytes()).hexdigest()}
            corpus = base / "corpus"
            system.materialize_corpus(source, corpus, hashes)
            self.assertEqual([path.name for path in corpus.iterdir()], ["arbitrary.pdf"])
            self.assertEqual(system.build_arguments(Path("/bin/tool"), corpus, False),
                             ["/bin/tool", "index", str(corpus), "--json"])

    def test_shared_role_views_use_one_cap_and_restore_chat_settings(self):
        class Shared:
            def __init__(self):
                self._lock = threading.RLock()
                self.model, self.effort, self.phase = "gpt-5.6-luna", "max", "chat"
                self.calls, self.rejections = [], []
            def complete(self, instructions, state, schema):
                if len(self.calls) >= 2:
                    raise RuntimeError("global cap")
                self.calls.append((self.phase, self.model, self.effort, instructions, state, schema))
                return {}, {}
        shared = Shared()
        index = RoleHost(shared, "index", {"model": "gpt-5.6-luna", "reasoning_effort": "max"})
        judge = RoleHost(shared, "judge", {"model": "gpt-5.6-luna", "reasoning_effort": "high"})
        index.complete("unchanged", {"state": 1}, {"type": "object"})
        judge.complete("unchanged", {"state": 1}, {"type": "object"})
        with self.assertRaisesRegex(RuntimeError, "global cap"):
            index.complete("", {}, {})
        self.assertEqual([row[2] for row in shared.calls], ["max", "high"])
        self.assertEqual((shared.model, shared.effort, shared.phase), ("gpt-5.6-luna", "max", "chat"))

    def test_source_default_roles_preserve_unspecified_index_effort(self):
        args = argparse.Namespace(profile="source-default-chat", model=None, reasoning_effort=None,
                                  index_model="gpt-5.6-luna", index_reasoning_effort="max",
                                  judge_model=None, judge_reasoning_effort=None)
        result = profiles.resolve(args)
        self.assertEqual(result["roles"]["chat"]["reasoning_effort"], "high")
        self.assertEqual(result["roles"]["judge"]["reasoning_effort"], "high")
        self.assertIsNone(result["source_default"]["index"]["reasoning_effort"])
        self.assertEqual(result["roles"]["index"]["reasoning_effort"], "max")

    def test_partial_jev_failure_retains_known_cost_but_has_no_total(self):
        jev = {"required": True, "attempted_calls": 2, "requests": 1, "usage": [{"cost": 0.01}],
               "accounting_complete": False}
        error = {"host_retrieval": {"jev": jev, "ledger_path": "/private/ledger"}}
        self.assertEqual(system.jev_receipt(error), jev)
        cost = system.jev_cost(jev)
        self.assertIsNone(cost["measured_cost_usd"])
        self.assertEqual(cost["known_cost_subtotal_usd"], 0.01)
        self.assertEqual(cost["missing_cost_receipts"], 1)
        self.assertFalse(cost["attempts_are_physical_requests"])

    def test_complete_jev_receipts_sum_explicit_costs(self):
        cost = system.jev_cost({"attempted_calls": 2, "requests": 2, "usage": [{"cost": 0.01}, {"cost": 0.02}],
                                "accounting_complete": True})
        self.assertAlmostEqual(cost["measured_cost_usd"], 0.03)
        self.assertTrue(cost["accounting_complete"])

    def test_ledger_recovery_uses_latest_search_once_and_keeps_torn_failure_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "attempt.jsonl"
            first = {"schema_version": "gptgrep.jev-attempt.v1", "event": "progress", "initial_status": "started",
                     "requests": 1, "attempted_calls": 1, "unobserved_attempts": 0,
                     "search": {"search_id": "s", "metrics": {"jev_models": ["typesafe/jev-1.13"], "jev_usage": [{"cost": .01}]}}}
            last = {**first, "event": "failed", "requests": 1, "attempted_calls": 2, "unobserved_attempts": 1,
                    "initial_status": "failed", "accounting_complete": False}
            path.write_bytes((json.dumps(first) + "\n" + json.dumps(last) + "\n" + '{"torn":').encode())
            recovered = system.ledger_recovery(path)
            self.assertTrue(recovered["torn_trailing_record"])
            self.assertEqual(len(recovered["jev"]["usage"]), 1)
            cost = system.jev_cost(recovered["jev"])
            self.assertEqual(cost["attempted_calls"], 2)
            self.assertEqual(cost["known_cost_subtotal_usd"], .01)
            self.assertIsNone(cost["measured_cost_usd"])
            path.write_text(json.dumps(first) + "\nmalformed\n" + json.dumps(last))
            with self.assertRaisesRegex(ValueError, "middle record"):
                system.ledger_recovery(path)

    def test_success_and_failure_ledger_locations_remain_visible(self):
        self.assertEqual(system.native_metrics({"status": "completed", "ledger_path": "/private/success"}, "x.pdf", {1})["ledger_path"], "/private/success")
        self.assertEqual(system.native_metrics({"host_retrieval": {"ledger_path": "/private/failure", "elapsed_ms": 52}}, "x.pdf", {1})["native_host_elapsed_ms"], 52)

    def test_missing_cases_or_judges_never_produce_eligible_scores(self):
        case = {"status": "completed", "metrics": {"jev_core_observed": True},
                "judge": {"status": "completed", "equivalent": True}}
        self.assertFalse(system.summarize([case], 2)["comparison_eligible"])
        self.assertIsNone(system.summarize([case], 2)["answer_equivalence_accuracy"])
        self.assertFalse(system.summarize([], 0)["comparison_eligible"])

    def test_fake_citation_excerpt_is_rejected_independently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = b"source PDF bytes"
            (root / "d.pdf").write_bytes(raw)
            sha = hashlib.sha256(raw).hexdigest()
            evidence = {"path": "d.pdf", "node_id": "a:b", "source_sha256": sha,
                        "byte_start": 0, "byte_end": 4, "excerpt_sha256": hashlib.sha256(b"fake").hexdigest()}
            report = {"tool_calls": [{"success": True, "evidence": [evidence]}], "citations": []}
            with self.assertRaisesRegex(ValueError, "excerpt differs"):
                system.verify_native_evidence(report, root, {"d.pdf": sha}, {"d.pdf": b"true text"})


if __name__ == "__main__":
    unittest.main()
