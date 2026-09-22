"""Index-only, receipt-backed replay for a declared document recovery run.

This is a RoleHost wrapper, not a general completion cache. Native calls and
their costs stay in the native host ledger. Reuse selections have a separate
durable ledger and reference original attempts, including their retained costs.
An SDK-ready document and final admission remain the runner's responsibility.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import threading

from bridge import AdapterError
from index_admission import TEXT_SCHEMA, canonical, digest_bytes, read_bound, read_json_bound, validate_call

MAX_PLAN_BYTES = 4 * 1024 * 1024
MAX_LEDGER_BYTES = 32 * 1024 * 1024
MAX_ORIGIN_CALLS = 4096
MAX_REUSE_EVENTS = 4096
MAX_INPUT_BYTES = 1024 * 1024
OUTPUT_CAP = 128 * 1024
ADAPTER_BINDINGS = ("bridge.py", "transports.py", "role_hosts.py")
_OWNERS = set()
_OWNERS_LOCK = threading.Lock()


def _clone(value):
    return json.loads(canonical(value))


def _digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("Replay evidence needs a SHA-256 binding")
    return value


def _root(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Replay evidence root is absent or redirected")
    return path.resolve()


def _plan(root, expected):
    plan, actual = read_json_bound(root / "plan.json", MAX_PLAN_BYTES)
    if actual != _digest(expected) or not isinstance(plan, dict):
        raise ValueError("Replay plan digest differs")
    return plan


def _identity(plan, source_name):
    if not isinstance(source_name, str) or not source_name or len(source_name.encode()) > 4096:
        raise ValueError("Invalid replay document identity")
    if "full" not in plan.get("variants", []):
        raise ValueError("Replay requires the original full indexing variant")
    source_sha = _digest(plan.get("source_hashes", {}).get(source_name))
    locked = plan.get("source_and_dependencies", {})
    revision, dependencies = locked.get("pageindex_revision"), locked.get("dependencies")
    if not isinstance(revision, str) or not revision or not isinstance(dependencies, dict) or not dependencies:
        raise ValueError("Replay SDK/dependency identity is missing")
    if any(not isinstance(key, str) or not isinstance(value, str) or not key or not value
           for key, value in dependencies.items()):
        raise ValueError("Replay dependency identity is invalid")
    profile = plan.get("profile", {}).get("roles", {}).get("index", {})
    if any(not isinstance(profile.get(key), str) or not profile[key]
           for key in ("model", "reasoning_effort", "service_tier")):
        raise ValueError("Replay index profile is missing")
    if profile["service_tier"] not in ("fast", "priority", "flex", "default"):
        raise ValueError("Replay index service tier is invalid")
    adapters = plan.get("adapter_files", {})
    bindings = {name: _digest(adapters.get(name)) for name in ADAPTER_BINDINGS}
    return _clone({"source_sha256": source_sha, "variant": "full", "pageindex_revision": revision,
                   "dependencies": dependencies, "python": plan.get("python"), "index_profile": profile,
                   "host_binary_sha256": _digest(plan.get("host_binary_sha256")), "adapter_bindings": bindings})


def _json_lines(raw):
    if raw and not raw.endswith(b"\n"):
        raise ValueError("Replay evidence ledger has an incomplete tail")
    def invalid(_value):
        raise ValueError("Non-finite replay evidence JSON")
    records = [json.loads(line, parse_constant=invalid) for line in raw.splitlines() if line.strip()]
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("Replay ledger entries must be objects")
    return records


@dataclass(frozen=True)
class IndexReplaySource:
    run_dir: Path
    source_name: str
    plan_sha256: str
    ledger_sha256: str
    ledger_bytes: int
    phase: str
    identity: dict
    input_cap: int
    output_cap: int
    calls: tuple


def load_index_replay_source(origin_run_dir, source_name, *, expected_plan_sha256, calls=None):
    """Load index metadata only; never inspect reader/judge request or response files.

    `calls`, when supplied by the runner, must be the complete original index
    phase slice and match the original ledger exactly. Failed/interrupted attempts
    remain in cost lineage but can never supply replay values.
    """
    root = _root(origin_run_dir)
    plan = _plan(root, expected_plan_sha256)
    identity = _identity(plan, source_name)
    phase = f"index:full:{source_name}"
    if (root / "calls").is_symlink():
        raise ValueError("Replay calls directory is redirected")
    raw = read_bound(root / "host-calls.jsonl", MAX_LEDGER_BYTES)
    ledger = _json_lines(raw)
    ordinals = [call.get("ordinal") for call in ledger]
    if any(type(number) is not int or number < 1 for number in ordinals) or len(ordinals) != len(set(ordinals)):
        raise ValueError("Origin ledger ordinals are invalid or duplicated")
    selected = [call for call in ledger if call.get("phase") == phase and call.get("role") == "index"]
    if not selected or len(selected) > MAX_ORIGIN_CALLS:
        raise ValueError("Origin index phase is empty or exceeds the replay bound")
    if calls is not None and sorted(canonical(call) for call in calls) != sorted(canonical(call) for call in selected):
        raise ValueError("Provided index calls differ from the complete origin phase ledger")
    input_cap, output_cap = plan.get("host_input_cap"), plan.get("host_output_cap")
    if type(input_cap) is not int or not 1 <= input_cap <= MAX_INPUT_BYTES or output_cap != OUTPUT_CAP:
        raise ValueError("Origin indexing bounds are invalid")
    profile = identity["index_profile"]
    for call in selected:
        if call.get("status") not in ("completed", "failed", "interrupted"):
            raise ValueError("Origin indexing attempt lacks a final disposition")
        if call.get("status") == "completed":
            validate_call(root, call, phase, profile, input_cap, output_cap)
    return IndexReplaySource(root, source_name, expected_plan_sha256, digest_bytes(raw), len(raw), phase,
                             identity, input_cap, output_cap,
                             tuple(_clone(call) for call in sorted(selected, key=lambda call: call["ordinal"])))


class IndexReplayHost:
    """One document's index RoleHost wrapper, held until all SDK calls drain.

    Use a context manager. Existing reuse ledgers require `resume=True`, which
    restarts per-request occurrence numbering and redelivers an already recorded
    occurrence idempotently. A producer cannot fund two different occurrences.
    Live misses remain ordinary host calls; this wrapper never adds them to its
    immutable donor pool. Additional donors require a new declared recovery run.
    """

    def __init__(self, live_index_role_host, *, consumer_dir, source_name,
                 expected_plan_sha256, sources, resume=False):
        self.live = live_index_role_host
        self.consumer_dir = _root(consumer_dir)
        self.source_name = source_name
        self.plan_sha256 = _digest(expected_plan_sha256)
        plan = _plan(self.consumer_dir, self.plan_sha256)
        self.identity = _identity(plan, source_name)
        self.identity_sha256 = digest_bytes(canonical(self.identity))
        self.expected_phase = f"index:full:{source_name}"
        self.max_input_bytes = plan.get("host_input_cap")
        if type(self.max_input_bytes) is not int or not 1 <= self.max_input_bytes <= MAX_INPUT_BYTES:
            raise ValueError("Consumer input bound is invalid")
        if plan.get("host_output_cap") != OUTPUT_CAP:
            raise ValueError("Consumer output bound differs")
        if _root(self.live.shared.run_dir) != self.consumer_dir or self.live.shared.max_input_bytes != self.max_input_bytes:
            raise ValueError("Live host does not belong to the declared consumer run/bound")
        self._check_scope(require_phase=False)
        self._lock = threading.RLock()
        self._closed = False
        self._inflight = 0
        self._counts = Counter()
        self._misses = Counter()
        self._deliveries = self._resumed_deliveries = 0
        self._slots, self._consumed, self._entries = {}, set(), []
        self._producers, self._groups = {}, defaultdict(list)
        source_refs, lineage = {}, {}
        for source in sources:
            source_plan = _plan(source.run_dir, source.plan_sha256)
            origin_prefix = read_bound(source.run_dir / "host-calls.jsonl", MAX_LEDGER_BYTES)[:source.ledger_bytes]
            if (digest_bytes(origin_prefix) != source.ledger_sha256
                    or source.identity != _identity(source_plan, source.source_name)
                    or source.phase != f"index:full:{source.source_name}"
                    or source.input_cap != source_plan.get("host_input_cap")
                    or source.output_cap != source_plan.get("host_output_cap")):
                raise ValueError("Origin replay snapshot or identity changed")
            original_calls = [call for call in _json_lines(origin_prefix)
                              if call.get("phase") == source.phase and call.get("role") == "index"]
            if sorted(canonical(call) for call in original_calls) != sorted(canonical(call) for call in source.calls):
                raise ValueError("Origin replay calls differ from their ledger snapshot")
            if source.identity != self.identity:
                raise ValueError("Origin raw document/SDK/dependencies/index profile/host identity differs")
            _plan(source.run_dir, source.plan_sha256)
            source_key = (source.plan_sha256, source.ledger_sha256, source.phase)
            source_refs[source_key] = {"run_dir": str(source.run_dir), "plan_sha256": source.plan_sha256,
                                      "ledger_sha256": source.ledger_sha256, "ledger_bytes": source.ledger_bytes,
                                      "phase": source.phase, "source_name": source.source_name}
            for call in source.calls:
                receipt_sha = digest_bytes(canonical(call))
                producer_id = digest_bytes(canonical({"plan_sha256": source.plan_sha256, "ordinal": call["ordinal"],
                                                      "receipt_sha256": receipt_sha}))
                lineage[producer_id] = {"producer_id": producer_id, "origin_plan_sha256": source.plan_sha256,
                    "origin_phase": source.phase, "origin_ordinal": call["ordinal"], "receipt_sha256": receipt_sha,
                    "status": call["status"], "request_sha256": call.get("request_sha256"),
                    "response_sha256": call.get("response_sha256"), "value_sha256": call.get("value_sha256"),
                    "usage": _clone(call.get("usage")), "elapsed_ms": call.get("elapsed_ms"),
                    "host_invoked": call.get("host_invoked"), "accounting_complete": call.get("accounting_complete")}
                if call["status"] == "completed" and producer_id not in self._producers:
                    validate_call(source.run_dir, call, source.phase, self.identity["index_profile"], source.input_cap, source.output_cap)
                    self._producers[producer_id] = (source, _clone(call))
                    self._groups[call["request_sha256"]].append(producer_id)
        if not lineage or len(lineage) > MAX_ORIGIN_CALLS:
            raise ValueError("Replay origin attempts are empty or exceed the bound")
        self._ambiguous = set()
        for request_sha, producers in self._groups.items():
            producers.sort(key=lambda producer: (self._producers[producer][0].plan_sha256,
                                                 self._producers[producer][1]["ordinal"], producer))
            if len({self._producers[producer][1]["value_sha256"] for producer in producers}) > 1:
                self._ambiguous.add(request_sha)
        self._lineage = {"schema_version": "gptgrep.index-replay-origins.v1", "identity": self.identity,
                         "sources": [source_refs[key] for key in sorted(source_refs)],
                         "attempts": [lineage[key] for key in sorted(lineage)],
                         "all_selected_origin_attempts_retained": True, "billing_usd": None,
                         "accounting_note": "Historical preparation, including failed and unused attempts; never newly billed reuse"}
        key = digest_bytes(source_name.encode())
        base = self.consumer_dir / "index-replay"
        self.directory = base / key
        if base.is_symlink() or self.directory.is_symlink():
            raise ValueError("Replay output directory is redirected")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ledger = self.directory / "reuse.jsonl"
        self._owner = None
        with _OWNERS_LOCK:
            if self.directory in _OWNERS:
                raise ValueError("Replay document already has an active owner")
            _OWNERS.add(self.directory)
        try:
            owner_path = self.directory / ".owner.lock"
            if owner_path.is_symlink():
                raise ValueError("Replay owner lock is redirected")
            self._owner = owner_path.open("a+b")
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lineage_raw = canonical(self._lineage) + b"\n"
            self.lineage_path = self.directory / "origin-lineage.json"
            self._manifest = {"schema_version": "gptgrep.index-replay.v1", "consumer_plan_sha256": self.plan_sha256,
                "source_name": source_name, "identity_sha256": self.identity_sha256,
                "consumer_input_cap": self.max_input_bytes, "origin_lineage_sha256": digest_bytes(lineage_raw),
                "ambiguous_request_keys": sorted(self._ambiguous),
                "policy": "exact request; deterministic producer order; distinct-value duplicates miss; one producer per occurrence"}
            manifest_path = self.directory / "manifest.json"
            if manifest_path.exists() and not resume:
                raise ValueError("Existing replay checkpoint requires explicit resume")
            self._write_once(manifest_path, canonical(self._manifest) + b"\n")
            self._write_once(self.lineage_path, lineage_raw)
            if self.ledger.exists():
                self._restore(read_bound(self.ledger, MAX_LEDGER_BYTES))
        except BaseException:
            self._release_owner()
            raise

    @staticmethod
    def _write_once(path, raw):
        if len(raw) > MAX_LEDGER_BYTES:
            raise ValueError("Replay metadata exceeds its bound")
        if path.exists():
            if read_bound(path, MAX_LEDGER_BYTES) != raw:
                raise ValueError("Replay checkpoint identity changed; use a new declared run")
            return
        with path.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _restore(self, raw):
        previous = None
        records = _json_lines(raw)
        if len(records) > MAX_REUSE_EVENTS:
            raise ValueError("Replay consumption ledger exceeds its event bound")
        for number, entry in enumerate(records, 1):
            expected = dict(entry)
            record_sha = expected.pop("record_sha256", None)
            if (entry.get("sequence") != number or entry.get("previous_sha256") != previous
                    or record_sha != digest_bytes(canonical(expected))
                    or entry.get("consumer_plan_sha256") != self.plan_sha256
                    or entry.get("identity_sha256") != self.identity_sha256
                    or entry.get("phase") != self.expected_phase):
                raise ValueError("Replay consumption chain or context differs")
            producer = entry.get("producer_id")
            if producer not in self._producers or producer in self._consumed:
                raise ValueError("Replay producer is absent or consumed more than once")
            _source, call = self._producers[producer]
            occurrence = entry.get("occurrence")
            if type(occurrence) is not int or occurrence < 1:
                raise ValueError("Invalid replay occurrence")
            slot = (entry.get("request_sha256"), occurrence)
            if (slot in self._slots or slot[0] != call["request_sha256"]
                    or slot[0] in self._ambiguous
                    or entry.get("response_sha256") != call["response_sha256"]
                    or entry.get("value_sha256") != call["value_sha256"]
                    or entry.get("disposition") != "historical_response_selected"
                    or entry.get("new_host_invocations") != 0):
                raise ValueError("Replay consumption does not bind its producer")
            self._slots[slot] = producer
            self._consumed.add(producer)
            self._entries.append(entry)
            previous = record_sha

    def _check_scope(self, require_phase=True):
        profile = self.identity["index_profile"]
        if (getattr(self.live, "role", None), self.live.model, self.live.effort, self.live.service_tier) != (
                "index", profile["model"], profile["reasoning_effort"], profile["service_tier"]):
            raise AdapterError("index_replay_scope", "Replay is restricted to the declared index role/profile", 401)
        if require_phase and self.live.phase != self.expected_phase:
            raise AdapterError("index_replay_scope", "Replay cannot serve another document, reader or judge phase", 401)
        if self.live.shared.max_input_bytes != self.max_input_bytes:
            raise AdapterError("index_replay_scope", "Replay input bound changed after its declaration", 401)

    @property
    def model(self): return self.live.model

    @property
    def effort(self): return self.live.effort

    @property
    def service_tier(self): return self.live.service_tier

    @property
    def role(self): return "index"

    @property
    def phase(self): return self.live.phase

    @phase.setter
    def phase(self, value):
        if value != self.expected_phase:
            raise AdapterError("index_replay_scope", "Replay phase is document-specific and index-only", 401)
        self.live.phase = value

    @property
    def calls(self): return self.live.calls

    @property
    def rejections(self): return self.live.rejections

    def _lookup(self, instructions, state, schema):
        self._check_scope()
        if self._closed:
            raise AdapterError("index_replay_closed", "Replay owner is closed", 401)
        if not isinstance(instructions, str) or schema != TEXT_SCHEMA or not isinstance(state, dict) or set(state) != {"messages"} or not isinstance(state["messages"], list):
            raise AdapterError("index_replay_scope", "Replay accepts only original index conversation requests", 401)
        try:
            _plan(self.consumer_dir, self.plan_sha256)
        except (ValueError, OSError) as error:
            raise AdapterError("index_replay_integrity", "Consumer replay plan changed", 401) from error
        request = {"instructions": instructions, "state": state, "schema": schema}
        payload = canonical(request)
        request_sha = digest_bytes(payload)
        self._counts[request_sha] += 1
        occurrence = self._counts[request_sha]
        if len(payload) > self.max_input_bytes:
            self._misses["consumer_input_limit"] += 1
            return None
        slot = (request_sha, occurrence)
        resumed = slot in self._slots
        producer = self._slots.get(slot)
        if producer is None:
            if request_sha in self._ambiguous:
                self._misses["ambiguous_producers"] += 1
                return None
            group = self._groups.get(request_sha, [])
            producer = next((candidate for candidate in group if candidate not in self._consumed), None)
            if producer is None:
                self._misses["producer_exhausted" if group else "unmatched_request"] += 1
                return None
        source, call = self._producers[producer]
        try:
            _plan(self.consumer_dir, self.plan_sha256)
            _plan(source.run_dir, source.plan_sha256)
            validate_call(source.run_dir, call, source.phase, self.identity["index_profile"], source.input_cap, source.output_cap)
            raw = read_bound(source.run_dir / "calls" / f"{call['ordinal']:05d}.request.json", source.input_cap)
            if raw != payload:
                raise ValueError("Exact request bytes differ")
            report, report_sha = read_json_bound(source.run_dir / "calls" / f"{call['ordinal']:05d}.response.json", source.output_cap + 65536)
            if report_sha != call["response_sha256"]:
                raise ValueError("Producer response changed during validation")
        except (ValueError, OSError) as error:
            raise AdapterError("index_replay_integrity", "Retained index request/response evidence failed revalidation", 401) from error
        if not resumed:
            entry = {"sequence": len(self._entries) + 1,
                "previous_sha256": self._entries[-1]["record_sha256"] if self._entries else None,
                "consumer_plan_sha256": self.plan_sha256, "identity_sha256": self.identity_sha256,
                "phase": self.expected_phase, "request_sha256": request_sha, "occurrence": occurrence,
                "producer_id": producer, "origin_plan_sha256": source.plan_sha256, "origin_ordinal": call["ordinal"],
                "response_sha256": call["response_sha256"], "value_sha256": call["value_sha256"],
                "disposition": "historical_response_selected", "new_host_invocations": 0,
                "historical_preparation_is_free": False}
            entry["record_sha256"] = digest_bytes(canonical(entry))
            raw = canonical(entry) + b"\n"
            if len(self._entries) >= MAX_REUSE_EVENTS or (self.ledger.stat().st_size if self.ledger.exists() else 0) + len(raw) > MAX_LEDGER_BYTES:
                raise AdapterError("index_replay_limit", "Replay consumption ledger exceeds its bound", 401)
            if self.ledger.is_symlink():
                raise AdapterError("index_replay_integrity", "Replay ledger was redirected", 401)
            with self.ledger.open("ab") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._entries.append(entry)
            self._slots[slot] = producer
            self._consumed.add(producer)
        self._deliveries += 1
        self._resumed_deliveries += int(resumed)
        return _clone(report["value"]), _clone(report)

    def complete(self, instructions, state, schema, *, on_reserved=None):
        request = _clone({"instructions": instructions, "state": state, "schema": schema})
        with self._lock:
            selected = self._lookup(**request)
            self._inflight += 1
        try:
            if selected is not None:
                return selected
            return self.live.complete(**request,
                                      **({"on_reserved": on_reserved} if on_reserved is not None else {}))
        finally:
            with self._lock:
                self._inflight -= 1

    async def acomplete(self, instructions, state, schema):
        # Lookup/claim is atomic across sync and async callers; no executor or
        # native host slot is spent on a retained response.
        request = _clone({"instructions": instructions, "state": state, "schema": schema})
        with self._lock:
            selected = self._lookup(**request)
            self._inflight += 1
        try:
            if selected is not None:
                return selected
            return await self.live.acomplete(**request)
        finally:
            with self._lock:
                self._inflight -= 1

    def summary(self):
        with self._lock:
            raw = read_bound(self.ledger, MAX_LEDGER_BYTES) if self.ledger.exists() else b""
            lineage_raw = read_bound(self.lineage_path, MAX_LEDGER_BYTES)
            if (raw != b"".join(canonical(entry) + b"\n" for entry in self._entries)
                    or digest_bytes(lineage_raw) != self._manifest["origin_lineage_sha256"]):
                raise ValueError("Replay ledger or historical cost lineage changed")
            return {"schema_version": "gptgrep.index-replay-summary.v1", "source_name": self.source_name,
                "source_sha256": self.identity["source_sha256"], "identity_sha256": self.identity_sha256,
                "consumer_plan_sha256": self.plan_sha256, "phase": self.expected_phase,
                "reuse_ledger": str(self.ledger.relative_to(self.consumer_dir)), "reuse_ledger_sha256": digest_bytes(raw),
                "origin_lineage": str(self.lineage_path.relative_to(self.consumer_dir)), "origin_lineage_sha256": digest_bytes(lineage_raw),
                "origin_attempts": len(self._lineage["attempts"]), "validated_completed_producers": len(self._producers),
                "ambiguous_request_keys": len(self._ambiguous), "unique_reused_producers": len(self._consumed),
                "replay_deliveries_this_session": self._deliveries, "resumed_deliveries_this_session": self._resumed_deliveries,
                "live_misses_this_session": sum(self._misses.values()), "miss_reasons": dict(self._misses),
                "new_host_invocations_by_replay": 0, "historical_preparation_is_free": False,
                "billing_usd": None, "all_selected_origin_attempts_retained": True,
                "basis": "Validated response selection per exact index request occurrence; SDK completion/admission required separately"}

    def _release_owner(self):
        if self._owner is not None:
            self._owner.close()
            self._owner = None
        with _OWNERS_LOCK:
            _OWNERS.discard(self.directory)

    def close(self):
        with self._lock:
            if self._inflight:
                raise ValueError("Drain index requests before closing replay ownership")
            self._closed = True
            self._release_owner()

    def __enter__(self): return self

    def __exit__(self, *_exception): self.close()
