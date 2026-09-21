#!/usr/bin/env python3
"""Prepare or run a synthetic live outer-SDK tool-capability probe before benchmarks."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

import locks
from bridge import LocalCodex
from capability import runtime_binding
from run import attach_raw_index, private_directory, returned_pages, write_json

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
QUESTION = "According to the source document, how many days is the Harbor retention window? Return only the integer number."


def load_fixture_generator():
    spec = importlib.util.spec_from_file_location("gptgrep_probe_pdf", REPO / "evals/generate_pdf.py")
    if spec is None or spec.loader is None:
        raise ValueError("Synthetic PDF generator is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def execute(args) -> dict:
    upstream = args.upstream.expanduser().resolve()
    benchmark = args.benchmark.expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve()
    binary = args.binary.expanduser().resolve()
    home = args.codex_home.expanduser().resolve()
    verified = locks.verify(upstream, benchmark)
    private_directory(run_dir)
    if args.stage == "run" and args.max_model_calls < 2:
        raise ValueError("A live decision/result probe needs an explicit budget of at least2 host invocations")
    result_path = run_dir / "capability.json"
    if result_path.exists():
        raise ValueError("A completed capability attempt is preserved here; use a new private directory")
    generator = load_fixture_generator()
    pdf_path = run_dir / "capability-fixture.pdf"
    expected_pdf = generator.pdf_bytes()
    if pdf_path.exists():
        if pdf_path.read_bytes() != expected_pdf:
            raise ValueError("Synthetic fixture bytes differ; use a new run directory")
    else:
        with pdf_path.open("xb") as output:
            output.write(expected_pdf)
    expected_sentence = generator.PAGE_LINES[1][2]
    number = re.search(r"\b(\d+) days\b", expected_sentence)
    if number is None:
        raise ValueError("Synthetic source does not contain the expected duration fact")
    expected_number = number.group(1)
    binding = runtime_binding(binary, args.model, args.reasoning_effort, home, args.max_input_bytes)
    host = LocalCodex(binary, args.codex_bin, home, run_dir, args.max_model_calls,
                      args.timeout, args.model, args.reasoning_effort, args.max_input_bytes)
    if host.calls:
        raise ValueError("An earlier host attempt is preserved here; use a new capability-probe directory")
    started = time.perf_counter()
    with (run_dir / ".owner.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prior_cwd = Path.cwd()
        os.chdir(run_dir)
        try:
            locks.import_upstream(upstream)
            from pageindex import PageIndexClient
            from transports import INDEX_ALIAS, PROVIDER, ResponsesTransport, chat_backend, protocol_identity, register_index_provider
            register_index_provider(host)
            transport = ResponsesTransport(host)
            backend = chat_backend(transport)
            client = PageIndexClient(mode="local", storage_path=run_dir / "store",
                                     index_model=f"{PROVIDER}/{INDEX_ALIAS}", chat_model=args.model,
                                     chat_backend=backend)
            attach_raw_index(client)
            try:
                with (run_dir / "upstream.stdout.log").open("a") as stdout_log, (run_dir / "upstream.stderr.log").open("a") as stderr_log:
                    with contextlib.redirect_stdout(stdout_log), contextlib.redirect_stderr(stderr_log):
                        index_path = run_dir / "fixture-index.json"
                        if index_path.exists():
                            index = locks.read_json(index_path)
                            if index["source_sha256"] != locks.digest(pdf_path):
                                raise ValueError("Prepared fixture index identity differs")
                        else:
                            doc = client.submit_document(str(pdf_path), mode="flash", wait=True)
                            index = {**doc, "source_sha256": locks.digest(pdf_path),
                                     "page_count": len(client.get_ocr(doc["doc_id"], format="page")["result"])}
                            write_json(index_path, index)
                        if index["page_count"] != 2:
                            raise ValueError("Capability fixture did not index as two physical pages")
                        plan = {
                            "schema_version": "gptgrep.pageindex.capability-plan.v1",
                            "stage": args.stage, "binding": binding, "adapter_protocol": protocol_identity(),
                            "source_and_dependencies": verified, "question": QUESTION,
                            "fixture_sha256": index["source_sha256"], "source_pages": 2,
                            "model_budget": args.max_model_calls, "real_host_invocations": len(host.calls),
                            "benchmark_score": None,
                        }
                        if args.stage == "prepare":
                            destination = run_dir / "prepare.json"
                            if destination.exists():
                                raise ValueError("Prepared probe plan already exists; run it or choose a new directory")
                            write_json(destination, plan)
                            return {**plan, "status": "prepared", "live_probe_passed": False}
                        host.phase = "capability:synthetic-document-lookup"
                        try:
                            envelope = client.chat(QUESTION, protocol="responses", doc_id=index["doc_id"],
                                                   reasoning_effort=args.reasoning_effort, max_turns=args.max_turns)
                            write_json(run_dir / "sdk-response.json", envelope)
                            text = "\n".join(part["text"] for item in envelope["output"] if item.get("type") == "message"
                                             for part in item.get("content", []) if part.get("type") == "output_text")
                            accessed = returned_pages(envelope, index["name"], 2)
                            value_text = text.strip().strip('`"\'').strip()
                            correct = re.fullmatch(re.escape(expected_number) + r"(?:\s+days?)?[.!]?", value_text, re.I) is not None
                            first = locks.read_json(run_dir / "calls/00001.request.json")
                            initial = first["instructions"] + json.dumps(first["state"], ensure_ascii=False)
                            withheld = expected_sentence not in initial
                            completed = sum(call["status"] == "completed" for call in host.calls)
                            tool_calls = [item for item in envelope.get("items", []) if item.get("type") == "function_call"]
                            stable = runtime_binding(binary, args.model, args.reasoning_effort, home, args.max_input_bytes) == binding
                            passed = 2 in accessed and correct and withheld and completed >= 2 and stable
                            result = {
                                "schema_version": "gptgrep.pageindex.capability.v1",
                                "status": "capability_verified" if passed else "capability_unavailable",
                                "binding": binding, "adapter_protocol": protocol_identity(),
                                "fixture_sha256": index["source_sha256"], "source_pages": 2,
                                "source_and_dependencies": verified,
                                "real_host_invocations": len(host.calls), "completed_host_turns": completed,
                                "native_sessions": [{key: call.get(key) for key in ("ordinal", "thread_id", "turn_id", "response_sha256")}
                                                    for call in host.calls if call["status"] == "completed"],
                                "accessed_physical_pages": sorted(accessed), "answer_correct": correct,
                                "answer_withheld_from_initial_request": withheld, "runtime_binding_stable": stable,
                                "sdk_tool_calls": [{"name": item["name"], "arguments": json.loads(item["arguments"])} for item in tool_calls],
                                "response_sha256": locks.digest(run_dir / "sdk-response.json"),
                                "wire_receipts": transport.wire_receipts, "benchmark_score": None,
                                "elapsed_ms": (time.perf_counter() - started) * 1000,
                            }
                        except Exception as error:
                            result = {
                                "schema_version": "gptgrep.pageindex.capability.v1", "status": "capability_unavailable",
                                "binding": binding, "adapter_protocol": protocol_identity(),
                                "real_host_invocations": len(host.calls),
                                "completed_host_turns": sum(call["status"] == "completed" for call in host.calls),
                                "error": str(error), "benchmark_score": None,
                            }
                        write_json(result_path, result)
                        return result
            finally:
                asyncio.run(backend["http_client"].aclose())
        finally:
            os.chdir(prior_cwd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["prepare", "run"], default="prepare")
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--binary", type=Path, default=REPO / "target/debug/gptgrep")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--reasoning-effort", default="max")
    parser.add_argument("--max-model-calls", type=int, default=0)
    parser.add_argument("--max-input-bytes", type=int, default=262144)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-turns", type=int, default=6)
    args = parser.parse_args()
    try:
        report = execute(args)
    except Exception as error:
        report = {"schema_version": "gptgrep.pageindex.capability.v1", "status": "failed", "error": str(error),
                  "benchmark_score": None}
    print(json.dumps({key: report.get(key) for key in (
        "schema_version", "status", "real_host_invocations", "completed_host_turns", "accessed_physical_pages",
        "answer_correct", "answer_withheld_from_initial_request", "benchmark_score", "error"
    )}, indent=2))
    return 0 if report["status"] in ("prepared", "capability_verified") else 1


if __name__ == "__main__":
    raise SystemExit(main())
