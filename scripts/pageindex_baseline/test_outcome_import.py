"""Synthetic, offline outcome retention and missing-case recovery regressions."""
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"

from capability import answer_evidence, index_context_observation
from index_admission import canonical, digest_bytes
from index_import import IndexOrigin, origin_declaration
from outcome_import import CompletedOutcomes, INFERENCE_FILES, OriginLedger, outcome_declaration
from run import benchmark_qualification, checkpoint_attempt, execute, fingerprint, judge_case
import test_index_import as index_fixtures
from test_qa_concurrency import CONSTANTS
from transports import decision_request, response_body


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b"\n")


def invocation(root, calls, phase, role, profile, request, value):
    number = len(calls) + 1
    report = {"status": "completed", "model": profile["model"], "requested_reasoning_effort": profile["reasoning_effort"],
              "effective_reasoning_effort": profile["reasoning_effort"], "requested_service_tier": "fast", "effective_service_tier": "priority",
              "auth_mode": "chatgpt", "model_provider": "openai", "thread_id": f"synthetic-thread-{number}",
              "turn_id": "synthetic-turn", "value": value, "usage": {"total_tokens": 11}}
    raw, response = canonical(request), canonical(report)
    call = {"ordinal": number, "phase": phase, "role": role, "status": "completed", "requested_model": profile["model"],
            "requested_effort": profile["reasoning_effort"], "requested_service_tier": "fast", "request_sha256": digest_bytes(raw),
            "instructions_sha256": digest_bytes(request["instructions"].encode()), "state_sha256": fingerprint(request["state"]),
            "schema_sha256": fingerprint(request["schema"]), "input_bytes": len(raw), "response_sha256": digest_bytes(response),
            "value_sha256": fingerprint(value), "thread_id": report["thread_id"], "turn_id": report["turn_id"],
            "usage": report["usage"], "elapsed_ms": 10}
    (root / "calls" / f"{number:05d}.request.json").write_bytes(raw)
    (root / "calls" / f"{number:05d}.response.json").write_bytes(response)
    write(root / "calls" / f"{number:05d}.attempt.json", call)
    calls.append(call)
    return call, report


