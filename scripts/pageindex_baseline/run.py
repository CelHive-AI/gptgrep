#!/usr/bin/env python3
"""Actual upstream Flash/SDK baselines with explicit local Codex backend adaptation."""
from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import signal
import threading
import sys
import time
import types

import locks
import profiles
import cohorts
import qualification
from role_hosts import RoleHost
from capability import answer_evidence, scoring_summary

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def checkpoint_attempt(path: Path, value: dict) -> None:
    """Append immutable attempt evidence and update only its current projection."""
    attempts = path.parent / "attempts" / path.stem
    attempts.mkdir(parents=True, exist_ok=True)
    ordinal = len(list(attempts.glob("*.json"))) + 1
    value["attempt_record_count"] = ordinal
    target = attempts / f"{ordinal:04d}.json"
    with target.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    write_json(path, value)


def may_attempt(status: str, retry_failed: bool) -> bool:
    return status in ("not_indexed", "not_answered", "budget_blocked", "index_unavailable") or (retry_failed and status != "completed")


def timing_fields(calls, name="wall_ms", source="elapsed_ms") -> dict:
    values = [item.get(source) for item in calls]
    known = [value for value in values if type(value) in (int, float) and math.isfinite(value) and value >= 0]
    missing = len(values) - len(known)
    subtotal = math.fsum(known)
    return {name: subtotal if missing == 0 else None, name + "_known_subtotal": subtotal, name + "_missing": missing}


def benchmark_qualification(answer, index, stored_pages, page_count, run_dir, host, plan):
    envelope = answer.get("sdk_envelope", {})
    expected_texts = {page["page_index"]: page["markdown"] for page in stored_pages}
    verified_calls = {}
    for call in envelope.get("items", []):
        if call.get("type") != "function_call" or call.get("name") != "get_page_content":
            continue
        outputs = [item for item in envelope.get("items", [])
                   if item.get("type") == "function_call_output" and item.get("call_id") == call.get("call_id")]
        pages = returned_pages({"items": [call, *outputs]}, index["name"], page_count, expected_texts)
        if pages:
            verified_calls[call["call_id"]] = pages
    return qualification.qualify(envelope, index["name"], verified_calls, run_dir,
                                 answer.get("host_call_ordinals", []), host.calls,
                                 plan["profile"]["roles"]["chat"],
                                 {"host_binary_sha256": plan["host_binary_sha256"],
                                  "adapter_files": plan["adapter_files"], "chat_profile": plan["profile"]["roles"]["chat"],
                                  "source_sha256": index["source_sha256"], "stored_pages_sha256": index["stored_pages_sha256"],
                                  "sdk_envelope_sha256": fingerprint(envelope)})


def private_directory(path: Path) -> None:
    if path.is_relative_to(REPO):
        ignored = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", str(path / "private-receipt.json")], check=False)
        if ignored.returncode != 0:
            raise ValueError("Run directory must be outside the repository or inside a gitignored private path")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def parse_pages(spec: str, page_count: int) -> set[int]:
    pages = set()
    for item in spec.split(","):
        endpoints = item.strip().split("-")
        if len(endpoints) == 1:
            start = end = int(endpoints[0])
        elif len(endpoints) == 2:
            start, end = map(int, endpoints)
        else:
            raise ValueError("Invalid page specification")
        if not 1 <= start <= end <= page_count:
            raise ValueError("Page specification is out of bounds")
        pages.update(range(start, end + 1))
    return pages


