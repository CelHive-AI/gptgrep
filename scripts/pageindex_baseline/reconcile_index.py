#!/usr/bin/env python3
"""Zero-model preview/apply reconciliation of a legacy index-admission false negative."""
from __future__ import annotations

import argparse
import ast
import contextlib
import fcntl
import hashlib
import json
from pathlib import Path
import sys
import time

import locks
from index_admission import LEGACY_GUARD_ERROR, audit_index_calls, canonical, digest_bytes, read_bound, read_json_bound

HERE = Path(__file__).resolve().parent
JSON_CAP = 64 * 1024 * 1024
LEDGER_CAP = 128 * 1024 * 1024
PDF_CAP = 512 * 1024 * 1024


def legacy_guard(source: str):
    tree = ast.parse(source)
    expected = ast.parse('any(call.get("status") != "completed" for call in host.calls[before_calls:])', mode="eval").body
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or ast.dump(node.test) != ast.dump(expected) or len(node.body) != 1:
            continue
        raised = node.body[0]
        if (isinstance(raised, ast.Raise) and isinstance(raised.exc, ast.Call)
                and isinstance(raised.exc.func, ast.Name) and raised.exc.func.id == "ValueError"
                and len(raised.exc.args) == 1 and isinstance(raised.exc.args[0], ast.Constant)
                and raised.exc.args[0].value == LEGACY_GUARD_ERROR):
            for parent in ast.walk(tree):
                body = getattr(parent, "body", None)
                if isinstance(parent, ast.Try) and isinstance(body, list) and node in body:
                    prior = body[:body.index(node)]
                    getters = {statement.targets[0].id: ast.dump(statement.value) for statement in prior if isinstance(statement, ast.Assign)
                               and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name)}
                    expected_getters = {"tree": 'client.get_document_structure(doc["doc_id"])',
                                        "pages": 'client.get_ocr(doc["doc_id"], format="page")["result"]'}
                    if all(getters.get(name) == ast.dump(ast.parse(value, mode="eval").body) for name, value in expected_getters.items()):
                        found.append(node.lineno)
    if len(found) != 1:
        raise ValueError("Frozen adapter does not have the unique supported legacy guard after SDK reads")
    return found[0]


