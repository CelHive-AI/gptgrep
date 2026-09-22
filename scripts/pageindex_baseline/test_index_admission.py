"""Offline exact-request recovery and zero-model, source-bound reconciliation."""
import copy
import fcntl
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from index_admission import LEGACY_GUARD_ERROR, TEXT_SCHEMA, audit_index_calls, canonical, digest_bytes
import reconcile_index
from run import checkpoint_attempt

PROFILE = {"model": "synthetic-model", "reasoning_effort": "medium", "service_tier": "fast"}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b"\n")


def invocation(root, ordinal, status, *, request_text="synthetic", phase="index:full:source.pdf"):
    request = {"instructions": "Synthetic index instruction", "state": {"messages": [{"role": "user", "content": request_text}]},
               "schema": TEXT_SCHEMA}
    request_bytes = canonical(request)
    protocol = {"kind": "terminal_error", "codex_error_info": "other", "will_retry": False,
                "http_status_code": None, "server_retry_notifications": 2, "usage": None, "accounting_complete": False}
    value = {"text": "Synthetic summary"}
    report = {"status": "completed", "model": PROFILE["model"], "requested_reasoning_effort": PROFILE["reasoning_effort"],
              "effective_reasoning_effort": PROFILE["reasoning_effort"], "requested_service_tier": "fast", "effective_service_tier": "priority",
              "auth_mode": "chatgpt", "model_provider": "openai", "thread_id": f"synthetic-{ordinal}", "turn_id": "synthetic-turn",
              "value": value, "usage": {"total_tokens": 7}}
    if status != "completed":
        report = {"schema_version": "gptgrep.error.v1", "code": "host_codex_terminal_error", "host_protocol": protocol}
    response_bytes = canonical(report)
    call = {"ordinal": ordinal, "phase": phase, "role": "index", "status": status,
            "requested_model": PROFILE["model"], "requested_effort": PROFILE["reasoning_effort"], "requested_service_tier": "fast",
            "request_sha256": digest_bytes(request_bytes), "instructions_sha256": digest_bytes(request["instructions"].encode()),
            "state_sha256": digest_bytes(canonical(request["state"])), "schema_sha256": digest_bytes(canonical(TEXT_SCHEMA)),
            "response_sha256": digest_bytes(response_bytes), "usage": report.get("usage"), "elapsed_ms": 11,
            "measurement_id": "synthetic-process", "process_started_monotonic_ns": ordinal * 100,
            "process_finished_monotonic_ns": ordinal * 100 + 50, "host_invoked": True}
    if status == "completed":
        call.update(thread_id=report["thread_id"], turn_id=report["turn_id"], value_sha256=digest_bytes(canonical(value)))
    else:
        call.update(error_code="host_codex_terminal_error", host_protocol=protocol, accounting_complete=False)
    (root / "calls").mkdir(parents=True, exist_ok=True)
    (root / "calls" / f"{ordinal:05d}.request.json").write_bytes(request_bytes)
    (root / "calls" / f"{ordinal:05d}.response.json").write_bytes(response_bytes)
    write_json(root / "calls" / f"{ordinal:05d}.attempt.json", call)
    return call


