"""Append-only technical recovery around an immutable native evaluation.

No product/index rebuilding, answer-dependent selection, or implicit retry. The
frozen native ask is a new fully accounted attempt, not a substage continuation.
Only this module's explicit run_plan front door can invoke its frozen transport.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace

SCHEMA = "gptgrep.native-recovery.v1"
POLICY = "pre_reader_initial_search_v1"
MAX_JSON = 4 * 1024 * 1024
MAX_LEDGER = 32 * 1024 * 1024
RECOVERABLE_CODES = frozenset({"host_jev_search_failed", "host_jev_initial_timeout"})
NATIVE_LEDGER = re.compile(r"attempt-[0-9]+-[0-9]+-[0-9]+\.jsonl")


class RecoveryError(ValueError):
    """Bounded control error; never include model content or private credentials."""


def require(condition, message):
    if not condition:
        raise RecoveryError(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def parse(raw):
    def invalid(_):
        raise RecoveryError("nonfinite_json")
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid)


def regular(path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file(), "missing_or_redirected_file")
    require(path.name != "auth.json" and not path.name.startswith(".env"), "credential_file_outside_scope")
    return path


def digest(path):
    path = regular(path)
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_json(path, cap=MAX_JSON):
    path = regular(path)
    require(path.stat().st_size <= cap, "json_byte_limit")
    return parse(path.read_bytes())


def read_events(path):
    if not Path(path).exists():
        return []
    path = regular(path)
    require(path.stat().st_size <= MAX_LEDGER, "ledger_byte_limit")
    lines = path.read_bytes().splitlines()
    require(len(lines) <= 4096, "ledger_event_limit")
    return [parse(line) for line in lines if line.strip()]


def new_json(path, value):
    path = Path(path)
    require(all(not parent.is_symlink() for parent in path.parents), "redirected_artifact_directory")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not path.is_symlink() and not path.exists(), "immutable_artifact_exists")
    raw = encoded(value) + b"\n"
    require(len(raw) <= MAX_LEDGER, "artifact_byte_limit")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def append_event(sidecar, event, **data):
    path = Path(sidecar) / "events.jsonl"
    previous = read_events(path)
    record = {"schema_version": SCHEMA, "sequence": len(previous) + 1,
              "previous_sha256": fingerprint(previous[-1]) if previous else None,
              "event": event, "observed_unix_ns": time.time_ns(), **data}
    raw = encoded(record) + b"\n"
    require(len(raw) <= 65536 and len(previous) < 1024, "recovery_event_limit")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "ab") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def verify_events(sidecar):
    previous = None
    publications = {}
    for number, event in enumerate(read_events(Path(sidecar) / "events.jsonl"), 1):
        require(event.get("schema_version") == SCHEMA and event.get("sequence") == number
                and event.get("previous_sha256") == (fingerprint(previous) if previous else None), "recovery_event_chain_changed")
        if "artifact" in event:
            name = event["artifact"]
            require(isinstance(name, str) and name not in publications, "artifact_journal_binding_invalid")
            path = Path(sidecar) / event["artifact"]
            require(path.resolve().is_relative_to(Path(sidecar)), "recovery_artifact_escape")
            require(digest(path) == event["sha256"], "retained_recovery_artifact_changed")
            publications[name] = event
        previous = event
    # Creation precedes publication. Never let a later inventory retroactively
    # approve an orphan. Frozen transport request/response journals are separate:
    # they must pass the frozen receipt validator before becoming an outcome.
    semantic = {
        "manifest.json": ("bound", "manifest"),
        "rounds/*/plan.json": ("round_planned", "plan"),
        "calls/*.lineage.json": ("call_reserved", "lineage"),
        "calls/*.host-ledgers.json": ("native_ledgers_bound", "native_ledger"),
        "rounds/*/outcomes/*.json": ("outcome_retained", "outcome"),
        "rounds/*/result.json": ("round_terminal", "result"),
    }
    for pattern, (event_name, kind) in semantic.items():
        for path in Path(sidecar).glob(pattern):
            publication = publications.get(str(path.relative_to(sidecar)))
            require(publication is not None, f"{kind}_not_journalled")
            require(publication["event"] == event_name, "semantic_publication_type_mismatch")


def inventory(root):
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), "inventory_root_invalid")
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        require(all(not (Path(directory) / name).is_symlink() for name in dirs), "redirected_directory")
        for name in files:
            path = Path(directory) / name
            result[str(path.relative_to(root))] = {"sha256": digest(path), "bytes": path.stat().st_size}
            require(len(result) <= 100000, "inventory_file_limit")
    return dict(sorted(result.items()))


def reservations(root):
    """An uncertain durable reservation consumes the cap even without a receipt."""
    root = Path(root)
    receipts = read_events(root / "host-calls.jsonl")
    numbers = [item.get("ordinal") for item in receipts]
    require(all(type(n) is int and n > 0 for n in numbers) and len(numbers) == len(set(numbers)), "invalid_receipt_ordinals")
    reserved = set(numbers)
    calls = root / "calls"
    if calls.exists():
        require(not calls.is_symlink(), "redirected_calls")
        for path in calls.iterdir():
            require(not path.is_symlink(), "redirected_call_artifact")
            match = re.fullmatch(r"([0-9]+)\.(?:request|attempt|process-start)\.json", path.name)
            if match:
                reserved.add(int(match[1]))
    require(sorted(reserved) == list(range(1, max(reserved, default=0) + 1)), "noncontiguous_reservations")
    return reserved, {item["ordinal"]: item for item in receipts}


def sidecar_path(origin):
    origin = Path(origin).resolve()
    return origin.with_name(origin.name + "-technical-recovery")


@contextmanager
def locked(origin, *, create=True):
    origin = Path(origin).resolve()
    sidecar = sidecar_path(origin)
    if create:
        sidecar.mkdir(mode=0o700, exist_ok=True)
    require(sidecar.is_dir() and not sidecar.is_symlink(), "recovery_directory_invalid")
    # Read-only open leaves the origin lock's bytes unchanged.
    origin_fd = os.open(regular(origin / ".owner.lock"), os.O_RDONLY | os.O_NOFOLLOW)
    side_fd = os.open(sidecar / ".owner.lock", (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY) | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(origin_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(side_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield sidecar
    finally:
        os.close(side_fd)
        os.close(origin_fd)


def technical_eligibility(metadata):
    """Pure metadata predicate. No question, document, answer or verdict inputs."""
    if metadata.get("reader_ever_reserved") is True:
        return False, "final_reader_already_attempted"
    if metadata.get("terminal_receipt") != "failed":
        return False, "not_failed_native_attempt"
    if metadata.get("failure_stage") != "initial_search" or metadata.get("failure_code") not in RECOVERABLE_CODES:
        return False, "failure_stage_not_admitted"
    if metadata.get("bound_terminal_ledger") is not True or metadata.get("reader_ever_reserved") is not False:
        return False, "absence_of_reader_unproven"
    return True, POLICY


def failure_metadata(run_dir, ordinal, generation, corpus, expected_payload=None):
    """Bind technical evidence without exposing request text to the selector."""
    _, receipts = reservations(run_dir)
    receipt = receipts.get(ordinal)
    require(receipt is not None and receipt.get("operation") == "native_ask", "native_receipt_missing")
    base = Path(run_dir) / "calls" / f"{ordinal:05d}"
    request_path, response_path = Path(str(base) + ".request.json"), Path(str(base) + ".response.json")
    request = read_json(request_path)
    require(receipt.get("request_sha256") == digest(request_path), "request_digest_mismatch")
    if expected_payload is not None:
        require(request == expected_payload, "recovery_payload_changed")
    result = {"ordinal": ordinal, "terminal_receipt": receipt.get("status"),
              "failure_stage": None, "failure_code": None, "reader_ever_reserved": None,
              "bound_terminal_ledger": False, "request_sha256": digest(request_path)}
    if receipt.get("status") == "completed":
        result["reader_ever_reserved"] = True
        return result
    if not response_path.exists():
        return result
    report = read_json(response_path)
    require(receipt.get("response_sha256") == digest(response_path), "response_digest_mismatch")
    result["response_sha256"] = digest(response_path)
    failure = report.get("host_retrieval")
    if not isinstance(failure, dict):
        return result
    result.update(failure_stage=failure.get("stage"), failure_code=failure.get("code"))
    require(report.get("code") == failure.get("code"), "typed_failure_code_mismatch")
    attempts = failure.get("model_attempts")
    if not isinstance(attempts, list) or len(attempts) > 2:
        return result
    require(all(isinstance(a, dict) and a.get("role") in ("query_planner", "final_reader") for a in attempts), "invalid_model_attempt_metadata")
    if any(a["role"] == "final_reader" for a in attempts):
        result["reader_ever_reserved"] = True
        return result
    ledger = Path(failure.get("ledger_path", ""))
    require(ledger.parent == Path(corpus) / ".gptgrep/host-attempts" and NATIVE_LEDGER.fullmatch(ledger.name), "native_ledger_path_mismatch")
    events = read_events(ledger)
    require(len(events) <= 1024 and ledger.stat().st_size <= MAX_JSON, "native_ledger_limit")
    expected = {"generation": generation, "query_sha256": hashlib.sha256(request["question"].encode()).hexdigest(), "document_scope": request["document"]}
    model_events = [event for event in events if event.get("model_attempts")]
    bound = bool(events) and events[-1].get("event") == "failed" and failure.get("generation") == generation
    bound = bound and all(event.get("generation") == generation for event in events)
    bound = bound and any(event.get("workflow") == expected for event in events)
    bound = bound and all(event.get("workflow") in (None, expected) for event in events)
    bound = bound and all(event.get("workflow") == expected for event in model_events)
    snapshots = [event["model_attempts"] for event in events if "model_attempts" in event]
    require(all(isinstance(snapshot, list) and len(snapshot) <= 2 and all(isinstance(a, dict) and a.get("role") in ("query_planner", "final_reader") for a in snapshot) for snapshot in snapshots), "invalid_model_ledger_metadata")
    reader_seen = any(a["role"] == "final_reader" for snapshot in snapshots for a in snapshot)
    bound = bound and bool(snapshots) and snapshots[-1] == attempts
    result.update(reader_ever_reserved=reader_seen if bound or reader_seen else None,
                  bound_terminal_ledger=bool(bound), native_ledger_sha256=digest(ledger))
    return result


def origin_candidates(origin, generation):
    _, receipts = reservations(origin)
    groups = {}
    for ordinal, receipt in receipts.items():
        if receipt.get("operation") == "native_ask":
            phase = receipt.get("phase")
            require(isinstance(phase, str) and re.fullmatch(r"answer:native:row-[0-9]+", phase), "native_phase_invalid")
            groups.setdefault(phase, []).append(ordinal)
    selected, excluded = [], []
    for ordinals in groups.values():
        evidence = [failure_metadata(origin, ordinal, generation, Path(origin) / "corpus") for ordinal in sorted(ordinals)]
        latest = evidence[-1]
        if any(item["reader_ever_reserved"] is True for item in evidence):
            latest = {**latest, "reader_ever_reserved": True}
        elif any(item["reader_ever_reserved"] is None for item in evidence):
            latest = {**latest, "reader_ever_reserved": None}
        eligible, reason = technical_eligibility(latest)
        item = {**latest, "eligible": eligible, "reason": reason}
        (selected if eligible else excluded).append(item)
    return sorted(selected, key=lambda item: item["ordinal"]), sorted(excluded, key=lambda item: item["ordinal"])


@dataclass(frozen=True)
class Inputs:
    origin: Path
    seal: Path
    declaration: Path
    judge_binary: Path
    judge_source: Path
    benchmark: Path
    upstream: Path

    def paths(self):
        return {key: str(Path(value).resolve()) for key, value in vars(self).items()}


def _implementation():
    files = [Path(__file__).resolve(), Path(__file__).resolve().parents[1] / "gptgrep_native_recovery.py"]
    return {str(path): digest(path) for path in files}


def initial_binding(inputs):
    paths = inputs.paths()
    origin, seal = Path(paths["origin"]), Path(paths["seal"])
    manifest = read_json(origin / "manifest.json")
    rows = manifest.get("source_rows")
    require(isinstance(rows, list) and all(type(row) is int and row >= 0 for row in rows)
            and len(rows) == len(set(rows)) == manifest.get("question_count"), "origin_cohort_binding_invalid")
    source = read_json(seal / "source.json")
    declaration = read_json(paths["declaration"])
    build = read_json(origin / "build.json")
    require(build.get("status") == "completed", "origin_index_incomplete")
    generation = build["generation_binding"]
    require(read_json(origin / "corpus/.gptgrep/CURRENT.json") == generation, "origin_generation_changed")
    require(re.fullmatch(r"[A-Za-z0-9-]+", generation["generation"]), "origin_generation_invalid")
    generation_root = origin / "corpus/.gptgrep/generations" / generation["generation"]
    require(digest(generation_root / "manifest.json") == generation["manifest_sha256"], "origin_generation_manifest_changed")
    documents = read_json(generation_root / "manifest.json")["documents"]
    for document in documents:
        require(document["path"] in manifest["source_hashes"]
                and document["source_sha256"] == manifest["source_hashes"][document["path"]]
                and re.fullmatch(r"[0-9a-f]{24}", document["id"]), "origin_document_binding_invalid")
        require(digest(generation_root / "text" / (document["id"] + ".txt")) == document["text_sha256"], "origin_canonical_text_changed")
    require({document["path"] for document in documents} == set(manifest["source_hashes"]), "origin_document_coverage_changed")
    for name, expected in manifest["source_hashes"].items():
        path = origin / "corpus" / name
        require(path.resolve().is_relative_to(origin / "corpus") and digest(path) == expected, "origin_source_changed")
    require(source["source_revision"] == declaration["source_revision"], "source_revision_mismatch")
    require(digest(seal / "source.json") == declaration["source_seal_sha256"], "source_seal_mismatch")
    binary = digest(seal / "gptgrep")
    require(binary == source["binary_sha256"] == manifest["binary_sha256"] == declaration["binary_sha256"], "native_binary_mismatch")
    require(digest(paths["judge_binary"]) == manifest["judge_binary_sha256"] == declaration["judge_binary_sha256"], "judge_binary_mismatch")
    runner = seal / "runner/scripts/gptgrep_system_eval.py"
    require(digest(runner) == manifest["runner_sha256"], "frozen_runner_mismatch")
    frozen = source.get("frozen_runner_files")
    require(isinstance(frozen, dict) and frozen, "frozen_runner_map_missing")
    actual_runner = {name: item["sha256"] for name, item in inventory(seal / "runner").items()}
    require(actual_runner == frozen, "frozen_runner_map_changed")
    for name, expected in manifest["adapter_files"].items():
        require(Path(name).name == name and digest(seal / "runner/scripts/pageindex_baseline" / name) == expected, "frozen_adapter_mismatch")
    budgets = declaration["budgets"]
    cap, rounds = budgets["max_outer_invocations_per_run"], budgets["technical_recovery_rounds_max_per_run"]
    require(type(cap) is int and cap == manifest["max_host_invocations"] and cap > 0, "invocation_cap_changed")
    require(type(rounds) is int and 1 <= rounds <= 2, "recovery_round_cap_invalid")
    consumed, receipts = reservations(origin)
    require(consumed == set(receipts), "origin_has_unfinished_reservations")
    require(all(item.get("status") in ("completed", "failed", "interrupted") for item in receipts.values()), "origin_not_terminal")
    require(len(consumed) <= cap, "origin_cap_exceeded")
    lock = read_json(seal / "runner/scripts/pageindex_baseline/sources.lock.json")
    external = {paths["declaration"]: digest(paths["declaration"]), paths["judge_binary"]: digest(paths["judge_binary"])}
    for label, directory in (("pageindex", paths["upstream"]), ("benchmark", paths["benchmark"]), ("judge", paths["judge_source"])):
        for name, expected in lock[label]["files"].items():
            path = Path(directory) / name
            require(path.resolve().is_relative_to(Path(directory)), "external_binding_escape")
            require(digest(path) == expected, "locked_input_mismatch")
            external[str(path)] = expected
    selected, excluded = origin_candidates(origin, generation["generation"])
    return {"schema_version": SCHEMA, "policy": POLICY, "paths": paths,
            "source_revision": source["source_revision"], "generation_binding": generation,
            "origin_manifest_sha256": digest(origin / "manifest.json"),
            "origin_files": inventory(origin), "seal_files": inventory(seal), "external_files": external,
            "implementation_files": _implementation(), "origin_reservations": len(consumed),
            "max_outer_invocations": cap, "max_rounds": rounds,
            "initial_eligible": selected, "initial_excluded": excluded,
            "allowed_origin_additions": "corpus/.gptgrep/host-attempts/attempt-<time>-<pid>-<ordinal>.jsonl",
            "continuation_semantics": "new_native_ask_repeats_planner_and_retrieval; no completed reader resampling"}


def verify_binding(binding, sidecar, *, permit_unregistered=False):
    require(binding["schema_version"] == SCHEMA and binding["policy"] == POLICY, "recovery_binding_invalid")
    require(binding["implementation_files"] == _implementation(), "recovery_implementation_changed")
    paths = binding["paths"]
    require(Path(sidecar) == sidecar_path(paths["origin"]), "recovery_location_changed")
    for directory, dirs, files in os.walk(sidecar, followlinks=False):
        require(all(not (Path(directory) / name).is_symlink() for name in dirs + files), "redirected_sidecar_artifact")
    verify_events(sidecar)
    for result in (Path(sidecar) / "rounds").glob("*/result.json"):
        verify_retained_files(sidecar, read_json(result, MAX_LEDGER).get("retained_sidecar_files", {}))
    require(inventory(paths["seal"]) == binding["seal_files"], "sealed_artifact_changed")
    for path, expected in binding["external_files"].items():
        require(digest(path) == expected, "external_input_changed")
    actual = inventory(paths["origin"])
    for name, expected in binding["origin_files"].items():
        require(actual.get(name) == expected, "origin_file_changed_or_deleted")
    accepted = {}
    for artifact in (Path(sidecar) / "calls").glob("*.host-ledgers.json"):
        for ledger in read_json(artifact)["ledgers"]:
            name = ledger["origin_relative_path"]
            require(name not in accepted, "native_ledger_adopted_twice")
            accepted[name] = {"sha256": ledger["sha256"], "bytes": ledger["bytes"]}
    extra = {name: info for name, info in actual.items() if name not in binding["origin_files"]}
    require(all(Path(name).parent == Path("corpus/.gptgrep/host-attempts") and NATIVE_LEDGER.fullmatch(Path(name).name) for name in extra), "undeclared_origin_addition")
    require(all(extra.get(name) == value for name, value in accepted.items()), "adopted_native_ledger_changed")
    if not permit_unregistered:
        require(extra == accepted, "unbound_native_ledger_addition")
    used, _ = reservations(sidecar)
    require(binding["origin_reservations"] + len(used) <= binding["max_outer_invocations"], "combined_invocation_cap_exceeded")
    return extra


def verify_retained_files(root, retained):
    for name, prior in retained.items():
        path = Path(root) / name
        require(path.resolve().is_relative_to(Path(root)), "prior_sidecar_path_invalid")
        if name in ("events.jsonl", "host-calls.jsonl"):
            with regular(path).open("rb") as stream:
                prefix = stream.read(prior["bytes"])
            require(hashlib.sha256(prefix).hexdigest() == prior["sha256"], "append_only_journal_changed")
        else:
            require(digest(path) == prior["sha256"], "prior_sidecar_file_changed")


def _outcomes(sidecar):
    result = {}
    for path in sorted((Path(sidecar) / "rounds").glob("*/outcomes/*.json")):
        value = read_json(path)
        result.setdefault(value["origin_ordinal"], []).append((path, value))
    return result


def _next_selection(binding, sidecar, before_round=None):
    outcomes = _outcomes(sidecar)
    selected = []
    for initial in binding["initial_eligible"]:
        ordinal = initial["ordinal"]
        previous = [(path, value) for path, value in outcomes.get(ordinal, [])
                    if before_round is None or value["round"] < before_round]
        if not previous:
            selected.append({"origin_ordinal": ordinal, "origin_request_sha256": initial["request_sha256"]})
        elif previous[-1][1]["status"] == "pre_reader_failure":
            last_path, last = previous[-1]
            allowed, _ = technical_eligibility(last["failure_metadata"])
            require(allowed, "recovery_outcome_inconsistent")
            selected.append({"origin_ordinal": ordinal, "origin_request_sha256": initial["request_sha256"],
                             "prior_outcome": str(last_path.relative_to(sidecar)), "prior_outcome_sha256": digest(last_path)})
    return selected


def prepare_plan(inputs):
    """Create an immutable plan; no frozen module import or model invocation."""
    with locked(inputs.origin) as sidecar:
        manifest_path = sidecar / "manifest.json"
        if manifest_path.exists():
            binding = read_json(manifest_path, MAX_LEDGER)
            require(binding["paths"] == inputs.paths(), "recovery_inputs_changed")
            verify_binding(binding, sidecar)
        else:
            require(set(path.name for path in sidecar.iterdir()) == {".owner.lock"}, "unbound_recovery_directory")
            binding = initial_binding(inputs)
            new_json(manifest_path, binding)
            append_event(sidecar, "bound", artifact="manifest.json", sha256=digest(manifest_path))
        plans = sorted((sidecar / "rounds").glob("*/plan.json"))
        if plans and not plans[-1].with_name("result.json").exists():
            return {"plan": str(plans[-1]), "sha256": digest(plans[-1]), "status": "existing_unfinished_plan"}
        require(len(plans) < binding["max_rounds"], "recovery_round_cap_reached")
        selected = _next_selection(binding, sidecar)
        require(selected, "no_eligible_technical_recovery")
        reserved, _ = reservations(sidecar)
        remaining = binding["max_outer_invocations"] - binding["origin_reservations"] - len(reserved)
        require(remaining >= len(selected), "insufficient_recovery_call_budget")
        number = len(plans) + 1
        path = sidecar / "rounds" / f"{number:04d}" / "plan.json"
        new_json(path, {"schema_version": SCHEMA, "round": number, "policy": POLICY,
                       "manifest_sha256": digest(manifest_path), "origin": binding["paths"]["origin"],
                       "selected": selected, "remaining_outer_reservations": remaining,
                       "prior_sidecar_files": inventory(sidecar), "new_native_asks_max": len(selected),
                       "new_judges_max": len(selected), "no_judge_retry": True})
        append_event(sidecar, "round_planned", round=number, artifact=str(path.relative_to(sidecar)), sha256=digest(path))
        return {"plan": str(path), "sha256": digest(path), "status": "plan_prepared",
                "eligible_origin_ordinals": [item["origin_ordinal"] for item in selected], "new_calls": 0}


class FrozenRuntime:
    """Use only the bound frozen runner for inference and evidence checks."""
    def __init__(self, binding):
        self.binding = binding
        paths = binding["paths"]
        runner = Path(paths["seal"]) / "runner/scripts/gptgrep_system_eval.py"
        scripts = runner.parent
        adapters = scripts / "pageindex_baseline"
        for name in ("bridge", "locks", "profiles", "cohorts", "role_hosts", "run", "native_models"):
            loaded = sys.modules.get(name)
            require(loaded is None or Path(loaded.__file__).resolve() == adapters / f"{name}.py", "nonfrozen_adapter_already_imported")
        sys.dont_write_bytecode = True
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        sys.path.insert(0, str(adapters))
        spec = importlib.util.spec_from_file_location("frozen_native_recovery_runner", runner)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)
        self.manifest = read_json(Path(paths["origin"]) / "manifest.json")
        checked = self.runner.locks.verify(Path(paths["upstream"]), Path(paths["benchmark"]), Path(paths["judge_source"]))
        require(checked == self.manifest["source_and_dependencies"], "frozen_dependencies_changed")
        self.corpus = Path(paths["origin"]) / "corpus"
        self.canonical = self.runner.native_snapshot(self.corpus, self.manifest["source_hashes"])
        require(read_json(self.corpus / ".gptgrep/CURRENT.json") == binding["generation_binding"], "generation_changed")
        self.constants = self.runner.judge_constants(Path(paths["judge_source"]))
        profile = self.manifest["profile"]["roles"]["chat"]
        self.args = SimpleNamespace(model=profile["model"], reasoning_effort=profile["reasoning_effort"],
            service_tier=profile["service_tier"], codex_bin=self.manifest["codex_bin"], codex_home=Path(self.manifest["codex_home"]),
            jev_model=self.manifest["jev_model_requested"], timeout=self.manifest["host_timeout_secs"],
            max_tool_calls=self.manifest["max_tool_calls"], max_input_bytes=self.manifest["host_input_cap"],
            experimental_query_plan=self.manifest.get("query_strategy", {}).get("experimental_query_plan", False),
            experimental_evidence_roles=self.manifest.get("query_strategy", {}).get("experimental_evidence_roles", False),
            planner_model=self.manifest.get("query_strategy", {}).get("planner", {}).get("model"))

    def host(self, sidecar):
        remaining = self.binding["max_outer_invocations"] - self.binding["origin_reservations"]
        return self.runner.LocalCodex(Path(self.binding["paths"]["judge_binary"]), self.args.codex_bin,
            self.args.codex_home, sidecar, remaining, self.args.timeout, self.args.model,
            self.args.reasoning_effort, self.args.max_input_bytes, service_tier=self.args.service_tier,
            reader_concurrency=1, judge_concurrency=1)

    def row(self, payload):
        match = re.fullmatch(r"answer:native:row-([0-9]+)", payload["phase"])
        require(match is not None, "origin_phase_invalid")
        number = int(match[1])
        require(number in self.manifest["source_rows"], "origin_row_not_declared")
        row = {"source_row": number, "question": payload["question"], "doc_id": payload["document"]}
        require(self.runner.reader_payload(row, self.args) == payload, "original_inference_arguments_changed")
        return row

    def ask(self, shared, payload, on_reserved):
        arguments = self.runner.ask_arguments(Path(self.binding["paths"]["seal"]) / "gptgrep", self.corpus, self.row(payload), self.args)
        return self.runner.invoke_native(shared, arguments, payload, self.args.timeout + 60, on_reserved=on_reserved)

    def completed_reader(self, shared, ordinal, payload):
        result = self.runner.completed_host_response(ordinal, payload, self.args.model, self.args.reasoning_effort,
                                                     payload["phase"], shared, self.args.service_tier)
        if result is None:
            return None
        report, _ = result
        require(report["generation"] == self.binding["generation_binding"]["generation"], "response_generation_changed")
        require(isinstance(report.get("answer"), str), "reader_answer_unavailable")
        self.runner.verify_native_evidence(report, self.corpus,
            {payload["document"]: self.manifest["source_hashes"][payload["document"]]}, self.canonical)
        return report

    def native_ledgers(self, shared, lineage, payload):
        receipt = shared.call_by_ordinal(lineage["sidecar_ordinal"])
        if receipt is None:
            return []
        paths = self.runner.owned_ledger_paths(self.corpus, receipt, lineage["prior_ledgers"],
                                               not_before_unix_ns=lineage["reservation_observed_unix_ns"])
        result = []
        for path in paths:
            optional = {}
            if "planner_profile" in inspect.signature(self.runner.ledger_recovery).parameters:
                optional["planner_profile"] = self.manifest.get("query_strategy", {}).get("planner")
            recovered = self.runner.ledger_recovery(path, generation=self.binding["generation_binding"]["generation"],
                query_sha256=hashlib.sha256(payload["question"].encode()).hexdigest(), document=payload["document"],
                reader_profile=self.manifest["profile"]["roles"]["chat"], planned=self.args.experimental_query_plan, **optional)
            require(recovered["case_identity_verified"], "new_native_ledger_unbound")
            result.append({"origin_relative_path": str(path.relative_to(self.binding["paths"]["origin"])),
                           "sha256": digest(path), "bytes": path.stat().st_size, "accounting": recovered})
        return result

    def judge(self, shared, payload, report, on_reserved, prior_ordinal=None):
        # Gold is opened only after a valid reader, and never passed to eligibility.
        questions = self.runner.locks.read_json(Path(self.binding["paths"]["benchmark"]) / "questions.json")
        rows = [{"source_row": number, **questions[number]} for number in self.manifest["source_rows"]]
        require(self.runner.fingerprint(rows) == self.manifest["question_sha256"], "judge_input_cohort_changed")
        source_row = self.row(payload)["source_row"]
        row = next(row for row in rows if row["source_row"] == source_row)
        require(row["question"] == payload["question"] and row["doc_id"] == payload["document"], "judge_case_binding_changed")
        constants = self.constants
        response = report["answer"]
        prompt = constants["PROMPT"].format(question=" ".join(row["question"].split()), answer=row["answer"],
            answer_format=row["answer_format"], response=response[:constants["MAX_RESPONSE_CHARS"]])
        host = self.runner.RoleHost(shared, "judge", self.manifest["profile"]["roles"]["judge"], phase=f"judge:native:row-{source_row}")
        if prior_ordinal is not None:
            recovered = self.runner.recover_judge(prior_ordinal, prompt, constants, host)
            require(recovered is not None, "prior_judge_unavailable_no_resampling")
            verdict, _ = recovered
        else:
            verdict, _ = host.complete(prompt, {}, constants["SCHEMA"], on_reserved=on_reserved)
        require(isinstance(verdict, dict) and type(verdict.get("equivalent")) is bool, "judge_verdict_invalid")
        return {"status": "completed", "equivalent": verdict["equivalent"],
                "rubric_sha256": self.runner.fingerprint(constants["PROMPT"]),
                "schema_sha256": self.runner.fingerprint(constants["SCHEMA"])}


def _quiescent(sidecar):
    reserved, receipts = reservations(sidecar)
    for ordinal in reserved - set(receipts):
        path = Path(sidecar) / "calls" / f"{ordinal:05d}.process-start.json"
        if path.exists():
            pid = read_json(path).get("host_pid")
            require(type(pid) is int and pid > 1, "pending_process_identity_missing")
            for signal in (os.kill, os.killpg):
                try:
                    signal(pid, 0)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    raise RecoveryError("pending_process_quiescence_unproven")
                raise RecoveryError("owned_recovery_process_still_active")


def _lineages(sidecar, round_number, origin_ordinal, operation):
    found = []
    for path in (Path(sidecar) / "calls").glob("*.lineage.json"):
        value = read_json(path)
        if (value.get("round"), value.get("origin_ordinal"), value.get("operation")) == (round_number, origin_ordinal, operation):
            found.append(value)
    require(len(found) <= 1, "duplicate_lineage_operation")
    return found[0] if found else None


def _adopt_ledgers(binding, sidecar, runtime, shared, lineage, payload):
    path = Path(sidecar) / "calls" / f"{lineage['sidecar_ordinal']:05d}.host-ledgers.json"
    if not path.exists():
        ledgers = runtime.native_ledgers(shared, lineage, payload)
        new_json(path, {"origin_ordinal": lineage["origin_ordinal"], "sidecar_ordinal": lineage["sidecar_ordinal"], "ledgers": ledgers})
        append_event(sidecar, "native_ledgers_bound", artifact=str(path.relative_to(sidecar)), sha256=digest(path))
    verify_binding(binding, sidecar)


def run_plan(plan_path, expected_sha256, *, runtime_factory=FrozenRuntime):
    plan_path = Path(plan_path).resolve()
    require(digest(plan_path) == expected_sha256, "recovery_plan_digest_mismatch")
    plan = read_json(plan_path, MAX_LEDGER)
    with locked(plan["origin"], create=False) as sidecar:
        require(plan_path == sidecar / "rounds" / f"{plan['round']:04d}" / "plan.json", "recovery_plan_location_invalid")
        binding = read_json(sidecar / "manifest.json", MAX_LEDGER)
        require(digest(sidecar / "manifest.json") == plan["manifest_sha256"], "recovery_manifest_changed")
        require(1 <= plan["round"] <= binding["max_rounds"], "recovery_round_cap_reached")
        result_path = plan_path.with_name("result.json")
        verify_binding(binding, sidecar, permit_unregistered=True)
        require(plan["selected"] == _next_selection(binding, sidecar, plan["round"]), "automatic_selection_changed")
        verify_retained_files(sidecar, plan["prior_sidecar_files"])
        if result_path.exists():
            verify_binding(binding, sidecar)
            return read_json(result_path)
        _quiescent(sidecar)
        runtime = runtime_factory(binding)
        shared = runtime.host(sidecar)
        origin = Path(binding["paths"]["origin"])
        corpus = origin / "corpus"
        try:
            # No reservation lacking pre-launch lineage can authorize a replacement.
            reserved, _ = reservations(sidecar)
            require(all((sidecar / "calls" / f"{number:05d}.lineage.json").exists() for number in reserved), "reservation_lineage_missing")
            for selected in plan["selected"]:
                ordinal = selected["origin_ordinal"]
                payload_path = origin / "calls" / f"{ordinal:05d}.request.json"
                require(digest(payload_path) == selected["origin_request_sha256"], "original_request_changed")
                payload = read_json(payload_path)
                outcome_path = plan_path.parent / "outcomes" / f"{ordinal:05d}.json"
                if outcome_path.exists():
                    continue
                if "prior_outcome" in selected:
                    prior = sidecar / selected["prior_outcome"]
                    require(digest(prior) == selected["prior_outcome_sha256"], "prior_recovery_outcome_changed")
                    require(technical_eligibility(read_json(prior)["failure_metadata"])[0], "prior_attempt_not_recoverable")
                else:
                    require(technical_eligibility(failure_metadata(origin, ordinal, binding["generation_binding"]["generation"], corpus))[0], "origin_attempt_not_recoverable")
                def reserve(operation):
                    def callback(receipt):
                        require(binding["origin_reservations"] + receipt["ordinal"] <= binding["max_outer_invocations"], "combined_invocation_cap_exceeded")
                        lineage = {"round": plan["round"], "origin_ordinal": ordinal, "operation": operation,
                            "origin_request_sha256": selected["origin_request_sha256"], "sidecar_ordinal": receipt["ordinal"],
                            "combined_reservation_ordinal": binding["origin_reservations"] + receipt["ordinal"],
                            "reservation_observed_unix_ns": time.time_ns(),
                            "prior_ledgers": sorted(path.name for path in (corpus / ".gptgrep/host-attempts").glob("*.jsonl"))}
                        path = sidecar / "calls" / f"{receipt['ordinal']:05d}.lineage.json"
                        new_json(path, lineage)
                        append_event(sidecar, "call_reserved", artifact=str(path.relative_to(sidecar)), sha256=digest(path), **lineage)
                    return callback
                lineage = _lineages(sidecar, plan["round"], ordinal, "native_ask")
                if lineage is None:
                    verify_binding(binding, sidecar)
                    runtime.ask(shared, payload, reserve("native_ask"))
                    lineage = _lineages(sidecar, plan["round"], ordinal, "native_ask")
                    require(lineage is not None, "native_ask_reservation_missing")
                _adopt_ledgers(binding, sidecar, runtime, shared, lineage, payload)
                native_ordinal = lineage["sidecar_ordinal"]
                outcome = {"schema_version": SCHEMA, "round": plan["round"], "origin_ordinal": ordinal,
                    "sidecar_native_ordinal": native_ordinal, "reader_status": "unavailable", "judge_status": "unjudged",
                    "equivalent": None, "status": "terminal_technical_failure"}
                try:
                    report = runtime.completed_reader(shared, native_ordinal, payload)
                    if report is None:
                        failure = failure_metadata(sidecar, native_ordinal, binding["generation_binding"]["generation"], corpus, payload)
                        eligible, reason = technical_eligibility(failure)
                        outcome.update(status="pre_reader_failure" if eligible else "terminal_technical_failure",
                                       failure_metadata=failure, reason=reason)
                    else:
                        outcome["reader_status"] = "valid"
                        outcome["reader_response_sha256"] = digest(sidecar / "calls" / f"{native_ordinal:05d}.response.json")
                        judge_lineage = _lineages(sidecar, plan["round"], ordinal, "judge")
                        try:
                            verdict = runtime.judge(shared, payload, report, reserve("judge"),
                                judge_lineage["sidecar_ordinal"] if judge_lineage else None)
                            judge_lineage = _lineages(sidecar, plan["round"], ordinal, "judge")
                            require(judge_lineage is not None, "judge_reservation_missing")
                            outcome.update(status="valid_pair", judge_status="completed", equivalent=verdict["equivalent"],
                                sidecar_judge_ordinal=judge_lineage["sidecar_ordinal"], judge=verdict)
                        except Exception as error:
                            attempted_judge = _lineages(sidecar, plan["round"], ordinal, "judge")
                            outcome.update(status="reader_valid_judge_unavailable", judge_status="unavailable",
                                           judge_error_type=type(error).__name__,
                                           sidecar_judge_ordinal=attempted_judge["sidecar_ordinal"] if attempted_judge else None)
                except Exception as error:
                    outcome.update(status="terminal_validation_failure", reason=type(error).__name__)
                verify_binding(binding, sidecar)
                new_json(outcome_path, outcome)
                append_event(sidecar, "outcome_retained", artifact=str(outcome_path.relative_to(sidecar)), sha256=digest(outcome_path))
            verify_binding(binding, sidecar)
            reserved, _ = reservations(sidecar)
            result = {"schema_version": SCHEMA, "round": plan["round"], "status": "terminal",
                      "plan_sha256": expected_sha256, "origin_reservations": binding["origin_reservations"],
                      "sidecar_reservations": len(reserved), "combined_reservations": binding["origin_reservations"] + len(reserved),
                      "retained_sidecar_files": inventory(sidecar),
                      "outcomes": [{"path": str(path.relative_to(sidecar)), "sha256": digest(path)} for path in sorted((plan_path.parent / "outcomes").glob("*.json"))]}
            new_json(result_path, result)
            append_event(sidecar, "round_terminal", artifact=str(result_path.relative_to(sidecar)), sha256=digest(result_path))
            return result
        finally:
            shared.close(cancel=True)


def analyze(origin):
    """Read-only lineage view; absent verdicts remain null, never false."""
    with locked(origin, create=False) as sidecar:
        binding = read_json(sidecar / "manifest.json", MAX_LEDGER)
        verify_binding(binding, sidecar)
        outcomes = _outcomes(sidecar)
        selected = []
        for ordinal, attempts in sorted(outcomes.items()):
            # A valid reader is frozen even when its judge is unavailable.
            first = next(((path, value) for path, value in attempts if value["reader_status"] == "valid"), None)
            selected.append({"origin_ordinal": ordinal,
                "selected_sidecar_outcome": str(first[0].relative_to(sidecar)) if first else None,
                "reader_status": first[1]["reader_status"] if first else "origin_retained",
                "judge_status": first[1]["judge_status"] if first else "unjudged",
                "equivalent": first[1]["equivalent"] if first else None,
                "all_attempts": [{"path": str(path.relative_to(sidecar)), "sha256": digest(path), "status": value["status"]} for path, value in attempts]})
        reserved, _ = reservations(sidecar)
        origin_manifest = read_json(Path(binding["paths"]["origin"]) / "manifest.json")
        return {"schema_version": SCHEMA, "origin": binding["paths"]["origin"],
            "origin_manifest_sha256": binding["origin_manifest_sha256"], "origin_summary_sha256": binding["origin_files"].get("summary.json", {}).get("sha256"),
            "unchanged_origin_outcomes": True, "question_denominator": origin_manifest["question_count"],
            "recovery_lineages": selected, "initial_excluded": binding["initial_excluded"],
            "combined_reservations": binding["origin_reservations"] + len(reserved),
            "comparison_eligible": False, "G5_accepted": False,
            "limits": ["Lineage view only; independent audit must admit any combined comparison.",
                       "First valid reader/judge retained without best-of; failed or absent verdicts remain unavailable."]}
