#!/usr/bin/env python3
"""Evaluate the actual GPTgrep native build + mandatory Jev + Codex reader on pinned tasks."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts/pageindex_baseline"))
import locks
import profiles
import cohorts
from bridge import AdapterError, LocalCodex, json_bytes, owned_process
from role_hosts import RoleHost
from run import fingerprint, judge_constants, private_directory, write_json


def ask_arguments(binary: Path, root: Path, row: dict, args) -> list[str]:
    # Only original question and known-document scope enter retrieval; gold stays in the evaluator.
    return [str(binary), "ask", "--document=" + row["doc_id"],
            "--jev-model", args.jev_model, "--codex-bin", args.codex_bin,
            "--codex-home", str(args.codex_home.expanduser().resolve()), "--model", args.model,
            "--reasoning-effort", args.reasoning_effort, "--timeout", str(args.timeout),
            "--max-tool-calls", str(args.max_tool_calls), "--json", "--", row["question"], str(root)]


def build_arguments(binary: Path, corpus: Path, optimize_merge: bool) -> list[str]:
    return [str(binary), "index", str(corpus), "--json"] + (["--optimize-merge"] if optimize_merge else [])


def jev_receipt(report: dict) -> dict | None:
    value = report.get("jev")
    if isinstance(value, dict):
        return value
    for field in ("host_retrieval", "retrieval", "error"):
        nested = report.get(field)
        if isinstance(nested, dict) and isinstance(nested.get("jev"), dict):
            return nested["jev"]
    return None


def jev_cost(jev: dict | None) -> dict:
    if jev is None:
        return {"attempted_calls": None, "validated_responses": None, "measured_cost_usd": None,
                "missing_cost_receipts": None, "accounting_complete": False}
    attempted, responses = jev.get("attempted_calls"), jev.get("requests")
    usage = jev.get("usage", [])
    known = []
    if isinstance(usage, list):
        for value in usage:
            cost = value.get("cost") if isinstance(value, dict) else None
            if type(cost) in (float, int) and math.isfinite(cost) and cost >= 0:
                known.append(cost)
    valid_counts = type(attempted) is int and type(responses) is int and 0 <= responses <= attempted
    missing = max(0, attempted - len(known)) if valid_counts else None
    complete = valid_counts and jev.get("accounting_complete") is True and len(known) == attempted and len(usage) == attempted
    return {"attempted_calls": attempted if valid_counts else None,
            "validated_responses": responses if valid_counts else None,
            "measured_cost_usd": math.fsum(known) if complete and attempted else None,
            "known_cost_subtotal_usd": math.fsum(known) if known else None,
            "missing_cost_receipts": missing, "accounting_complete": complete,
            "attempts_are_physical_requests": False}


def native_metrics(report: dict, doc_id: str, gold_pages: set[int]) -> dict:
    tools = report.get("tool_calls", report.get("host_retrieval", {}).get("receipts", report.get("receipts", [])))
    tools = tools if isinstance(tools, list) else []
    cited = report.get("citations", [])
    cited = cited if isinstance(cited, list) else []
    pages = set()
    for tool in tools:
        if tool.get("success") is not True:
            continue
        for evidence in tool.get("evidence", []):
            if evidence.get("path") == doc_id and type(evidence.get("page_start")) is int and type(evidence.get("page_end")) is int:
                pages.update(range(evidence["page_start"], evidence["page_end"] + 1))
    jev = jev_receipt(report)
    initial = jev.get("initial_status") if jev else None
    required = bool(jev and jev.get("required") is True)
    observed = bool(required and type(jev.get("requests")) is int and jev["requests"] > 0
                    and initial in ("reranked", "filtered_all"))
    successful = report.get("status") == "completed"
    searches = jev.get("searches", []) if jev else []
    search_times = [search.get("metrics", {}).get("elapsed_ms") for search in searches]
    return {
        "jev_core_observed": observed, "jev_initial_status": initial, "jev": jev,
        "jev_accounting": jev_cost(jev), "tool_calls": len(tools),
        "tool_successes": sum(tool.get("success") is True for tool in tools),
        "required_initial_tool_calls": sum(tool.get("required_initial") is True for tool in tools),
        "reader_tool_calls": sum(tool.get("required_initial") is not True for tool in tools),
        "tool_failures": sum(tool.get("success") is False for tool in tools),
        "invalid_argument_errors": sum(tool.get("error_code") == "invalid_arguments" for tool in tools)
            if any("error_code" in tool for tool in tools) else None,
        "accessed_physical_pages": sorted(pages), "page_access_recall": len(pages & gold_pages) / len(gold_pages) if gold_pages else None,
        "citation_count": len(cited), "citation_validation_completed": successful,
        "cited_target_pages": sorted({page for citation in cited if citation.get("path") == doc_id
                                     for page in range(citation.get("page_start", 1), citation.get("page_end", 0) + 1)}),
        "native_host_usage": report.get("usage"), "native_host_elapsed_ms": report.get("elapsed_ms", report.get("host_retrieval", {}).get("elapsed_ms")),
        "jev_search_elapsed_ms": sum(search_times) if search_times and all(isinstance(value, (int, float)) for value in search_times) else None,
        "native_startup_elapsed_ms": None, "model_inference_elapsed_ms": None,
        "timing_components_may_overlap": True,
        "ledger_path": report.get("ledger_path", report.get("host_retrieval", {}).get("ledger_path")),
        "billing_usd": None,
    }


def invoke_native(shared: LocalCodex, arguments: list[str], payload: dict, timeout: int) -> tuple[dict, dict]:
    with shared._lock:
        if len(shared.calls) >= shared.max_calls:
            raise AdapterError("call_budget", "Global native-host invocation budget exhausted")
        number = len(shared.calls) + 1
        request_path = shared.run_dir / "calls" / f"{number:05d}.request.json"
        response_path = shared.run_dir / "calls" / f"{number:05d}.response.json"
        with request_path.open("xb") as output:
            output.write(json_bytes(payload))
        receipt = {"ordinal": number, "phase": payload["phase"], "operation": "native_ask",
                   "requested_model": payload["model"], "requested_effort": payload["reasoning_effort"],
                   "request_sha256": locks.digest(request_path), "host_invoked": True}
        shared.start_attempt(receipt)
        started = time.perf_counter()
        report = {}
        try:
            process = owned_process(arguments, shared.run_dir, timeout)
            if len(process.stdout) > 4 * 1024 * 1024:
                raise ValueError("Native report exceeded4MiB")
            with response_path.open("xb") as output:
                output.write(process.stdout)
            report = locks.read_json(response_path)
            receipt["response_sha256"] = locks.digest(response_path)
            receipt["exit_code"] = process.returncode
            receipt["jev"] = jev_receipt(report)
            receipt["retrieval_failure"] = report.get("retrieval")
            receipt["host_retrieval_failure"] = report.get("host_retrieval")
            if process.returncode != 0 or report.get("status") != "completed":
                raise RuntimeError(str(report.get("code", "native_ask_failed")))
            if report.get("model") != payload["model"] or report.get("requested_reasoning_effort") != payload["reasoning_effort"]:
                raise ValueError("Native reader changed model/effort")
            if report.get("auth_mode") != "chatgpt" or report.get("model_provider") != "openai" or not report.get("thread_id") or not report.get("turn_id"):
                raise ValueError("Native reader identity is unavailable")
            receipt.update(status="completed", model=report["model"], model_provider=report["model_provider"],
                           thread_id=report["thread_id"], turn_id=report["turn_id"], usage=report.get("usage"),
                           reported_host_elapsed_ms=report.get("elapsed_ms"))
        except Exception as error:
            receipt.update(status="failed", error=str(error), error_code=getattr(error, "code", report.get("code", type(error).__name__)))
            if hasattr(error, "cleanup"):
                receipt["timeout_cleanup"] = error.cleanup
        receipt["elapsed_ms"] = (time.perf_counter() - started) * 1000
        shared._append(receipt)
        return report, receipt


def ledger_recovery(path: Path) -> dict:
    """Retain cumulative latest-per-search evidence when the final CLI JSON is absent."""
    data = path.read_bytes()
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("Native Jev attempt ledger exceeds its protocol bound")
    events, torn = [], False
    lines = data.splitlines()
    for number, line in enumerate(lines):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            if number != len(lines) - 1:
                raise ValueError("Native Jev ledger has a malformed middle record")
            torn = True
            break
        if not isinstance(event, dict) or event.get("schema_version") != "gptgrep.jev-attempt.v1":
            raise ValueError("Native Jev ledger schema differs")
        events.append(event)
    searches = {}
    for event in events:
        search = event.get("search")
        if isinstance(search, dict) and isinstance(search.get("search_id"), str):
            searches[search["search_id"]] = search
    last = events[-1] if events else {}
    usage, models = [], []
    for search in searches.values():
        metrics = search.get("metrics", {})
        usage.extend(metrics.get("jev_usage", []))
        models.extend(metrics.get("jev_models", []))
    # Counters in the final complete event are cumulative across searches.
    # This is recovered accounting, not a fabricated successful HostReport.
    jev = {"required": True, "initial_status": last.get("initial_status"),
           "requests": last.get("requests"), "attempted_calls": last.get("attempted_calls"),
           "unobserved_attempts": last.get("unobserved_attempts"),
           "models": sorted(set(models)), "usage": usage, "searches": list(searches.values()),
           "accounting_complete": bool(not torn and last.get("event") == "completed" and last.get("accounting_complete") is True)}
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "complete_events": len(events), "torn_trailing_record": torn,
            "terminal_event": last.get("event"), "source": "durable_host_ledger", "jev": jev}


def summarize(cases: list[dict], expected: int) -> dict:
    judged = [case for case in cases if case.get("judge", {}).get("status") == "completed"]
    completed = [case for case in cases if case.get("status") == "completed"]
    complete = expected > 0 and len(cases) == expected and len(completed) == expected and len(judged) == expected
    core = all(case.get("metrics", {}).get("jev_core_observed") for case in completed) and len(completed) == expected
    correct = sum(case["judge"]["equivalent"] is True for case in judged)
    latency = [case["elapsed_ms"] for case in cases if isinstance(case.get("elapsed_ms"), (int, float))]
    costs = [case.get("metrics", {}).get("jev_accounting", {}) for case in cases]
    cost_complete = expected > 0 and len(costs) == expected and all(cost.get("accounting_complete") for cost in costs)
    known_costs = [cost["known_cost_subtotal_usd"] for cost in costs if cost.get("known_cost_subtotal_usd") is not None]
    return {
        "question_denominator": expected, "materialized_cases": len(cases),
        "completed_responses": len(completed), "failed_cases": expected - len(completed),
        "judged": len(judged), "judge_unavailable": expected - len(judged), "correct": correct,
        "comparison_eligible": complete and core,
        "answer_equivalence_accuracy": correct / expected if complete and core and expected else None,
        "observed_judge_accuracy": correct / expected if len(judged) == expected and expected else None,
        "observed_judge_accuracy_lower_bound": correct / expected if expected else None,
        "mean_page_access_recall": statistics.mean(case["metrics"]["page_access_recall"] for case in cases)
            if expected > 0 and len(cases) == expected and all(isinstance(case.get("metrics", {}).get("page_access_recall"), (int, float)) for case in cases) else None,
        "latency_ms": {"samples": len(latency), "median": statistics.median(latency) if latency else None,
                       "p95": sorted(latency)[min(len(latency) - 1, math.ceil(len(latency) * .95) - 1)] if latency else None},
        "jev_cost_accounting_complete": cost_complete,
        "measured_jev_cost_usd": math.fsum(known_costs) if cost_complete and known_costs else None,
        "known_jev_cost_subtotal_usd": math.fsum(known_costs) if known_costs else None,
        "total_provider_billing_usd": None,
        "no_universal_winner_claim": True,
    }


def compare_reports(native: dict, baseline: dict) -> dict:
    reasons = []
    if native.get("summary", {}).get("comparison_eligible") is not True or baseline.get("baseline_eligible") is not True:
        reasons.append("Both systems must have complete eligible outcomes")
    for field in ("source_rows", "source_hashes", "question_sha256", "cohort_manifest_sha256"):
        if native.get(field) != baseline.get(field):
            reasons.append(f"Different {field}")
    for role in ("chat", "judge"):
        if native.get("profile", {}).get("roles", {}).get(role) != baseline.get("profile", {}).get("roles", {}).get(role):
            reasons.append(f"Different {role} profile")
    if not native.get("known_document_scope") or not baseline.get("known_document_scope"):
        reasons.append("Known-document protocol scope is not established for both")
    baselines = baseline.get("answers", [])
    if len({row.get("variant") for row in baselines}) != 1:
        reasons.append("Select one baseline variant per paired comparison")
    base_map = {row["source_row"]: row for row in baselines}
    pairs = []
    if not reasons:
        for case in native["cases"]:
            other = base_map[case["source_row"]]
            for field in ("rubric_sha256", "schema_sha256"):
                if case["judge"].get(field) != other["judge"].get(field):
                    reasons.append(f"Different judge {field}")
            pairs.append({"source_row": case["source_row"], "native_correct": case["judge"]["equivalent"],
                          "baseline_correct": other["judge"]["equivalent"],
                          "native_page_recall": case["metrics"]["page_access_recall"],
                          "baseline_page_recall": other.get("page_access_recall"),
                          "native_latency_ms": case["elapsed_ms"], "baseline_latency_ms": other.get("elapsed_ms")})
    return {"compatible": not reasons, "reasons": reasons, "paired_cases": pairs if not reasons else [],
            "g5_pass": None, "universal_winner": None,
            "limits": ["Fresh-host SDK versus persistent native-reader overhead differs",
                       "Baseline citation fidelity may be unavailable; do not substitute zero",
                       "Paired quality, grounding, build work and model usage require joint interpretation"]}


def materialize_corpus(source_directory: Path, corpus: Path, source_hashes: dict) -> None:
    corpus.mkdir()
    for name, expected in source_hashes.items():
        destination = corpus / name
        if destination.parent != corpus or destination.suffix.lower() != ".pdf":
            raise ValueError("Only source-relative PDF filenames enter the evaluation corpus")
        shutil.copyfile(source_directory / name, destination)
        if locks.digest(destination) != expected:
            raise ValueError("Source changed during private corpus copy")


def native_snapshot(corpus: Path, source_hashes: dict) -> dict:
    pointer = locks.read_json(corpus / ".gptgrep/CURRENT.json")
    generation = pointer["generation"]
    if not isinstance(generation, str) or not re.fullmatch(r"[A-Za-z0-9-]+", generation):
        raise ValueError("Invalid native generation")
    directory = corpus / ".gptgrep/generations" / generation
    manifest_file = directory / "manifest.json"
    if locks.digest(manifest_file) != pointer["manifest_sha256"]:
        raise ValueError("Native manifest digest mismatch")
    documents = locks.read_json(manifest_file)["documents"]
    result = {}
    for doc in documents:
        if doc["path"] not in source_hashes or doc["source_sha256"] != source_hashes[doc["path"]] or not re.fullmatch(r"[0-9a-f]{24}", doc["id"]):
            raise ValueError("Native document provenance mismatch")
        data = (directory / "text" / (doc["id"] + ".txt")).read_bytes()
        if hashlib.sha256(data).hexdigest() != doc["text_sha256"]:
            raise ValueError("Canonical extraction digest mismatch")
        result[doc["path"]] = data
    if set(result) != set(source_hashes):
        raise ValueError("Native snapshot document coverage differs")
    return result


def verify_native_evidence(report: dict, corpus: Path, sources: dict, text: dict) -> dict:
    for name, expected in sources.items():
        if locks.digest(corpus / name) != expected:
            raise ValueError("Selected source changed during native retrieval")
    evidence = [item for tool in report.get("tool_calls", []) if tool.get("success") is True for item in tool.get("evidence", [])]
    citations = report.get("citations", [])
    for item in evidence + citations:
        name = item.get("path")
        if name not in sources or item.get("source_sha256") != sources[name] or locks.digest(corpus / name) != sources[name]:
            raise ValueError("Native evidence source is stale or outside the selected corpus")
        start, end = item.get("byte_start"), item.get("byte_end")
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(text[name]):
            raise ValueError("Native evidence byte range is invalid")
        excerpt = text[name][start:end]
        excerpt.decode("utf-8")
        if hashlib.sha256(excerpt).hexdigest() != item.get("excerpt_sha256"):
            raise ValueError("Native excerpt differs from its source-bound canonical extraction")
    issued = {(item["node_id"], item["byte_start"], item["byte_end"], item["excerpt_sha256"]) for item in evidence}
    if any((item["node_id"], item["byte_start"], item["byte_end"], item["excerpt_sha256"]) not in issued for item in citations):
        raise ValueError("Native citation was not issued as evidence")
    return {"source_digest_verified": True, "raw_evidence_integrity_verified": True if evidence else None,
            "citation_byte_integrity_verified": True if citations else None,
            "issued_citation_coverage": 1.0 if citations else None,
            "semantic_citation_entailment_verified": None}


def execute(args) -> dict:
    profile = profiles.resolve(args)
    profile["roles"]["index"] = {"engine": "deterministic_native", "model": None, "reasoning_effort": None}
    profile["index_effort_note"] = "Native build is deterministic; no Jev or generative indexing stage is claimed."
    binary = args.binary.expanduser().resolve()
    judge_binary = (args.judge_binary or args.binary).expanduser().resolve()
    benchmark, upstream = args.benchmark.expanduser().resolve(), args.upstream.expanduser().resolve()
    judge_source = args.judge_source.expanduser().resolve()
    verified = locks.verify(upstream, benchmark, judge_source)
    questions = locks.read_json(benchmark / "questions.json")
    groups, cohort_sha = cohorts.load(len(questions), locks.digest(benchmark / "questions.json"))
    indices = cohorts.select(args.rows, len(questions), groups)
    rows = [{"source_row": index, **questions[index]} for index in indices]
    names = sorted({row["doc_id"] for row in rows})
    run_dir = args.run_dir.expanduser().resolve()
    private_directory(run_dir)
    if (run_dir / "manifest.json").exists():
        raise ValueError("This experiment is already materialized; use a new run directory")
    corpus = run_dir / "corpus"
    if corpus.exists():
        raise ValueError("Corpus destination already exists; use a new run directory")
    manifest = {
        "schema_version": "gptgrep.system-eval.v1", "variant": "gptgrep-native-jev",
        "profile": profile, "source_rows": indices, "question_count": len(rows), "document_count": len(names),
        "cohort_manifest_sha256": cohort_sha,
        "question_sha256": fingerprint(rows), "source_hashes": {name: locks.digest(benchmark / "documents" / name) for name in names},
        "binary_sha256": locks.digest(binary), "judge_binary_sha256": locks.digest(judge_binary),
        "source_and_dependencies": verified, "known_document_scope": True,
        "max_host_invocations": args.max_model_calls, "host_timeout_secs": args.timeout,
        "max_tool_calls": args.max_tool_calls, "jev_model_requested": args.jev_model,
        "judge_source_sha256": locks.digest(judge_source / "eval/judge.py"),
        "adapter_files": {name: locks.digest(REPO / "scripts/pageindex_baseline" / name)
                          for name in ("bridge.py", "role_hosts.py", "profiles.py", "locks.py", "run.py", "cohorts.py")},
        "runner_sha256": locks.digest(Path(__file__)),
        "build": {"engine": "native LiteParse/Rust tree/tgrep index", "generative": False,
                  "jev_indexing_stage": False, "optimize_merge": args.optimize_merge},
    }
    with (run_dir / ".owner.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (run_dir / "manifest.json").open("x") as output:
            json.dump(manifest, output, ensure_ascii=False, indent=2)
        if args.stage == "plan":
            return {**manifest, "status": "plan_prepared", "comparison_eligible": False, "host_invocations": 0}
        if args.max_model_calls <= 0:
            raise ValueError("Live evaluation requires an explicit positive host-invocation budget")
        materialize_corpus(benchmark / "documents", corpus, manifest["source_hashes"])
        shared = LocalCodex(judge_binary, args.codex_bin, args.codex_home.expanduser().resolve(), run_dir,
                            args.max_model_calls, args.timeout, args.model, args.reasoning_effort, args.max_input_bytes)
        judge_host = RoleHost(shared, "judge", profile["roles"]["judge"])
        build_args = build_arguments(binary, corpus, args.optimize_merge)
        started = time.perf_counter()
        try:
            process = owned_process(build_args, run_dir, args.build_timeout)
            (run_dir / "build.response.json").write_bytes(process.stdout)
            build = locks.read_json(run_dir / "build.response.json")
            build_ok = process.returncode == 0 and build.get("indexed_files") == len(names)
            build["exit_code"] = process.returncode
        except Exception as error:
            build, build_ok = {"error": str(error)}, False
        build.update(wall_ms=(time.perf_counter() - started) * 1000, status="completed" if build_ok else "failed",
                     model_invocations=0, jev_indexing_stage=False)
        write_json(run_dir / "build.json", build)
        try:
            canonical = native_snapshot(corpus, manifest["source_hashes"]) if build_ok else {}
        except Exception as error:
            canonical, build_ok = {}, False
            build.update(status="failed", source_integrity_error=str(error))
            write_json(run_dir / "build.json", build)
        constants = judge_constants(judge_source)
        cases = []
        for row in rows:
            case_dir = run_dir / "cases" / f"{row['source_row']:03d}"
            case_dir.mkdir(parents=True)
            case = {"source_row": row["source_row"], "doc_id": row["doc_id"], "status": "not_run",
                    "case_identity": fingerprint({"row": row, "manifest": manifest})}
            if not build_ok:
                case.update(status="build_failed", error="Native corpus build did not complete")
            else:
                payload = {"operation": "ask", "phase": f"answer:native:row-{row['source_row']}",
                           "question": row["question"], "document": row["doc_id"],
                           "model": args.model, "reasoning_effort": args.reasoning_effort}
                try:
                    ledger_directory = corpus / ".gptgrep/host-attempts"
                    before_ledgers = set(ledger_directory.glob("*.jsonl"))
                    report, receipt = invoke_native(shared, ask_arguments(binary, corpus, row, args), payload, args.timeout + 60)
                    write_json(case_dir / "native-response.json", report)
                    recovered = [ledger_recovery(path) for path in sorted(set(ledger_directory.glob("*.jsonl")) - before_ledgers)]
                    write_json(case_dir / "ledger-recovery.json", recovered)
                    case["native_attempt_ledgers"] = [{key: value for key, value in item.items() if key != "jev"} for item in recovered]
                    if jev_receipt(report) is None and len(recovered) == 1:
                        # Preserve the original CLI response above; this annotation is
                        # explicitly recovered accounting and never changes its status.
                        report = {**report, "jev": recovered[0]["jev"], "jev_receipt_source": "durable_host_ledger"}
                    case.update(status=receipt["status"], elapsed_ms=receipt["elapsed_ms"], host_receipt=receipt,
                                metrics=native_metrics(report, row["doc_id"], set(json.loads(row["evidence_pages"]))))
                    case["metrics"]["jev_receipt_source"] = report.get("jev_receipt_source", "final_cli_report" if jev_receipt(report) is not None else "unavailable")
                    if receipt["status"] == "completed":
                        validation_started = time.perf_counter()
                        case["metrics"].update(verify_native_evidence(report, corpus,
                            {row["doc_id"]: manifest["source_hashes"][row["doc_id"]]}, canonical))
                        case["evidence_validation_ms"] = (time.perf_counter() - validation_started) * 1000
                        response = report.get("answer", "")
                        if not isinstance(response, str):
                            raise ValueError("Native response has no answer string")
                        prompt = constants["PROMPT"].format(question=" ".join(row["question"].split()),
                                                           answer=row["answer"], answer_format=row["answer_format"],
                                                           response=response[:constants["MAX_RESPONSE_CHARS"]])
                        judge_host.phase = f"judge:native:row-{row['source_row']}"
                        try:
                            verdict, _ = judge_host.complete(prompt, {}, constants["SCHEMA"])
                            case["judge"] = {"status": "completed", **verdict, "model": judge_host.model,
                                             "reasoning_effort": judge_host.effort, "rubric_sha256": fingerprint(constants["PROMPT"]),
                                             "schema_sha256": fingerprint(constants["SCHEMA"]),
                                             "response_truncated": len(response) > constants["MAX_RESPONSE_CHARS"]}
                        except Exception as error:
                            case["judge"] = {"status": "unavailable", "error": str(error)}
                    else:
                        case["error"] = receipt.get("error")
                except Exception as error:
                    case.update(status="failed", error=str(error), error_code=getattr(error, "code", type(error).__name__))
            write_json(case_dir / "case.json", case)
            cases.append(case)
        if locks.digest(binary) != manifest["binary_sha256"] or locks.digest(judge_binary) != manifest["judge_binary_sha256"]:
            raise ValueError("Executable changed during the evaluation")
        summary = summarize(cases, len(rows))
        report = {**manifest, "status": "completed" if summary["comparison_eligible"] else "incomplete",
                  "build_result": build, "summary": summary, "cases": cases, "host_invocations": len(shared.calls),
                  "completed_host_turns": sum(call["status"] == "completed" for call in shared.calls),
                  "cohorts": {name: summarize([case for case in cases if case["source_row"] in members], len(set(indices) & members))
                              for name, members in groups.items()},
                  "timing_note": "Native ask latency includes Jev and Codex. Fresh-host SDK and persistent native-reader overhead remain distinct.",
                  "model_usage_note": "Host invocations are not provider-internal request counts; unknown usage/billing stays null."}
        if args.baseline_summary:
            report["paired_comparison"] = compare_reports(report, locks.read_json(args.baseline_summary.expanduser().resolve()))
        write_json(run_dir / "summary.json", report)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["plan", "run"], default="plan")
    parser.add_argument("--rows", default="all")
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--judge-source", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--binary", type=Path, default=REPO / "target/debug/gptgrep")
    parser.add_argument("--judge-binary", type=Path)
    parser.add_argument("--baseline-summary", type=Path, help="Read only after native evaluation; compare compatible complete outcomes")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    profiles.add_arguments(parser)
    parser.add_argument("--jev-model", default="typesafe/jev-1.13")
    parser.add_argument("--max-model-calls", type=int, default=0)
    parser.add_argument("--max-input-bytes", type=int, default=262144)
    parser.add_argument("--max-tool-calls", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--build-timeout", type=int, default=300)
    parser.add_argument("--optimize-merge", action="store_true")
    args = parser.parse_args()
    if min(args.timeout, args.build_timeout, args.max_tool_calls, args.max_input_bytes) <= 0 or args.max_model_calls < 0:
        parser.error("Invalid execution bounds")
    try:
        report = execute(args)
    except Exception as error:
        report = {"schema_version": "gptgrep.system-eval.v1", "status": "failed", "error": str(error)}
    print(json.dumps({key: report.get(key) for key in (
        "schema_version", "status", "variant", "question_count", "document_count", "host_invocations", "summary", "error"
    )}, indent=2))
    return 0 if report["status"] in ("completed", "plan_prepared") else 1


if __name__ == "__main__":
    raise SystemExit(main())