def returned_pages(envelope: dict, source_name: str, page_count: int, expected_texts: dict | None = None) -> set[int]:
    """Only successful SDK tool results count; requested pages do not prove access."""
    calls = {item["call_id"]: item for item in envelope.get("items", [])
             if item.get("type") == "function_call" and item.get("name") == "get_page_content"}

    def objects(value):
        if isinstance(value, str):
            try:
                yield from objects(json.loads(value))
            except ValueError:
                return
        elif isinstance(value, list):
            for part in value:
                yield from objects(part)
        elif isinstance(value, dict):
            if "success" in value or "error" in value:
                yield value
                return  # source page text inside the envelope is data, not another tool result
            for key in ("text", "content"):
                if key in value:
                    yield from objects(value[key])

    accessed = set()
    for item in envelope.get("items", []):
        if item.get("type") != "function_call_output" or item.get("call_id") not in calls:
            continue
        arguments = json.loads(calls[item["call_id"]]["arguments"])
        if arguments.get("doc_name") != source_name:
            continue
        for obj in objects(item.get("output")):
            if obj.get("success") is True and obj.get("doc_name") == source_name and obj.get("returned_pages"):
                included = parse_pages(obj["returned_pages"], page_count)
                if expected_texts is not None:
                    delivered = {item.get("page"): item.get("text") for item in obj.get("content", []) if isinstance(item, dict)}
                    if any(page not in delivered or delivered[page] != expected_texts.get(page) for page in included):
                        raise ValueError("SDK returned-page text differs from its source-bound stored extraction")
                accessed.update(included)
    return accessed


def attach_raw_index(client):
    """Only this documented raw ablation changes the instance's indexing selection."""
    from pageindex.flash import page_index_flash
    from pageindex.flash.api import flash_rejection_reason
    from pageindex.errors import PageIndexAPIError
    from pageindex.utils import write_node_id

    def raw_index(_self, file_path):
        result = page_index_flash(file_path, summary=False, optimize=False)
        reason = flash_rejection_reason(result)
        if reason:
            raise PageIndexAPIError(f"Raw Flash rejected source: {reason}")
        structure = result["structure"]
        write_node_id(structure)
        return structure, None
    client._api._index_flash = types.MethodType(raw_index, client._api)


def gptgrep_tree(parsed: dict) -> list[dict]:
    """Convert actual Rust tree spans; SDK retains its own PyPDF2 reading text."""
    nodes, roots = {}, []
    pages = len(parsed["pages"])
    for node in parsed["nodes"]:
        if node["id"] in nodes or not 1 <= node["page_start"] <= node["page_end"] <= pages:
            raise ValueError("Invalid GPTgrep tree identity/page range")
        nodes[node["id"]] = {"title": node["title"], "node_id": node["id"],
                             "start_index": node["page_start"], "end_index": node["page_end"]}
    for node in parsed["nodes"]:
        item = nodes[node["id"]]
        if node["parent_id"] is None:
            roots.append(item)
        else:
            if node["parent_id"] not in nodes or node["parent_id"] == node["id"]:
                raise ValueError("Invalid GPTgrep tree parent")
            nodes[node["parent_id"]].setdefault("nodes", []).append(item)
    seen = set()
    def walk(items):
        for item in items:
            if item["node_id"] in seen:
                raise ValueError("Cyclic GPTgrep tree")
            seen.add(item["node_id"])
            walk(item.get("nodes", []))
    walk(roots)
    if len(seen) != len(nodes):
        raise ValueError("GPTgrep tree has unreachable nodes")
    return roots


def attach_gptgrep_index(client, binary: Path, timeout: int):
    def controlled_index(_self, file_path):
        process = subprocess.run([str(binary), "parse", str(file_path), "--json"],
                                 capture_output=True, timeout=timeout, check=False)
        if process.returncode != 0:
            raise RuntimeError("GPTgrep parse failed in the common-SDK tree control")
        parsed = json.loads(process.stdout)
        return gptgrep_tree(parsed), None
    client._api._index_flash = types.MethodType(controlled_index, client._api)


def judge_constants(source: Path) -> dict:
    wanted = {"PROMPT", "SCHEMA", "MAX_RESPONSE_CHARS", "EFFORT", "MODEL"}
    constants = {}
    for node in ast.parse((source / "eval/judge.py").read_text()).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    constants[target.id] = ast.literal_eval(node.value)
    if set(constants) != wanted:
        raise ValueError("Pinned judge constants are unavailable")
    return constants


