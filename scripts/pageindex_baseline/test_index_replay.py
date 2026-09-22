"""Offline synthetic index replay contracts; no model, SDK, question or gold reads."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from bridge import AdapterError
from index_admission import TEXT_SCHEMA, canonical, digest_bytes
from index_replay import IndexReplayHost, load_index_replay_source

PROFILE = {"model": "synthetic-index-model", "reasoning_effort": "medium", "service_tier": "fast"}
INSTRUCTIONS = "Synthetic source-only indexing instruction"
SOURCE = "source.pdf"


def request(text="raw paragraph"):
    return {"instructions": INSTRUCTIONS, "state": {"messages": [{"role": "user", "content": text}]},
            "schema": copy.deepcopy(TEXT_SCHEMA)}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical(value) + b"\n"
    path.write_bytes(raw)
    return digest_bytes(raw)


def plan(cap=262144):
    return {"variants": ["full"], "source_hashes": {SOURCE: digest_bytes(b"synthetic raw document")},
            "source_and_dependencies": {"pageindex_revision": "synthetic-pinned-sdk",
                                        "dependencies": {"synthetic-parser": "1.0"}},
            "profile": {"roles": {"index": copy.deepcopy(PROFILE)}},
            "host_binary_sha256": digest_bytes(b"synthetic host binary"), "python": "synthetic-runtime",
            "adapter_files": {name: digest_bytes(name.encode()) for name in ("bridge.py", "transports.py", "role_hosts.py")},
            "host_input_cap": cap, "host_output_cap": 131072}


def invocation(root, ordinal, *, text="raw paragraph", value="source summary", status="completed"):
    payload = request(text)
    raw = canonical(payload)
    report = {"status": "completed", "model": PROFILE["model"], "requested_reasoning_effort": PROFILE["reasoning_effort"],
              "effective_reasoning_effort": PROFILE["reasoning_effort"], "requested_service_tier": "fast",
              "effective_service_tier": "priority", "auth_mode": "chatgpt", "model_provider": "openai",
              "thread_id": f"synthetic-thread-{ordinal}", "turn_id": "synthetic-turn",
              "value": {"text": value}, "usage": {"total_tokens": 100 + ordinal}}
    if status != "completed":
        report = {"status": "failed", "code": "synthetic-service-error", "usage": None}
    response = canonical(report)
    call = {"ordinal": ordinal, "phase": f"index:full:{SOURCE}", "role": "index", "status": status,
            "requested_model": PROFILE["model"], "requested_effort": PROFILE["reasoning_effort"],
            "requested_service_tier": "fast", "request_sha256": digest_bytes(raw),
            "instructions_sha256": digest_bytes(INSTRUCTIONS.encode()), "state_sha256": digest_bytes(canonical(payload["state"])),
            "schema_sha256": digest_bytes(canonical(TEXT_SCHEMA)), "response_sha256": digest_bytes(response),
            "usage": report["usage"], "elapsed_ms": 10 + ordinal, "host_invoked": True,
            "measurement_id": "synthetic-measurement"}
    if status == "completed":
        call.update(thread_id=report["thread_id"], turn_id=report["turn_id"], value_sha256=digest_bytes(canonical(report["value"])))
    else:
        call.update(error_code="synthetic-service-error", accounting_complete=False)
    (root / "calls").mkdir(exist_ok=True)
    (root / "calls" / f"{ordinal:05d}.request.json").write_bytes(raw)
    (root / "calls" / f"{ordinal:05d}.response.json").write_bytes(response)
    return call


class FakeIndexHost:
    def __init__(self, directory, profile=None, cap=1048576):
        profile = profile or PROFILE
        self.shared = SimpleNamespace(run_dir=directory, max_input_bytes=cap)
        self.role, self.phase = "index", f"index:full:{SOURCE}"
        self.model, self.effort, self.service_tier = profile["model"], profile["reasoning_effort"], profile["service_tier"]
        self.calls, self.rejections = [], []

    def complete(self, instructions, state, schema, *, on_reserved=None):
        payload = {"instructions": instructions, "state": state, "schema": schema}
        if len(canonical(payload)) > self.shared.max_input_bytes:
            self.rejections.append({"code": "input_limit", "host_invoked": False})
            raise AdapterError("input_limit", "Synthetic host input cap", 400)
        self.calls.append(copy.deepcopy(payload))
        if on_reserved:
            on_reserved({"ordinal": len(self.calls)})
        value = {"text": "synthetic live miss"}
        return value, {"status": "completed", "value": value, "model": self.model}

    async def acomplete(self, instructions, state, schema):
        await asyncio.sleep(0)
        return self.complete(instructions, state, schema)


class ReplayTests(unittest.TestCase):
    def fixture(self, root, specifications=None, extra_records=None):
        origin, consumer = root / "origin", root / "consumer"
        origin.mkdir(); consumer.mkdir()
        origin_plan = plan()
        origin_sha = write_json(origin / "plan.json", origin_plan)
        calls = [invocation(origin, index, **settings) for index, settings in enumerate(specifications or [{}], 1)]
        raw = b"".join(canonical(call) + b"\n" for call in [*calls, *(extra_records or [])])
        (origin / "host-calls.jsonl").write_bytes(raw)
        consumer_plan = plan(1048576)
        # These unrelated task-cohort bindings deliberately differ. Only raw
        # source/index identities choose replay; no question/gold data is read.
        consumer_plan["question_sha256"] = digest_bytes(b"different cohort metadata")
        consumer_sha = write_json(consumer / "plan.json", consumer_plan)
        source = load_index_replay_source(origin, SOURCE, expected_plan_sha256=origin_sha, calls=calls)
        return origin, consumer, source, consumer_sha, FakeIndexHost(consumer)

    def wrapper(self, consumer, source, sha, live, **kwargs):
        return IndexReplayHost(live, consumer_dir=consumer, source_name=SOURCE,
                               expected_plan_sha256=sha, sources=[source], **kwargs)

    def test_exact_reuse_and_larger_cap_miss_keep_native_calls_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            origin, consumer, source, sha, live = self.fixture(Path(temporary), [{}, {"status": "failed", "text": "failed work"}])
            original = {path.relative_to(origin): path.read_bytes() for path in origin.rglob('*') if path.is_file()}
            reserved = []
            with self.wrapper(consumer, source, sha, live) as wrapper:
                value, report = wrapper.complete(**request(), on_reserved=reserved.append)
                self.assertEqual(value, {"text": "source summary"})
                self.assertEqual(report["thread_id"], "synthetic-thread-1")
                self.assertEqual(live.calls, [])
                self.assertEqual(reserved, [])
                large = request("source text " * 30000)
                self.assertGreater(len(canonical(large)), 262144)
                self.assertLess(len(canonical(large)), 1048576)
                self.assertEqual(wrapper.complete(**large, on_reserved=reserved.append)[0]["text"], "synthetic live miss")
                self.assertEqual(len(live.calls), 1)
                self.assertEqual(len(reserved), 1)
                summary = wrapper.summary()
                self.assertEqual(summary["unique_reused_producers"], 1)
                self.assertEqual(summary["origin_attempts"], 2)
                self.assertEqual(summary["new_host_invocations_by_replay"], 0)
                self.assertFalse(summary["historical_preparation_is_free"])
                self.assertIsNone(summary["billing_usd"])
                lineage = json.loads((consumer / summary["origin_lineage"]).read_bytes())
                failed = next(item for item in lineage["attempts"] if item["status"] == "failed")
                self.assertIsNone(failed["usage"])
            self.assertEqual(original, {path.relative_to(origin): path.read_bytes() for path in origin.rglob('*') if path.is_file()})

    def test_distinct_value_duplicates_are_live_misses_not_best_or_latest(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, consumer, source, sha, live = self.fixture(Path(temporary), [{"value": "first"}, {"value": "second"}])
            with self.wrapper(consumer, source, sha, live) as wrapper:
                self.assertEqual(wrapper.complete(**request())[0]["text"], "synthetic live miss")
                summary = wrapper.summary()
                self.assertEqual(summary["ambiguous_request_keys"], 1)
                self.assertEqual(summary["unique_reused_producers"], 0)
                self.assertEqual(summary["miss_reasons"], {"ambiguous_producers": 1})

    def test_identical_duplicates_are_consumed_once_in_origin_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, consumer, source, sha, live = self.fixture(Path(temporary), [{}, {}])
            with self.wrapper(consumer, source, sha, live) as wrapper:
                first = wrapper.complete(**request())
                first[0]["text"] = "caller mutation"
                second = wrapper.complete(**request())
                self.assertEqual(second[0]["text"], "source summary")
                self.assertEqual(second[1]["thread_id"], "synthetic-thread-2")
                wrapper.complete(**request())
                entries = [json.loads(line) for line in wrapper.ledger.read_text().splitlines()]
                self.assertEqual([entry["origin_ordinal"] for entry in entries], [1, 2])
                self.assertEqual([entry["occurrence"] for entry in entries], [1, 2])
                self.assertEqual(len(live.calls), 1)
                self.assertEqual(wrapper.summary()["miss_reasons"], {"producer_exhausted": 1})

    def test_resume_redelivers_reserved_occurrence_without_second_consumption(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, consumer, source, sha, live = self.fixture(Path(temporary))
            with self.wrapper(consumer, source, sha, live) as wrapper:
                wrapper.complete(**request())
                ledger, original = wrapper.ledger, wrapper.ledger.read_bytes()
            with self.assertRaises(ValueError):
                self.wrapper(consumer, source, sha, live)
            with self.wrapper(consumer, source, sha, live, resume=True) as resumed:
                self.assertEqual(resumed.complete(**request())[0]["text"], "source summary")
                self.assertEqual(ledger.read_bytes(), original)
                self.assertEqual(resumed.summary()["resumed_deliveries_this_session"], 1)
                resumed.complete(**request())
                self.assertEqual(len(live.calls), 1)
                self.assertEqual(resumed.summary()["unique_reused_producers"], 1)

    def test_sync_and_async_consumption_are_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, consumer, source, sha, live = self.fixture(Path(temporary), [{}, {}, {}, {}])
            with self.wrapper(consumer, source, sha, live) as wrapper:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    replies = list(executor.map(lambda _: wrapper.complete(**request()), range(2)))
                async def concurrent():
                    return await asyncio.gather(wrapper.acomplete(**request()), wrapper.acomplete(**request()))
                replies.extend(asyncio.run(concurrent()))
                self.assertEqual(len({report["thread_id"] for _, report in replies}), 4)
                self.assertEqual(wrapper.summary()["unique_reused_producers"], 4)
                self.assertEqual(live.calls, [])

    def test_two_active_owners_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, consumer, source, sha, live = self.fixture(Path(temporary))
            with self.wrapper(consumer, source, sha, live):
                with self.assertRaises(ValueError):
                    self.wrapper(consumer, source, sha, live, resume=True)

    def test_identity_mismatches_fail_before_any_live_call(self):
        for field in ("source", "sdk", "dependencies", "model", "effort", "tier", "binary", "adapter"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                _, consumer, source, _, live = self.fixture(Path(temporary))
                changed = plan(1048576)
                if field == "source": changed["source_hashes"][SOURCE] = digest_bytes(b"other raw source")
                elif field == "sdk": changed["source_and_dependencies"]["pageindex_revision"] = "other-sdk"
                elif field == "dependencies": changed["source_and_dependencies"]["dependencies"] = {"synthetic-parser": "2.0"}
                elif field == "model": changed["profile"]["roles"]["index"]["model"] = "other-model"; live.model = "other-model"
                elif field == "effort": changed["profile"]["roles"]["index"]["reasoning_effort"] = "high"; live.effort = "high"
                elif field == "tier": changed["profile"]["roles"]["index"]["service_tier"] = "flex"; live.service_tier = "flex"
                elif field == "binary": changed["host_binary_sha256"] = digest_bytes(b"other binary")
                else: changed["adapter_files"]["bridge.py"] = digest_bytes(b"other bridge")
                sha = write_json(consumer / "plan.json", changed)
                with self.assertRaises(ValueError): self.wrapper(consumer, source, sha, live)
                self.assertEqual(live.calls, [])

    def test_request_bytes_change_misses_and_schema_role_changes_fail_closed(self):
        extra = {"ordinal": 2, "phase": "chat:synthetic", "role": "chat", "status": "completed"}
        with tempfile.TemporaryDirectory() as temporary:
            origin, consumer, source, sha, live = self.fixture(Path(temporary), extra_records=[extra])
            self.assertFalse((origin / "calls/00002.request.json").exists())
            with self.wrapper(consumer, source, sha, live) as wrapper:
                wrapper.complete(**request("raw paragraph "))
                self.assertEqual(len(live.calls), 1)
                invalid = request(); invalid["schema"] = {"type": "string"}
                with self.assertRaises(AdapterError): wrapper.complete(**invalid)
                with self.assertRaises(AdapterError): wrapper.phase = "judge:synthetic"
                live.role = "chat"
                with self.assertRaises(AdapterError): wrapper.complete(**request())
                self.assertEqual(len(live.calls), 1)
            with self.assertRaises(ValueError):
                load_index_replay_source(origin, SOURCE, expected_plan_sha256=source.plan_sha256, calls=[extra])

    def test_tampered_producer_and_consumer_plan_never_fall_back_live(self):
        for artifact in ("request", "response", "plan"):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as temporary:
                origin, consumer, source, sha, live = self.fixture(Path(temporary))
                with self.wrapper(consumer, source, sha, live) as wrapper:
                    target = consumer / "plan.json" if artifact == "plan" else origin / f"calls/00001.{artifact}.json"
                    target.write_bytes(target.read_bytes() + b" ")
                    with self.assertRaises(AdapterError): wrapper.complete(**request())
                    self.assertEqual(live.calls, [])

    def test_duplicate_producer_or_truncated_consumption_ledger_is_rejected(self):
        for damaged in ("duplicate", "truncated"):
            with self.subTest(damaged=damaged), tempfile.TemporaryDirectory() as temporary:
                _, consumer, source, sha, live = self.fixture(Path(temporary))
                with self.wrapper(consumer, source, sha, live) as wrapper:
                    wrapper.complete(**request())
                    ledger = wrapper.ledger
                raw = ledger.read_bytes()
                if damaged == "duplicate":
                    previous = json.loads(raw)
                    repeated = {**previous, "sequence": 2, "occurrence": 2, "previous_sha256": previous["record_sha256"]}
                    repeated.pop("record_sha256")
                    repeated["record_sha256"] = digest_bytes(canonical(repeated))
                    ledger.write_bytes(raw + canonical(repeated) + b"\n")
                else:
                    ledger.write_bytes(raw + b'{"partial":')
                with self.assertRaises(ValueError): self.wrapper(consumer, source, sha, live, resume=True)
                self.assertEqual(live.calls, [])

    def test_duplicate_origin_reference_does_not_double_count_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, consumer, source, sha, live = self.fixture(Path(temporary), [{}, {"status": "failed"}])
            with IndexReplayHost(live, consumer_dir=consumer, source_name=SOURCE,
                                 expected_plan_sha256=sha, sources=[source, source]) as wrapper:
                wrapper.complete(**request())
                summary = wrapper.summary()
                self.assertEqual(summary["origin_attempts"], 2)
                self.assertEqual(summary["validated_completed_producers"], 1)
                self.assertEqual(summary["unique_reused_producers"], 1)
                lineage = json.loads((consumer / summary["origin_lineage"]).read_bytes())
                self.assertEqual(len({entry["producer_id"] for entry in lineage["attempts"]}), 2)
                self.assertEqual(len(lineage["sources"]), 1)

    def test_consumer_cap_and_native_reservation_are_never_bypassed(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, consumer, source, _, _ = self.fixture(Path(temporary))
            cap = len(canonical(request())) - 1
            sha = write_json(consumer / "plan.json", plan(cap))
            live = FakeIndexHost(consumer, cap=cap)
            with self.wrapper(consumer, source, sha, live) as wrapper:
                with self.assertRaises(AdapterError): wrapper.complete(**request())
                self.assertEqual(wrapper.summary()["unique_reused_producers"], 0)
                self.assertEqual(wrapper.summary()["miss_reasons"], {"consumer_input_limit": 1})
                self.assertEqual(live.calls, [])
                self.assertEqual(len(live.rejections), 1)


if __name__ == "__main__":
    unittest.main()
