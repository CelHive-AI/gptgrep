"""Offline concurrent case attribution, durable native reservations and stage order."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bridge import AdapterError, LocalCodex, json_bytes
from role_hosts import RoleHost
from run import checkpoint_attempt, judge_case, qa_stage_summary, read_case, run_case_pool, task_concurrency_report


PROFILE = {"roles": {"chat": {"model": "synthetic-reader", "reasoning_effort": "max"},
                     "judge": {"model": "synthetic-judge", "reasoning_effort": "high"}}}
CONSTANTS = {"PROMPT": "Question: {question}\nExpected: {answer}\nFormat: {answer_format}\nResponse: {response}",
             "SCHEMA": {"type": "object", "properties": {"equivalent": {"type": "boolean"}, "abstained": {"type": "boolean"}},
                        "required": ["equivalent", "abstained"], "additionalProperties": False},
             "MAX_RESPONSE_CHARS": 10000, "EFFORT": "high"}


def host_at(root, cap=100):
    return LocalCodex(root / "never-executed", "never-executed", root, root, cap,
                      reader_concurrency=5, judge_concurrency=5)


class SyntheticProcess:
    def __init__(self, concurrency=5):
        self.barrier = threading.Barrier(concurrency)
        self.seen = []
        self.lock = threading.Lock()

    def __call__(self, arguments, cwd, timeout, *, cancelled=None, on_start=None):
        request_path = Path(arguments[arguments.index("--input") + 1])
        request = json.loads(request_path.read_bytes())
        number = int(request_path.name.split(".")[0])
        with self.lock:
            self.seen.append(request)
        on_start(800000 + number)
        self.barrier.wait(timeout=5)
        time.sleep(.005)
        model = arguments[arguments.index("--model") + 1]
        effort = arguments[arguments.index("--reasoning-effort") + 1]
        if model == "synthetic-reader":
            value = {"text": "Response " + request["state"]["question"]}
        else:
            value = {"equivalent": False, "abstained": False}
        report = {"status": "completed", "model": model, "requested_reasoning_effort": effort,
                  "effective_reasoning_effort": effort, "requested_service_tier": "fast", "effective_service_tier": "priority",
                  "auth_mode": "chatgpt", "model_provider": "openai", "thread_id": f"synthetic-{number}",
                  "turn_id": "synthetic-turn", "usage": {"total_tokens": 11}, "value": value}
        return subprocess.CompletedProcess(arguments, 0, json_bytes(report), b"")


class CaseConcurrencyTests(unittest.TestCase):
    def crashed_judge(self, root):
        host = host_at(root)
        answer = {"status": "completed", "source_row": 0, "variant": "full", "doc_id": "source.txt",
                  "identity": "synthetic-source-bound-case", "question": "synthetic question", "answer": "synthetic gold",
                  "answer_format": "text", "response": "synthetic reader response", "host_call_ordinals": []}
        args = SimpleNamespace(stage="run", retry_failed=False)
        append = host._append
        def crash(receipt):
            append(receipt)
            raise KeyboardInterrupt("synthetic crash after durable judge receipt")
        try:
            with patch("bridge.owned_process", SyntheticProcess(concurrency=1)), patch.object(host, "_append", side_effect=crash):
                with self.assertRaises(KeyboardInterrupt):
                    judge_case(answer, args=args, profile=PROFILE, constants=CONSTANTS, host=host, variant_dir=root / "full")
            self.assertEqual(host.calls[0]["status"], "completed")
            self.assertFalse(json.loads((root / "calls/00001.response.json").read_bytes())["value"]["equivalent"])
            saved = json.loads((root / "full/question-000.json").read_bytes())
            self.assertEqual(saved["judge"]["status"], "interrupted")
            return saved, (root / "host-calls.jsonl").read_bytes()
        finally:
            host.close(cancel=True)

    def test_post_receipt_crash_recovers_false_judge_without_any_retry(self):
        for retry_failed in (False, True):
            with self.subTest(retry_failed=retry_failed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                saved, ledger = self.crashed_judge(root)
                prior_attempts = {path: path.read_bytes() for path in (root / "full/attempts").rglob("*.json")}
                host = host_at(root)
                try:
                    with patch("bridge.owned_process") as process:
                        result = judge_case(saved, args=SimpleNamespace(stage="run", retry_failed=retry_failed), profile=PROFILE,
                                            constants=CONSTANTS, host=host, variant_dir=root / "full")
                        process.assert_not_called()
                    self.assertEqual(result["judge"]["status"], "completed")
                    self.assertFalse(result["judge"]["equivalent"])
                    self.assertTrue(result["judge"]["recovered_completed_judge"])
                    self.assertEqual(result["judge"]["host_call_ordinal"], 1)
                    self.assertEqual(result["judge_stage_disposition"], "reused")
                    self.assertEqual(len(host.calls), 1)
                    self.assertEqual(host.calls[0]["usage"], {"total_tokens": 11})
                    self.assertEqual((root / "host-calls.jsonl").read_bytes(), ledger)
                    for path, raw in prior_attempts.items():
                        self.assertEqual(path.read_bytes(), raw)
                finally:
                    host.close(cancel=True)

    def test_tampered_judge_bindings_block_replacement_calls(self):
        for tamper in ("request", "response", "case", "full_reader_response", "rubric", "schema", "profile", "tier", "phase", "cached_verdict"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                saved, _ledger = self.crashed_judge(root)
                profile, constants = copy.deepcopy(PROFILE), copy.deepcopy(CONSTANTS)
                host = host_at(root)
                try:
                    if tamper == "cached_verdict":
                        saved = judge_case(saved, args=SimpleNamespace(stage="run", retry_failed=True), profile=profile,
                                           constants=constants, host=host, variant_dir=root / "full")
                        saved["judge"]["equivalent"] = True
                    elif tamper in ("request", "response"):
                        target = root / f"calls/00001.{tamper}.json"
                        value = json.loads(target.read_bytes())
                        if tamper == "request":
                            value["instructions"] += " changed"
                        else:
                            value["value"]["equivalent"] = True
                        target.write_bytes(json_bytes(value))
                    elif tamper == "case":
                        saved["identity"] = "different-source-case"
                    elif tamper == "full_reader_response":
                        saved["response"] += " changed"
                    elif tamper == "rubric":
                        constants["PROMPT"] += " changed"
                    elif tamper == "schema":
                        constants["SCHEMA"]["description"] = "changed"
                    elif tamper == "profile":
                        profile["roles"]["judge"]["reasoning_effort"] = "medium"
                    elif tamper == "tier":
                        profile["roles"]["judge"]["service_tier"] = "flex"
                    else:
                        saved["judge"]["host_phase"] = "judge:another-case"
                    with patch("bridge.owned_process") as process:
                        with self.assertRaises(ValueError):
                            judge_case(saved, args=SimpleNamespace(stage="run", retry_failed=True), profile=profile,
                                       constants=constants, host=host, variant_dir=root / "full")
                        process.assert_not_called()
                    self.assertEqual(len(host.calls), 1)
                finally:
                    host.close(cancel=True)

    def test_retained_reader_final_cannot_be_replaced_after_sdk_checkpoint_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = host_at(root)
            row = {"source_row": 0, "doc_id": "source.txt", "question": "synthetic question"}
            args = SimpleNamespace(stage="run", retry_failed=False, max_turns=3, reasoning_effort="max")
            options = {"args": args, "plan": {"profile": PROFILE, "adapter_mode": "synthetic"},
                       "indexes": {"source.txt": {"status": "completed", "cache_key": "synthetic", "doc_id": "synthetic"}},
                       "metadata": {}, "variant": "full", "variant_dir": root / "full", "benchmark": root}
            class HttpClient:
                async def aclose(self):
                    pass
            def factory(role):
                class Client:
                    def chat(self, question, **_options):
                        role.complete("synthetic reader instruction", {"question": question}, {})
                return Client(), SimpleNamespace(wire_receipts=[]), {"http_client": HttpClient()}
            append = host._append
            def crash(receipt):
                append(receipt)
                raise KeyboardInterrupt("synthetic crash before SDK envelope")
            try:
                with patch("bridge.owned_process", SyntheticProcess(concurrency=1)), patch.object(host, "_append", side_effect=crash):
                    with self.assertRaises(KeyboardInterrupt):
                        read_case(row, host=host, client_factory=factory, **options)
            finally:
                host.close(cancel=True)
            resumed = host_at(root)
            try:
                args.retry_failed = True
                with patch("bridge.owned_process") as process, patch("run.benchmark_qualification"):
                    with self.assertRaisesRegex(ValueError, "completed reader final.*refuse replacement"):
                        read_case(row, host=resumed, client_factory=factory, **options)
                    process.assert_not_called()
                self.assertEqual(len(resumed.calls), 1)
                self.assertEqual(resumed.calls[0]["usage"], {"total_tokens": 11})
            finally:
                resumed.close(cancel=True)

    def test_worker_interrupt_cancels_before_next_queued_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = host_at(Path(temporary))
            def worker(number):
                if number == 0:
                    raise KeyboardInterrupt("synthetic first-worker interruption")
                self.assertTrue(host._cancelled.is_set())
                with host.external_attempt({}, phase="answer:queued", model="synthetic-reader", effort="max", service_tier="fast"):
                    self.fail("Cancelled queued work reserved a paid attempt")
            try:
                with patch("bridge.owned_process") as process:
                    with self.assertRaises(KeyboardInterrupt):
                        run_case_pool([0, 1], worker, 1, host, "reader", [])
                    process.assert_not_called()
                self.assertEqual(host.calls, [])
            finally:
                host.close(cancel=True)

    def test_five_readers_then_five_judges_keep_attribution_and_wrong_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "documents").mkdir()
            (root / "documents/source.txt").write_text("Synthetic source only.")
            variant_dir = root / "full"
            variant_dir.mkdir()
            host = host_at(root)
            args = SimpleNamespace(stage="run", retry_failed=False, max_turns=3, reasoning_effort="max")
            indexes = {"source.txt": {"status": "completed", "cache_key": "synthetic-index", "doc_id": "synthetic-doc",
                                       "name": "source.txt", "source_sha256": "synthetic-source", "stored_pages_sha256": "synthetic-pages"}}
            plan = {"profile": PROFILE, "adapter_mode": "synthetic",
                    "source_hashes": {"source.txt": hashlib.sha256((root / "documents/source.txt").read_bytes()).hexdigest()}}
            rows = [{"source_row": number, "doc_id": "source.txt", "question": f"synthetic-question-{number}",
                     "answer": f"synthetic-gold-{number}", "answer_format": "text", "evidence_pages": "[]"} for number in range(5)]
            instances, closed, intervals = [], [], []
            class HttpClient:
                async def aclose(self):
                    closed.append(self)
            def factory(role_host):
                transport = SimpleNamespace(wire_receipts=[])
                class Client:
                    def chat(self, question, **options):
                        value, _ = role_host.complete("synthetic reader instruction", {"question": question},
                                                       {"type": "object", "properties": {"text": {"type": "string"}},
                                                        "required": ["text"], "additionalProperties": False})
                        transport.wire_receipts.append({"phase": role_host.phase, "status": "completed"})
                        return {"items": [], "output": [{"type": "message", "content": [{"type": "output_text", "text": value["text"]}]}]}
                    def get_ocr(self, doc_id, format):
                        return {"result": [{"page_index": 1, "markdown": "Synthetic source only."}]}
                client = Client()
                instances.append((client, transport, role_host))
                return client, transport, {"http_client": HttpClient()}
            def reader(row):
                return read_case(row, args=args, plan=plan, indexes=indexes, metadata={"source.txt": {"pages": 1}},
                                 variant="full", variant_dir=variant_dir, benchmark=root, host=host, client_factory=factory)
            def judge(answer):
                return judge_case(answer, args=args, profile=PROFILE, constants=CONSTANTS, host=host, variant_dir=variant_dir)
            process = SyntheticProcess()
            try:
                with patch("bridge.owned_process", process), patch("run.benchmark_qualification", return_value={"verified": False}):
                    read_results = run_case_pool(rows, reader, 5, host, "reader", intervals)
                    self.assertTrue(all(call["role"] == "chat" for call in host.calls))
                    answers = run_case_pool([answer for answer, _ in read_results], judge, 5, host, "judge", intervals)
                    completed_bytes = {answer["source_row"]: (variant_dir / f"question-{answer['source_row']:03d}.json").read_bytes()
                                       for answer in answers}
                    # Completed false judgments must not be repeated on either ordinary resume or explicit failure retry.
                    args.retry_failed = True
                    resumed = run_case_pool(rows, reader, 5, host, "reader", intervals)
                    answers_again = run_case_pool([answer for answer, _ in resumed], judge, 5, host, "judge", intervals)
                self.assertEqual(len(host.calls), 10)
                self.assertEqual(len(process.seen), 10)
                self.assertEqual(len({id(item[1]) for item in instances}), 10)
                self.assertEqual(len(closed), 10)
                self.assertEqual(host.concurrency_report()["by_role"]["chat"]["measured_peak"], 5)
                self.assertEqual(host.concurrency_report()["by_role"]["judge"]["measured_peak"], 5)
                for answer in answers_again:
                    self.assertEqual(answer["status"], "completed")
                    self.assertEqual(answer["response"], "Response " + answer["question"])
                    self.assertFalse(answer["judge"]["equivalent"])
                    self.assertEqual(len(answer["host_call_ordinals"]), 1)
                    self.assertEqual(len(answer["judge"]["host_call_ordinals"]), 1)
                    read_receipt = host.call_by_ordinal(answer["host_call_ordinals"][0])
                    judge_receipt = host.call_by_ordinal(answer["judge"]["host_call_ordinals"][0])
                    self.assertEqual(read_receipt["phase"], answer["host_phase"])
                    self.assertEqual(judge_receipt["phase"], answer["judge"]["host_phase"])
                    self.assertEqual(judge_receipt["ordinal"], answer["judge"]["host_call_ordinal"])
                    self.assertEqual((variant_dir / f"question-{answer['source_row']:03d}.json").read_bytes(), completed_bytes[answer["source_row"]])
                for request in process.seen[:5]:
                    self.assertNotIn("synthetic-gold", json.dumps(request))
                reader_end = max(call["process_finished_monotonic_ns"] for call in host.calls if call["role"] == "chat")
                judge_start = min(call["process_started_monotonic_ns"] for call in host.calls if call["role"] == "judge")
                self.assertLess(reader_end, judge_start)
                self.assertEqual(task_concurrency_report(intervals)["by_role"]["reader"]["measured_peak"], 5)
                stages = qa_stage_summary(root)
                self.assertEqual(len(stages["records"]), 4)
                self.assertEqual(sum(item["task_dispositions"]["new_attempt"] for item in stages["records"]), 10)
                self.assertEqual(sum(item["task_dispositions"]["reused"] for item in stages["records"]), 10)
                self.assertEqual(stages["wall_ms_missing"], 0)
                self.assertTrue(all(item["status"] == "drained" for item in stages["records"]))
            finally:
                host.close(cancel=True)

    def test_native_external_inflight_reservations_bound_cap_without_cross_talk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = host_at(root, cap=3)
            barrier = threading.Barrier(3)
            def process(arguments, cwd, timeout, *, cancelled, on_start):
                on_start(810000 + int(arguments[1]))
                barrier.wait(timeout=5)
                time.sleep(.005)
                return subprocess.CompletedProcess(arguments, 0, b'{}', b'')
            def invoke(number):
                with host.external_attempt({"case": number}, phase=f"answer:synthetic-{number}", model="synthetic-reader",
                                           effort="max", service_tier="fast") as attempt:
                    self.assertTrue(attempt.request_path.exists())
                    self.assertTrue((root / "calls" / f"{attempt.receipt['ordinal']:05d}.attempt.json").exists())
                    self.assertIsNone(host.call_by_ordinal(attempt.receipt["ordinal"]))
                    attempt.run(["synthetic", str(attempt.receipt["ordinal"])], 5)
                    attempt.receipt.update(status="completed", usage={"total_tokens": number + 1})
                return attempt.receipt
            try:
                with patch("bridge.owned_process", process), ThreadPoolExecutor(max_workers=6) as workers:
                    futures = [workers.submit(invoke, number) for number in range(6)]
                    failures = []
                    for future in futures:
                        try:
                            future.result()
                        except AdapterError as error:
                            failures.append(error.code)
                self.assertEqual(failures, ["call_budget"] * 3)
                self.assertEqual([call["ordinal"] for call in host.calls], [1, 2, 3])
                self.assertEqual(host.concurrency_report()["by_role"]["chat"]["measured_peak"], 3)
                for call in host.calls:
                    self.assertEqual(host.calls_for_phase(call["phase"]), [call])
                    self.assertEqual(call["role"], "chat")
            finally:
                host.close(cancel=True)

    def test_judge_reservation_callback_precedes_process_and_failure_stays_final(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = host_at(root, cap=1)
            reserved = {}
            role = RoleHost(host, "judge", PROFILE["roles"]["judge"], phase="judge:synthetic")
            def process(arguments, cwd, timeout, **callbacks):
                self.assertEqual(reserved["ordinal"], 1)
                self.assertTrue((root / "calls/00001.attempt.json").exists())
                raise AdapterError("synthetic_terminal", "synthetic failure", 401)
            try:
                with patch("bridge.owned_process", process):
                    with self.assertRaises(AdapterError):
                        role.complete("synthetic", {}, {}, on_reserved=reserved.update)
                self.assertEqual(host.call_by_ordinal(1)["status"], "failed")
                self.assertEqual(host.call_by_ordinal(1)["phase"], role.phase)
                self.assertEqual(len(host.calls), 1)
            finally:
                host.close(cancel=True)

    def test_pool_failure_cancels_owned_calls_and_drains_final_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = host_at(root)
            started = threading.Event()
            intervals = []
            def process(arguments, cwd, timeout, *, cancelled, on_start):
                on_start(820001)
                started.set()
                while not cancelled():
                    time.sleep(.001)
                raise AdapterError("host_cancelled", "synthetic cancellation", 401)
            def worker(number):
                if number == 0:
                    self.assertTrue(started.wait(3))
                    raise ValueError("synthetic case setup failure")
                with host.external_attempt({}, phase="answer:cancel", model="synthetic-reader", effort="max", service_tier="fast") as attempt:
                    attempt.run(["synthetic"], 5)
            try:
                with patch("bridge.owned_process", process):
                    with self.assertRaisesRegex(ValueError, "setup failure"):
                        run_case_pool([0, 1], worker, 2, host, "reader", intervals)
                self.assertEqual(len(host.calls), 1)
                self.assertEqual(host.calls[0]["status"], "interrupted")
                self.assertFalse(host.calls[0]["accounting_complete"])
                self.assertEqual(len(intervals), 2)
                self.assertEqual(host.concurrency_report()["completed_intervals"], 1)
                self.assertEqual(qa_stage_summary(root)["records"][0]["status"], "interrupted")
            finally:
                host.close(cancel=True)

    def test_callback_failure_before_launch_retains_reserved_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = host_at(root, cap=1)
            def failed_checkpoint(receipt):
                raise OSError("synthetic checkpoint failure")
            try:
                with patch("bridge.owned_process") as process:
                    with self.assertRaises(AdapterError):
                        host.complete("synthetic", {}, {}, on_reserved=failed_checkpoint, role="judge")
                    process.assert_not_called()
                self.assertEqual(host.calls[0]["status"], "failed")
                self.assertFalse(host.calls[0]["host_process_started"])
                self.assertFalse(host.calls[0]["accounting_complete"])
                with self.assertRaisesRegex(AdapterError, "budget"):
                    host.complete("no free replay", {}, {})
            finally:
                host.close(cancel=True)

    def test_missing_stage_final_duration_stays_unknown_on_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "qa-stages/reader-synthetic.json"
            checkpoint_attempt(target, {"role": "reader", "pool_id": "synthetic", "status": "started",
                                        "configured_concurrency": 5, "task_count": 5})
            summary = qa_stage_summary(root)
            self.assertIsNone(summary["wall_ms"])
            self.assertEqual(summary["wall_ms_missing"], 1)
            self.assertEqual(summary["by_role"]["reader"]["missing_final_receipts"], 1)

    def test_reader_cancelled_before_reservation_is_interrupted_without_free_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = host_at(root)
            host.cancel()
            row = {"source_row": 0, "doc_id": "source.txt", "question": "synthetic question"}
            class HttpClient:
                async def aclose(self):
                    pass
            def factory(role):
                class Client:
                    def chat(self, *_arguments, **_options):
                        role.complete("synthetic", {}, {})
                return Client(), SimpleNamespace(wire_receipts=[]), {"http_client": HttpClient()}
            try:
                with patch("bridge.owned_process") as process:
                    answer, _ = read_case(row, args=SimpleNamespace(stage="run", max_turns=3, reasoning_effort="max", retry_failed=False),
                                          plan={"profile": PROFILE, "adapter_mode": "synthetic"},
                                          indexes={"source.txt": {"status": "completed", "cache_key": "synthetic", "doc_id": "synthetic"}},
                                          metadata={}, variant="full", variant_dir=root / "full", benchmark=root, host=host,
                                          client_factory=factory)
                    process.assert_not_called()
                self.assertEqual(answer["status"], "interrupted")
                self.assertEqual(answer["host_call_ordinals"], [])
                self.assertEqual(host.calls, [])
            finally:
                host.close(cancel=True)


if __name__ == "__main__":
    unittest.main()
