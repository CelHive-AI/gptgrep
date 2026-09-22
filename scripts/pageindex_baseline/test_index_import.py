"""Offline declared-origin validation and completed SDK artifact imports."""
import copy
import fcntl
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from index_admission import canonical, digest_bytes
from index_import import CRITICAL_ADAPTERS, IndexOrigin, origin_declaration
from run import checkpoint_attempt
import test_index_admission as fixtures


class IndexImportTests(unittest.TestCase):
    def fixture(self, root):
        args, verified, modules, index = fixtures.ReconciliationTests().fixture(root)
        plan = json.loads((args.run_dir / "plan.json").read_bytes())
        plan.update(full_62_task_scope=True, source_rows=list(range(62)), question_sha256="synthetic-questions",
                    cohort_manifest_sha256="synthetic-cohort", host_timeout_secs=180, sdk_max_turns=10,
                    python="3.13.5", qa_stage_strategy="readers_then_judges")
        for name in CRITICAL_ADAPTERS:
            raw = ("synthetic source " + name).encode()
            (args.frozen_adapter_dir / name).write_bytes(raw)
            plan["adapter_files"][name] = digest_bytes(raw)
        fixtures.write_json(args.run_dir / "plan.json", plan)
        record = json.loads(index.read_bytes())
        cache = {"source": record["source_sha256"], "variant": "full", "upstream": verified["pageindex_revision"],
                 "model": plan["model"], "effort": plan["reasoning_effort"], "input_cap": plan["host_input_cap"],
                 "adapter": plan["adapter_files"], "dependencies": verified["dependencies"], "all_role_profiles": plan["profile"],
                 "host_concurrency_by_role": plan["host_concurrency_by_role"], "host_binary_sha256": plan["host_binary_sha256"]}
        doc = args.run_dir / "full/store/docs/pi-synthetic"
        tree, pages = (json.loads((doc / name).read_bytes()) for name in ("tree.json", "pages.json"))
        record.update(status="completed", cache_key=digest_bytes(canonical(cache)), doc_id="pi-synthetic", name="source.pdf",
                      tree=tree, tree_sha256=digest_bytes(canonical(tree)), stored_page_count=1,
                      stored_pages_sha256=digest_bytes(canonical(pages)))
        record.pop("error")
        checkpoint_attempt(index, record)
        runtime = SimpleNamespace(index_origin_run=args.run_dir,
                                  index_origin_plan_sha256=digest_bytes((args.run_dir / "plan.json").read_bytes()),
                                  index_origin_frozen_adapter_dir=args.frozen_adapter_dir, index_origin_binary=args.binary)
        consumer = copy.deepcopy(plan)
        consumer["host_input_cap"] = 1048576
        consumer["adapter_files"]["run.py"] = "new-declared-admission-integration"
        return args, runtime, consumer, modules, index

    def test_import_is_zero_model_retains_origin_costs_and_has_no_fresh_build_timing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, runtime, consumer, modules, _index = self.fixture(root)
            declaration = origin_declaration(runtime, consumer)
            before = {path: path.read_bytes() for path in args.run_dir.rglob("*") if path.is_file()}
            origin = IndexOrigin(declaration, consumer)
            try:
                with patch.dict(sys.modules, modules), patch("bridge.owned_process") as process:
                    imported = origin.import_completed("source.pdf", args.benchmark / "documents/source.pdf", 1, root / "consumer", "new-cache-key")
                    again = origin.import_completed("source.pdf", args.benchmark / "documents/source.pdf", 1, root / "consumer", "new-cache-key")
                    process.assert_not_called()
                self.assertEqual(imported["status"], "completed")
                self.assertEqual(imported["host_invocations"], 0)
                self.assertEqual(imported["host_call_ordinals"], [])
                self.assertIsNone(imported["elapsed_ms"])
                self.assertEqual(imported["origin_index_elapsed_ms"], 1234)
                self.assertEqual(imported["origin_index_admission"]["usage_missing"], 1)
                self.assertEqual(imported["index_import"]["origin_call_ordinals"], [1, 2])
                self.assertEqual(imported["index_import"]["origin_id"], origin.summary()["origin_id"])
                self.assertEqual(again["tree_sha256"], imported["tree_sha256"])
                self.assertEqual(origin.summary()["origin_invocation_slots"], 2)
                self.assertEqual(origin.summary()["origin_failed_or_interrupted_invocations"], 1)
                self.assertIsNone(origin.summary()["billing_usd"])
                self.assertFalse(declaration["qa_reuse"])
                self.assertFalse(declaration["judge_reuse"])
                for path, raw in before.items():
                    self.assertEqual(path.read_bytes(), raw)
            finally:
                origin.close()

    def test_changed_declared_conditions_and_nested_origins_block(self):
        for condition in ("smaller_cap", "too_large_cap", "model", "binary", "transport", "source", "cohort", "sdk_max_turns", "python", "qa_stage_strategy", "nested"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temporary:
                args, runtime, consumer, _modules, _index = self.fixture(Path(temporary))
                if condition == "smaller_cap":
                    consumer["host_input_cap"] = 131072
                elif condition == "too_large_cap":
                    consumer["host_input_cap"] = 1048577
                elif condition == "model":
                    consumer["profile"]["roles"]["index"]["model"] = "different-model"
                elif condition == "binary":
                    consumer["host_binary_sha256"] = "different-binary"
                elif condition == "transport":
                    consumer["adapter_files"]["transports.py"] = "different-transport"
                elif condition == "source":
                    consumer["source_hashes"]["source.pdf"] = "different-source"
                elif condition == "cohort":
                    consumer["source_rows"] = [0]
                elif condition == "sdk_max_turns":
                    consumer["sdk_max_turns"] = 20
                elif condition == "python":
                    consumer["python"] = "3.14.0"
                elif condition == "qa_stage_strategy":
                    consumer["qa_stage_strategy"] = "interleaved"
                else:
                    plan_path = args.run_dir / "plan.json"
                    plan = json.loads(plan_path.read_bytes())
                    plan["index_origin"] = {"policy": "another-origin"}
                    fixtures.write_json(plan_path, plan)
                    runtime.index_origin_plan_sha256 = digest_bytes(plan_path.read_bytes())
                with self.assertRaises(ValueError):
                    origin_declaration(runtime, consumer)

    def test_active_origin_cannot_be_used(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, runtime, consumer, _modules, _index = self.fixture(Path(temporary))
            declaration = origin_declaration(runtime, consumer)
            with (args.run_dir / ".owner.lock").open("rb") as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    IndexOrigin(declaration, consumer)

    def test_incomplete_indexes_are_left_for_exact_request_replay_and_live_misses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, runtime, consumer, _modules, index = self.fixture(root)
            value = json.loads(index.read_bytes())
            value.update(status="failed", error="Adapter rejected indexing prompts; full index is incomplete",
                         rejections=[{"code": "input_limit", "host_invoked": False}])
            checkpoint_attempt(index, value)
            origin = IndexOrigin(origin_declaration(runtime, consumer), consumer)
            try:
                self.assertIsNone(origin.import_completed("source.pdf", args.benchmark / "documents/source.pdf", 1, root / "consumer", "new-cache"))
                self.assertFalse((root / "consumer").exists())
                self.assertEqual(len(origin.phase_calls("source.pdf")), 2)
            finally:
                origin.close()

    def test_tampered_ready_artifact_is_not_overwritten_or_rebuilt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, runtime, consumer, modules, _index = self.fixture(root)
            origin = IndexOrigin(origin_declaration(runtime, consumer), consumer)
            try:
                with patch.dict(sys.modules, modules):
                    origin.import_completed("source.pdf", args.benchmark / "documents/source.pdf", 1, root / "consumer", "new-cache")
                    target = root / "consumer/full/store/docs/pi-synthetic/tree.json"
                    target.write_bytes(b"changed")
                    with self.assertRaisesRegex(ValueError, "never overwrite"):
                        origin.import_completed("source.pdf", args.benchmark / "documents/source.pdf", 1, root / "consumer", "new-cache")
                    self.assertEqual(target.read_bytes(), b"changed")
            finally:
                origin.close()


if __name__ == "__main__":
    unittest.main()
