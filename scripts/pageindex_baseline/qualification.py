"""Qualify the local transport from a real benchmark SDK tool roundtrip.

No question, reference answer or evidence-page label participates in admission.
The caller validates returned page bytes against the locked SDK extraction.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def qualify(envelope: dict, source_name: str, verified_pages_by_call: dict[str, set[int]], run_dir: Path,
            call_ordinals: list[int], calls: list[dict], profile: dict, binding: dict) -> dict:
    result = {"schema_version": "gptgrep.pageindex.benchmark-qualification.v1",
              "verified": False, "basis": "same-run original SDK source-page tool roundtrip",
              "binding": binding, "requires_correct_answer": False}
    if not verified_pages_by_call:
        return {**result, "reason": "No source-verified SDK page output in this response"}
    selected = {call["ordinal"]: call for call in calls if call.get("ordinal") in call_ordinals}
    native = []
    for ordinal in call_ordinals:
        receipt = selected.get(ordinal, {})
        if receipt.get("status") != "completed":
            continue
        request_path = run_dir / "calls" / f"{ordinal:05d}.request.json"
        response_path = run_dir / "calls" / f"{ordinal:05d}.response.json"
        request_bytes, response_bytes = request_path.read_bytes(), response_path.read_bytes()
        if hashlib.sha256(request_bytes).hexdigest() != receipt.get("request_sha256") or hashlib.sha256(response_bytes).hexdigest() != receipt.get("response_sha256"):
            raise ValueError("Benchmark qualification request/response digest differs")
        request, response = json.loads(request_bytes), json.loads(response_bytes)
        if response.get("status") != "completed" or response.get("auth_mode") != "chatgpt" or response.get("model_provider") != "openai":
            continue
        if response.get("model") != profile["model"] or response.get("requested_reasoning_effort") != profile["reasoning_effort"]:
            continue
        identity = response.get("thread_id"), response.get("turn_id")
        if not all(isinstance(value, str) and value for value in identity):
            continue
        if identity != (receipt.get("thread_id"), receipt.get("turn_id")):
            raise ValueError("Benchmark qualification native session differs")
        native.append((ordinal, request, response, receipt))
    items = envelope.get("items", [])
    for call in items:
        if call.get("type") != "function_call" or call.get("name") != "get_page_content":
            continue
        verified_pages = verified_pages_by_call.get(call.get("call_id"))
        if not verified_pages:
            continue
        arguments = json.loads(call["arguments"])
        if arguments.get("doc_name") != source_name:
            continue
        outputs = [item for item in items if item.get("type") == "function_call_output" and item.get("call_id") == call.get("call_id")]
        for ordinal, _, response, receipt in native:
            planned = response.get("value", {}).get("tool_calls", [])
            if not any(item.get("name") == "get_page_content" and json.loads(item.get("arguments", "{}")) == arguments for item in planned):
                continue
            for later_ordinal, later_request, later_response, later_receipt in native:
                if later_ordinal <= ordinal or (later_response["thread_id"], later_response["turn_id"]) == (response["thread_id"], response["turn_id"]):
                    continue
                delivered = later_request.get("state", {}).get("input", [])
                if not any(digest(output) == digest(item) for output in outputs for item in delivered if isinstance(item, dict)):
                    continue
                return {**result, "verified": True, "call_id": call["call_id"],
                        "source_name": source_name, "verified_pages": sorted(verified_pages),
                        "sdk_call_sha256": digest(call), "sdk_output_sha256": [digest(output) for output in outputs],
                        "native_calls": [{key: entry.get(key) for key in ("ordinal", "requested_model", "requested_effort", "thread_id", "turn_id", "request_sha256", "response_sha256")}
                                         for entry in (receipt, later_receipt)]}
    return {**result, "reason": "No completed native decision followed by consumption of the actual SDK page output"}
