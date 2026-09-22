"""Admission evidence for original SDK retries; this module never invokes a model."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import jsonschema

LEGACY_GUARD_ERROR = "An indexing model invocation failed; preserve its partial work without claiming a complete full index"
TEXT_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}},
               "required": ["text"], "additionalProperties": False}
SERVICE_ERRORS = {None, "other", "serverOverloaded", "rateLimitExceeded", "usageLimitExceeded",
                  "httpConnectionFailed", "responseStreamConnectionFailed", "internalServerError",
                  "responseStreamDisconnected", "responseTooManyFailedAttempts"}


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_bound(path: Path, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("Evidence file is absent, redirected or exceeds its bound")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("Evidence file exceeded its bound while reading")
    return raw


def read_json_bound(path: Path, maximum: int):
    raw = read_bound(path, maximum)
    def reject(value):
        raise ValueError("Non-finite evidence JSON")
    return json.loads(raw, parse_constant=reject), digest_bytes(raw)


def service_failure(call):
    if call.get("status") != "failed":
        return False
    if call.get("error_code") == "host_process_timeout":
        return call.get("timeout_cleanup", {}).get("reaped") is True
    protocol = call.get("host_protocol", {})
    if not isinstance(protocol, dict) or not (protocol.get("codex_error_info") is None or isinstance(protocol.get("codex_error_info"), str)):
        return False
    kinds = {"host_codex_terminal_error": "terminal_error", "host_codex_failed_turn": "failed_turn"}
    return (call.get("error_code") in kinds and protocol.get("kind") == kinds[call["error_code"]]
            and protocol.get("codex_error_info") in SERVICE_ERRORS and protocol.get("will_retry") in (None, False)
            and protocol.get("accounting_complete") is False)


def validate_call(run_dir, call, phase, profile, input_cap, output_cap):
    ordinal = call.get("ordinal")
    if type(ordinal) is not int or ordinal < 1:
        raise ValueError("Invalid indexing invocation ordinal")
    if (call.get("phase"), call.get("role"), call.get("requested_model"), call.get("requested_effort"), call.get("requested_service_tier")) != (
            phase, "index", profile["model"], profile["reasoning_effort"], profile["service_tier"]):
        raise ValueError("Indexing invocation phase/profile differs")
    request_path = run_dir / "calls" / f"{ordinal:05d}.request.json"
    response_path = run_dir / "calls" / f"{ordinal:05d}.response.json"
    request, request_sha = read_json_bound(request_path, input_cap)
    if request_sha != call.get("request_sha256") or not isinstance(request, dict):
        raise ValueError("Indexing request digest differs")
    if set(request) != {"instructions", "state", "schema"} or not isinstance(request["instructions"], str) or request["schema"] != TEXT_SCHEMA:
        raise ValueError("Indexing request does not have the fixed original transport schema")
    expected_parts = {"instructions_sha256": digest_bytes(request["instructions"].encode()),
                      "state_sha256": digest_bytes(canonical(request["state"])),
                      "schema_sha256": digest_bytes(canonical(request["schema"]))}
    if any(call.get(key) != value for key, value in expected_parts.items()):
        raise ValueError("Indexing request part bindings differ")
    if call.get("status") not in ("completed", "failed", "interrupted"):
        raise ValueError("Indexing invocation lacks a final receipt")
    if response_path.exists():
        report, response_sha = read_json_bound(response_path, output_cap + 65536)
        if response_sha != call.get("response_sha256"):
            raise ValueError("Indexing response digest differs")
    else:
        report = None
        if call.get("status") == "completed" or call.get("response_sha256"):
            raise ValueError("Indexing response evidence is missing")
    if call.get("status") == "completed":
        if not isinstance(report, dict) or (report.get("status"), report.get("model"), report.get("requested_reasoning_effort"),
                report.get("requested_service_tier"), report.get("auth_mode"), report.get("model_provider")) != (
                "completed", profile["model"], profile["reasoning_effort"], profile["service_tier"], "chatgpt", "openai"):
            raise ValueError("Completed indexing response profile/runtime differs")
        normalize = lambda tier: "priority" if tier in ("fast", "priority") else tier
        if (report.get("effective_reasoning_effort") not in (None, profile["reasoning_effort"])
                or (report.get("effective_service_tier") is not None
                    and normalize(report["effective_service_tier"]) != normalize(profile["service_tier"]))):
            raise ValueError("Completed indexing effective settings differ")
        identity = report.get("thread_id"), report.get("turn_id")
        if not all(isinstance(value, str) and value for value in identity) or identity != (call.get("thread_id"), call.get("turn_id")):
            raise ValueError("Completed indexing native identity differs")
        value = report.get("value")
        try:
            jsonschema.Draft202012Validator(TEXT_SCHEMA).validate(value)
        except jsonschema.ValidationError as error:
            raise ValueError("Completed indexing value fails the fixed response schema") from error
        if digest_bytes(canonical(value)) != call.get("value_sha256"):
            raise ValueError("Completed indexing value digest differs")
    return request_sha


def audit_index_calls(run_dir: Path, calls: list[dict], phase: str, profile: dict,
                      input_cap: int = 262144, output_cap: int = 131072) -> dict:
    """A later exact request may resolve a service failure; no failures are erased."""
    ordinals = [call.get("ordinal") for call in calls]
    if len(set(ordinals)) != len(ordinals):
        raise ValueError("Duplicate indexing invocation ordinals")
    hashes = {call["ordinal"]: validate_call(run_dir, call, phase, profile, input_cap, output_cap) for call in calls}
    completed = [call for call in calls if call["status"] == "completed"]
    failures = [call for call in calls if call["status"] != "completed"]
    recoveries, unresolved = [], []
    for failed in failures:
        if not service_failure(failed):
            unresolved.append({"ordinal": failed["ordinal"], "reason": "not_a_classified_service_failure"})
            continue
        later = []
        for success in completed:
            end, start = failed.get("process_finished_monotonic_ns"), success.get("process_started_monotonic_ns")
            if (success["ordinal"] > failed["ordinal"] and hashes[success["ordinal"]] == hashes[failed["ordinal"]]
                    and isinstance(failed.get("measurement_id"), str) and failed["measurement_id"]
                    and success.get("measurement_id") == failed["measurement_id"]
                    and type(end) is int and type(start) is int and 0 < end <= start):
                later.append(success)
        if not later:
            unresolved.append({"ordinal": failed["ordinal"], "reason": "no_validated_later_exact_request"})
        else:
            success = min(later, key=lambda call: call["ordinal"])
            recoveries.append({"failed_ordinal": failed["ordinal"], "completed_ordinal": success["ordinal"],
                               "request_sha256": hashes[failed["ordinal"]], "completed_response_sha256": success["response_sha256"]})
    durations = [call.get("elapsed_ms") for call in calls]
    known = [value for value in durations if type(value) in (int, float) and math.isfinite(value) and value >= 0]
    missing = len(calls) - len(known)
    return {"schema_version": "gptgrep.index-admission.v1", "complete": not unresolved,
            "basis": "Validated request-level resolution plus separately established original SDK completion; no per-node consumption claim",
            "host_invocations": len(calls), "completed_invocations": len(completed), "failed_or_interrupted_invocations": len(failures),
            "resolved_failures": recoveries, "unresolved_failures": unresolved,
            "host_call_ordinals": sorted(ordinals), "usage_missing": sum(call.get("usage") is None for call in calls),
            "host_wall_ms": math.fsum(known) if missing == 0 else None,
            "host_wall_ms_known_subtotal": math.fsum(known), "host_wall_ms_missing": missing,
            "host_elapsed_aggregation": "Sum of invocation durations, not elapsed index wall time; original index elapsed_ms is retained separately",
            "provider_request_count": None, "billing_usd": None, "all_attempts_retained": True}
