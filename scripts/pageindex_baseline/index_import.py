"""Declared, zero-model reuse of independently admitted SDK preparation artifacts."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shutil
import tempfile
import time

import locks
from index_admission import audit_index_calls, canonical, digest_bytes, read_bound, read_json_bound
from reconcile_index import JSON_CAP, bound_ledger, select_sdk_document

POLICY = "sdk-index-import-and-exact-request-replay.v1"
CRITICAL_ADAPTERS = ("bridge.py", "transports.py", "profiles.py", "role_hosts.py", "locks.py")


def origin_declaration(args, consumer_plan):
    origin_path = getattr(args, "index_origin_run", None)
    options = ("index_origin_plan_sha256", "index_origin_frozen_adapter_dir", "index_origin_binary")
    if origin_path is None:
        if any(getattr(args, name, None) is not None for name in options):
            raise ValueError("Index origin settings require an explicit origin run")
        return None
    if any(getattr(args, name, None) is None for name in options):
        raise ValueError("Index reuse requires the origin plan digest, frozen adapter and binary")
    root = origin_path.expanduser().resolve()
    plan, plan_sha = read_json_bound(root / "plan.json", 4 * 1024 * 1024)
    if plan_sha != args.index_origin_plan_sha256 or "full" not in plan.get("variants", []):
        raise ValueError("Index origin plan digest or full variant differs")
    if plan.get("index_origin") is not None:
        raise ValueError("Nested index origins require separate lineage admission")
    for key in ("source_hashes", "question_sha256", "source_rows", "cohort_manifest_sha256", "source_and_dependencies", "host_binary_sha256", "host_output_cap", "host_timeout_secs", "host_concurrency_by_role", "sdk_max_turns", "python", "qa_stage_strategy"):
        if plan.get(key) != consumer_plan.get(key):
            raise ValueError("Index origin differs from the declared consumer source/runtime conditions")
    if plan.get("profile", {}).get("roles") != consumer_plan.get("profile", {}).get("roles"):
        raise ValueError("Index origin role profiles differ")
    if not 1 <= plan["host_input_cap"] <= consumer_plan["host_input_cap"] <= 1048576:
        raise ValueError("Index reuse permits only an explicit nondecreasing input admission bound up to1MiB")
    if not plan.get("full_62_task_scope") or not consumer_plan.get("full_62_task_scope"):
        raise ValueError("This declared index-reuse experiment retains the full cohort")
    frozen = args.index_origin_frozen_adapter_dir.expanduser().resolve()
    for name, expected in plan["adapter_files"].items():
        if name != Path(name).name or digest_bytes(read_bound(frozen / name, 4 * 1024 * 1024)) != expected:
            raise ValueError("Frozen index-origin adapter differs from its plan")
    for name in CRITICAL_ADAPTERS:
        if name not in plan["adapter_files"] or plan["adapter_files"][name] != consumer_plan["adapter_files"].get(name):
            raise ValueError("Index origin transport/profile adapter changed")
    binary = args.index_origin_binary.expanduser().resolve()
    if locks.digest(binary) != plan["host_binary_sha256"]:
        raise ValueError("Frozen index-origin binary differs")
    return {"policy": POLICY, "run_dir": str(root), "plan_sha256": plan_sha,
            "frozen_adapter_dir": str(frozen), "binary": str(binary),
            "origin_input_cap": plan["host_input_cap"], "consumer_input_cap": consumer_plan["host_input_cap"],
            "qa_reuse": False, "judge_reuse": False,
            "declaration": "Preparation reuse and admission bound selected before new-run QA outcomes; all selected QA/judge cases run independently"}


class IndexOrigin:
    def __init__(self, declaration, consumer_plan):
        self.declaration = declaration
        self.run_dir = Path(declaration["run_dir"])
        lock = self.run_dir / ".owner.lock"
        if lock.is_symlink():
            raise ValueError("Origin owner lock is redirected")
        self._owner = lock.open("rb")
        try:
            fcntl.flock(self._owner, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.plan, plan_sha = read_json_bound(self.run_dir / "plan.json", 4 * 1024 * 1024)
            if plan_sha != declaration["plan_sha256"]:
                raise ValueError("Origin plan changed after declaration")
            self.calls, self.ledger_sha = bound_ledger(self.run_dir)
            rejection_path = self.run_dir / "adapter-rejections.json"
            self.rejections, self.rejections_sha = read_json_bound(rejection_path, JSON_CAP) if rejection_path.exists() else (None, None)
            self.consumer_plan = consumer_plan
            journals = sorted((self.run_dir / "calls").glob("*.attempt.json"))
            if len(journals) > 65536:
                raise ValueError("Origin start-journal evidence exceeds its bound")
            started = {int(path.name.split(".", 1)[0]) for path in journals}
            final = {call["ordinal"] for call in self.calls}
            self.pending_ordinals = sorted(started - final)
            self.journal_hash = digest_bytes(canonical({path.name: locks.digest(path) for path in journals}))
            self.origin_id = digest_bytes(canonical({"plan_sha256": plan_sha, "ledger_sha256": self.ledger_sha,
                                                     "start_journals_sha256": self.journal_hash, "rejections_sha256": self.rejections_sha}))
        except BaseException:
            self._owner.close()
            raise

    def close(self):
        self._owner.close()

    def phase_calls(self, source):
        phase = f"index:full:{source}"
        return [call for call in self.calls if call.get("phase") == phase]

    def summary(self):
        from run import timing_fields
        return {"policy": POLICY, "origin_id": self.origin_id, "plan_sha256": self.declaration["plan_sha256"],
                "ledger_sha256": self.ledger_sha, "start_journals_sha256": self.journal_hash,
                "origin_prompt_rejections": len(self.rejections) if self.rejections is not None else None,
                "origin_rejections_sha256": self.rejections_sha,
                "origin_invocation_slots": len(self.calls) + len(self.pending_ordinals),
                "origin_completed_invocations": sum(call.get("status") == "completed" for call in self.calls),
                "origin_failed_or_interrupted_invocations": sum(call.get("status") != "completed" for call in self.calls),
                "origin_missing_final_ordinals": self.pending_ordinals,
                "origin_usage_missing": sum(call.get("usage") is None for call in self.calls) + len(self.pending_ordinals),
                "origin_by_phase": {role: {"invocations": len(selected), **timing_fields(selected),
                                             "usage_missing": sum(call.get("usage") is None for call in selected)}
                                    for role, selected in ((name, [call for call in self.calls if call.get("phase", "").split(":", 1)[0] == name])
                                                           for name in ("index", "answer", "judge", "interrupted_unknown"))},
                "provider_request_count": None, "billing_usd": None,
                "accounting": "One unique origin ledger lineage including all failed attempts; cache uses are not newly billed invocations",
                "qa_or_judge_outcome_reused": False}

    def import_completed(self, source, source_path, page_count, consumer_dir, cache_key):
        path = self.run_dir / "full" / (digest_bytes(canonical(source))[:16] + ".index.json")
        if not path.exists():
            return None
        record, record_sha = read_json_bound(path, JSON_CAP)
        if record.get("status") != "completed":
            return None
        if record.get("variant") != "full" or record.get("source") != source or record.get("source_sha256") != self.plan["source_hashes"][source]:
            raise ValueError("Completed origin index source binding differs")
        cache_binding = {"source": self.plan["source_hashes"][source], "variant": "full",
            "upstream": self.plan["source_and_dependencies"]["pageindex_revision"], "model": self.plan["model"],
            "effort": self.plan["reasoning_effort"], "input_cap": self.plan["host_input_cap"], "adapter": self.plan["adapter_files"],
            "dependencies": self.plan["source_and_dependencies"]["dependencies"], "all_role_profiles": self.plan["profile"],
            "host_concurrency_by_role": self.plan["host_concurrency_by_role"], "host_binary_sha256": self.plan["host_binary_sha256"]}
        if "index_origin" in self.plan:
            cache_binding["index_origin"] = self.plan["index_origin"]
        expected_cache = digest_bytes(canonical(cache_binding))
        if record.get("cache_key") != expected_cache:
            raise ValueError("Completed origin cache key differs from its immutable plan")
        previous = sorted((path.parent / "attempts" / path.stem).glob("*.json"))
        if not previous or locks.digest(previous[-1]) != record_sha:
            raise ValueError("Completed origin projection lacks matching immutable attempt evidence")
        numbers = record.get("host_call_ordinals")
        if not isinstance(numbers, list) or not numbers or any(type(number) is not int or number < 1 for number in numbers) or len(set(numbers)) != len(numbers):
            raise ValueError("Completed origin index invocation IDs are invalid")
        calls = [call for call in self.calls if call["ordinal"] in set(numbers)]
        if sorted(call["ordinal"] for call in calls) != sorted(numbers) or record.get("host_invocations") != len(calls) or record.get("rejections") != []:
            raise ValueError("Completed origin index has missing invocations or prompt rejections")
        admission = audit_index_calls(self.run_dir, calls, f"index:full:{source}", self.plan["profile"]["roles"]["index"],
                                      self.plan["host_input_cap"], self.plan["host_output_cap"])
        if not admission["complete"]:
            raise ValueError("Completed origin index contains unresolved invocation failures")
        evidence = {}
        for call in calls:
            relative = f"calls/{call['ordinal']:05d}.attempt.json"
            start, start_sha = read_json_bound(self.run_dir / relative, 1024 * 1024)
            if any(start.get(key) != call.get(key) for key in (
                    "ordinal", "phase", "role", "request_sha256", "requested_model", "requested_effort", "requested_service_tier")):
                raise ValueError("Origin invocation start/final bindings differ")
            evidence[relative] = start_sha
            evidence[f"calls/{call['ordinal']:05d}.request.json"] = call["request_sha256"]
            if call.get("response_sha256"):
                evidence[f"calls/{call['ordinal']:05d}.response.json"] = call["response_sha256"]
        if locks.digest(source_path) != self.plan["source_hashes"][source]:
            raise ValueError("Import source PDF differs from the origin")
        from pageindex.local_api import LocalAPI
        from pageindex.naming import sanitize_filename, truncate_filename
        api = LocalAPI(str(self.run_dir / "full/store"), model="unused-zero-model-import", summary_model="unused-zero-model-import")
        sdk, artifacts = select_sdk_document(self.run_dir, source, source_path, page_count, api, sanitize_filename, truncate_filename)
        if any(record.get(key) != sdk[key] for key in ("doc_id", "name", "tree_sha256", "stored_pages_sha256", "stored_page_count")):
            raise ValueError("Completed origin index differs from its independently verified SDK artifact")
        started = time.perf_counter()
        directory = consumer_dir / "full/store/docs" / sdk["doc_id"]
        if not directory.resolve().is_relative_to(consumer_dir.resolve()):
            raise ValueError("Consumer SDK directory is redirected outside its run")
        directory.parent.mkdir(parents=True, exist_ok=True)
        if directory.exists():
            for relative, expected in artifacts.items():
                if locks.digest(directory / Path(relative).name) != expected:
                    raise ValueError("Existing consumer SDK artifact differs; never overwrite it")
        else:
            temporary = Path(tempfile.mkdtemp(prefix=".index-import-", dir=directory.parent))
            try:
                for relative, expected in artifacts.items():
                    raw = read_bound(self.run_dir / relative, JSON_CAP)
                    if digest_bytes(raw) != expected:
                        raise ValueError("Origin SDK artifact changed during copy")
                    with (temporary / Path(relative).name).open("xb") as output:
                        output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                temporary.rename(directory)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        if locks.digest(path) != record_sha or locks.digest(self.run_dir / "host-calls.jsonl") != self.ledger_sha:
            raise ValueError("Origin evidence changed during import")
        if any(locks.digest(self.run_dir / relative) != expected for relative, expected in {**artifacts, **evidence}.items()):
            raise ValueError("Origin request/response/store evidence changed during import")
        if locks.digest(source_path) != self.plan["source_hashes"][source]:
            raise ValueError("Locked source PDF changed during import")
        return {"variant": "full", "source": source, "source_sha256": self.plan["source_hashes"][source],
                "cache_key": cache_key, "status": "completed", **sdk,
                "host_invocations": 0, "host_call_ordinals": [], "rejections": [], "elapsed_ms": None,
                "import_elapsed_ms": (time.perf_counter() - started) * 1000,
                "origin_index_elapsed_ms": record.get("elapsed_ms"), "origin_index_admission": admission,
                "index_import": {"policy": POLICY, "origin_id": self.origin_id,
                                 "origin_plan_sha256": self.declaration["plan_sha256"], "origin_ledger_sha256": self.ledger_sha,
                                 "origin_index_sha256": record_sha, "origin_doc_id": sdk["doc_id"],
                                 "origin_call_ordinals": numbers, "origin_store_sha256": artifacts,
                                 "origin_call_evidence_sha256": evidence, "model_calls": 0},
                "timing_scope": "Imported preparation; fresh index wall time is unavailable, local copy and original attempt time are separate"}
