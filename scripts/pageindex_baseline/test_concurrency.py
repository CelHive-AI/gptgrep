"""Offline races, durable caps, owned-process cleanup and actual async dispatch."""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge import AdapterError, LocalCodex, json_bytes, owned_process
from role_hosts import RoleHost
from run import timing_fields
import profiles


class FakeProcess:
    """Synthetic process boundary; never launches Codex or an inference request."""
    def __init__(self, barrier=None, fail=(), slow_first=False, effective_tier="priority"):
        self.barrier, self.fail, self.slow_first = barrier, set(fail), slow_first
        self.effective_tier = effective_tier
        self.seen = []
        self.lock = threading.Lock()

    def __call__(self, arguments, cwd, timeout, *, cancelled=None, on_start=None):
        request_path = Path(arguments[arguments.index("--input") + 1])
        ordinal = int(request_path.name.split(".", 1)[0])
        request = json.loads(request_path.read_bytes())
        model = arguments[arguments.index("--model") + 1]
        effort = arguments[arguments.index("--reasoning-effort") + 1]
        tier = arguments[arguments.index("--service-tier") + 1]
        with self.lock:
            self.seen.append((ordinal, request, model, effort))
        if on_start:
            on_start(900000 + ordinal)
        trace = Path(arguments[arguments.index("--protocol-trace") + 1])
        trace.write_bytes(b'{"method":"synthetic/process-boundary"}\n')
        if self.barrier:
            self.barrier.wait(timeout=8)
        time.sleep(0.06 if self.slow_first and ordinal == 1 else 0.005)
        value = request["state"].get("result", {"text": "synthetic summary"})
        report = {"status": "failed" if ordinal in self.fail else "completed", "code": "synthetic_failure",
                  "model": model, "requested_reasoning_effort": effort, "effective_reasoning_effort": effort,
                  "requested_service_tier": tier, "effective_service_tier": self.effective_tier,
                  "auth_mode": "chatgpt", "model_provider": "openai", "thread_id": f"synthetic-{ordinal}",
                  "turn_id": "synthetic-turn", "usage": {"total_tokens": ordinal * 11}, "value": value}
        return subprocess.CompletedProcess(arguments, 2 if ordinal in self.fail else 0, json_bytes(report), b"")


def host_at(root: Path, cap=20, concurrency=4):
    return LocalCodex(root / "never-executed", "never-executed", root, root, cap,
                      host_concurrency=concurrency)


