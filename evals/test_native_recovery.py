"""Synthetic sidecar tests: no benchmark inputs, native processes, or model calls."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("synthetic_native_recovery", ROOT / "scripts/pageindex_baseline/native_recovery.py")
recovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(recovery.encoded(value) + b"\n")


def append(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(recovery.encoded(value) + b"\n")


def payload(number):
    return {"operation": "ask", "phase": f"answer:native:row-{number}",
            "question": "Which invented shelf holds the copper bead?", "document": "invented.txt",
            "model": "synthetic-reader", "reasoning_effort": "max", "service_tier": "fast",
            "experimental_query_plan": True, "experimental_evidence_roles": True, "max_input_bytes": 1024}


def native_call(run, corpus, number, request, state, callback=None):
    base = run / "calls" / f"{number:05d}"
    put(Path(str(base) + ".request.json"), request)
    receipt = {"ordinal": number, "operation": "native_ask", "phase": request["phase"], "role": "chat",
               "request_sha256": recovery.digest(Path(str(base) + ".request.json"))}
    put(Path(str(base) + ".attempt.json"), receipt)
    if callback:
        callback(receipt)
    models = [{"role": "query_planner", "status": "completed"}]
    if state != "pre_reader_failure":
        models.append({"role": "final_reader", "status": "completed" if state == "success" else "failed"})
    ledger = corpus / ".gptgrep/host-attempts" / f"attempt-{len(list((corpus / '.gptgrep/host-attempts').glob('*.jsonl'))) + 1}-{1000 + number}-0.jsonl"
    workflow = {"generation": "g-synthetic", "query_sha256": recovery.hashlib.sha256(request["question"].encode()).hexdigest(), "document_scope": request["document"]}
    append(ledger, {"schema_version": "gptgrep.jev-attempt.v1", "event": "workflow_bound",
                    "generation": "g-synthetic", "workflow": workflow, "model_attempts": []})
    append(ledger, {"schema_version": "gptgrep.jev-attempt.v1", "event": "completed" if state == "success" else "failed",
                    "generation": "g-synthetic", "workflow": workflow, "model_attempts": models})
    if state == "success":
        report = {"status": "completed", "generation": "g-synthetic", "answer": "An invented answer.",
                  "model_attempts": models, "ledger_path": str(ledger)}
        receipt["status"] = "completed"
    else:
        stage = "initial_search" if state == "pre_reader_failure" else "citation_validation"
        code = "host_jev_search_failed" if state == "pre_reader_failure" else "host_citation_validation_failed"
        report = {"code": code, "host_retrieval": {"code": code, "stage": stage,
                  "generation": "g-synthetic", "model_attempts": models, "ledger_path": str(ledger)}}
        receipt["status"] = "failed"
    put(Path(str(base) + ".response.json"), report)
    receipt["response_sha256"] = recovery.digest(Path(str(base) + ".response.json"))
    append(run / "host-calls.jsonl", receipt)
    return report, receipt


class Fixture:
    def __init__(self, root, cap=12):
        root = root.resolve()
        self.root = root
        self.origin = root / "synthetic-run"
        self.seal = root / "synthetic-seal"
        self.origin.mkdir()
        (self.origin / ".owner.lock").touch()
        corpus = self.origin / "corpus"
        corpus.mkdir()
        (corpus / "invented.txt").write_text("Copper beads sit on the blue shelf.\n")
        (corpus / ".gptgrep/host-attempts").mkdir(parents=True)
        generation_file = corpus / ".gptgrep/generations/g-synthetic/manifest.json"
        text = generation_file.parent / "text" / ("a" * 24 + ".txt")
        text.parent.mkdir(parents=True)
        text.write_bytes((corpus / "invented.txt").read_bytes())
        put(generation_file, {"documents": [{"path": "invented.txt", "id": "a" * 24,
            "source_sha256": recovery.digest(corpus / "invented.txt"), "text_sha256": recovery.digest(text)}]})
        generation = {"generation": "g-synthetic", "manifest_sha256": recovery.digest(generation_file)}
        put(corpus / ".gptgrep/CURRENT.json", generation)
        put(self.origin / "build.json", {"status": "completed", "generation_binding": generation})
        runner = self.seal / "runner/scripts/gptgrep_system_eval.py"
        runner.parent.mkdir(parents=True)
        runner.write_text("# frozen synthetic runner\n")
        (self.seal / "gptgrep").write_text("synthetic binary, never executed\n")
        self.judge_binary = root / "synthetic-judge"
        self.judge_binary.write_text("synthetic judge, never executed\n")
        self.upstream, self.benchmark, self.judge_source = (root / name for name in ("upstream", "benchmark", "judge"))
        for directory in (self.upstream, self.benchmark, self.judge_source):
            directory.mkdir()
            (directory / "bound.txt").write_text("synthetic locked input\n")
        lock = {label: {"files": {"bound.txt": recovery.digest(directory / "bound.txt")}} for label, directory in
                (("pageindex", self.upstream), ("benchmark", self.benchmark), ("judge", self.judge_source))}
        put(self.seal / "runner/scripts/pageindex_baseline/sources.lock.json", lock)
        source = {"source_revision": "synthetic-revision", "binary_sha256": recovery.digest(self.seal / "gptgrep"),
                  "frozen_runner_files": {name: info["sha256"] for name, info in recovery.inventory(self.seal / "runner").items()}}
        put(self.seal / "source.json", source)
        self.declaration = root / "declaration.json"
        put(self.declaration, {**source, "source_seal_sha256": recovery.digest(self.seal / "source.json"),
            "judge_binary_sha256": recovery.digest(self.judge_binary),
            "budgets": {"max_outer_invocations_per_run": cap, "technical_recovery_rounds_max_per_run": 2}})
        put(self.origin / "manifest.json", {**source, "judge_binary_sha256": recovery.digest(self.judge_binary),
            "runner_sha256": recovery.digest(runner), "adapter_files": {}, "max_host_invocations": cap,
            "source_hashes": {"invented.txt": recovery.digest(corpus / "invented.txt")},
            "question_count": 4, "source_rows": [0, 1, 2, 3]})
        for number, state in [(1, "pre_reader_failure"), (2, "post_reader_failure"), (3, "success"), (4, "success")]:
            native_call(self.origin, corpus, number, payload(number - 1), state)
            put(self.origin / "cases" / f"{number-1:03d}" / "case.json",
                {"status": "completed" if state == "success" else "failed", "judge": {"equivalent": False} if state == "success" else {}})
        put(self.origin / "summary.json", {"synthetic": True, "outcomes_retained": 4})
        self.inputs = recovery.Inputs(self.origin, self.seal, self.declaration, self.judge_binary,
                                      self.judge_source, self.benchmark, self.upstream)
        self.before = recovery.inventory(self.origin)

    def plan(self):
        return recovery.prepare_plan(self.inputs)

    def unchanged(self, test):
        current = recovery.inventory(self.origin)
        for name, expected in self.before.items():
            test.assertEqual(current[name], expected, name)
        test.assertTrue(all(Path(name).parent == Path("corpus/.gptgrep/host-attempts") for name in set(current) - set(self.before)))


class FakeHost:
    def __init__(self, binding, sidecar):
        self.binding, self.run_dir = binding, sidecar
        (sidecar / "calls").mkdir(exist_ok=True)
        self.closed = False

    def number(self):
        number = len(recovery.reservations(self.run_dir)[0]) + 1
        recovery.require(number + self.binding["origin_reservations"] <= self.binding["max_outer_invocations"], "synthetic_call_budget")
        return number

    def call_by_ordinal(self, number):
        return recovery.reservations(self.run_dir)[1].get(number)

    def close(self, cancel=False):
        self.closed = True


class FakeRuntime:
    def __init__(self, state="success", verdict=False, crash_after_reader=False, invalid_reader=False, judge_fail=False):
        self.state, self.verdict = state, verdict
        self.crash_after_reader, self.invalid_reader, self.judge_fail = crash_after_reader, invalid_reader, judge_fail
        self.ask_calls = self.judge_calls = 0

    def __call__(self, binding):
        self.binding = binding
        return self

    def host(self, sidecar):
        self.shared = FakeHost(self.binding, sidecar)
        return self.shared

    def ask(self, shared, request, on_reserved):
        self.ask_calls += 1
        result = native_call(shared.run_dir, Path(self.binding["paths"]["origin"]) / "corpus",
                             shared.number(), request, self.state, on_reserved)
        if self.crash_after_reader:
            self.crash_after_reader = False
            raise RuntimeError("synthetic crash after durable completed reader")
        return result

    def completed_reader(self, shared, number, request):
        if self.invalid_reader:
            raise ValueError("synthetic citation validation failure")
        receipt = shared.call_by_ordinal(number)
        if receipt["status"] != "completed":
            return None
        return recovery.read_json(shared.run_dir / "calls" / f"{number:05d}.response.json")

    def native_ledgers(self, shared, lineage, request):
        report = recovery.read_json(shared.run_dir / "calls" / f"{lineage['sidecar_ordinal']:05d}.response.json")
        path = Path(report.get("ledger_path", report.get("host_retrieval", {}).get("ledger_path")))
        return [{"origin_relative_path": str(path.relative_to(self.binding["paths"]["origin"])),
                 "sha256": recovery.digest(path), "bytes": path.stat().st_size, "accounting": {"case_identity_verified": True}}]

    def judge(self, shared, request, report, on_reserved, prior_ordinal=None):
        if prior_ordinal is not None:
            return {"status": "completed", "equivalent": self.verdict}
        self.judge_calls += 1
        number = shared.number()
        put(shared.run_dir / "calls" / f"{number:05d}.request.json", {"synthetic_judge": True})
        receipt = {"ordinal": number, "role": "judge", "status": "failed" if self.judge_fail else "completed"}
        put(shared.run_dir / "calls" / f"{number:05d}.attempt.json", receipt)
        on_reserved(receipt)
        append(shared.run_dir / "host-calls.jsonl", receipt)
        if self.judge_fail:
            raise RuntimeError("synthetic judge transport failed")
        return {"status": "completed", "equivalent": self.verdict}


class NativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Fixture(Path(self.temp.name))

    def run_plan(self, plan, runtime):
        return recovery.run_plan(plan["plan"], plan["sha256"], runtime_factory=runtime)

    def test_plan_is_automatic_metadata_only_and_origin_read_only(self):
        plan = self.fixture.plan()
        self.assertEqual(plan["eligible_origin_ordinals"], [1])
        self.assertEqual(plan["new_calls"], 0)
        self.assertEqual(recovery.reservations(recovery.sidecar_path(self.fixture.origin))[0], set())
        self.assertEqual(recovery.inventory(self.fixture.origin), self.fixture.before)
        binding = recovery.read_json(recovery.sidecar_path(self.fixture.origin) / "manifest.json", recovery.MAX_LEDGER)
        reasons = {item["ordinal"]: item["reason"] for item in binding["initial_excluded"]}
        self.assertEqual(reasons[2], "final_reader_already_attempted")
        self.assertEqual(reasons[3], "final_reader_already_attempted")
        metadata = binding["initial_eligible"][0]
        for extra in ({"question": "changed", "document": "renamed"}, {"gold": True, "equivalent": False}):
            self.assertEqual(recovery.technical_eligibility(metadata), recovery.technical_eligibility({**metadata, **extra}))
        for change in ({"bound_terminal_ledger": False}, {"reader_ever_reserved": None}, {"failure_stage": "citation_validation"}, {"failure_code": "host_invalid_output"}):
            self.assertFalse(recovery.technical_eligibility({**metadata, **change})[0])

    def test_valid_wrong_outcome_is_kept_without_best_of_or_origin_rewrite(self):
        plan = self.fixture.plan()
        runtime = FakeRuntime(verdict=False)
        result = self.run_plan(plan, runtime)
        self.assertEqual((runtime.ask_calls, runtime.judge_calls), (1, 1))
        self.assertEqual(result["combined_reservations"], 6)
        self.assertEqual(self.run_plan(plan, runtime), result)
        self.assertEqual((runtime.ask_calls, runtime.judge_calls), (1, 1))
        view = recovery.analyze(self.fixture.origin)
        self.assertEqual(view["question_denominator"], 4)
        self.assertIs(view["recovery_lineages"][0]["equivalent"], False)
        self.assertFalse(view["comparison_eligible"])
        with self.assertRaisesRegex(recovery.RecoveryError, "no_eligible"):
            self.fixture.plan()
        self.fixture.unchanged(self)

    def test_post_reader_failure_and_judge_failure_never_admit_another_reader(self):
        plan = self.fixture.plan()
        runtime = FakeRuntime(state="post_reader_failure")
        self.run_plan(plan, runtime)
        self.assertEqual((runtime.ask_calls, runtime.judge_calls), (1, 0))
        with self.assertRaisesRegex(recovery.RecoveryError, "no_eligible"):
            self.fixture.plan()
        view = recovery.analyze(self.fixture.origin)
        self.assertIsNone(view["recovery_lineages"][0]["equivalent"])
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other))
            plan = fixture.plan()
            runtime = FakeRuntime(judge_fail=True)
            self.run_plan(plan, runtime)
            self.assertEqual((runtime.ask_calls, runtime.judge_calls), (1, 1))
            self.assertIsNone(recovery.analyze(fixture.origin)["recovery_lineages"][0]["equivalent"])
            with self.assertRaisesRegex(recovery.RecoveryError, "no_eligible"):
                fixture.plan()

    def test_two_round_cap_and_combined_reservations_include_failed_asks(self):
        first = self.fixture.plan()
        runtime = FakeRuntime(state="pre_reader_failure")
        self.run_plan(first, runtime)
        second = self.fixture.plan()
        self.assertEqual(recovery.read_json(second["plan"])["round"], 2)
        self.run_plan(second, runtime)
        self.assertEqual(runtime.ask_calls, 2)
        self.assertEqual(recovery.analyze(self.fixture.origin)["combined_reservations"], 6)
        with self.assertRaisesRegex(recovery.RecoveryError, "round_cap"):
            self.fixture.plan()
        self.fixture.unchanged(self)

    def test_second_round_success_retains_first_failure_and_counts_once(self):
        first = self.fixture.plan()
        self.run_plan(first, FakeRuntime(state="pre_reader_failure"))
        second = self.fixture.plan()
        self.run_plan(second, FakeRuntime(verdict=True))
        view = recovery.analyze(self.fixture.origin)
        self.assertEqual(view["combined_reservations"], 7)
        self.assertTrue(view["recovery_lineages"][0]["equivalent"])
        self.assertEqual(len(view["recovery_lineages"][0]["all_attempts"]), 2)

    def test_crash_after_completed_reader_resumes_judge_without_new_ask(self):
        plan = self.fixture.plan()
        runtime = FakeRuntime(crash_after_reader=True)
        with self.assertRaises(RuntimeError):
            self.run_plan(plan, runtime)
        self.assertEqual(runtime.ask_calls, 1)
        self.run_plan(plan, runtime)
        self.assertEqual((runtime.ask_calls, runtime.judge_calls), (1, 1))
        self.fixture.unchanged(self)

    def test_exact_cap_never_resets_and_missing_judge_stays_unavailable(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), cap=5)
            plan = fixture.plan()
            runtime = FakeRuntime()
            self.run_plan(plan, runtime)
            view = recovery.analyze(fixture.origin)
            self.assertEqual(view["combined_reservations"], 5)
            self.assertEqual(len(recovery.reservations(recovery.sidecar_path(fixture.origin))[0]), 1)
            self.assertIsNone(view["recovery_lineages"][0]["equivalent"])
            self.assertEqual(view["recovery_lineages"][0]["judge_status"], "unavailable")

    def test_pending_reservation_consumes_cap_and_missing_lineage_blocks_replay(self):
        plan = self.fixture.plan()
        sidecar = recovery.sidecar_path(self.fixture.origin)
        put(sidecar / "calls/00001.request.json", {"synthetic": True})
        put(sidecar / "calls/00001.attempt.json", {"ordinal": 1})
        self.assertEqual(len(recovery.reservations(sidecar)[0]), 1)
        runtime = FakeRuntime()
        with self.assertRaisesRegex(recovery.RecoveryError, "lineage_missing"):
            self.run_plan(plan, runtime)
        self.assertEqual(runtime.ask_calls, 0)

    def test_plan_hash_origin_and_seal_mutation_fail_before_calls(self):
        plan = self.fixture.plan()
        runtime = FakeRuntime()
        with self.assertRaisesRegex(recovery.RecoveryError, "plan_digest"):
            recovery.run_plan(plan["plan"], "f" * 64, runtime_factory=runtime)
        source = self.fixture.origin / "corpus/invented.txt"
        source.write_text("modified")
        with self.assertRaisesRegex(recovery.RecoveryError, "origin_file_changed"):
            self.run_plan(plan, runtime)
        self.assertEqual(runtime.ask_calls, 0)

    def test_changed_binary_generation_and_adopted_ledger_are_rejected(self):
        for target in ("binary", "generation", "adopted_ledger", "retained_response"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                plan = fixture.plan()
                runtime = FakeRuntime()
                if target in ("adopted_ledger", "retained_response"):
                    self.run_plan(plan, runtime)
                if target == "binary":
                    path = fixture.seal / "gptgrep"
                elif target == "generation":
                    path = fixture.origin / "corpus/.gptgrep/CURRENT.json"
                elif target == "adopted_ledger":
                    path = sorted((fixture.origin / "corpus/.gptgrep/host-attempts").glob("*.jsonl"))[-1]
                else:
                    path = recovery.sidecar_path(fixture.origin) / "calls/00001.response.json"
                with path.open("ab") as output:
                    output.write(b" ")
                before = runtime.ask_calls
                with self.assertRaises(recovery.RecoveryError):
                    self.run_plan(plan, runtime)
                self.assertEqual(runtime.ask_calls, before)

    def test_sidecar_symlink_cannot_redirect_writes_into_origin(self):
        plan = self.fixture.plan()
        sidecar = recovery.sidecar_path(self.fixture.origin)
        (sidecar / "calls").symlink_to(self.fixture.origin / "calls", target_is_directory=True)
        with self.assertRaisesRegex(recovery.RecoveryError, "redirected_sidecar"):
            self.run_plan(plan, FakeRuntime())
        self.assertEqual(recovery.inventory(self.fixture.origin), self.fixture.before)

    def test_frozen_argument_adapter_preserves_original_input_without_gold(self):
        runtime = recovery.FrozenRuntime.__new__(recovery.FrozenRuntime)
        runtime.manifest = {"source_rows": [0]}
        runtime.args = object()
        request = payload(0)
        runtime.runner = type("Runner", (), {"reader_payload": staticmethod(lambda row, args: {
            **request, "question": row["question"], "document": row["doc_id"]})})()
        row = runtime.row(request)
        self.assertEqual(set(row), {"source_row", "question", "doc_id"})
        self.assertEqual(row["question"], request["question"])
        with self.assertRaisesRegex(recovery.RecoveryError, "inference_arguments_changed"):
            runtime.row({**request, "model": "different-model"})

    def test_unregistered_origin_addition_and_changed_old_ledger_are_blocking(self):
        plan = self.fixture.plan()
        unexpected = self.fixture.origin / "unexpected.json"
        put(unexpected, {})
        runtime = FakeRuntime()
        with self.assertRaisesRegex(recovery.RecoveryError, "undeclared_origin_addition"):
            self.run_plan(plan, runtime)
        unexpected.unlink()
        old = next((self.fixture.origin / "corpus/.gptgrep/host-attempts").glob("*.jsonl"))
        with old.open("ab") as stream:
            stream.write(b"{}\n")
        with self.assertRaisesRegex(recovery.RecoveryError, "origin_file_changed"):
            self.run_plan(plan, runtime)
        self.assertEqual(runtime.ask_calls, 0)

    def test_retained_outcome_tamper_and_duplicate_owner_lock_are_blocking(self):
        plan = self.fixture.plan()
        with recovery.locked(self.fixture.origin):
            with self.assertRaises(BlockingIOError):
                self.run_plan(plan, FakeRuntime())
        self.run_plan(plan, FakeRuntime())
        outcome = next((recovery.sidecar_path(self.fixture.origin) / "rounds/0001/outcomes").glob("*.json"))
        data = recovery.read_json(outcome)
        data["equivalent"] = True
        put(outcome, data)
        with self.assertRaisesRegex(recovery.RecoveryError, "retained_recovery_artifact_changed"):
            recovery.analyze(self.fixture.origin)

    def test_outcome_creation_publication_crash_gap_fails_closed_even_without_tampering(self):
        for tampered in (False, True):
            with self.subTest(tampered=tampered), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                plan = fixture.plan()
                runtime = FakeRuntime(verdict=False)
                original_append = recovery.append_event

                def crash_before_publication(sidecar, event, **data):
                    if event == "outcome_retained":
                        raise RuntimeError("synthetic crash before outcome publication")
                    return original_append(sidecar, event, **data)

                with patch.object(recovery, "append_event", side_effect=crash_before_publication):
                    with self.assertRaisesRegex(RuntimeError, "before outcome publication"):
                        self.run_plan(plan, runtime)
                sidecar = recovery.sidecar_path(fixture.origin)
                outcome = next((sidecar / "rounds/0001/outcomes").glob("*.json"))
                value = recovery.read_json(outcome)
                self.assertIs(value["equivalent"], False)
                self.assertFalse(any(event["event"] == "outcome_retained" for event in recovery.read_events(sidecar / "events.jsonl")))
                if tampered:
                    value["equivalent"] = value["judge"]["equivalent"] = True
                    put(outcome, value)
                for operation in (lambda: self.run_plan(plan, runtime), lambda: recovery.analyze(fixture.origin), fixture.plan):
                    with self.assertRaisesRegex(recovery.RecoveryError, "outcome_not_journalled"):
                        operation()
                self.assertEqual((runtime.ask_calls, runtime.judge_calls), (1, 1))
                self.assertFalse((sidecar / "rounds/0001/result.json").exists())
                fixture.unchanged(self)

    def test_every_sidecar_semantic_creation_requires_its_publication_event(self):
        stages = {"bound": "manifest", "round_planned": "plan", "call_reserved": "lineage",
                  "native_ledgers_bound": "native_ledger", "round_terminal": "result"}
        for event_name, kind in stages.items():
            with self.subTest(event=event_name), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                runtime = FakeRuntime(verdict=False)
                original_append = recovery.append_event

                def stop_before_publication(sidecar, event, **data):
                    if event == event_name:
                        raise RuntimeError("synthetic semantic publication crash")
                    return original_append(sidecar, event, **data)

                if event_name in ("bound", "round_planned"):
                    with patch.object(recovery, "append_event", side_effect=stop_before_publication):
                        with self.assertRaisesRegex(RuntimeError, "publication crash"):
                            fixture.plan()
                else:
                    plan = fixture.plan()
                    with patch.object(recovery, "append_event", side_effect=stop_before_publication):
                        with self.assertRaisesRegex(RuntimeError, "publication crash"):
                            self.run_plan(plan, runtime)
                sidecar = recovery.sidecar_path(fixture.origin)
                prior_calls = (runtime.ask_calls, runtime.judge_calls)
                operations = [fixture.plan, lambda: recovery.analyze(fixture.origin)]
                plan_path = sidecar / "rounds/0001/plan.json"
                if plan_path.exists():
                    operations.append(lambda: recovery.run_plan(plan_path, recovery.digest(plan_path), runtime_factory=runtime))
                for operation in operations:
                    with self.assertRaisesRegex(recovery.RecoveryError, kind + "_not_journalled"):
                        operation()
                self.assertEqual((runtime.ask_calls, runtime.judge_calls), prior_calls)
                fixture.unchanged(self)

    def test_orphan_result_cannot_remove_a_tampered_response_from_retained_inventory(self):
        plan = self.fixture.plan()
        runtime = FakeRuntime(verdict=False)
        original_append = recovery.append_event

        def stop_before_round_publication(sidecar, event, **data):
            if event == "round_terminal":
                raise RuntimeError("synthetic round publication crash")
            return original_append(sidecar, event, **data)

        with patch.object(recovery, "append_event", side_effect=stop_before_round_publication):
            with self.assertRaisesRegex(RuntimeError, "round publication crash"):
                self.run_plan(plan, runtime)
        sidecar = recovery.sidecar_path(self.fixture.origin)
        result_path = sidecar / "rounds/0001/result.json"
        result = recovery.read_json(result_path)
        self.assertIn("calls/00001.response.json", result["retained_sidecar_files"])
        put(sidecar / "calls/00001.response.json", {"status": "completed", "answer": "tampered synthetic answer"})
        del result["retained_sidecar_files"]["calls/00001.response.json"]
        put(result_path, result)
        for operation in (lambda: self.run_plan(plan, runtime), lambda: recovery.analyze(self.fixture.origin), self.fixture.plan):
            with self.assertRaisesRegex(recovery.RecoveryError, "result_not_journalled"):
                operation()
        self.assertEqual((runtime.ask_calls, runtime.judge_calls), (1, 1))
        self.fixture.unchanged(self)

    def test_missing_or_torn_failure_ledger_never_proves_reader_absence(self):
        ledger = next((self.fixture.origin / "corpus/.gptgrep/host-attempts").glob("attempt-1-*.jsonl"))
        with ledger.open("ab") as stream:
            stream.write(b'{"torn"')
        with self.assertRaises(ValueError):
            self.fixture.plan()
        self.assertFalse((recovery.sidecar_path(self.fixture.origin) / "manifest.json").exists())

    def test_front_door_requires_explicit_execute_and_exposes_no_case_selector(self):
        script = ROOT / "scripts/gptgrep_native_recovery.py"
        result = subprocess.run([sys.executable, "-B", str(script), "run", "--plan", "missing", "--plan-sha256", "none"], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"--execute", result.stderr)
        result = subprocess.run([sys.executable, "-B", str(script), "plan", "--help"], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(b"--ordinal", result.stdout)
        self.assertNotIn(b"--retry-failed", result.stdout)


if __name__ == "__main__":
    unittest.main()
