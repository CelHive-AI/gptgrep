"""Explicit retention of completed source-bound outcomes; never a completion cache.

The producer is read-only. Validation uses its original case and call identities;
the consumer stores the exact producer checkpoint separately from its projection.
No method in this module can dispatch a reader, judge or index completion.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import jsonschema

import locks
from index_admission import canonical, digest_bytes, read_bound, read_json_bound
from reconcile_index import JSON_CAP

POLICY = "completed-outcome-carry-forward.v1"
INFERENCE_FILES = ("bridge.py", "transports.py", "profiles.py", "role_hosts.py", "locks.py",
                   "qualification.py", "capability.py", "cohorts.py")
INFERENCE_FUNCTIONS = ("reader_request", "read_case", "judge_case", "retained_judge",
                       "benchmark_qualification", "returned_pages", "parse_pages", "judge_constants")
TRANSIENT = {"attempt_record_count", "reader_stage_disposition", "judge_stage_disposition"}


def _functions(source):
    nodes = {node.name: ast.dump(node, include_attributes=False) for node in ast.parse(source).body
             if isinstance(node, ast.FunctionDef)}
    if any(name not in nodes for name in INFERENCE_FUNCTIONS):
        raise ValueError("Outcome origin lacks the required inference functions")
    return {name: digest_bytes(nodes[name].encode()) for name in INFERENCE_FUNCTIONS}


def outcome_declaration(args, plan):
    if not getattr(args, "carry_completed_outcomes", False):
        return None
    origin = plan.get("index_origin")
    if plan.get("variants") != ["full"] or origin is None or not args.judge_source:
        raise ValueError("Completed outcome retention requires only full, a declared origin and the pinned judge source")
    original, digest = read_json_bound(Path(origin["run_dir"]) / "plan.json", 4 * 1024 * 1024)
    if digest != origin["plan_sha256"] or original.get("completed_outcome_origin") is not None:
        raise ValueError("Outcome origin changed or already has an outcome lineage")
    for name in INFERENCE_FILES:
        if original["adapter_files"].get(name) != plan["adapter_files"].get(name):
            raise ValueError("Outcome origin inference helper changed")
    for name in ("adapter_mode", "qualification_policy", "turn_budget_unit", "upstream_defaults",
                 "known_document_scope", "requested_service_tier", "model", "reasoning_effort"):
        if original.get(name) != plan.get(name):
            raise ValueError("Outcome origin inference controls changed")
    frozen = Path(origin["frozen_adapter_dir"]) / "run.py"
    raw = read_bound(frozen, 4 * 1024 * 1024)
    if digest_bytes(raw) != original["adapter_files"]["run.py"]:
        raise ValueError("Outcome origin frozen runner changed")
    functions = _functions(raw.decode())
    if functions != _functions((Path(__file__).parent / "run.py").read_text()):
        raise ValueError("Outcome origin reader/judge inference functions changed")
    return {"policy": POLICY, "origin_plan_sha256": digest,
            "inference_function_sha256": functions,
            "retains_completed_readers": True, "retains_completed_judges": True, "completion_response_replay": False,
            "selection": "First immutable complete reader and judge pair for every runtime row, independent of verdict or page access",
            "partial_completed_components": "block replacement; never resample",
            "input_admission": {"origin_bytes": original["host_input_cap"], "consumer_bytes": plan["host_input_cap"],
                                "change": "Nondecreasing preflight byte admission only; original successful request bytes fit both bounds"},
            "new_calls_for_carried_components": 0,
            "timing_scope": "Historical completed outcomes plus missing-case recovery; not a cold full-cohort run"}


class OriginLedger:
    """Only the read interfaces needed by the existing qualification/judge checks."""
    def __init__(self, origin):
        self.run_dir, self.calls = origin.run_dir, copy.deepcopy(origin.calls)
        self._lock = threading.RLock()
        limits = origin.plan["host_concurrency_by_role"]
        self.host_concurrency = limits["index"]
        self.role_concurrency = {"index": limits["index"], "chat": limits["reader"], "judge": limits["judge"]}
        self.total_concurrency = max(limits.values())
        self.service_tier = origin.plan["profile"]["roles"]["judge"]["service_tier"]

    def calls_for_phase(self, phase):
        return sorted((call for call in self.calls if call.get("phase") == phase), key=lambda call: call["ordinal"])

    def call_by_ordinal(self, ordinal):
        return next((call for call in self.calls if call["ordinal"] == ordinal), None)

    def concurrency_report(self, calls=None):
        from bridge import LocalCodex
        return LocalCodex.concurrency_report(self, calls)


class CompletedOutcomes:
    def __init__(self, origin, plan, rows):
        from run import fingerprint
        if (plan.get("completed_outcome_origin") or {}).get("policy") != POLICY:
            raise ValueError("Completed outcome retention was not declared")
        self.origin, self.plan, self.host = origin, plan, OriginLedger(origin)
        self.evidence, self.selected, self.gaps, self.results = {}, {}, [], {}
        self.rows = copy.deepcopy(rows)
        if ([row["source_row"] for row in rows] != plan["source_rows"]
                or len({row["source_row"] for row in rows}) != len(rows)
                or fingerprint(rows) != origin.plan["question_sha256"]):
            raise ValueError("Outcome cohort rows differ from the immutable source")
        if origin.pending_ordinals:
            raise ValueError("Outcome origin has unfinished invocation journals")
        self._read("plan.json", 4 * 1024 * 1024)
        self._read("host-calls.jsonl", 128 * 1024 * 1024, json_value=False)
        if (origin.run_dir / "adapter-rejections.json").exists():
            self._read("adapter-rejections.json")
        for row in rows:
            self._select(row)

    def _read(self, relative, cap=JSON_CAP, *, json_value=True):
        path = self.origin.run_dir / relative
        if not path.resolve().is_relative_to(self.origin.run_dir.resolve()):
            raise ValueError("Outcome origin evidence is redirected outside its run")
        raw = read_bound(path, cap)
        self.evidence[str(relative)] = digest_bytes(raw)
        return json.loads(raw) if json_value else raw

    @staticmethod
    def _stable(value):
        return {key: item for key, item in value.items() if key not in TRANSIENT}

    def _select(self, row):
        relative = Path("full") / f"question-{row['source_row']:03d}.json"
        attempts = sorted((self.origin.run_dir / relative.parent / "attempts" / relative.stem).glob("*.json"))
        if len(attempts) > 4096:
            raise ValueError("Outcome attempt evidence exceeds its bound")
        records = [(str(path.relative_to(self.origin.run_dir)), self._read(path.relative_to(self.origin.run_dir))) for path in attempts]
        current = self._read(relative) if (self.origin.run_dir / relative).exists() else None
        complete = [(path, value) for path, value in records if value.get("status") == "completed" and value.get("judge", {}).get("status") == "completed"]
        prefixes = tuple(f"{role}:full:row-{row['source_row']}:" for role in ("answer", "judge"))
        row_calls = [call for call in self.host.calls if str(call.get("phase", "")).startswith(prefixes)]
        if not complete:
            if row_calls or any(value.get("status") not in ("not_answered", "index_unavailable", "budget_blocked") or value.get("judge") for _, value in records) or (current and (current.get("status") == "completed" or current.get("judge"))):
                raise ValueError("Partial completed or started outcome evidence blocks replacement")
            if current and current.get("status") not in ("not_answered", "index_unavailable", "budget_blocked"):
                raise ValueError("Only unavailable or unstarted origin cases may execute fresh")
            unavailable = relative.with_name(relative.stem + ".unavailable.json")
            if (self.origin.run_dir / unavailable).exists():
                value = self._read(unavailable)
                if (value.get("variant"), value.get("source_row"), value.get("status"), value.get("question_denominator_retained")) != ("full", row["source_row"], "index_unavailable", True):
                    raise ValueError("Unavailable origin case denominator binding differs")
            self.gaps.append(row["source_row"])
            return
        path, first = complete[0]
        if current is None or self._stable(current) != self._stable(first) or any(self._stable(value) != self._stable(first) for _, value in complete):
            raise ValueError("Completed outcome projection differs from the first immutable pair")
        if any(first.get(key) != value for key, value in row.items()) or first.get("variant") != "full":
            raise ValueError("Completed outcome source row differs")
        for _, value in records:
            if value.get("status") == "completed" and any(value.get(key) != first.get(key) for key in ("identity", "response", "sdk_envelope", "host_phase", "host_call_ordinals")):
                raise ValueError("An earlier completed reader component was replaced")
        phases = {first.get("host_phase"), first["judge"].get("host_phase")}
        if any(call.get("phase") not in phases and call.get("status") == "completed" for call in row_calls):
            raise ValueError("Additional completed outcome components require separate admission")
        self.selected[row["source_row"]] = (path, first)

    def _call(self, call, phase, role):
        profile = self.origin.plan["profile"]["roles"][role]
        expected = (phase, role, profile["model"], profile["reasoning_effort"], profile["service_tier"], "completed")
        if tuple(call.get(key) for key in ("phase", "role", "requested_model", "requested_effort", "requested_service_tier", "status")) != expected:
            raise ValueError("Outcome invocation phase/profile/status differs")
        prefix = f"calls/{call['ordinal']:05d}"
        start = self._read(prefix + ".attempt.json", 1024 * 1024)
        if any(start.get(key) != call.get(key) for key in ("ordinal", "phase", "role", "request_sha256", "requested_model", "requested_effort", "requested_service_tier")):
            raise ValueError("Outcome start/final invocation bindings differ")
        raw = self._read(prefix + ".request.json", min(self.origin.plan["host_input_cap"], self.plan["host_input_cap"]), json_value=False)
        request = json.loads(raw)
        if digest_bytes(raw) != call.get("request_sha256") or set(request) != {"instructions", "state", "schema"}:
            raise ValueError("Outcome request bytes or model payload fields differ")
        parts = {"instructions_sha256": digest_bytes(request["instructions"].encode()), "state_sha256": digest_bytes(canonical(request["state"])),
                 "schema_sha256": digest_bytes(canonical(request["schema"])), "input_bytes": len(raw)}
        if any(call.get(key) != value for key, value in parts.items()):
            raise ValueError("Outcome request part bindings differ")
        report = self._read(prefix + ".response.json", self.origin.plan["host_output_cap"] + 65536)
        if self.evidence[prefix + ".response.json"] != call.get("response_sha256"):
            raise ValueError("Outcome response bytes differ")
        if tuple(report.get(key) for key in ("status", "model", "requested_reasoning_effort", "requested_service_tier", "auth_mode", "model_provider")) != (
                "completed", profile["model"], profile["reasoning_effort"], profile["service_tier"], "chatgpt", "openai"):
            raise ValueError("Outcome native runtime profile differs")
        normalize = lambda tier: "priority" if tier in ("fast", "priority") else tier
        if report.get("effective_reasoning_effort") not in (None, profile["reasoning_effort"]) or (report.get("effective_service_tier") is not None and normalize(report["effective_service_tier"]) != normalize(profile["service_tier"])):
            raise ValueError("Outcome effective inference controls differ")
        native = report.get("thread_id"), report.get("turn_id")
        if not all(isinstance(item, str) and item for item in native) or native != (call.get("thread_id"), call.get("turn_id")):
            raise ValueError("Outcome native identities differ")
        try:
            jsonschema.Draft202012Validator(request["schema"]).validate(report.get("value"))
        except jsonschema.ValidationError:
            raise ValueError("Outcome value fails its original schema") from None
        if digest_bytes(canonical(report["value"])) != call.get("value_sha256"):
            raise ValueError("Outcome value binding differs")
        return request, report

    def _reader(self, answer, index, client, pages, page_count):
        from capability import answer_evidence, index_context_observation
        from pageindex.agent_tools import targeting_block
        from pageindex.integrations.openai_agents import build_openai_tools
        from pageindex.local_chat import _conversation_cache_key, _managed_instructions
        from run import benchmark_qualification, fingerprint, returned_pages
        from transports import decision_request
        phase, numbers = answer.get("host_phase"), answer.get("host_call_ordinals")
        if not isinstance(phase, str) or not phase.startswith(f"answer:full:row-{answer['source_row']}:attempt-"):
            raise ValueError("Outcome reader phase differs from its original case")
        calls = self.host.calls_for_phase(phase)
        if not calls or numbers != [call["ordinal"] for call in calls] or answer.get("host_invocations") != len(calls) or len(calls) > self.plan["sdk_max_turns"]:
            raise ValueError("Outcome reader invocation binding or turn budget differs")
        if any(item.get("phase") == phase for item in (self.origin.rejections or [])):
            raise ValueError("Completed outcome reader has a retained prompt rejection")
        wires, envelope = answer.get("wire_receipts"), answer.get("sdk_envelope")
        if not isinstance(wires, list) or len(wires) != len(calls) or not isinstance(envelope, dict):
            raise ValueError("Outcome SDK envelope/wire evidence is incomplete")
        managed = _managed_instructions(client, [])
        tools = [{"type": "function", "name": tool.name, "description": tool.description,
                  "parameters": tool.params_json_schema, "strict": getattr(tool, "strict_json_schema", True)}
                 for tool in build_openai_tools(client, doc_ids=[index["doc_id"]])]
        chat = self.origin.plan["profile"]["roles"]["chat"]
        if (envelope.get("status"), envelope.get("model"), envelope.get("instructions"), envelope.get("tools")) != ("completed", chat["model"], managed, tools):
            raise ValueError("Outcome SDK instructions/tools/profile differ")
        items, output = envelope.get("items"), envelope.get("output")
        if not isinstance(items, list) or output != [item for item in items if item.get("type") != "function_call_output"]:
            raise ValueError("Outcome SDK transcript and output differ")
        first = [{"role": "user", "content": targeting_block(client, index["doc_id"])}, {"role": "user", "content": answer["question"]}]
        cache = _conversation_cache_key(chat["model"], managed, index["doc_id"], first[1:])
        cursor, contexts = 0, []
        final_value = None
        for call, wire in zip(calls, wires):
            request, report = self._call(call, phase, "chat")
            body = {"include": [], "input": first + items[:cursor], "instructions": managed, "model": chat["model"],
                    "prompt_cache_key": cache, "reasoning": {"effort": chat["reasoning_effort"]}, "tools": tools}
            instructions, state, schema, provenance = decision_request(body)
            if request != {"instructions": instructions, "state": state, "schema": schema}:
                raise ValueError("Outcome reader request differs from the original SDK conversation")
            value = report["value"]
            final_value = value
            count = len(value["tool_calls"]) + bool(value["text"])
            segment = items[cursor:cursor + count]
            cursor += count
            if len(segment) != count or not count:
                raise ValueError("Outcome SDK response segment is missing")
            for returned, action in zip(segment, value["tool_calls"]):
                if returned.get("type") != "function_call" or returned.get("name") != action["name"] or json.loads(returned["arguments"]) != json.loads(action["arguments"]):
                    raise ValueError("Outcome SDK action differs from the native decision")
            if value["text"] and (segment[-1].get("type") != "message" or segment[-1].get("content") != [{"type": "output_text", "text": value["text"], "annotations": []}]):
                raise ValueError("Outcome SDK text differs from the native decision")
            expected_wire = {"phase": phase, "status": "completed", "requested_service_tier": chat["service_tier"],
                             "effective_service_tier": report.get("effective_service_tier"), "request_sha256": fingerprint(body),
                             "request_bytes": len(canonical(body)), "instructions_sha256": fingerprint(managed),
                             "input_sha256": fingerprint(body["input"]), "tool_schemas_sha256": fingerprint(tools),
                             "output_sha256": fingerprint(segment), "assistant_tool_calls": len(value["tool_calls"]),
                             "index_context": index_context_observation(body["input"]), **provenance}
            if wire != expected_wire:
                raise ValueError("Outcome wire receipt differs from its original request/response")
            contexts.append(wire["index_context"])
            tool_ids = [item["call_id"] for item in segment if item.get("type") == "function_call"]
            outputs = []
            while cursor < len(items) and items[cursor].get("type") == "function_call_output":
                outputs.append(items[cursor]["call_id"])
                cursor += 1
            if sorted(outputs) != sorted(tool_ids):
                raise ValueError("Outcome SDK action/output pairing differs")
        text = "\n".join(part["text"] for item in output if item.get("type") == "message" for part in item.get("content", []) if part.get("type") == "output_text")
        if cursor != len(items) or not text.strip() or text != answer.get("response") or final_value["tool_calls"]:
            raise ValueError("Outcome final reader output differs")
        accessed = returned_pages(envelope, index["name"], page_count, {page["page_index"]: page["markdown"] for page in pages})
        gold = set(json.loads(answer["evidence_pages"]))
        fields = {"accessed_physical_pages": sorted(accessed), "page_access_recall": len(accessed & gold) / len(gold) if gold else None,
                  "index_context_observations": contexts, "index_metadata_supplied": any(item["index_metadata_supplied"] for item in contexts),
                  "index_summary_supplied": any(item["index_summary_supplied"] for item in contexts),
                  "raw_page_output_integrity_verified": True if accessed else None, "source_digest_verified": True,
                  "sdk_usage_authoritative": False, **answer_evidence(answer)}
        if any(answer.get(key) != value for key, value in fields.items()):
            raise ValueError("Outcome evidence metrics differ from its original SDK transcript")
        proof = benchmark_qualification(answer, index, pages, page_count, self.origin.run_dir, self.host, self.origin.plan)
        if answer.get("transport_qualification") != proof:
            raise ValueError("Outcome source-page qualification differs")
        return proof

    def prepare(self, indexes, metadata, benchmark, consumer_dir, client, constants):
        """Validate every retained pair before the first consumer QA task can run."""
        from run import fingerprint, judge_case
        for row in self.rows:
            number = row["source_row"]
            if number not in self.selected:
                continue
            path, answer = self.selected[number]
            index = indexes[row["doc_id"]]
            imported = index.get("index_import", {})
            if index.get("status") != "completed" or imported.get("origin_id") != self.origin.origin_id:
                raise ValueError("A retained outcome requires its exact independently imported SDK index")
            original_path = "full/" + fingerprint(row["doc_id"])[:16] + ".index.json"
            original_index = self._read(original_path)
            if self.evidence[original_path] != imported.get("origin_index_sha256") or any(index.get(key) != original_index.get(key) for key in ("doc_id", "name", "source_sha256", "tree_sha256", "stored_pages_sha256", "stored_page_count")):
                raise ValueError("Outcome original and copied SDK index bindings differ")
            for relative, expected in imported.get("origin_store_sha256", {}).items():
                self._read(relative, json_value=False)
                if self.evidence[relative] != expected or digest_bytes(read_bound(consumer_dir / relative, JSON_CAP)) != expected:
                    raise ValueError("Outcome original/copy SDK artifact bytes differ")
            if len(imported.get("origin_store_sha256", {})) != 3:
                raise ValueError("Outcome needs exact doc/tree/page artifact bindings")
            if locks.digest(benchmark / "documents" / row["doc_id"]) != self.origin.plan["source_hashes"][row["doc_id"]]:
                raise ValueError("Outcome raw document differs")
            identity = fingerprint({"row": row, "index": original_index["cache_key"], "max_turns": self.origin.plan["sdk_max_turns"], "transport": self.origin.plan["adapter_mode"]})
            if answer.get("identity") != identity:
                raise ValueError("Outcome original reader case identity differs")
            pages = json.loads(read_bound(consumer_dir / "full/store/docs" / index["doc_id"] / "pages.json", JSON_CAP))
            proof = self._reader(answer, original_index, client, pages, metadata[row["doc_id"]]["pages"])
            judge = answer["judge"]
            call = self.host.call_by_ordinal(judge.get("host_call_ordinal"))
            if call is None:
                raise ValueError("Outcome original judge invocation is missing")
            self._call(call, judge.get("host_phase"), "judge")
            # This is the existing completed-judge validator, under the original
            # case/ordinal identity. A completed pair cannot take its model path.
            validated = judge_case(copy.deepcopy(answer), args=SimpleNamespace(stage="judge", retry_failed=False),
                                   profile=self.origin.plan["profile"], constants=constants,
                                   host=self.host, variant_dir=self.origin.run_dir / "full")
            if validated.get("judge_stage_disposition") != "reused" or validated["judge"] != judge:
                raise ValueError("Outcome original judge was not retained exactly")
            current_identity = fingerprint({"row": row, "index": index["cache_key"], "max_turns": self.plan["sdk_max_turns"], "transport": self.plan["adapter_mode"]})
            projected = copy.deepcopy(answer)
            provenance = {"policy": POLICY, "origin_id": self.origin.origin_id,
                          "origin_plan_sha256": self.origin.declaration["plan_sha256"], "origin_ledger_sha256": self.origin.ledger_sha,
                          "origin_case_path": path, "origin_case_sha256": self.evidence[path], "origin_case_identity": identity,
                          "consumer_case_identity": current_identity, "origin_reader_phase": answer["host_phase"],
                          "origin_reader_call_ordinals": answer["host_call_ordinals"], "origin_judge_phase": judge["host_phase"],
                          "origin_judge_call_ordinals": judge["host_call_ordinals"], "origin_reader_elapsed_ms": answer.get("elapsed_ms"),
                          "origin_native_calls": [{key: self.host.call_by_ordinal(ordinal).get(key) for key in (
                              "ordinal", "phase", "role", "thread_id", "turn_id", "request_sha256", "response_sha256")}
                              for ordinal in [*answer["host_call_ordinals"], *judge["host_call_ordinals"]]],
                          "model_calls": 0}
            projected.update(outcome_import=provenance, host_invocations=0, host_call_ordinals=[], elapsed_ms=None,
                             reader_stage_disposition="carried", judge_stage_disposition="carried",
                             timing_scope=self.plan["completed_outcome_origin"]["timing_scope"])
            projected.pop("host_phase", None)
            projected.pop("achieved_host_concurrency", None)
            projected.pop("wire_receipts", None)
            projected.pop("attempt_record_count", None)
            projected["judge"].update(host_invocations=0, host_call_ordinals=[])
            for key in ("host_phase", "host_call_ordinal", "host_request_sha256", "host_response_sha256", "achieved_host_concurrency"):
                projected["judge"].pop(key, None)
            projected["transport_qualification"] = {**proof, "basis": "Revalidated original SDK roundtrip retained from the declared outcome origin", "outcome_origin_id": self.origin.origin_id}
            self.results[number] = projected
        self.assert_unchanged()
        manifest = {"policy": POLICY, "origin_id": self.origin.origin_id, "origin_plan_sha256": self.origin.declaration["plan_sha256"],
                    "origin_ledger_sha256": self.origin.ledger_sha, "question_denominator": len(self.rows),
                    "retained_rows": sorted(self.results), "fresh_rows": self.gaps,
                    "evidence_sha256": self.evidence, "model_calls": 0,
                    "selection": self.plan["completed_outcome_origin"]["selection"]}
        target = consumer_dir / "outcome-origin.json"
        self._immutable(target, canonical(manifest) + b"\n")
        for number, result in self.results.items():
            path, _ = self.selected[number]
            raw = read_bound(self.origin.run_dir / path, JSON_CAP)
            if digest_bytes(raw) != self.evidence[path]:
                raise ValueError("Outcome origin checkpoint changed during copying")
            self._immutable(consumer_dir / "outcome-origin/cases" / f"{number:03d}.json", raw)
            self._immutable(consumer_dir / "full" / f"question-{number:03d}.json", canonical(result) + b"\n")

    @staticmethod
    def _immutable(path, raw):
        if path.exists():
            if read_bound(path, JSON_CAP) != raw:
                raise ValueError("Retained outcome consumer artifact changed; refuse replacement")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            import os
            os.fsync(stream.fileno())

    def assert_unchanged(self):
        if any(digest_bytes(read_bound(self.origin.run_dir / name, 128 * 1024 * 1024)) != expected for name, expected in self.evidence.items()):
            raise ValueError("Outcome origin evidence changed during validation")

    def reader(self, row, fresh):
        number = row["source_row"]
        if number in self.results:
            return copy.deepcopy(self.results[number]), []
        if number not in self.gaps:
            raise ValueError("Outcome row has not passed carry-forward admission")
        return fresh(row)

    def judge(self, answer, fresh):
        number = answer["source_row"]
        if number in self.results:
            if answer != self.results[number]:
                raise ValueError("Carried outcome changed before judge retention")
            return copy.deepcopy(answer)
        if number not in self.gaps:
            raise ValueError("Outcome row has not passed carry-forward admission")
        return fresh(answer)

    def summary(self, new_calls):
        from run import timing_fields
        historical = self.origin.summary()
        return {"policy": POLICY, "origin_id": self.origin.origin_id, "retained_completed_cases": len(self.results),
                "qa_or_judge_outcome_reused": bool(self.results),
                "reader_outcomes_retained": len(self.results), "judge_outcomes_retained": len(self.results),
                "fresh_case_count": len(self.gaps), "question_denominator": len(self.rows), "new_calls_for_retained_cases": 0,
                "historical_ledger_lineages": 1, "historical_invocation_slots": historical["origin_invocation_slots"],
                "new_host_invocations": len(new_calls), "combined_invocation_slots": historical["origin_invocation_slots"] + len(new_calls),
                "combined_usage_missing": historical["origin_usage_missing"] + sum(call.get("usage") is None for call in new_calls),
                **timing_fields(self.origin.calls + list(new_calls), "combined_host_process_wall_ms"),
                "provider_request_count": None, "billing_usd": None,
                "accounting": "The historical_index_origin ledger is counted once, including index/reader/judge failures; retained cases and index replay add no billed calls",
                "timing_scope": self.plan["completed_outcome_origin"]["timing_scope"], "cold_full_run_elapsed_ms": None}