class AdmissionTests(unittest.TestCase):
    def test_later_exact_recovery_preserves_failed_usage_and_durations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = [invocation(root, 1, "failed"), invocation(root, 2, "failed"), invocation(root, 3, "completed")]
            original = copy.deepcopy(calls)
            result = audit_index_calls(root, calls, "index:full:source.pdf", PROFILE)
            self.assertTrue(result["complete"])
            self.assertEqual([item["failed_ordinal"] for item in result["resolved_failures"]], [1, 2])
            self.assertEqual({item["completed_ordinal"] for item in result["resolved_failures"]}, {3})
            self.assertEqual(result["host_invocations"], 3)
            self.assertEqual(result["usage_missing"], 2)
            self.assertEqual(result["host_wall_ms"], 33)
            self.assertIsNone(result["billing_usd"])
            self.assertEqual(calls, original)

    def test_unresolved_interrupted_identity_failures_and_earlier_success_remain_blocking(self):
        for mutation in ("different_request", "interrupted", "identity", "overlap", "different_process", "no_later"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                calls = [invocation(root, 1, "failed"), invocation(root, 2, "completed",
                         request_text="different" if mutation == "different_request" else "synthetic")]
                if mutation == "interrupted":
                    calls[0]["status"] = "interrupted"
                elif mutation == "identity":
                    calls[0]["error_code"] = "host_identity"
                elif mutation == "overlap":
                    calls[1]["process_started_monotonic_ns"] = 101
                elif mutation == "different_process":
                    calls[1]["measurement_id"] = "different-process"
                elif mutation == "no_later":
                    calls = [invocation(root, 1, "completed"), invocation(root, 2, "failed")]
                result = audit_index_calls(root, calls, "index:full:source.pdf", PROFILE)
                self.assertFalse(result["complete"])
                self.assertEqual(len(result["unresolved_failures"]), 1)

    def test_tamper_and_profile_changes_do_not_admit_a_completion(self):
        for mutation in ("request", "response", "profile", "schema", "native_identity"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                calls = [invocation(root, 1, "failed"), invocation(root, 2, "completed")]
                if mutation in ("request", "response"):
                    target = root / f"calls/00002.{mutation}.json"
                    target.write_bytes(target.read_bytes() + b" ")
                elif mutation == "profile":
                    calls[1]["requested_effort"] = "max"
                elif mutation == "schema":
                    calls[1]["schema_sha256"] = "changed"
                else:
                    calls[1]["thread_id"] = "different-native-thread"
                with self.assertRaises(ValueError):
                    audit_index_calls(root, calls, "index:full:source.pdf", PROFILE)


class ReconciliationTests(unittest.TestCase):
    def fixture(self, root):
        run = root / "run"
        run.mkdir()
        (run / ".owner.lock").touch()
        frozen, benchmark, upstream, judge = (root / name for name in ("frozen", "benchmark", "upstream", "judge"))
        for directory in (frozen, benchmark / "documents", upstream, judge):
            directory.mkdir(parents=True)
        binary = root / "native-binary"
        binary.write_bytes(b"synthetic never executed")
        source = benchmark / "documents/source.pdf"
        source.write_bytes(b"Synthetic PDF bytes; parser is injected by this offline fixture.")
        source_sha = digest_bytes(source.read_bytes())
        write_json(benchmark / "documents.json", [{"doc_id": "source.pdf", "pages": 1}])
        frozen_source = 'def execute():\n    try:\n        tree = client.get_document_structure(doc["doc_id"])\n        pages = client.get_ocr(doc["doc_id"], format="page")["result"]\n        if any(call.get("status") != "completed" for call in host.calls[before_calls:]):\n            raise ValueError(' + repr(LEGACY_GUARD_ERROR) + ')\n    except Exception:\n        pass\n'
        (frozen / "run.py").write_text(frozen_source)
        (frozen / "sources.lock.json").write_bytes(b"{}")
        shutil.copyfile(reconcile_index.HERE / "requirements.lock", frozen / "requirements.lock")
        verified = {"source_lock_sha256": digest_bytes(b"{}"), "dependencies": {"synthetic": "1"}, "pageindex_revision": "synthetic-source"}
        plan = {"schema_version": "gptgrep.pageindex.pair.v5", "variants": ["full"], "source_hashes": {"source.pdf": source_sha},
                "adapter_files": {"run.py": digest_bytes(frozen_source.encode())}, "host_binary_sha256": digest_bytes(binary.read_bytes()),
                "source_and_dependencies": verified, "model": "synthetic-model", "reasoning_effort": "max", "host_input_cap": 262144,
                "host_output_cap": 131072, "profile": {"roles": {"index": PROFILE}},
                "host_concurrency_by_role": {"index": 64, "reader": 5, "judge": 5}}
        write_json(run / "plan.json", plan)
        cache = {"source": source_sha, "variant": "full", "upstream": verified["pageindex_revision"], "model": plan["model"],
                 "effort": plan["reasoning_effort"], "input_cap": plan["host_input_cap"], "adapter": plan["adapter_files"],
                 "dependencies": verified["dependencies"], "all_role_profiles": plan["profile"],
                 "host_concurrency_by_role": plan["host_concurrency_by_role"], "host_binary_sha256": plan["host_binary_sha256"]}
        calls = [invocation(run, 1, "failed"), invocation(run, 2, "completed")]
        (run / "host-calls.jsonl").write_bytes(b"".join(canonical(call) + b"\n" for call in calls))
        write_json(run / "adapter-rejections.json", [])
        record_path = run / "full" / (digest_bytes(canonical("source.pdf"))[:16] + ".index.json")
        record = {"source": "source.pdf", "source_sha256": source_sha, "variant": "full", "cache_key": digest_bytes(canonical(cache)),
                  "status": "failed", "error": LEGACY_GUARD_ERROR, "host_call_start": 1, "host_call_ordinals": [1, 2],
                  "host_invocations": 2, "rejections": [], "elapsed_ms": 1234}
        checkpoint_attempt(record_path, record)
        document = run / "full/store/docs/pi-synthetic"
        document.mkdir(parents=True)
        (run / "full/store/.lock").touch()
        write_json(run / "full/store/manifest.json", {"sentinel": "never rewritten"})
        write_json(document / "doc.json", {"id": "pi-synthetic", "name": "source.pdf", "status": "completed", "mode": "flash", "pageNum": 1})
        write_json(document / "tree.json", [{"node_id": "synthetic-node", "title": "Synthetic", "start_index": 1, "end_index": 1}])
        write_json(document / "pages.json", [{"page_index": 1, "markdown": "Synthetic source text"}])
        args = SimpleNamespace(run_dir=run, upstream=upstream, benchmark=benchmark, frozen_adapter_dir=frozen, binary=binary,
                               judge_source=judge, source="source.pdf", plan_sha256=digest_bytes((run / "plan.json").read_bytes()),
                               apply=False, expected_preview_sha256=None)
        class FakeAPI:
            def __init__(self, storage_path, **_settings):
                self.root = Path(storage_path)
            @staticmethod
            def _extract_page_texts(_source):
                return ["Synthetic source text"]
            @staticmethod
            def _check_page_bounds(_tree, _count):
                pass
            def get_tree(self, doc_id, **_options):
                return {"status": "completed", "retrieval_ready": True,
                        "result": json.loads((self.root / "docs" / doc_id / "tree.json").read_bytes())}
            def get_ocr(self, doc_id, **_options):
                return {"status": "completed", "retrieval_ready": True,
                        "result": json.loads((self.root / "docs" / doc_id / "pages.json").read_bytes())}
        package, local_api, naming = (ModuleType(name) for name in ("pageindex", "pageindex.local_api", "pageindex.naming"))
        local_api.LocalAPI = FakeAPI
        naming.sanitize_filename = lambda name: name
        naming.truncate_filename = lambda name, suffix="": Path(name).stem + suffix + Path(name).suffix
        modules = {"pageindex": package, "pageindex.local_api": local_api, "pageindex.naming": naming}
        return args, verified, modules, record_path

    def test_preview_is_read_only_and_apply_preserves_all_prior_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, verified, modules, index = self.fixture(Path(temporary))
            before = {path: path.read_bytes() for path in args.run_dir.rglob("*") if path.is_file()}
            with patch("reconcile_index.locks.verify", return_value=verified), patch("reconcile_index.locks.import_upstream"), \
                    patch.dict(sys.modules, modules), patch("bridge.owned_process") as process:
                preview = reconcile_index.execute(args)
                self.assertEqual(preview["status"], "preview")
                self.assertEqual({path: path.read_bytes() for path in args.run_dir.rglob("*") if path.is_file()}, before)
                args.apply, args.expected_preview_sha256 = True, preview["preview_sha256"]
                result = reconcile_index.execute(args)
                process.assert_not_called()
            self.assertEqual(result["status"], "applied")
            current = json.loads(index.read_bytes())
            self.assertEqual(current["status"], "completed")
            self.assertEqual(current["elapsed_ms"], 1234)
            self.assertEqual(current["host_invocations"], 2)
            self.assertEqual(current["index_admission"]["usage_missing"], 1)
            self.assertEqual(current["index_admission"]["resolved_failures"][0]["completed_ordinal"], 2)
            for path, raw in before.items():
                if path != index:
                    self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(len(list((args.run_dir / "reconciliations").glob("*.json"))), 1)

    def test_active_owner_and_changed_preview_block_apply_without_writes(self):
        for condition in ("owner", "cas"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temporary:
                args, verified, modules, index = self.fixture(Path(temporary))
                original = index.read_bytes()
                with patch("reconcile_index.locks.verify", return_value=verified), patch("reconcile_index.locks.import_upstream"), patch.dict(sys.modules, modules):
                    preview = reconcile_index.execute(args)
                    args.apply, args.expected_preview_sha256 = True, preview["preview_sha256"]
                    if condition == "owner":
                        with (args.run_dir / ".owner.lock").open("rb") as owner:
                            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            with self.assertRaises(BlockingIOError):
                                reconcile_index.execute(args)
                    else:
                        target = args.run_dir / "full/store/docs/pi-synthetic/doc.json"
                        value = json.loads(target.read_bytes())
                        value["description"] = "Changed metadata after review"
                        write_json(target, value)
                        with self.assertRaisesRegex(ValueError, "Reviewed preview differs"):
                            reconcile_index.execute(args)
                self.assertEqual(index.read_bytes(), original)
                self.assertFalse((args.run_dir / "reconciliations").exists())

    def test_rejections_ambiguous_source_extraction_and_frozen_changes_block(self):
        for condition in ("input_limit", "rejection", "duplicate", "pages", "bounds", "frozen", "plan", "source", "unresolved"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temporary:
                args, verified, modules, index = self.fixture(Path(temporary))
                if condition in ("input_limit", "rejection"):
                    record = json.loads(index.read_bytes())
                    if condition == "input_limit":
                        record["error"] = "Adapter rejected indexing prompts; full index is incomplete"
                    else:
                        record["rejections"] = [{"code": "input_limit"}]
                    checkpoint_attempt(index, record)
                elif condition == "duplicate":
                    source = args.run_dir / "full/store/docs/pi-synthetic"
                    other = source.with_name("pi-duplicate")
                    shutil.copytree(source, other)
                    write_json(other / "doc.json", {"id": "pi-duplicate", "name": "source_1.pdf", "status": "completed", "mode": "flash", "pageNum": 1})
                elif condition == "pages":
                    write_json(args.run_dir / "full/store/docs/pi-synthetic/pages.json", [{"page_index": 1, "markdown": "Changed source text"}])
                elif condition == "bounds":
                    write_json(args.run_dir / "full/store/docs/pi-synthetic/tree.json", [{"node_id": "n", "start_index": 1, "end_index": 2}])
                elif condition == "frozen":
                    with (args.frozen_adapter_dir / "run.py").open("a") as output:
                        output.write("# changed\n")
                elif condition == "plan":
                    args.plan_sha256 = "changed"
                elif condition == "source":
                    (args.benchmark / "documents/source.pdf").write_bytes(b"Changed PDF")
                else:
                    ledger = args.run_dir / "host-calls.jsonl"
                    calls = [json.loads(line) for line in ledger.read_bytes().splitlines()]
                    calls[0]["error_code"] = "host_identity"
                    ledger.write_bytes(b"".join(canonical(call) + b"\n" for call in calls))
                before = index.read_bytes()
                with patch("reconcile_index.locks.verify", return_value=verified), patch("reconcile_index.locks.import_upstream"), \
                        patch.dict(sys.modules, modules), patch("bridge.owned_process") as process:
                    with self.assertRaises(ValueError):
                        reconcile_index.execute(args)
                    process.assert_not_called()
                self.assertEqual(index.read_bytes(), before)
                self.assertFalse((args.run_dir / "reconciliations").exists())


if __name__ == "__main__":
    unittest.main()