def bound_ledger(run_dir):
    raw = read_bound(run_dir / "host-calls.jsonl", LEDGER_CAP)
    if raw and not raw.endswith(b"\n"):
        raise ValueError("Host ledger has an unfinished final record")
    lines = raw.splitlines()
    if len(lines) > 65536:
        raise ValueError("Host ledger exceeds the invocation evidence bound")
    calls = [json.loads(line) for line in lines]
    numbers = [call.get("ordinal") for call in calls]
    if any(type(number) is not int or number < 1 for number in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError("Host ledger ordinals are invalid or duplicated")
    return calls, digest_bytes(raw)


def select_sdk_document(run_dir, source, source_path, page_count, api, sanitize, truncate):
    """Read raw metadata, never list_metas (which refreshes the SDK manifest)."""
    store = run_dir / "full" / "store"
    names = {sanitize(source)}
    names.update(truncate(sanitize(source), suffix=f"_{number}") for number in range(1, 100))
    metadata_paths = sorted((store / "docs").glob("*/doc.json"))
    if len(metadata_paths) > 10000:
        raise ValueError("SDK store exceeds the document evidence bound")
    candidates = []
    for path in metadata_paths:
        if not path.resolve().is_relative_to(store.resolve()):
            raise ValueError("SDK document metadata is redirected outside its store")
        meta, meta_sha = read_json_bound(path, 1024 * 1024)
        if meta.get("name") in names:
            candidates.append((path, meta, meta_sha))
    # Even a prior ready copy with equal text makes attempt attribution ambiguous.
    if len(candidates) != 1:
        raise ValueError("Source does not have exactly one SDK document candidate; never select latest or best")
    path, meta, meta_sha = candidates[0]
    doc_id = path.parent.name
    if meta.get("id") != doc_id or meta.get("status") != "completed" or meta.get("mode") != "flash" or meta.get("pageNum") != page_count:
        raise ValueError("SDK document is not a source-sized completed Flash artifact")
    raw_tree, raw_tree_sha = read_json_bound(path.parent / "tree.json", JSON_CAP)
    stored_pages, stored_pages_sha = read_json_bound(path.parent / "pages.json", JSON_CAP)
    if not isinstance(raw_tree, list) or not raw_tree or not isinstance(stored_pages, list) or len(stored_pages) != page_count:
        raise ValueError("SDK tree/pages are absent or incomplete")
    texts = api._extract_page_texts(str(source_path))
    if not any(text.strip() for text in texts):
        raise ValueError("Locked source extraction has no readable page content")
    expected = [{"page_index": number + 1, "markdown": text} for number, text in enumerate(texts)]
    if stored_pages != expected:
        raise ValueError("SDK pages differ from fresh pinned PyPDF2 source extraction")
    stack, node_ids = list(raw_tree), set()
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            raise ValueError("SDK tree contains an invalid node")
        start, end, node_id = node.get("start_index"), node.get("end_index"), node.get("node_id")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= page_count:
            raise ValueError("SDK tree has invalid source page bounds")
        if not isinstance(node_id, str) or not node_id or node_id in node_ids:
            raise ValueError("SDK tree has missing or duplicate node identities")
        node_ids.add(node_id)
        children = node.get("nodes") or []
        if not isinstance(children, list):
            raise ValueError("SDK child nodes are invalid")
        stack.extend(children)
    api._check_page_bounds(raw_tree, page_count)
    tree_envelope = api.get_tree(doc_id, node_summary=True, include_text=False)
    pages_envelope = api.get_ocr(doc_id, format="page")
    if any(value.get("status") != "completed" or value.get("retrieval_ready") is not True for value in (tree_envelope, pages_envelope)):
        raise ValueError("Pinned SDK does not expose ready tree/page artifacts")
    if pages_envelope["result"] != stored_pages:
        raise ValueError("SDK page getter differs from its stored extraction")
    tree = tree_envelope["result"]
    artifacts = {str(file.relative_to(run_dir)): digest for file, digest in (
        (path, meta_sha), (path.parent / "tree.json", raw_tree_sha), (path.parent / "pages.json", stored_pages_sha))}
    return {"doc_id": doc_id, "name": meta["name"], "tree": tree, "tree_sha256": digest_bytes(canonical(tree)),
            "stored_page_count": page_count, "stored_pages_sha256": digest_bytes(canonical(stored_pages))}, artifacts


def prepare(args):
    run_dir, upstream, benchmark, frozen, binary = (getattr(args, name).expanduser().resolve()
                                                 for name in ("run_dir", "upstream", "benchmark", "frozen_adapter_dir", "binary"))
    if any(not (run_dir / directory).resolve().is_relative_to(run_dir) for directory in ("full", "calls")):
        raise ValueError("Run evidence directories are redirected outside the run")
    plan, plan_sha = read_json_bound(run_dir / "plan.json", 4 * 1024 * 1024)
    if plan_sha != args.plan_sha256 or plan.get("schema_version") != "gptgrep.pageindex.pair.v5" or "full" not in plan.get("variants", []):
        raise ValueError("Original plan digest/schema/full variant differs")
    source = args.source
    if source != Path(source).name or source not in plan["source_hashes"]:
        raise ValueError("Source must be an exact plan-bound relative document name")
    frozen_hashes = {}
    for name, expected in plan["adapter_files"].items():
        if name != Path(name).name:
            raise ValueError("Invalid frozen adapter filename")
        actual = digest_bytes(read_bound(frozen / name, 4 * 1024 * 1024))
        if actual != expected:
            raise ValueError("Frozen adapter source differs from the original plan")
        frozen_hashes[name] = actual
    guard_line = legacy_guard(read_bound(frozen / "run.py", 4 * 1024 * 1024).decode())
    if locks.digest(binary) != plan["host_binary_sha256"]:
        raise ValueError("Frozen host binary differs from the original plan")
    verified = locks.verify(upstream, benchmark, args.judge_source.expanduser().resolve())
    if verified != plan["source_and_dependencies"] or locks.digest(frozen / "sources.lock.json") != verified["source_lock_sha256"]:
        raise ValueError("Source/dependency pins differ from the original plan")
    if locks.digest(frozen / "requirements.lock") != locks.digest(HERE / "requirements.lock"):
        raise ValueError("Frozen dependency lock differs")
    source_path = benchmark / "documents" / source
    if source_path.stat().st_size > PDF_CAP or locks.digest(source_path) != plan["source_hashes"][source]:
        raise ValueError("Original PDF differs from its locked source or exceeds the512MiB bound")
    records, _ = read_json_bound(benchmark / "documents.json", JSON_CAP)
    matching = [item for item in records if item.get("doc_id") == source]
    if len(matching) != 1 or type(matching[0].get("pages")) is not int or not 1 <= matching[0]["pages"] <= 100000:
        raise ValueError("Original document page metadata is unavailable or ambiguous")
    page_count = matching[0]["pages"]
    path = run_dir / "full" / (digest_bytes(canonical(source))[:16] + ".index.json")
    record, record_sha = read_json_bound(path, JSON_CAP)
    if (record.get("status"), record.get("error"), record.get("variant"), record.get("source"), record.get("source_sha256")) != (
            "failed", LEGACY_GUARD_ERROR, "full", source, plan["source_hashes"][source]):
        raise ValueError("Only the exact legacy guard false-negative is eligible")
    expected_cache = digest_bytes(canonical({"source": plan["source_hashes"][source], "variant": "full",
        "upstream": verified["pageindex_revision"], "model": plan["model"], "effort": plan["reasoning_effort"],
        "input_cap": plan["host_input_cap"], "adapter": plan["adapter_files"], "dependencies": verified["dependencies"],
        "all_role_profiles": plan["profile"], "host_concurrency_by_role": plan["host_concurrency_by_role"],
        "host_binary_sha256": plan["host_binary_sha256"]}))
    if record.get("cache_key") != expected_cache or record.get("rejections") != []:
        raise ValueError("Index cache binding differs or pre-call rejections remain")
    attempt_paths = sorted((path.parent / "attempts" / path.stem).glob("*.json"))
    if not attempt_paths or len(attempt_paths) > 10000 or locks.digest(attempt_paths[-1]) != record_sha:
        raise ValueError("Current index projection differs from its immutable final attempt")
    attempts = {str(item.relative_to(run_dir)): locks.digest(item) for item in attempt_paths}
    calls, ledger_sha = bound_ledger(run_dir)
    numbers = record.get("host_call_ordinals")
    if (not isinstance(numbers, list) or not numbers or any(type(number) is not int for number in numbers)
            or numbers != list(range(numbers[0], numbers[-1] + 1)) or record.get("host_call_start") != numbers[0]
            or record.get("host_invocations") != len(numbers)):
        raise ValueError("Original document-attempt invocation interval differs")
    phase = f"index:full:{source}"
    selected = sorted((call for call in calls if call["ordinal"] in set(numbers)), key=lambda call: call["ordinal"])
    if [call["ordinal"] for call in selected] != numbers or any(call.get("phase") == phase and call["ordinal"] > numbers[-1] for call in calls):
        raise ValueError("The selected document attempt is incomplete or superseded")
    rejected_path = run_dir / "adapter-rejections.json"
    rejection_evidence = {}
    if rejected_path.exists():
        rejected, rejected_sha = read_json_bound(rejected_path, JSON_CAP)
        rejection_evidence[str(rejected_path.relative_to(run_dir))] = rejected_sha
        if any(item.get("phase") == phase for item in rejected):
            raise ValueError("Retained pre-call document rejections prevent admission")
    admission = audit_index_calls(run_dir, selected, phase, plan["profile"]["roles"]["index"], plan["host_input_cap"], plan["host_output_cap"])
    if not admission["complete"] or not admission["resolved_failures"]:
        raise ValueError("Every failed invocation must have a validated later exact-request recovery")
    journals = {}
    for call in selected:
        attempt_path = run_dir / "calls" / f"{call['ordinal']:05d}.attempt.json"
        started, started_sha = read_json_bound(attempt_path, 1024 * 1024)
        for key in ("ordinal", "phase", "role", "request_sha256", "requested_model", "requested_effort", "requested_service_tier"):
            if started.get(key) != call.get(key):
                raise ValueError("Invocation start journal differs from its final receipt")
        journals[str(attempt_path.relative_to(run_dir))] = started_sha
        journals[f"calls/{call['ordinal']:05d}.request.json"] = call["request_sha256"]
        if call.get("response_sha256"):
            journals[f"calls/{call['ordinal']:05d}.response.json"] = call["response_sha256"]
    locks.import_upstream(upstream)
    from pageindex.local_api import LocalAPI
    from pageindex.naming import sanitize_filename, truncate_filename
    names = [sanitize_filename(name) for name in plan["source_hashes"]]
    if len(names) != len(set(names)):
        raise ValueError("Plan source names collide under the pinned SDK naming contract")
    api = LocalAPI(str(run_dir / "full/store"), model="unused-zero-model-reconciliation", summary_model="unused-zero-model-reconciliation")
    sdk, artifacts = select_sdk_document(run_dir, source, source_path, page_count, api, sanitize_filename, truncate_filename)
    checks = {"plan.json": plan_sha, "host-calls.jsonl": ledger_sha, str(path.relative_to(run_dir)): record_sha,
              **attempts, **journals, **artifacts, **rejection_evidence}
    for relative, expected in checks.items():
        if locks.digest(run_dir / relative) != expected:
            raise ValueError("Run evidence changed during reconciliation preview")
    if locks.digest(source_path) != plan["source_hashes"][source]:
        raise ValueError("Original PDF changed during source extraction")
    proposed = {**record, **sdk, "status": "completed", "index_admission": admission}
    proposed.pop("error")
    certificate = {"schema_version": "gptgrep.index-reconciliation.v1", "source": source,
                   "plan_sha256": plan_sha, "source_sha256": plan["source_hashes"][source], "original_index_sha256": record_sha,
                   "cache_key": record["cache_key"], "frozen_adapter_files": frozen_hashes,
                   "frozen_guard_line": guard_line, "host_binary_sha256": plan["host_binary_sha256"],
                   "source_and_dependencies": verified, "evidence_sha256": checks,
                   "index_admission": admission, "doc_id": sdk["doc_id"], "name": sdk["name"],
                   "tree_sha256": sdk["tree_sha256"], "stored_pages_sha256": sdk["stored_pages_sha256"],
                   "stored_page_count": sdk["stored_page_count"], "proposed_index_sha256": digest_bytes(canonical(proposed)),
                   "reconciler_files": {name: locks.digest(HERE / name) for name in ("reconcile_index.py", "index_admission.py", "locks.py", "run.py")},
                   "model_calls": 0, "original_attempts_latency_and_unknown_usage_retained": True,
                   "source_binding": "Locked original PDF plus equal fresh pinned PyPDF2 page extraction; SDK store has no raw PDF copy"}
    return certificate, proposed, path


def immutable_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(canonical(value) + b"\n")
        output.flush()
        import os
        os.fsync(output.fileno())


def execute(args):
    run_dir = args.run_dir.expanduser().resolve()
    with contextlib.ExitStack() as stack:
        if args.apply:
            if not args.expected_preview_sha256:
                raise ValueError("Apply requires the exact reviewed preview SHA256")
            for lock in (run_dir / ".owner.lock", run_dir / "full/store/.lock"):
                if lock.is_symlink():
                    raise ValueError("Run/store owner lock is redirected")
                handle = stack.enter_context(lock.open("rb"))
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        certificate, proposed, path = prepare(args)
        preview_sha = digest_bytes(canonical(certificate))
        if not args.apply:
            return {"status": "preview", "preview_sha256": preview_sha, "certificate": certificate, "model_calls": 0}
        if preview_sha != args.expected_preview_sha256:
            raise ValueError("Reviewed preview differs from current evidence; regenerate and review it")
        receipt_path = run_dir / "reconciliations" / f"{preview_sha}.json"
        receipt = {**certificate, "preview_sha256": preview_sha, "applied_at_unix_ns": time.time_ns()}
        immutable_json(receipt_path, receipt)
        from run import checkpoint_attempt
        proposed["index_reconciliation"] = {"receipt": str(receipt_path.relative_to(run_dir)),
                                             "receipt_sha256": locks.digest(receipt_path), "preview_sha256": preview_sha,
                                             "prior_guard_failure": LEGACY_GUARD_ERROR, "model_calls": 0}
        checkpoint_attempt(path, proposed)
        return {"status": "applied", "preview_sha256": preview_sha, "index_sha256": locks.digest(path),
                "recovery_receipt": str(receipt_path.relative_to(run_dir)), "model_calls": 0,
                "original_attempts_latency_and_unknown_usage_retained": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("run-dir", "upstream", "benchmark", "judge-source", "frozen-adapter-dir", "binary"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source", required=True, help="Exact source filename from the original full-run plan")
    parser.add_argument("--plan-sha256", required=True, help="Previously observed original plan digest")
    parser.add_argument("--apply", action="store_true", help="Apply only after review and while owner/store locks are free")
    parser.add_argument("--expected-preview-sha256")
    args = parser.parse_args()
    try:
        result = execute(args)
    except BlockingIOError:
        result = {"status": "blocked", "error": "Run or SDK store owner is active; no reconciliation applied", "model_calls": 0}
    except Exception as error:
        result = {"status": "blocked", "error": str(error) if type(error) is ValueError else type(error).__name__, "model_calls": 0}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] in ("preview", "applied") else 1


if __name__ == "__main__":
    raise SystemExit(main())