class OutcomeImportTests(unittest.TestCase):
    def fixture(self, root, total=62, completed=56):
        args, runtime, consumer, modules, index_path = index_fixtures.IndexImportTests().fixture(root)
        plan = json.loads((args.run_dir / "plan.json").read_bytes())
        for name in (*INFERENCE_FILES, "run.py"):
            raw = (Path(__file__).parent / name).read_bytes()
            (args.frozen_adapter_dir / name).write_bytes(raw)
            plan["adapter_files"][name] = digest_bytes(raw)
        plan["profile"]["roles"].update(chat={"model": "synthetic-reader", "reasoning_effort": "max", "service_tier": "fast"},
                                          judge={"model": "synthetic-judge", "reasoning_effort": "high", "service_tier": "fast"})
        plan.update(adapter_mode="original SDK tools and executor; Codex schema-JSON decisions via injected Responses transport",
                    model="synthetic-reader", reasoning_effort="max", question_count=total,
                    source_rows=list(range(total)), python=platform.python_version(), known_document_scope=True,
                    requested_service_tier="fast", qualification_policy="actual original SDK page roundtrip within this live benchmark; no synthetic prerequisite",
                    turn_budget_unit="Original Agents SDK model-loop turns; not native tool-call count",
                    upstream_defaults={"benchmark_max_turns_argument": None, "pinned_sdk_effective_max_turns": 10,
                                       "benchmark_concurrency": 5, "index_summary_concurrency": 64, "index_expansion_concurrency": 32})
        rows = [{"source_row": number, "doc_id": "source.pdf", "question": f"synthetic question {number}", "answer": "synthetic gold",
                 "answer_format": "text", "evidence_pages": "[1]"} for number in range(total)]
        plan["question_sha256"] = fingerprint(rows)
        write(args.benchmark / "questions.json", [{key: value for key, value in row.items() if key != "source_row"} for row in rows])
        write(args.run_dir / "plan.json", plan)
        runtime.index_origin_plan_sha256 = locks_digest(args.run_dir / "plan.json")
        runtime.carry_completed_outcomes, runtime.judge_source = True, args.judge_source
        consumer = copy.deepcopy(plan)
        consumer["host_input_cap"] = 1048576
        consumer["adapter_files"]["outcome_import.py"] = locks_digest(Path(__file__).parent / "outcome_import.py")
        consumer["index_origin"] = origin_declaration(runtime, consumer)
        consumer["completed_outcome_origin"] = outcome_declaration(runtime, consumer)
        index = json.loads(index_path.read_bytes())
        cache = {"source": index["source_sha256"], "variant": "full", "upstream": plan["source_and_dependencies"]["pageindex_revision"],
                 "model": plan["model"], "effort": plan["reasoning_effort"], "input_cap": plan["host_input_cap"],
                 "adapter": plan["adapter_files"], "dependencies": plan["source_and_dependencies"]["dependencies"],
                 "all_role_profiles": plan["profile"], "host_concurrency_by_role": plan["host_concurrency_by_role"],
                 "host_binary_sha256": plan["host_binary_sha256"]}
        index["cache_key"] = fingerprint(cache)
        checkpoint_attempt(index_path, index)
        consumer_dir = root / "consumer"
        consumer_dir.mkdir()
        tool = SimpleNamespace(name="get_page_content", description="Read source pages", strict_json_schema=True,
                               params_json_schema={"type": "object", "properties": {"doc_name": {"type": "string"}, "pages": {"type": "string"}},
                                                   "required": ["doc_name", "pages"], "additionalProperties": False})
        tools = [{"type": "function", "name": tool.name, "description": tool.description, "parameters": tool.params_json_schema, "strict": True}]
        managed = "Synthetic original SDK instructions"
        scope = lambda _client, doc_id: "Synthetic document " + doc_id
        cache_key = lambda model, instructions, doc_id, items: "pageindex-" + hashlib.sha256(json.dumps([model, instructions, [doc_id], items[0]], sort_keys=True).encode()).hexdigest()[:16]
        for name in ("pageindex.agent_tools", "pageindex.integrations", "pageindex.integrations.openai_agents", "pageindex.local_chat"):
            modules[name] = ModuleType(name)
        modules["pageindex.agent_tools"].targeting_block = scope
        modules["pageindex.integrations.openai_agents"].build_openai_tools = lambda _client, doc_ids: [tool]
        modules["pageindex.local_chat"]._managed_instructions = lambda _client, extras: managed
        modules["pageindex.local_chat"]._conversation_cache_key = cache_key
        calls = [json.loads(line) for line in (args.run_dir / "host-calls.jsonl").read_bytes().splitlines()]
        for row in rows[:completed]:
            number = row["source_row"]
            phase = f"answer:full:row-{number}:attempt-synthetic"
            initial = [{"role": "user", "content": scope(None, index["doc_id"])}, {"role": "user", "content": row["question"]}]
            transcript, wires, numbers = [], [], []
            decisions = []
            if number == 0:
                decisions.append({"text": "", "tool_calls": [{"name": "get_page_content", "arguments": json.dumps({"doc_name": "source.pdf", "pages": "1"})}]})
            decisions.append({"text": "Synthetic abstention" if number % 2 else "Synthetic response", "tool_calls": []})
            for value in decisions:
                body = {"include": [], "input": copy.deepcopy(initial + transcript), "instructions": managed, "model": "synthetic-reader",
                        "tools": tools, "reasoning": {"effort": "max"}, "prompt_cache_key": cache_key("synthetic-reader", managed, index["doc_id"], initial[1:])}
                instruction, state, schema, provenance = decision_request(body)
                call, report = invocation(args.run_dir, calls, phase, "chat", plan["profile"]["roles"]["chat"],
                                          {"instructions": instruction, "state": state, "schema": schema}, value)
                segment = response_body(body, value, "synthetic-reader")["output"]
                wires.append({"phase": phase, "status": "completed", "requested_service_tier": "fast", "effective_service_tier": "priority",
                              "request_sha256": fingerprint(body), "request_bytes": len(canonical(body)), "instructions_sha256": fingerprint(managed),
                              "input_sha256": fingerprint(body["input"]), "tool_schemas_sha256": fingerprint(tools), "output_sha256": fingerprint(segment),
                              "assistant_tool_calls": len(value["tool_calls"]), "index_context": index_context_observation(body["input"]), **provenance})
                numbers.append(call["ordinal"])
                transcript.extend(segment)
                for item in segment:
                    if item["type"] == "function_call":
                        transcript.append({"type": "function_call_output", "call_id": item["call_id"], "output": [{"type": "input_text", "text": json.dumps({
                            "success": True, "doc_name": "source.pdf", "returned_pages": "1", "content": [{"page": 1, "text": "Synthetic source text"}]})}]})
            envelope = {"status": "completed", "model": "synthetic-reader", "instructions": managed, "tools": tools,
                        "items": transcript, "output": [item for item in transcript if item["type"] != "function_call_output"]}
            answer = {**row, "variant": "full", "status": "completed", "identity": fingerprint({"row": row, "index": index["cache_key"], "max_turns": 10, "transport": plan["adapter_mode"]}),
                      "response": decisions[-1]["text"], "sdk_envelope": envelope, "host_phase": phase, "host_call_ordinals": numbers,
                      "host_invocations": len(numbers), "wire_receipts": wires, "elapsed_ms": 15,
                      "accessed_physical_pages": [1] if number == 0 else [], "page_access_recall": 1.0 if number == 0 else 0.0,
                      "index_context_observations": [wire["index_context"] for wire in wires], "index_metadata_supplied": False, "index_summary_supplied": False,
                      "raw_page_output_integrity_verified": True if number == 0 else None, "source_digest_verified": True, "sdk_usage_authoritative": False}
            answer.update(answer_evidence(answer))
            source = SimpleNamespace(run_dir=args.run_dir, calls=calls, plan=plan)
            ledger = OriginLedger(source)
            answer["transport_qualification"] = benchmark_qualification(answer, index, [{"page_index": 1, "markdown": "Synthetic source text"}], 1, args.run_dir, ledger, plan)
            # Let the unmodified judge path generate its metadata and checkpoint,
            # while replacing only the model boundary with deterministic bytes.
            def complete(role, prompt, state, schema, *, on_reserved):
                call, report = invocation(args.run_dir, calls, role.phase, "judge", plan["profile"]["roles"]["judge"],
                                          {"instructions": prompt, "state": state, "schema": schema}, {"equivalent": False, "abstained": bool(number % 2)})
                ledger.calls = copy.deepcopy(calls)
                on_reserved(call)
                return report["value"], report
            with patch("role_hosts.RoleHost.complete", complete):
                judge_case(answer, args=SimpleNamespace(stage="run", retry_failed=False), profile=plan["profile"], constants=CONSTANTS, host=ledger, variant_dir=args.run_dir / "full")
        (args.run_dir / "host-calls.jsonl").write_bytes(b"".join(canonical(call) + b"\n" for call in calls))
        for row in rows[completed:]:
            checkpoint_attempt(args.run_dir / "full" / f"question-{row['source_row']:03d}.unavailable.json",
                               {"source_row": row["source_row"], "variant": "full", "status": "index_unavailable", "question_denominator_retained": True})
        origin = IndexOrigin(consumer["index_origin"], consumer)
        with patch.dict(sys.modules, modules):
            copied = origin.import_completed("source.pdf", args.benchmark / "documents/source.pdf", 1, consumer_dir, "consumer-cache")
        return SimpleNamespace(args=args, runtime=runtime, plan=plan, consumer=consumer, consumer_dir=consumer_dir, rows=rows,
                               modules=modules, origin=origin, index=index, indexes={"source.pdf": copied})

    def prepare(self, fixture):
        outcomes = CompletedOutcomes(fixture.origin, fixture.consumer, fixture.rows)
        with patch.dict(sys.modules, fixture.modules), patch("bridge.owned_process") as process:
            outcomes.prepare(fixture.indexes, {"source.pdf": {"pages": 1}}, fixture.args.benchmark, fixture.consumer_dir, None, CONSTANTS)
            process.assert_not_called()
        return outcomes

    def test_full_denominator_false_abstained_and_zero_page_outcomes_are_retained_without_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(Path(temporary))
            try:
                before = {path: path.read_bytes() for path in fixture.args.run_dir.rglob("*") if path.is_file()}
                outcomes = self.prepare(fixture)
                fresh_rows, fresh_judges = [], []
                def fresh(row):
                    fresh_rows.append(row["source_row"])
                    return {**row, "status": "not_answered"}, []
                def judge(answer):
                    fresh_judges.append(answer["source_row"])
                    return answer
                answers = [outcomes.reader(row, fresh)[0] for row in fixture.rows]
                merged = [outcomes.judge(answer, judge) for answer in answers]
                self.assertEqual(len(merged), 62)
                self.assertEqual(len({row["source_row"] for row in merged}), 62)
                self.assertEqual(fresh_rows, list(range(56, 62)))
                self.assertEqual(fresh_judges, fresh_rows)
                self.assertEqual(len(outcomes.results), 56)
                self.assertTrue(all(answer["judge"]["equivalent"] is False for answer in merged[:56]))
                self.assertTrue(merged[1]["judge"]["abstained"])
                self.assertEqual(merged[1]["accessed_physical_pages"], [])
                self.assertEqual(merged[1]["host_call_ordinals"], [])
                self.assertEqual(merged[1]["judge"]["host_call_ordinals"], [])
                self.assertEqual(merged[1]["host_invocations"], 0)
                self.assertNotIn("host_phase", merged[1])
                self.assertIsNone(merged[1]["elapsed_ms"])
                self.assertTrue(merged[0]["transport_qualification"]["verified"])
                original_path = fixture.args.run_dir / merged[1]["outcome_import"]["origin_case_path"]
                self.assertEqual((fixture.consumer_dir / "outcome-origin/cases/001.json").read_bytes(), original_path.read_bytes())
                self.assertEqual({path: path.read_bytes() for path in fixture.args.run_dir.rglob("*") if path.is_file()}, before)
                summary = outcomes.summary([{"elapsed_ms": 20, "usage": None}])
                self.assertEqual(summary["historical_ledger_lineages"], 1)
                self.assertEqual(summary["combined_invocation_slots"], len(fixture.origin.calls) + 1)
                self.assertEqual(summary["new_calls_for_retained_cases"], 0)
                self.assertIsNone(summary["billing_usd"])
                self.assertIsNone(summary["cold_full_run_elapsed_ms"])
                self.prepare(fixture)  # Exact resume consumes no new origin or consumer call.
            finally:
                fixture.origin.close()

    def test_tamper_blocks_instead_of_resampling(self):
        for kind in ("request", "response", "wire", "envelope", "question", "judge", "pages", "consumer_projection"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(Path(temporary), total=2, completed=1)
                try:
                    path = fixture.args.run_dir / "full/question-000.json"
                    answer = json.loads(path.read_bytes())
                    if kind in ("request", "response"):
                        target = fixture.args.run_dir / "calls" / f"{answer['host_call_ordinals'][0]:05d}.{kind}.json"
                        target.write_bytes(target.read_bytes() + b" ")
                    elif kind == "pages":
                        target = fixture.consumer_dir / "full/store/docs/pi-synthetic/pages.json"
                        target.write_bytes(b"[]")
                    elif kind == "consumer_projection":
                        self.prepare(fixture)
                        target = fixture.consumer_dir / "full/question-000.json"
                        target.write_bytes(target.read_bytes() + b" ")
                    else:
                        if kind == "wire":
                            answer["wire_receipts"][0]["request_sha256"] = "changed"
                        elif kind == "envelope":
                            answer["sdk_envelope"]["instructions"] = "changed"
                        elif kind == "question":
                            answer["question"] = "changed"
                        else:
                            answer["judge"]["case_sha256"] = "changed"
                        write(path, answer)
                        # Match the immutable pair too: validation must reach the
                        # raw bindings, not merely compare the two cached files.
                        candidates = sorted((path.parent / "attempts" / path.stem).glob("*.json"))
                        for item in candidates:
                            value = json.loads(item.read_bytes())
                            if value.get("judge", {}).get("status") == "completed":
                                write(item, answer)
                    with patch("bridge.owned_process") as process:
                        with self.assertRaises(ValueError):
                            self.prepare(fixture)
                        process.assert_not_called()
                finally:
                    fixture.origin.close()

    def test_partial_completed_component_and_missing_immutable_pair_block(self):
        for kind in ("partial", "missing_pair", "started_without_projection", "failed_without_projection"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(Path(temporary), total=2, completed=1)
                try:
                    if kind == "partial":
                        checkpoint_attempt(fixture.args.run_dir / "full/question-001.json", {**fixture.rows[1], "variant": "full", "status": "completed", "response": "retained partial"})
                    elif kind == "missing_pair":
                        for path in (fixture.args.run_dir / "full/attempts/question-000").glob("*.json"):
                            value = json.loads(path.read_bytes())
                            if value.get("judge", {}).get("status") == "completed":
                                path.unlink()
                    else:
                        path = fixture.args.run_dir / "full/question-001.json"
                        checkpoint_attempt(path, {**fixture.rows[1], "variant": "full", "status": kind.split("_", 1)[0]})
                        path.unlink()
                    with self.assertRaisesRegex(ValueError, "Partial completed"):
                        self.prepare(fixture)
                finally:
                    fixture.origin.close()

    def test_default_off_and_changed_inference_controls_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(Path(temporary), total=2, completed=1)
            try:
                runtime = copy.copy(fixture.runtime)
                runtime.carry_completed_outcomes = False
                self.assertIsNone(outcome_declaration(runtime, fixture.consumer))
                for kind in ("variant", "missing_origin", "instructions", "functions", "profile", "timeout", "concurrency", "turns", "source", "python"):
                    with self.subTest(kind=kind):
                        consumer = copy.deepcopy(fixture.consumer)
                        if kind == "variant":
                            consumer["variants"] = ["raw", "full"]
                        elif kind == "missing_origin":
                            consumer["index_origin"] = None
                        elif kind == "instructions":
                            consumer["adapter_files"]["transports.py"] = "changed"
                        elif kind == "functions":
                            consumer["adapter_mode"] = "changed"
                        elif kind == "profile":
                            consumer["profile"]["roles"]["judge"]["reasoning_effort"] = "max"
                        elif kind == "timeout":
                            consumer["host_timeout_secs"] += 1
                        elif kind == "concurrency":
                            consumer["host_concurrency_by_role"]["reader"] = 1
                        elif kind == "turns":
                            consumer["sdk_max_turns"] += 1
                        elif kind == "source":
                            consumer["source_hashes"]["source.pdf"] = "changed"
                        else:
                            consumer["python"] = "changed"
                        with self.assertRaises(ValueError):
                            if kind not in ("variant", "missing_origin", "functions"):
                                origin_declaration(fixture.runtime, consumer)
                            outcome_declaration(fixture.runtime, consumer)
            finally:
                fixture.origin.close()

    def test_execute_staged_pools_route_only_six_gaps_to_fresh_functions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self.fixture(root)
            fixture.origin.close()
            args = SimpleNamespace(**vars(fixture.runtime), stage="run", variant="full", rows="all",
                upstream=fixture.args.upstream, benchmark=fixture.args.benchmark, run_dir=root / "executed", binary=fixture.args.binary,
                codex_bin="never-executed", codex_home=root, model="synthetic-reader", reasoning_effort="max",
                profile="matched-luna-max", service_tier="fast", max_model_calls=1000, index_host_concurrency=64,
                reader_concurrency=5, judge_concurrency=5, max_input_bytes=1048576, timeout=180, max_turns=10,
                capability_receipt=None, retry_failed=False)
            class Client:
                def __init__(self, mode, storage_path, **_options):
                    self.store = Path(storage_path)
                def get_document_structure(self, doc_id):
                    return json.loads((self.store / "docs" / doc_id / "tree.json").read_bytes())
                def get_ocr(self, doc_id, **_options):
                    return {"result": json.loads((self.store / "docs" / doc_id / "pages.json").read_bytes())}
            fixture.modules["pageindex"].PageIndexClient = Client
            def read(row, **_options):
                return {**row, "variant": "full", "status": "completed", "reader_stage_disposition": "new_attempt",
                        "response": "Fresh synthetic answer", "page_access_recall": 0, "transport_qualification": {"verified": False}}, []
            def judge(answer, **_options):
                return {**answer, "judge": {"status": "completed", "equivalent": False, "abstained": False}, "judge_stage_disposition": "new_attempt"}
            groups = {"development8": set(range(8)), "heldout54": set(range(8, 62))}
            with patch.dict(sys.modules, fixture.modules), patch("run.locks.verify", return_value=fixture.plan["source_and_dependencies"]), \
                    patch("run.locks.import_upstream"), patch("run.profiles.resolve", return_value=fixture.plan["profile"]), \
                    patch("run.cohorts.load", return_value=(groups, fixture.plan["cohort_manifest_sha256"])), \
                    patch("run.judge_constants", return_value=CONSTANTS), patch("run.read_case", side_effect=read) as reader, \
                    patch("run.judge_case", side_effect=judge) as fresh_judge, patch("bridge.owned_process") as process:
                # Preserve the actual validator when prepare imports judge_case;
                # only the consumer fresh path is intercepted below.
                original_prepare = CompletedOutcomes.prepare
                def prepare(outcomes, *arguments):
                    with patch("run.judge_case", judge_case):
                        return original_prepare(outcomes, *arguments)
                with patch.object(CompletedOutcomes, "prepare", prepare):
                    result = execute(args)
                process.assert_not_called()
            self.assertEqual(reader.call_count, 6)
            self.assertEqual(fresh_judge.call_count, 6)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["summary"]["full"]["question_denominator"], 62)
            self.assertEqual(result["completed_outcome_carry_forward"]["retained_completed_cases"], 56)
            self.assertTrue(result["completed_outcome_carry_forward"]["qa_or_judge_outcome_reused"])
            self.assertEqual(result["completed_outcome_carry_forward"]["reader_outcomes_retained"], 56)
            self.assertEqual(result["completed_outcome_carry_forward"]["judge_outcomes_retained"], 56)
            self.assertEqual(result["historical_index_origin"]["reuse_scope"], "index_preparation_only")
            self.assertEqual(result["index_origin"]["outcome_retention_policy_reference"], "completed_outcome_origin")
            self.assertEqual(result["completed_outcome_carry_forward"]["historical_ledger_lineages"], 1)
            self.assertEqual(sum(stage["task_dispositions"]["carried"] for stage in result["qa_stage_timings"]["records"]), 112)
            self.assertEqual([stage["role"] for stage in sorted(result["qa_stage_timings"]["records"], key=lambda item: item["started_unix_ns"])], ["reader", "judge"])


def locks_digest(path):
    return digest_bytes(path.read_bytes())


if __name__ == "__main__":
    unittest.main()
