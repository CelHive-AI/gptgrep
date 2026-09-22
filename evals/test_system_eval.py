"""Synthetic evaluator controls; no provider calls or benchmark task special cases."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from subprocess import CompletedProcess
from unittest.mock import patch

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
        return argparse.Namespace(jev_model="typesafe/jev-1.13", codex_bin="codex", service_tier="fast",
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
            def complete(self, instructions, state, schema, **overrides):
                if len(self.calls) >= 2:
                    raise RuntimeError("global cap")
                self.calls.append((overrides.get("phase", self.phase), overrides.get("model", self.model),
                                   overrides.get("effort", self.effort), instructions, state, schema))
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

    def test_service_tier_default_alias_and_unknown_metadata(self):
        parser = argparse.ArgumentParser()
        profiles.add_arguments(parser)
        self.assertEqual(parser.parse_args([]).service_tier, "fast")
        arguments = system.ask_arguments(Path("/bin/tool"), Path("/private/corpus"),
                                         {"question": "ordinary query", "doc_id": "arbitrary.pdf"}, self.args())
        self.assertEqual(arguments[arguments.index("--service-tier") + 1], "fast")
        self.assertTrue(system.tier_matches("fast", "priority"))
        self.assertTrue(system.tier_matches("fast", None))
        self.assertFalse(system.tier_matches("fast", "flex"))
        self.assertIsNone(system.service_tier_metadata("fast", {})["effective_service_tier"])
        self.assertIsNone(system.service_tier_metadata("fast", {})["reported_requested_service_tier"])

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

    def test_ledger_selection_binds_pid_time_query_and_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / ".gptgrep/host-attempts"
            directory.mkdir(parents=True)
            for name in ("attempt-100-42-0.jsonl", "attempt-250-42-0.jsonl", "attempt-260-43-0.jsonl", "attempt-450-42-0.jsonl"):
                (directory / name).write_text("")
            receipt = {"host_pid": 42, "process_finished_unix_ns": 400}
            selected = system.owned_ledger_paths(root, receipt, not_before_unix_ns=200)
            self.assertEqual([path.name for path in selected], ["attempt-250-42-0.jsonl"])
            self.assertEqual(system.owned_ledger_paths(root, receipt), [])
            query = hashlib.sha256(b"unseen query").hexdigest()
            event = {"schema_version": "gptgrep.jev-attempt.v1", "generation": "generation", "event": "completed",
                     "attempted_calls": 1, "requests": 1, "unobserved_attempts": 0, "accounting_complete": True,
                     "search": {"search_id": "owned", "required_initial": True, "query_sha256": query,
                                "document_scope": "random.pdf", "generation": "generation", "metrics": {"jev_usage": [{"cost": .01}]}}}
            selected[0].write_text(json.dumps(event) + "\n")
            self.assertTrue(system.ledger_recovery(selected[0], generation="generation", query_sha256=query, document="random.pdf")["case_identity_verified"])
            with self.assertRaisesRegex(ValueError, "query/document scope"):
                system.ledger_recovery(selected[0], generation="generation", query_sha256=query, document="different.pdf")

    def test_interrupted_stage_time_stays_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system.checkpoint_attempt(root / "stage-readers.json", {"execution_id": "lost", "status": "started", "wall_ms": None})
            system.checkpoint_attempt(root / "stage-readers.json", {"execution_id": "resumed", "status": "completed", "wall_ms": 3})
            history = system.stage_history(root)
            self.assertIsNone(history["active_stage_wall_ms"])
            self.assertEqual(history["known_active_stage_wall_ms_subtotal"], 3)
            self.assertEqual(history["missing_stage_durations"], 1)

    @unittest.skipUnless(hasattr(os, "killpg"), "Owned process groups require POSIX")
    def test_stage_cancellation_reaps_owned_process_without_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = system.LocalCodex(root / "unused", "codex", root, root, 4, reader_concurrency=2)
            started = threading.Event()
            original = host._process_started
            def on_start(receipt, pid):
                original(receipt, pid)
                started.set()
            def worker(row):
                if row["source_row"] == 1:
                    self.assertTrue(started.wait(2))
                    raise RuntimeError("synthetic cancellation")
                with host.external_attempt({"synthetic": True}, phase="answer:native:row-0", model="gpt-5.6-luna",
                                           effort="max", service_tier="fast") as attempt:
                    attempt.run([sys.executable, "-c", "import time;time.sleep(30)"], timeout=3)
                    attempt.receipt["status"] = "completed"
                return {"source_row": 0, "status": "completed"}
            try:
                with patch.object(host, "_process_started", side_effect=on_start):
                    with self.assertRaisesRegex(RuntimeError, "synthetic cancellation"):
                        system.bounded_stage([{"source_row": 0}, {"source_row": 1}], 2, worker, host, root, "readers")
                receipt = host.calls[0]
                self.assertTrue(receipt["timeout_cleanup"]["reaped"])
                with self.assertRaises(ProcessLookupError):
                    os.killpg(receipt["host_pid"], 0)
            finally:
                host.close(cancel=True)

    def test_resume_binding_changes_only_the_cumulative_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "manifest.json"
            original = {"source_hashes": {"arbitrary.pdf": "source"}, "profile": {"model": "fixed"},
                        "question_sha256": "questions", "host_input_cap": 500, "binary_sha256": "binary", "max_host_invocations": 2}
            first = system.reuse_manifest(target, original)
            retained = target.read_bytes()
            self.assertEqual(first, system.reuse_manifest(target, {**original, "max_host_invocations": 9}))
            self.assertEqual(target.read_bytes(), retained)
            for field in ("source_hashes", "profile", "question_sha256", "host_input_cap", "binary_sha256"):
                with self.assertRaisesRegex(ValueError, "conditions changed"):
                    system.reuse_manifest(target, {**original, field: "changed"})


class ResumeExecutionTests(unittest.TestCase):
    """Execute the real evaluator state machine with synthetic subprocess responses."""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.benchmark = self.root / "reference"
        (self.benchmark / "documents").mkdir(parents=True)
        self.rows = [{"doc_id": name, "question": question, "answer": "private fixture reference",
                      "answer_format": "Str", "evidence_pages": "[1]"}
                     for name, question in (("alpha.pdf", "first fixture query"), ("omega.pdf", "second fixture query"))]
        for row in self.rows:
            (self.benchmark / "documents" / row["doc_id"]).write_bytes(b"synthetic raw PDF source")
        (self.benchmark / "questions.json").write_text(json.dumps(self.rows))
        judge = self.root / "judge/eval"
        judge.mkdir(parents=True)
        (judge / "judge.py").write_text("# synthetic judge fixture\n")
        (self.root / "binary").write_bytes(b"synthetic executable identity")
        self.args = argparse.Namespace(stage="run", rows="all", profile="matched-luna-max", model=None, reasoning_effort=None,
            index_model="gpt-5.6-luna", index_reasoning_effort="max", judge_model=None, judge_reasoning_effort=None,
            service_tier="fast",
            upstream=self.root / "upstream", benchmark=self.benchmark, judge_source=self.root / "judge", run_dir=self.root / "run",
            binary=self.root / "binary", judge_binary=None, codex_bin="codex", codex_home=self.root / "account",
            max_model_calls=8, timeout=180, max_input_bytes=262144, max_tool_calls=12, build_timeout=300,
            reader_concurrency=1, judge_concurrency=1,
            jev_model="typesafe/jev-1.13", optimize_merge=False, baseline_summary=None, retry_failed=False)
        self.constants = {"PROMPT": "Q:{question}\nREF:{answer}\nFORMAT:{answer_format}\nOUTPUT:{response}",
            "MAX_RESPONSE_CHARS": 12000, "MODEL": "gpt-5.6-luna", "EFFORT": "high",
            "SCHEMA": {"type": "object", "properties": {"equivalent": {"type": "boolean"}, "abstained": {"type": "boolean"}, "reason": {"type": "string"}},
                       "required": ["equivalent", "abstained", "reason"], "additionalProperties": False}}
        self.counts = {"index": 0, "ask": 0, "judge": 0}
        self.seen_arguments = []
        self.model_delay = 0
        self.interrupt_second_ask = False
        self.interrupt_first_judge = False
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target, value in (("verify", {"source_lock_sha256": "synthetic-lock"}),):
            self.stack.enter_context(patch.object(system.locks, target, return_value=value))
        self.stack.enter_context(patch.object(system.cohorts, "load", return_value=({"development8": {0}, "heldout54": {1}}, "synthetic-cohort")))
        self.stack.enter_context(patch.object(system, "judge_constants", return_value=self.constants))
        self.stack.enter_context(patch.object(system, "owned_process", side_effect=self.process))
        self.stack.enter_context(patch("bridge.owned_process", side_effect=self.process))

    def process(self, arguments, cwd, timeout, **hooks):
        self.seen_arguments.append(arguments)
        if hooks.get("on_start") is not None:
            hooks["on_start"](999999)  # Synthetic callback; no process/model is launched.
        operation = arguments[1]
        if operation == "index":
            self.counts["index"] += 1
            corpus = Path(arguments[2])
            generation = corpus / ".gptgrep/generations/fixture-generation"
            (generation / "text").mkdir(parents=True)
            docs = []
            for row in self.rows:
                name = row["doc_id"]
                identifier = hashlib.sha256(name.encode()).hexdigest()[:24]
                text = b"canonical fixture text"
                (generation / "text" / (identifier + ".txt")).write_bytes(text)
                docs.append({"path": name, "id": identifier,
                             "source_sha256": system.locks.digest(corpus / name), "text_sha256": hashlib.sha256(text).hexdigest()})
            (generation / "manifest.json").write_text(json.dumps({"documents": docs}))
            (corpus / ".gptgrep/CURRENT.json").write_text(json.dumps({"generation": "fixture-generation",
                "manifest_sha256": system.locks.digest(generation / "manifest.json")}))
            report = {"indexed_files": len(self.rows)}
        else:
            if self.model_delay:
                time.sleep(self.model_delay)
            model = arguments[arguments.index("--model") + 1]
            effort = arguments[arguments.index("--reasoning-effort") + 1]
            tier = arguments[arguments.index("--service-tier") + 1]
            report = {"status": "completed", "model": model, "requested_reasoning_effort": effort,
                      "requested_service_tier": tier, "effective_service_tier": "priority" if tier == "fast" else tier,
                      "model_provider": "openai", "auth_mode": "chatgpt", "thread_id": "synthetic-thread", "turn_id": "synthetic-turn"}
            if operation == "ask":
                self.counts["ask"] += 1
                if self.interrupt_second_ask and self.counts["ask"] == 2:
                    raise KeyboardInterrupt("synthetic interruption after durable invocation start")
                report.update(generation="fixture-generation", answer="synthetic answer", tool_calls=[], citations=[],
                    jev={"required": True, "initial_status": "reranked", "requests": 2, "attempted_calls": 2,
                         "unobserved_attempts": 0, "models": ["typesafe/jev-1.13"], "usage": [{"cost": .01}, {"cost": .01}],
                         "searches": [], "accounting_complete": True})
            elif operation == "host-complete":
                self.counts["judge"] += 1
                if self.interrupt_first_judge and self.counts["judge"] == 1:
                    raise KeyboardInterrupt("synthetic interrupted judge")
                request = system.locks.read_json(Path(arguments[arguments.index("--input") + 1]))
                report["value"] = {"equivalent": "first fixture query" not in request["instructions"], "abstained": False, "reason": "Synthetic control"}
            else:
                raise AssertionError(operation)
        return CompletedProcess(arguments, 0, json.dumps(report).encode(), b"")

    def test_budget_resume_reuses_wrong_judged_answer_and_stable_generation(self):
        self.args.max_model_calls = 3
        first = system.execute(self.args)
        saved = (self.args.run_dir / "calls/00001.response.json").read_bytes()
        generation = (self.args.run_dir / "corpus/.gptgrep/CURRENT.json").read_bytes()
        self.assertEqual(first["summary"]["question_denominator"], 2)
        self.assertFalse(first["cases"][0]["judge"]["equivalent"])
        self.args.max_model_calls = 8
        self.args.retry_failed = True
        second = system.execute(self.args)
        self.assertEqual(self.counts, {"index": 1, "ask": 2, "judge": 2})
        self.assertEqual(second["host_invocations"], 4)
        self.assertEqual(second["summary"]["answer_equivalence_accuracy"], .5)
        self.assertEqual(second["cumulative_attempt_accounting"]["native_ask_attempts"], 2)
        self.assertAlmostEqual(second["summary"]["measured_jev_cost_usd"], .04)
        self.assertEqual((self.args.run_dir / "calls/00001.response.json").read_bytes(), saved)
        self.assertEqual((self.args.run_dir / "corpus/.gptgrep/CURRENT.json").read_bytes(), generation)
        self.assertEqual(system.execute(self.args)["host_invocations"], 4)
        self.assertEqual(self.counts, {"index": 1, "ask": 2, "judge": 2})

    def test_interrupted_ask_is_retained_requires_retry_and_keeps_unknown_cost(self):
        self.interrupt_second_ask = True
        with self.assertRaises(KeyboardInterrupt):
            system.execute(self.args)
        preserved = (self.args.run_dir / "calls/00002.request.json").read_bytes()
        stopped = system.execute(self.args)
        self.assertEqual(stopped["cases"][1]["status"], "interrupted")
        self.assertGreaterEqual(stopped["host_invocations"], 2)
        self.assertEqual(self.counts["ask"], 2)
        self.args.retry_failed = True
        resumed = system.execute(self.args)
        self.assertEqual(resumed["host_invocations"], len((self.args.run_dir / "host-calls.jsonl").read_text().splitlines()))
        self.assertEqual(resumed["summary"]["question_denominator"], 2)
        self.assertEqual(resumed["cumulative_attempt_accounting"]["native_ask_attempts"], 3)
        self.assertEqual(resumed["cumulative_attempt_accounting"]["unknown_accounting_attempts"], 1)
        self.assertIsNone(resumed["summary"]["measured_jev_cost_usd"])
        self.assertAlmostEqual(resumed["summary"]["known_jev_cost_subtotal_usd"], .04)
        self.assertFalse(resumed["cohorts"]["heldout54"]["jev_cost_accounting_complete"])
        self.assertEqual(resumed["all_host_wall_ms_missing"], 0)
        self.assertGreater(resumed["all_host_wall_ms_known_subtotal"], 0)
        self.assertEqual((self.args.run_dir / "calls/00002.request.json").read_bytes(), preserved)
        self.assertEqual(self.counts, {"index": 1, "ask": 3, "judge": 2})

    def test_interruption_before_request_does_not_reserve_a_later_cases_ordinal(self):
        with patch.object(system, "invoke_native", side_effect=KeyboardInterrupt("before request")):
            with self.assertRaises(KeyboardInterrupt):
                system.execute(self.args)
        partial = system.execute(self.args)
        self.assertEqual(partial["host_invocations"], len((self.args.run_dir / "host-calls.jsonl").read_text().splitlines()) if (self.args.run_dir / "host-calls.jsonl").exists() else 0)
        self.assertEqual(partial["cases"][0]["status"], "interrupted")
        self.args.retry_failed = True
        final = system.execute(self.args)
        self.assertEqual(final["host_invocations"], 4)
        self.assertEqual(final["cumulative_attempt_accounting"]["native_ask_attempts"], 2)
        self.assertTrue(final["cumulative_attempt_accounting"]["accounting_complete"])

    def test_interrupted_judge_retry_reuses_the_completed_reader(self):
        self.interrupt_first_judge = True
        with self.assertRaises(KeyboardInterrupt):
            system.execute(self.args)
        first_response = (self.args.run_dir / "calls/00001.response.json").read_bytes()
        partial = system.execute(self.args)
        self.assertEqual(partial["cases"][0]["status"], "completed")
        self.assertEqual(partial["cases"][0]["judge"]["status"], "unavailable")
        self.assertEqual(self.counts["index"], 1)
        self.assertEqual(self.counts["ask"], 2)
        self.args.retry_failed = True
        final = system.execute(self.args)
        self.assertEqual(final["host_invocations"], len((self.args.run_dir / "host-calls.jsonl").read_text().splitlines()))
        self.assertEqual(self.counts, {"index": 1, "ask": 2, "judge": 3})
        self.assertEqual((self.args.run_dir / "calls/00001.response.json").read_bytes(), first_response)
        self.assertFalse(final["cases"][0]["judge"]["equivalent"])

    def crash_after_append(self, phase):
        append = system.LocalCodex._append
        def crash(host, receipt):
            append(host, receipt)
            if receipt.get("phase") == phase and receipt.get("status") == "completed":
                raise KeyboardInterrupt("after durable completion before case checkpoint")
        return patch.object(system.LocalCodex, "_append", new=crash)

    def test_completed_reader_receipt_recovers_without_another_ask(self):
        self.args.max_model_calls = 1
        with self.crash_after_append("answer:native:row-0"):
            with self.assertRaises(KeyboardInterrupt):
                system.execute(self.args)
        raw = (self.args.run_dir / "calls/00001.response.json").read_bytes()
        self.args.retry_failed = True
        report = system.execute(self.args)
        self.assertEqual(self.counts, {"index": 1, "ask": 1, "judge": 0})
        self.assertEqual(report["host_invocations"], 1)
        self.assertTrue(report["cases"][0]["recovered_completed_reader"])
        self.assertEqual(report["cases"][0]["status"], "completed")
        self.assertAlmostEqual(report["cumulative_attempt_accounting"]["known_jev_cost_subtotal_usd"], .02)
        self.assertEqual((self.args.run_dir / "calls/00001.response.json").read_bytes(), raw)

    def test_validating_reader_checkpoint_recovers_without_another_ask(self):
        self.args.max_model_calls = 1
        checkpoint = system.checkpoint_attempt
        def crash(path, value):
            checkpoint(path, value)
            if path.name == "case.json" and value.get("status") == "validating":
                raise KeyboardInterrupt("after validating checkpoint")
        with patch.object(system, "checkpoint_attempt", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                system.execute(self.args)
        self.args.retry_failed = True
        report = system.execute(self.args)
        self.assertEqual(self.counts, {"index": 1, "ask": 1, "judge": 0})
        self.assertEqual(report["host_invocations"], 1)
        self.assertTrue(report["cases"][0]["recovered_completed_reader"])

    def test_completed_false_judge_is_recovered_without_new_calls(self):
        self.args.max_model_calls = 3
        with self.crash_after_append("judge:native:row-0"):
            with self.assertRaises(KeyboardInterrupt):
                system.execute(self.args)
        response = self.args.run_dir / "calls/00003.response.json"
        saved = response.read_bytes()
        self.assertFalse(json.loads(saved)["value"]["equivalent"])
        self.args.retry_failed = True
        report = system.execute(self.args)
        self.assertEqual(self.counts, {"index": 1, "ask": 2, "judge": 1})
        self.assertEqual(report["host_invocations"], 3)
        self.assertFalse(report["cases"][0]["judge"]["equivalent"])
        self.assertTrue(report["cases"][0]["judge"]["recovered_completed_judge"])
        self.assertEqual(response.read_bytes(), saved)
        self.assertEqual(report["all_host_wall_ms_missing"], 0)
        self.assertIsNotNone(report["all_host_wall_ms"])

    def test_completed_judge_request_or_response_tampering_blocks_retry(self):
        self.args.max_model_calls = 3
        with self.crash_after_append("judge:native:row-0"):
            with self.assertRaises(KeyboardInterrupt):
                system.execute(self.args)
        self.args.retry_failed = True
        for kind in ("request", "response"):
            with self.subTest(kind=kind):
                target = self.args.run_dir / f"calls/00003.{kind}.json"
                saved = target.read_bytes()
                target.write_bytes(saved + b" ")
                with self.assertRaisesRegex(ValueError, "binding differs|digest differs"):
                    system.execute(self.args)
                self.assertEqual(self.counts, {"index": 1, "ask": 2, "judge": 1})
                target.write_bytes(saved)

    def test_resume_refuses_replaced_generation_even_with_retry(self):
        system.execute(self.args)
        pointer = self.args.run_dir / "corpus/.gptgrep/CURRENT.json"
        value = system.locks.read_json(pointer)
        pointer.write_text(json.dumps({**value, "generation": "changed-generation"}))
        self.args.retry_failed = True
        with self.assertRaisesRegex(ValueError, "generation changed"):
            system.execute(self.args)
        self.assertEqual(self.counts, {"index": 1, "ask": 2, "judge": 2})

    def test_reader_and_judge_forward_fast_and_retain_priority_acknowledgement(self):
        report = system.execute(self.args)
        for arguments in self.seen_arguments:
            if arguments[1] in ("ask", "host-complete"):
                self.assertEqual(arguments[arguments.index("--service-tier") + 1], "fast")
        self.assertEqual(report["service_tier"], "fast")
        for case in report["cases"]:
            self.assertEqual(case["host_receipt"]["requested_service_tier"], "fast")
            self.assertEqual(case["metrics"]["effective_service_tier"], "priority")
            self.assertEqual(case["judge"]["requested_service_tier"], "fast")
            self.assertEqual(case["judge"]["effective_service_tier"], "priority")
        system.execute(self.args)
        self.assertEqual(self.counts, {"index": 1, "ask": 2, "judge": 2})

    def test_service_tier_change_cannot_reuse_a_run(self):
        system.execute(self.args)
        self.args.service_tier = "flex"
        with self.assertRaisesRegex(ValueError, "conditions changed"):
            system.execute(self.args)
        self.assertEqual(self.counts, {"index": 1, "ask": 2, "judge": 2})

    def test_parallel_stages_overlap_with_exact_case_bindings_and_reuse(self):
        self.args.reader_concurrency = self.args.judge_concurrency = 5
        self.model_delay = .04
        report = system.execute(self.args)
        measured = report["owned_process_concurrency"]
        self.assertGreater(measured["by_role"]["chat"]["measured_peak"], 1)
        self.assertGreater(measured["by_role"]["judge"]["measured_peak"], 1)
        self.assertLessEqual(measured["measured_peak"], 5)
        calls = [json.loads(line) for line in (self.args.run_dir / "host-calls.jsonl").read_text().splitlines()]
        readers = [call for call in calls if call["role"] == "chat"]
        judges = [call for call in calls if call["role"] == "judge"]
        self.assertLessEqual(max(call["process_finished_monotonic_ns"] for call in readers),
                             min(call["process_started_monotonic_ns"] for call in judges))
        self.assertEqual(sorted(call["ordinal"] for call in calls), [1, 2, 3, 4])
        for case in report["cases"]:
            self.assertEqual(case["host_receipt"]["phase"], f"answer:native:row-{case['source_row']}")
            verdict = case["judge"]
            call = next(call for call in calls if call["ordinal"] == verdict["host_ordinal"])
            self.assertEqual(call["phase"], f"judge:native:row-{case['source_row']}")
        again = system.execute(self.args)
        self.assertEqual(again["host_invocations"], 4)
        self.assertEqual(self.counts, {"index": 1, "ask": 2, "judge": 2})
        latest = system.locks.read_json(self.args.run_dir / "stage-readers.json")
        self.assertEqual(latest["reused_completed_tasks"], 2)
        self.assertTrue(again["stage_execution_history"]["latest_invocation_is_not_automatically_cold"])

    def test_parallel_inflight_reservations_obey_global_cap(self):
        self.args.reader_concurrency = self.args.judge_concurrency = 5
        self.args.max_model_calls = 1
        self.model_delay = .04
        report = system.execute(self.args)
        self.assertEqual(report["host_invocations"], 1)
        self.assertEqual(self.counts, {"index": 1, "ask": 1, "judge": 0})
        self.assertEqual(report["summary"]["question_denominator"], 2)
        self.assertEqual(sorted(case["status"] for case in report["cases"]), ["budget_blocked", "completed"])


if __name__ == "__main__":
    unittest.main()
