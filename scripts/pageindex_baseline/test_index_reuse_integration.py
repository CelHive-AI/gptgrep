"""Synthetic full-cohort index import/replay integration; zero inference requests."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"

from index_admission import TEXT_SCHEMA, canonical, digest_bytes
from index_import import CRITICAL_ADAPTERS
from run import checkpoint_attempt, execute, fingerprint
import profiles
import test_index_admission as fixtures


class IndexReuseIntegrationTests(unittest.TestCase):
    def test_import_one_ready_index_replay_partial_index_and_execute_only_new_large_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original, verified, modules, ready_index = fixtures.ReconciliationTests().fixture(root)
            consumer_dir = root / "consumer"
            original_plan = json.loads((original.run_dir / "plan.json").read_bytes())
            extra = original.benchmark / "documents/incomplete.pdf"
            extra.write_bytes(b"Second synthetic PDF")
            rows = [{"doc_id": "source.pdf" if number % 2 == 0 else "incomplete.pdf", "question": f"synthetic question {number}",
                     "answer": "synthetic gold", "answer_format": "text", "evidence_pages": "[]"} for number in range(62)]
            fixtures.write_json(original.benchmark / "questions.json", rows)
            fixtures.write_json(original.benchmark / "documents.json", [{"doc_id": name, "pages": 1} for name in ("source.pdf", "incomplete.pdf")])
            args = SimpleNamespace(stage="index", variant="full", rows="all", upstream=original.upstream,
                benchmark=original.benchmark, judge_source=original.judge_source, run_dir=consumer_dir, binary=original.binary,
                codex_bin="never-executed", codex_home=root, model="synthetic-reader", reasoning_effort="max",
                profile="matched-luna-max", index_model="synthetic-model", index_reasoning_effort="medium",
                judge_model="synthetic-judge", judge_reasoning_effort="high", service_tier="fast", max_model_calls=10,
                index_host_concurrency=64, reader_concurrency=5, judge_concurrency=5, max_input_bytes=1048576,
                timeout=180, max_turns=10, capability_receipt=None, retry_failed=False,
                index_origin_run=original.run_dir, index_origin_plan_sha256=None,
                index_origin_frozen_adapter_dir=original.frozen_adapter_dir, index_origin_binary=original.binary)
            profile = profiles.resolve(args)
            for name in CRITICAL_ADAPTERS:
                raw = (Path(__file__).parent / name).read_bytes()
                (original.frozen_adapter_dir / name).write_bytes(raw)
                original_plan["adapter_files"][name] = digest_bytes(raw)
            original_plan.update(profile=profile, model=args.model, reasoning_effort=args.reasoning_effort,
                source_rows=list(range(62)), full_62_task_scope=True, cohort_manifest_sha256="synthetic-cohort",
                question_sha256=fingerprint([{"source_row": number, **row} for number, row in enumerate(rows)]),
                source_hashes={name: digest_bytes((original.benchmark / "documents" / name).read_bytes()) for name in ("source.pdf", "incomplete.pdf")},
                host_timeout_secs=180, python=platform.python_version(), sdk_max_turns=args.max_turns,
                qa_stage_strategy="readers_then_judges")
            fixtures.write_json(original.run_dir / "plan.json", original_plan)
            args.index_origin_plan_sha256 = digest_bytes((original.run_dir / "plan.json").read_bytes())
            calls = [fixtures.invocation(original.run_dir, 1, "failed"), fixtures.invocation(original.run_dir, 2, "completed"),
                     fixtures.invocation(original.run_dir, 3, "completed", phase="index:full:incomplete.pdf")]
            for call in calls:
                request_path = original.run_dir / "calls" / f"{call['ordinal']:05d}.request.json"
                request = json.loads(request_path.read_bytes())
                request["instructions"] = "Return the assistant response to the supplied conversation exactly as requested, inside the text field."
                raw = canonical(request)
                request_path.write_bytes(raw)
                call.update(request_sha256=digest_bytes(raw), instructions_sha256=digest_bytes(request["instructions"].encode()))
                fixtures.write_json(original.run_dir / "calls" / f"{call['ordinal']:05d}.attempt.json", call)
            (original.run_dir / "host-calls.jsonl").write_bytes(b"".join(canonical(call) + b"\n" for call in calls))
            def cache_key(source):
                return fingerprint({"source": original_plan["source_hashes"][source], "variant": "full",
                    "upstream": verified["pageindex_revision"], "model": args.model, "effort": args.reasoning_effort,
                    "input_cap": 262144, "adapter": original_plan["adapter_files"], "dependencies": verified["dependencies"],
                    "all_role_profiles": profile, "host_concurrency_by_role": original_plan["host_concurrency_by_role"],
                    "host_binary_sha256": original_plan["host_binary_sha256"]})
            doc = original.run_dir / "full/store/docs/pi-synthetic"
            tree, pages = (json.loads((doc / name).read_bytes()) for name in ("tree.json", "pages.json"))
            ready = json.loads(ready_index.read_bytes())
            ready.update(status="completed", cache_key=cache_key("source.pdf"), doc_id="pi-synthetic", name="source.pdf", tree=tree,
                         tree_sha256=fingerprint(tree), stored_page_count=1, stored_pages_sha256=fingerprint(pages))
            ready.pop("error")
            checkpoint_attempt(ready_index, ready)
            incomplete = original.run_dir / "full" / (fingerprint("incomplete.pdf")[:16] + ".index.json")
            checkpoint_attempt(incomplete, {"variant": "full", "source": "incomplete.pdf", "source_sha256": original_plan["source_hashes"]["incomplete.pdf"],
                "cache_key": cache_key("incomplete.pdf"), "status": "failed", "host_invocations": 1, "host_call_ordinals": [3],
                "error": "Adapter rejected indexing prompts; full index is incomplete", "rejections": [{"code": "input_limit"}]})
            submissions = []
            class Client:
                def __init__(self, mode, storage_path, **_options):
                    self.store = Path(storage_path)
                def submit_document(self, source_path, **_options):
                    import litellm
                    from transports import PROVIDER, INDEX_ALIAS
                    submissions.append(Path(source_path).name)
                    provider = next(entry["custom_handler"] for entry in litellm.custom_provider_map if entry["provider"] == PROVIDER)
                    selected = []
                    for content in ("synthetic", "x" * 350000):
                        reply = provider.completion(INDEX_ALIAS, [{"role": "user", "content": content}])
                        selected.append(reply.choices[0].message.content)
                    destination = self.store / "docs/pi-new"
                    fixtures.write_json(destination / "doc.json", {"id": "pi-new", "name": Path(source_path).name, "status": "completed", "mode": "flash", "pageNum": 1})
                    fixtures.write_json(destination / "tree.json", [{"node_id": "new-node", "start_index": 1, "end_index": 1, "summary": "\n".join(selected)}])
                    fixtures.write_json(destination / "pages.json", pages)
                    return {"doc_id": "pi-new", "name": Path(source_path).name}
                def get_document_structure(self, doc_id):
                    return json.loads((self.store / "docs" / doc_id / "tree.json").read_bytes())
                def get_ocr(self, doc_id, **_options):
                    return {"result": json.loads((self.store / "docs" / doc_id / "pages.json").read_bytes())}
            modules["pageindex"].PageIndexClient = Client
            def process(arguments, cwd, timeout, *, cancelled, on_start):
                request = json.loads(Path(arguments[arguments.index("--input") + 1]).read_bytes())
                self.assertEqual(arguments[arguments.index("--max-input-bytes") + 1], "1048576")
                self.assertGreater(len(canonical(request)), 262144)
                on_start(990001)
                report = {"status": "completed", "model": "synthetic-model", "requested_reasoning_effort": "medium",
                          "effective_reasoning_effort": "medium", "requested_service_tier": "fast", "effective_service_tier": "priority",
                          "auth_mode": "chatgpt", "model_provider": "openai", "thread_id": "new-synthetic-thread", "turn_id": "synthetic-turn",
                          "value": {"text": "new synthetic summary"}, "usage": {"total_tokens": 11}}
                return subprocess.CompletedProcess(arguments, 0, canonical(report), b"")
            before = {path: path.read_bytes() for path in original.run_dir.rglob("*") if path.is_file()}
            groups = {"development8": set(range(8)), "heldout54": set(range(8, 62))}
            with patch.dict(sys.modules, modules), patch("run.locks.verify", return_value=verified), patch("run.locks.import_upstream"), \
                    patch("run.cohorts.load", return_value=(groups, "synthetic-cohort")), patch("bridge.owned_process", side_effect=process) as model:
                result = execute(args)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(submissions, ["incomplete.pdf"])
            self.assertEqual(model.call_count, 1)
            self.assertEqual(result["host_invocations"], 1)
            self.assertEqual(result["imported_index_count"], 1)
            self.assertEqual(result["replayed_index_count"], 1)
            self.assertEqual(result["historical_index_origin"]["origin_invocation_slots"], 3)
            replayed = next(item for item in result["indexes"] if item["source"] == "incomplete.pdf")
            self.assertEqual(replayed["index_replay"]["unique_reused_producers"], 1)
            self.assertEqual(replayed["index_replay"]["live_misses_this_session"], 1)
            self.assertEqual(replayed["index_replay"]["new_host_invocations_by_replay"], 0)
            self.assertTrue(replayed["index_admission"]["complete"])
            self.assertEqual(result["answers"], [])
            for path, raw in before.items():
                self.assertEqual(path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