class ConcurrencyTests(unittest.TestCase):
    def test_distinct_role_phase_schema_bindings_overlap_without_mutating_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = host_at(root, concurrency=2)
            index = RoleHost(host, "index", {"model": "synthetic-index", "reasoning_effort": "medium"})
            judge = RoleHost(host, "judge", {"model": "synthetic-judge", "reasoning_effort": "high", "service_tier": "priority"})
            index.phase, judge.phase = "index:synthetic-a", "judge:synthetic-b"
            schemas = [{"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
                       {"type": "object", "properties": {"accepted": {"type": "boolean"}}, "required": ["accepted"], "additionalProperties": False}]
            fake = FakeProcess(threading.Barrier(2))
            try:
                with patch("bridge.owned_process", fake), ThreadPoolExecutor(max_workers=2) as workers:
                    one = workers.submit(index.complete, "index instruction", {"result": {"text": "a"}}, schemas[0])
                    two = workers.submit(judge.complete, "judge instruction", {"result": {"accepted": True}}, schemas[1])
                    self.assertEqual(one.result()[0], {"text": "a"})
                    self.assertEqual(two.result()[0], {"accepted": True})
                self.assertEqual((host.model, host.effort, host.phase), ("gpt-5.6-luna", "max", "unselected"))
                by_phase = {item["phase"]: item for item in host.calls}
                self.assertEqual(by_phase["index:synthetic-a"]["requested_model"], "synthetic-index")
                self.assertEqual(by_phase["judge:synthetic-b"]["requested_effort"], "high")
                self.assertEqual(by_phase["index:synthetic-a"]["requested_service_tier"], "fast")
                self.assertEqual(by_phase["judge:synthetic-b"]["requested_service_tier"], "priority")
                self.assertNotEqual(host.calls[0]["schema_sha256"], host.calls[1]["schema_sha256"])
                self.assertEqual(host.concurrency_report()["measured_peak"], 2)
                self.assertGreater(host.concurrency_report()["overlap_ms"], 0)
                self.assertTrue(all(item["protocol_trace_status"] == "available" for item in host.calls))
            finally:
                host.close(cancel=True)

    def test_concurrent_reservations_respect_cumulative_cap_and_keep_failure_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = host_at(root, cap=3, concurrency=4)
            fake = FakeProcess(threading.Barrier(3), fail={2})
            try:
                with patch("bridge.owned_process", fake), ThreadPoolExecutor(max_workers=10) as workers:
                    futures = [workers.submit(host.complete, "synthetic", {}, {"type": "object"},
                                              phase=f"index:synthetic-{number}", role="index") for number in range(10)]
                    errors = []
                    for future in futures:
                        try:
                            future.result()
                        except AdapterError as error:
                            errors.append(error.code)
                self.assertEqual([item["ordinal"] for item in host.calls], [1, 2, 3])
                self.assertEqual(len(fake.seen), 3)
                self.assertEqual(errors.count("call_budget"), 7)
                self.assertEqual(errors.count("synthetic_failure"), 1)
                self.assertEqual(host.calls[1]["status"], "failed")
                self.assertEqual(host.calls[1]["usage"], {"total_tokens": 22})
                self.assertFalse(host.calls[1]["accounting_complete"])
                self.assertEqual(len(list((root / "calls").glob("*.attempt.json"))), 3)
            finally:
                host.close(cancel=True)
            resumed = host_at(root, cap=3)
            try:
                with self.assertRaisesRegex(AdapterError, "budget"):
                    resumed.complete("no replay", {}, {})
            finally:
                resumed.close(cancel=True)

    def test_out_of_order_completion_and_missing_middle_receipt_recover_in_ordinal_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = host_at(root, cap=4, concurrency=2)
            try:
                with patch("bridge.owned_process", FakeProcess(threading.Barrier(2), slow_first=True)), ThreadPoolExecutor(max_workers=2) as workers:
                    futures = [workers.submit(host.complete, "synthetic", {}, {}, role="index") for _ in range(2)]
                    for future in futures:
                        future.result()
                written = [json.loads(line)["ordinal"] for line in host.ledger.read_text().splitlines()]
                self.assertEqual(written, [2, 1])
                for ordinal in (3, 4):
                    (root / "calls" / f"{ordinal:05d}.request.json").write_bytes(b"{}")
                    host.start_attempt({"ordinal": ordinal, "phase": "index:synthetic", "host_invoked": True})
                host._append({"ordinal": 4, "phase": "index:synthetic", "status": "completed", "usage": {"total_tokens": 44}})
            finally:
                host.close(cancel=True)
            resumed = host_at(root, cap=4)
            try:
                self.assertEqual([item["ordinal"] for item in resumed.calls], [1, 2, 3, 4])
                self.assertEqual(resumed.calls[2]["status"], "interrupted")
                self.assertIsNone(resumed.calls[2]["usage"])
                self.assertEqual(resumed.calls[3]["usage"], {"total_tokens": 44})
                with self.assertRaisesRegex(AdapterError, "budget"):
                    resumed.complete("no replay", {}, {})
            finally:
                resumed.close(cancel=True)

    def test_external_native_attempt_advances_next_judge_ordinal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = host_at(root, cap=2, concurrency=1)
            try:
                (root / "calls/00001.request.json").write_bytes(b"{}")
                host.start_attempt({"ordinal": 1, "phase": "answer:native", "host_invoked": True})
                host._append({"ordinal": 1, "phase": "answer:native", "status": "completed"})
                judge = RoleHost(host, "judge", {"model": "synthetic-judge", "reasoning_effort": "high"})
                with patch("bridge.owned_process", FakeProcess()):
                    judge.complete("synthetic", {}, {"type": "object"})
                self.assertEqual([item["ordinal"] for item in host.calls], [1, 2])
                self.assertEqual(host.calls[1]["requested_effort"], "high")
            finally:
                host.close(cancel=True)

    def test_reader_and_judge_each_keep_serial_ceiling(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = host_at(Path(temporary), cap=8, concurrency=4)
            try:
                with patch("bridge.owned_process", FakeProcess()), ThreadPoolExecutor(max_workers=8) as workers:
                    futures = [workers.submit(host.complete, "synthetic", {}, {}, role=role)
                               for role in ("chat", "judge") for _ in range(4)]
                    for future in futures:
                        future.result()
                for role in ("chat", "judge"):
                    self.assertEqual(host.concurrency_report()["by_role"][role]["measured_peak"], 1)
            finally:
                host.close(cancel=True)

    def test_owned_process_cancellation_reaps_only_its_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            event = threading.Event()
            timer = threading.Timer(0.05, event.set)
            timer.start()
            try:
                with self.assertRaises(AdapterError) as raised:
                    owned_process([sys.executable, "-c", "import time;time.sleep(5)"], Path(temporary), 2,
                                  cancelled=event.is_set)
                self.assertEqual(raised.exception.code, "host_cancelled")
                self.assertTrue(raised.exception.cleanup["reaped"])
                self.assertTrue(raised.exception.cleanup["term_sent"])
            finally:
                timer.join()

    def test_profile_defaults_and_explicit_legacy_are_distinct(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--model")
        parser.add_argument("--reasoning-effort")
        profiles.add_arguments(parser)
        current = profiles.resolve(parser.parse_args([]))
        self.assertEqual(current["roles"]["index"]["reasoning_effort"], "medium")
        self.assertEqual(current["roles"]["chat"]["reasoning_effort"], "max")
        self.assertEqual(current["roles"]["judge"]["reasoning_effort"], "high")
        self.assertIsNone(current["source_default"]["index"]["reasoning_effort"])
        self.assertEqual(current["requested_service_tier"], "fast")
        self.assertTrue(all(role["service_tier"] == "fast" for role in current["roles"].values()))
        legacy = profiles.resolve(parser.parse_args(["--profile", "legacy-luna-max-control"]))
        self.assertEqual(legacy["roles"]["index"]["reasoning_effort"], "max")

    def test_reported_tier_alias_missing_and_mismatch_remain_distinct(self):
        for actual in ("priority", None, "default"):
            with self.subTest(actual=actual), tempfile.TemporaryDirectory() as temporary:
                host = host_at(Path(temporary), cap=1)
                try:
                    with patch("bridge.owned_process", FakeProcess(effective_tier=actual)):
                        if actual == "default":
                            with self.assertRaises(AdapterError) as raised:
                                host.complete("synthetic", {}, {})
                            self.assertEqual(raised.exception.code, "host_service_tier")
                        else:
                            host.complete("synthetic", {}, {})
                    self.assertEqual(host.calls[0]["requested_service_tier"], "fast")
                    self.assertEqual(host.calls[0]["effective_service_tier"], actual)
                    self.assertEqual(host.calls[0]["usage"], {"total_tokens": 11})
                finally:
                    host.close(cancel=True)

    def test_missing_outer_duration_is_not_a_measured_zero(self):
        values = [{"elapsed_ms": 2.5, "reported_host_elapsed_ms": None}, {"status": "interrupted"}]
        self.assertEqual(timing_fields(values), {"wall_ms": None, "wall_ms_known_subtotal": 2.5, "wall_ms_missing": 1})
        self.assertEqual(timing_fields(values, "inner", "reported_host_elapsed_ms"),
                         {"inner": None, "inner_known_subtotal": 0.0, "inner_missing": 2})


class AsyncConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_litellm_async_dispatch_exceeds_small_default_executor(self):
        import litellm
        from transports import INDEX_ALIAS, PROVIDER, register_index_provider
        with tempfile.TemporaryDirectory() as temporary:
            host = host_at(Path(temporary), cap=20, concurrency=20)
            index = RoleHost(host, "index", {"model": "gpt-5.6-luna", "reasoning_effort": "medium"})
            index.phase = "index:synthetic-parallel"
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
            fake = FakeProcess(threading.Barrier(20))
            try:
                register_index_provider(index)
                with patch("bridge.owned_process", fake):
                    values = await asyncio.gather(*(litellm.acompletion(
                        model=PROVIDER + "/" + INDEX_ALIAS, messages=[{"role": "user", "content": "synthetic"}],
                        max_retries=0) for _ in range(20)))
                self.assertEqual(len(values), 20)
                self.assertTrue(all(value.choices[0].message.content == "synthetic summary" for value in values))
                self.assertEqual(host.concurrency_report()["measured_peak"], 20)
                self.assertEqual(len({item["ordinal"] for item in host.calls}), 20)
            finally:
                host.close(cancel=True)

    async def test_async_cancellation_drains_its_attempt_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = host_at(Path(temporary), concurrency=2)
            started = threading.Event()
            def pending(arguments, cwd, timeout, *, cancelled, on_start):
                on_start(900001)
                started.set()
                while not cancelled():
                    time.sleep(0.002)
                raise AdapterError("host_cancelled", "synthetic cancellation", 401)
            try:
                with patch("bridge.owned_process", pending):
                    task = asyncio.create_task(host.acomplete("synthetic", {}, {}, phase="index:cancel", role="index"))
                    self.assertTrue(await asyncio.to_thread(started.wait, 3))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertEqual(len(host.calls), 1)
                self.assertEqual(host.calls[0]["status"], "interrupted")
                self.assertEqual(host.calls[0]["error_code"], "host_cancelled")
                self.assertFalse(host.calls[0]["accounting_complete"])
                self.assertEqual(host.concurrency_report()["completed_intervals"], 1)
            finally:
                host.close(cancel=True)


if __name__ == "__main__":
    unittest.main()