def reader_request(row: dict, document_id: str, effort: str, max_turns: int) -> tuple[str, dict]:
    return row["question"], {"protocol": "responses", "doc_id": document_id,
                             "reasoning_effort": effort, "max_turns": max_turns}


def execute(args) -> dict:
    from bridge import LocalCodex
    profile = profiles.resolve(args)
    index_host_concurrency = getattr(args, "index_host_concurrency", 64)
    if not 1 <= index_host_concurrency <= 64:
        raise ValueError("Index host concurrency must be1..64")
    roots = {name: getattr(args, name).expanduser().resolve() for name in ("upstream", "benchmark", "run_dir")}
    upstream, benchmark, run_dir = roots["upstream"], roots["benchmark"], roots["run_dir"]
    judge_source = args.judge_source.expanduser().resolve() if args.judge_source else None
    verified = locks.verify(upstream, benchmark, judge_source)
    questions = locks.read_json(benchmark / "questions.json")
    metadata = {row["doc_id"]: row for row in locks.read_json(benchmark / "documents.json")}
    groups, cohort_sha = cohorts.load(len(questions), locks.digest(benchmark / "questions.json"))
    selected = cohorts.select(args.rows, len(questions), groups)
    if not selected or any(not 0 <= row < len(questions) for row in selected):
        raise ValueError("Invalid selected question rows")
    rows = [{"source_row": index, **questions[index]} for index in selected]
    filenames = list(dict.fromkeys(row["doc_id"] for row in rows))
    variants = ["raw", "full"] if args.variant == "both" else [args.variant]
    adapter_files = {name: locks.digest(HERE / name) for name in ("locks.py", "bridge.py", "transports.py", "run.py", "capability.py", "profiles.py", "role_hosts.py", "cohorts.py", "qualification.py")}
    binary = args.binary.expanduser().resolve()
    historical_capability = None
    if args.capability_receipt is not None:
        historical_capability = {"receipt_sha256": locks.digest(args.capability_receipt.expanduser().resolve()),
                                 "admission_effect": "none; historical supporting evidence only"}
    dev_rows = groups["development8"]
    plan = {
        "schema_version": "gptgrep.pageindex.pair.v5", "stage": args.stage, "variants": variants,
        "profile": profile,
        "requested_service_tier": args.service_tier,
        "known_document_scope": True,
        "cohort_manifest_sha256": cohort_sha,
        "source_rows": selected, "question_count": len(rows), "document_count": len(filenames),
        "physical_pages": sum(metadata[name]["pages"] for name in filenames),
        "full_62_task_scope": len(rows) == len(questions) == 62,
        "development_rows": sorted(dev_rows.intersection(selected)),
        "heldout_rows": sorted(set(selected) - dev_rows),
        "model": args.model, "reasoning_effort": args.reasoning_effort,
        "host_input_cap": args.max_input_bytes, "host_output_cap": 131072,
        "max_host_invocations": args.max_model_calls, "host_timeout_secs": args.timeout,
        "sdk_max_turns": args.max_turns, "host_concurrency": index_host_concurrency,
        "host_concurrency_by_role": {"index": index_host_concurrency, "reader": 1, "judge": 1},
        "upstream_defaults": {"benchmark_max_turns_argument": None, "pinned_sdk_effective_max_turns": 10,
                              "benchmark_concurrency": 5, "index_summary_concurrency": 64,
                              "index_expansion_concurrency": 32},
        "concurrency_adaptation": "Upstream index scheduling retained under an explicit host ceiling; reader and judge serial1, not upstream QA-throughput5 reproduction",
        "turn_budget_unit": "Original Agents SDK model-loop turns; not native tool-call count",
        "source_hashes": {name: locks.digest(benchmark / "documents" / name) for name in filenames},
        "question_sha256": fingerprint(rows), "adapter_files": adapter_files,
        "adapter_mode": "original SDK tools and executor; Codex schema-JSON decisions via injected Responses transport",
        "source_and_dependencies": verified, "python": platform.python_version(),
        "host_binary_sha256": locks.digest(binary) if binary.is_file() else None,
        "qualification_policy": "actual original SDK page roundtrip within this live benchmark; no synthetic prerequisite",
        "historical_capability_support": historical_capability,
        "retry_failed": args.retry_failed,
        "historical_upstream_results_reproduced": False,
    }
    private_directory(run_dir)
    with (run_dir / ".owner.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        initial_plan = run_dir / "plan.json"
        if initial_plan.exists():
            prior = locks.read_json(initial_plan)
            for key in ("variants", "source_rows", "source_hashes", "question_sha256", "cohort_manifest_sha256", "source_and_dependencies", "adapter_files", "model", "reasoning_effort", "profile", "host_input_cap", "host_binary_sha256", "sdk_max_turns", "host_timeout_secs", "host_concurrency_by_role"):
                if prior.get(key) != plan.get(key):
                    raise ValueError("Run identity changed; use a new private directory without rewriting prior evidence")
        else:
            write_json(initial_plan, plan)
        stages = run_dir / "stage-plans"
        ordinal = len(list(stages.glob("*.json"))) + 1 if stages.exists() else 1
        write_json(stages / f"{ordinal:03d}-{args.stage}.json", plan)
        if args.stage == "plan":
            return {**plan, "status": "plan_prepared", "host_invocations": 0, "paired_baseline_complete": False}
        if args.stage in ("answer", "judge", "run") and args.max_model_calls <= 0:
            raise ValueError("Model stages require an explicit positive host-completion budget")
        if args.stage in ("judge", "run") and judge_source is None:
            raise ValueError("Judge stages require the locked private judge source")
        host = LocalCodex(binary, args.codex_bin, args.codex_home.expanduser().resolve(), run_dir,
                          args.max_model_calls, args.timeout, args.model, args.reasoning_effort, args.max_input_bytes,
                          host_concurrency=index_host_concurrency, service_tier=args.service_tier)
        index_host = RoleHost(host, "index", profile["roles"]["index"])
        chat_host = RoleHost(host, "chat", profile["roles"]["chat"])
        judge_host = RoleHost(host, "judge", profile["roles"]["judge"])
        before_cwd = Path.cwd()
        previous_signals = {}
        if threading.current_thread() is threading.main_thread():
            def interrupted(signum, _frame):
                failure = KeyboardInterrupt("Baseline interrupted; owned host cleanup requested")
                failure.exit_code = 128 + signum
                raise failure
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_signals[signum] = signal.signal(signum, interrupted)
        os.chdir(run_dir)
        try:
            locks.import_upstream(upstream)
            from transports import PROVIDER, INDEX_ALIAS, ResponsesTransport, chat_backend, register_index_provider
            from pageindex import PageIndexClient
            register_index_provider(index_host)
            index_records, answer_records = [], []
            wire = locks.read_json(run_dir / "wire-receipts.json") if (run_dir / "wire-receipts.json").exists() else []
            # Save upstream progress privately. It is not a public report channel.
            with (run_dir / "upstream.stdout.log").open("a") as stdout_log, (run_dir / "upstream.stderr.log").open("a") as stderr_log:
                with contextlib.redirect_stdout(stdout_log), contextlib.redirect_stderr(stderr_log):
                    for variant in variants:
                        variant_dir = run_dir / variant
                        variant_dir.mkdir(exist_ok=True)
                        transport = ResponsesTransport(chat_host)
                        backend = chat_backend(transport)
                        client = PageIndexClient(
                            mode="local", storage_path=variant_dir / "store",
                            index_model=f"{PROVIDER}/{INDEX_ALIAS}", chat_model=args.model,
                            chat_backend=backend,
                        )
                        if variant == "raw":
                            attach_raw_index(client)
                        elif variant == "gptgrep-tree":
                            attach_gptgrep_index(client, binary, args.timeout)
                        for name in filenames:
                            cache_path = variant_dir / (fingerprint(name)[:16] + ".index.json")
                            cache_key = fingerprint({"source": plan["source_hashes"][name], "variant": variant,
                                                     "upstream": verified["pageindex_revision"], "model": args.model,
                                                     "effort": args.reasoning_effort, "input_cap": args.max_input_bytes,
                                                     "adapter": adapter_files, "dependencies": verified["dependencies"],
                                                     "all_role_profiles": profile,
                                                     "host_concurrency_by_role": plan["host_concurrency_by_role"],
                                                     "host_binary_sha256": plan["host_binary_sha256"]})
                            if cache_path.exists():
                                record = locks.read_json(cache_path)
                                if record.get("cache_key") != cache_key:
                                    raise ValueError("Index cache identity changed; use a new private run directory")
                            else:
                                record = {"variant": variant, "source": name, "source_sha256": plan["source_hashes"][name],
                                          "cache_key": cache_key, "status": "not_indexed"}
                            if args.stage in ("index", "run") and may_attempt(record["status"], args.retry_failed):
                                if variant == "full" and len(host.calls) >= host.max_calls:
                                    if not cache_path.exists():
                                        record.update(status="budget_blocked", host_invocations=0)
                                        checkpoint_attempt(cache_path, record)
                                    index_records.append({key: value for key, value in record.items() if key != "tree"})
                                    continue
                                index_host.phase = f"index:{variant}:{name}"
                                before_calls, before_rejections = len(host.calls), len(host.rejections)
                                started = time.perf_counter()
                                record.pop("error", None)
                                record.update(status="started", host_call_start=before_calls + 1)
                                checkpoint_attempt(cache_path, record)
                                try:
                                    try:
                                        doc = client.submit_document(str(benchmark / "documents" / name), mode="flash", wait=True)
                                    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                                        host.cancel()
                                        raise
                                    finally:
                                        # SDK cancellation drains per-call owned-process cleanup and receipts
                                        # before another document can change the phase or ordinal slice.
                                        host.drain()
                                    tree = client.get_document_structure(doc["doc_id"])
                                    pages = client.get_ocr(doc["doc_id"], format="page")["result"]
                                    if len(pages) != metadata[name]["pages"]:
                                        raise ValueError("SDK stored-page coverage differs from source metadata")
                                    if len(host.rejections) != before_rejections:
                                        raise ValueError("Adapter rejected indexing prompts; full index is incomplete")
                                    if any(call.get("status") != "completed" for call in host.calls[before_calls:]):
                                        raise ValueError("An indexing model invocation failed; preserve its partial work without claiming a complete full index")
                                    record.update(status="completed", doc_id=doc["doc_id"], name=doc["name"],
                                                  tree_sha256=fingerprint(tree), stored_page_count=len(pages),
                                                  stored_pages_sha256=fingerprint(pages),
                                                  tree=tree)
                                except Exception as error:
                                    record.update(status="failed", error=str(error))
                                record.update(elapsed_ms=(time.perf_counter() - started) * 1000,
                                              host_invocations=len(host.calls) - before_calls,
                                              host_call_ordinals=[call["ordinal"] for call in host.calls[before_calls:]],
                                              achieved_host_concurrency=host.concurrency_report(host.calls[before_calls:]),
                                              rejections=host.rejections[before_rejections:])
                                checkpoint_attempt(cache_path, record)
                            index_records.append({key: value for key, value in record.items() if key != "tree"})
                            if record["status"] == "completed":
                                if fingerprint(client.get_document_structure(record["doc_id"])) != record["tree_sha256"]:
                                    raise ValueError("Cached SDK tree digest changed")
                                if fingerprint(client.get_ocr(record["doc_id"], format="page")["result"]) != record["stored_pages_sha256"]:
                                    raise ValueError("Cached SDK reading-text digest changed")
                        indexes = {record["source"]: record for record in index_records if record["variant"] == variant}
                        if args.stage in ("answer", "judge", "run"):
                            for row in rows:
                                answer_path = variant_dir / f"question-{row['source_row']:03d}.json"
                                index = indexes[row["doc_id"]]
                                if index["status"] != "completed":
                                    unavailable = {"variant": variant, "source_row": row["source_row"], "status": "index_unavailable",
                                                   "index_status": index["status"], "question_denominator_retained": True}
                                    answer_records.append(unavailable)
                                    # Preserve one outcome for every selected row, including build failures.
                                    checkpoint_attempt(variant_dir / f"question-{row['source_row']:03d}.unavailable.json", unavailable)
                                    continue
                                identity = fingerprint({"row": row, "index": index["cache_key"], "max_turns": args.max_turns,
                                                        "transport": plan["adapter_mode"]})
                                if answer_path.exists():
                                    answer = locks.read_json(answer_path)
                                    if answer.get("identity") != identity:
                                        raise ValueError("Answer cache identity changed; use a new run directory")
                                else:
                                    answer = {**row, "variant": variant, "identity": identity, "status": "not_answered"}
                                if args.stage in ("answer", "run") and may_attempt(answer["status"], args.retry_failed):
                                    if len(host.calls) >= host.max_calls:
                                        if not answer_path.exists():
                                            answer.update(status="budget_blocked", host_invocations=0)
                                            checkpoint_attempt(answer_path, answer)
                                        answer_records.append({key: value for key, value in answer.items()
                                                               if key not in ("sdk_envelope", "response", "question", "answer")})
                                        continue
                                    chat_host.phase = f"answer:{variant}:row-{row['source_row']}"
                                    started, before_calls = time.perf_counter(), len(host.calls)
                                    before_wire = len(transport.wire_receipts)
                                    answer.pop("judge", None)
                                    answer.pop("error", None)
                                    answer.update(status="started", host_call_start=before_calls + 1)
                                    checkpoint_attempt(answer_path, answer)
                                    try:
                                        question, request_options = reader_request(row, index["doc_id"], args.reasoning_effort, args.max_turns)
                                        envelope = client.chat(question, **request_options)
                                        if locks.digest(benchmark / "documents" / row["doc_id"]) != plan["source_hashes"][row["doc_id"]]:
                                            raise ValueError("Reference source changed during SDK retrieval")
                                        text = "\n".join(part["text"] for item in envelope["output"] if item.get("type") == "message"
                                                         for part in item.get("content", []) if part.get("type") == "output_text")
                                        if not text.strip():
                                            raise ValueError("SDK returned no answer text")
                                        stored_pages = client.get_ocr(index["doc_id"], format="page")["result"]
                                        expected_texts = {page["page_index"]: page["markdown"] for page in stored_pages}
                                        pages = returned_pages(envelope, index["name"], metadata[row["doc_id"]]["pages"], expected_texts)
                                        gold = set(json.loads(row["evidence_pages"]))
                                        contexts = [wire["index_context"] for wire in transport.wire_receipts[before_wire:]
                                                    if wire.get("status") == "completed" and "index_context" in wire]
                                        answer.update(status="completed", response=text, sdk_envelope=envelope,
                                                      accessed_physical_pages=sorted(pages), page_access_recall=len(pages & gold) / len(gold) if gold else None,
                                                      index_metadata_supplied=any(context["index_metadata_supplied"] for context in contexts),
                                                      index_summary_supplied=any(context["index_summary_supplied"] for context in contexts),
                                                      index_context_observations=contexts,
                                                      raw_page_output_integrity_verified=True if pages else None,
                                                      source_digest_verified=True,
                                                      sdk_usage_authoritative=False)
                                        answer.update(answer_evidence(answer))
                                    except Exception as error:
                                        answer.update(status="failed", error=str(error))
                                    answer.update(elapsed_ms=(time.perf_counter() - started) * 1000, host_invocations=len(host.calls) - before_calls,
                                                  host_call_ordinals=[call["ordinal"] for call in host.calls[before_calls:]],
                                                  wire_receipts=transport.wire_receipts[before_wire:])
                                    checkpoint_attempt(answer_path, answer)
                                if answer["status"] == "completed":
                                    answer["transport_qualification"] = benchmark_qualification(
                                        answer, index, client.get_ocr(index["doc_id"], format="page")["result"],
                                        metadata[row["doc_id"]]["pages"], run_dir, host, plan)
                                    write_json(answer_path, answer)
                                if args.stage in ("judge", "run") and answer["status"] == "completed" and (
                                        "judge" not in answer or (args.retry_failed and answer["judge"].get("status") != "completed")):
                                    constants = judge_constants(judge_source)
                                    response = str(answer["response"])
                                    prompt = constants["PROMPT"].format(
                                        question=" ".join(row["question"].split()), answer=row["answer"],
                                        answer_format=row["answer_format"], response=response[:constants["MAX_RESPONSE_CHARS"]])
                                    judge_host.phase = f"judge:{variant}:row-{row['source_row']}"
                                    try:
                                        value, _ = judge_host.complete(prompt, {}, constants["SCHEMA"])
                                        answer["judge"] = {"status": "completed", **value, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                                                           "response_truncated": len(response) > constants["MAX_RESPONSE_CHARS"],
                                                           "upstream_effort": constants["EFFORT"], "actual_requested_effort": judge_host.effort,
                                                           "model": judge_host.model, "rubric_sha256": fingerprint(constants["PROMPT"]),
                                                           "schema_sha256": fingerprint(constants["SCHEMA"])}
                                    except Exception as error:
                                        answer["judge"] = {"status": "unavailable", "error": str(error)}
                                    checkpoint_attempt(answer_path, answer)
                                answer.update(answer_evidence(answer))
                                answer_records.append({key: value for key, value in answer.items()
                                                       if key not in ("sdk_envelope", "response", "question", "answer")})
                        wire.extend(transport.wire_receipts)
                        write_json(run_dir / "wire-receipts.json", wire)
                        asyncio.run(backend["http_client"].aclose())
            write_json(run_dir / "wire-receipts.json", wire)
            write_json(run_dir / "adapter-rejections.json", host.rejections)
            summary = {}
            qualifications = {}
            for variant in variants:
                answered = [row for row in answer_records if row["variant"] == variant]
                proofs = [row["transport_qualification"] for row in answered if row.get("transport_qualification", {}).get("verified") is True]
                qualifications[variant] = {"verified": bool(proofs), "successful_roundtrips": len(proofs), "proofs": proofs,
                                           "all_selected_rows_retained": True}
                summary[variant] = scoring_summary(answered, len(rows), adapter_verified=bool(proofs))
                for cohort, included in groups.items():
                    cohort_rows = [row for row in answered if row["source_row"] in included]
                    denominator = len(set(selected).intersection(included))
                    summary[variant][cohort] = scoring_summary(cohort_rows, denominator, adapter_verified=bool(proofs))
            adapter_verified = all(item["verified"] for item in qualifications.values())
            index_complete = all(row["status"] == "completed" for row in index_records)
            baseline_eligible = all(row["baseline_eligible"] for row in summary.values())
            paired_complete = set(variants) == {"raw", "full"} and baseline_eligible
            stage_complete = index_complete and (
                args.stage == "index" or (args.stage == "answer" and adapter_verified and all(row["completed_responses"] == len(rows) for row in summary.values()))
                or (args.stage in ("judge", "run") and baseline_eligible)
            )
            status = "adapter_capability_unavailable" if args.stage != "index" and not adapter_verified else ("completed" if stage_complete else "incomplete")
            outer_timing = timing_fields(host.calls, "host_process_wall_ms")
            inner_timing = timing_fields(host.calls, "reported_host_wall_ms", "reported_host_elapsed_ms")
            result = {**plan, "status": status, "adapter_capability_verified": adapter_verified,
                      "benchmark_qualification": qualifications,
                      "baseline_eligible": baseline_eligible, "indexes": index_records,
                      "answers": answer_records, "summary": summary, "host_invocations": len(host.calls),
                      "completed_host_turns": sum(call["status"] == "completed" for call in host.calls),
                      "host_calls_by_role": {
                          role: {"attempts": len(items), "completed": sum(item.get("status") == "completed" for item in items),
                                 "failed_or_interrupted": sum(item.get("status") != "completed" for item in items),
                                 **timing_fields(items),
                                 "usage_missing": sum(item.get("usage") is None for item in items)}
                          for role, items in ((name, [call for call in host.calls if call.get("phase", "").split(":", 1)[0] == name])
                                              for name in ("index", "answer", "judge", "interrupted_unknown"))},
                      "adapter_rejections": len(host.rejections), "paired_baseline_complete": paired_complete,
                      "achieved_host_concurrency": host.concurrency_report(),
                      **outer_timing, **inner_timing,
                      "reported_host_timing_missing": inner_timing["reported_host_wall_ms_missing"],
                      "host_elapsed_aggregation": "Sum of per-invocation durations, not elapsed concurrent benchmark wall time; measured process overlap is separate",
                      "native_initialization_timing_available": False,
                      "gptgrep_comparison_complete": False, "provider_request_count": None, "billing_usd": None,
                      "usage_source": "private host-calls.jsonl; SDK aggregate usage is not authoritative",
                      "tools_interface_parity_claimed": False}
            write_json(run_dir / "summary.json", result)
            return result
        finally:
            try:
                host.close(cancel=True)
                write_json(run_dir / "host-concurrency.json", host.concurrency_report())
                write_json(run_dir / "adapter-rejections.json", host.rejections)
            finally:
                for signum, previous in previous_signals.items():
                    signal.signal(signum, previous)
                os.chdir(before_cwd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["plan", "index", "answer", "judge", "run"], default="plan")
    parser.add_argument("--variant", choices=["raw", "full", "both", "gptgrep-tree"], default="both")
    parser.add_argument("--rows", default="all", help="all, explicit dev8, or comma-separated zero-based rows")
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--judge-source", type=Path)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--binary", type=Path, default=REPO / "target/debug/gptgrep")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    profiles.add_arguments(parser)
    parser.add_argument("--max-model-calls", type=int, default=0, help="Cap total local host invocations for this run;0 forbids model calls")
    parser.add_argument("--index-host-concurrency", type=int, default=64, help="Owned index-host upper ceiling1..64; upstream summary64/expansion32 scheduling still applies")
    parser.add_argument("--max-input-bytes", type=int, default=262144)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-turns", type=int, default=10, help="Original SDK model-loop turns (source default10); distinct from native tool-call count")
    parser.add_argument("--capability-receipt", type=Path, help="Optional historical supporting receipt; does not qualify this benchmark")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failed/interrupted stages while retaining immutable prior attempts and all billed calls")
    args = parser.parse_args()
    if args.max_model_calls < 0 or not 1 <= args.max_input_bytes <= 1048576 or args.timeout <= 0 or args.max_turns <= 0 or not 1 <= args.index_host_concurrency <= 64:
        parser.error("Invalid call, input, time or turn bounds")
    try:
        report = execute(args)
    except KeyboardInterrupt as error:
        report = {"schema_version": "gptgrep.pageindex.pair.v5", "stage": args.stage, "status": "interrupted",
                  "error": "Interrupted; started attempts and any known usage remain in the private host ledger",
                  "exit_code": getattr(error, "exit_code", 130), "paired_baseline_complete": False}
    except Exception as error:
        report = {"schema_version": "gptgrep.pageindex.pair.v5", "stage": args.stage, "status": "failed",
                  "error": str(error), "paired_baseline_complete": False}
    # Full private content lives at run-dir; terminal output contains only status.
    print(json.dumps({key: report.get(key) for key in (
        "schema_version", "stage", "status", "question_count", "document_count", "physical_pages",
        "full_62_task_scope", "host_invocations", "completed_host_turns", "summary", "paired_baseline_complete", "error"
    )}, indent=2))
    return 0 if report["status"] in ("completed", "plan_prepared") else report.get("exit_code", 1)


if __name__ == "__main__":
    raise SystemExit(main())
