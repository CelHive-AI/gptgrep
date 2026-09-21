"""Capability admission and scoring eligibility, separate from raw judge outcomes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def answer_evidence(answer: dict) -> dict:
    pages = answer.get("accessed_physical_pages") or []
    observed = bool(pages) and all(type(page) is int and page >= 1 for page in pages)
    metadata = answer.get("index_metadata_supplied") is True
    summary = answer.get("index_summary_supplied") is True
    kind = "raw_pages" if observed else ("index_summary_or_metadata" if metadata or summary else "none")
    return {"evidence_access": kind, "raw_page_evidence": observed,
            "summary_evidence_supplied": summary, "metadata_evidence_supplied": metadata,
            "citation_fidelity_verified": None}


def scoring_summary(records: list[dict], denominator: int, adapter_verified: bool = False) -> dict:
    completed = [row for row in records if row.get("status") == "completed"]
    evidence = [answer_evidence(row) for row in completed]
    page_count = sum(row["raw_page_evidence"] for row in evidence)
    judged = [row for row in records if row.get("judge", {}).get("status") == "completed"]
    correct = sum(row["judge"]["equivalent"] is True for row in judged)
    abstained = sum(row["judge"]["abstained"] is True for row in judged)
    all_judged = denominator > 0 and len(judged) == denominator
    eligible = adapter_verified and all_judged and len(completed) == denominator
    recalls = [row["page_access_recall"] for row in completed if isinstance(row.get("page_access_recall"), (int, float))]
    return {
        "question_denominator": denominator,
        "adapter_capability_verified": adapter_verified,
        "completed_responses": len(completed), "raw_page_evidence_responses": page_count,
        "zero_page_responses": len(completed) - page_count,
        "evidence_access_counts": {kind: sum(row["evidence_access"] == kind for row in evidence)
                                   for kind in ("raw_pages", "index_summary_or_metadata", "none")},
        "summary_evidence_responses": sum(row["summary_evidence_supplied"] for row in evidence),
        "mean_page_access_recall": sum(recalls) / denominator if denominator and len(recalls) == denominator else None,
        "citation_fidelity_verified": None,
        "judged": len(judged), "correct": correct, "abstained": abstained,
        "committed_answers": len(judged) - abstained,
        "baseline_eligible": eligible,
        "answer_equivalence_accuracy": correct / denominator if eligible else None,
        "observed_judge_accuracy": correct / denominator if all_judged else None,
        "observed_judge_accuracy_lower_bound": correct / denominator if denominator else None,
        "judge_unavailable": max(0, denominator - len(judged)),
    }


def index_context_observation(items: list) -> dict:
    """Observe index context actually carried to the model, without judging its claims."""
    kinds = set()
    summary = False

    def text_parts(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from text_parts(item)
        elif isinstance(value, dict):
            for key in ("text", "content"):
                if key in value:
                    yield from text_parts(value[key])

    def has_description(value):
        if not isinstance(value, dict):
            return False
        description = value.get("description")
        return isinstance(description, str) and bool(description.strip()) and description != "No description provided"

    def has_tree_summary(value):
        if isinstance(value, list):
            return any(has_tree_summary(item) for item in value)
        if not isinstance(value, dict):
            return False
        return any(isinstance(value.get(key), str) and bool(value[key].strip()) for key in ("summary", "prefix_summary")) or has_tree_summary(value.get("nodes", []))

    # The pinned SDK prepends its controlled targeting block, before the user's question.
    if items:
        for text in text_parts(items[0]):
            for marker in ("Document metadata: ", "Documents metadata: "):
                if marker not in text:
                    continue
                try:
                    metadata, _ = json.JSONDecoder().raw_decode(text.split(marker, 1)[1])
                except ValueError:
                    continue
                entries = metadata if isinstance(metadata, list) else [metadata]
                if any(isinstance(entry, dict) and entry.get("name") for entry in entries):
                    kinds.add("target_metadata")
                    summary |= any(has_description(entry) for entry in entries)

    calls = {item.get("call_id"): item for item in items if isinstance(item, dict) and item.get("type") == "function_call"}
    def envelopes(value):
        if isinstance(value, str):
            try:
                yield from envelopes(json.loads(value))
            except ValueError:
                return
        elif isinstance(value, list):
            for item in value:
                yield from envelopes(item)
        elif isinstance(value, dict):
            if "success" in value or "error" in value:
                yield value
                return
            for key in ("text", "content"):
                if key in value:
                    yield from envelopes(value[key])
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        name = calls.get(item.get("call_id"), {}).get("name")
        if name not in ("get_document", "get_document_structure", "browse_documents"):
            continue
        for result in envelopes(item.get("output")):
            if result.get("success") is not True:
                continue
            kinds.add(name)
            if name == "get_document_structure":
                summary |= has_tree_summary(result.get("structure"))
            elif name == "get_document":
                summary |= has_description(result)
            else:
                summary |= any(has_description(doc) for doc in result.get("documents", []) if isinstance(doc, dict))
    return {"index_metadata_supplied": bool(kinds), "index_summary_supplied": summary,
            "index_context_kinds": sorted(kinds)}


def runtime_binding(binary: Path, model: str, effort: str, codex_home: Path, max_input_bytes: int) -> dict:
    return {
        "host_binary_sha256": file_sha(binary),
        "model": model, "reasoning_effort": effort, "codex_home": str(codex_home.resolve()),
        "max_input_bytes": max_input_bytes,
        "adapter_files": {name: file_sha(HERE / name) for name in (
            "bridge.py", "transports.py", "capability.py", "capability_probe.py",
        )},
        "source_lock_sha256": file_sha(HERE / "sources.lock.json"),
    }


def verify_receipt(path: Path, expected: dict) -> dict:
    receipt = json.loads(path.read_text())
    if receipt.get("schema_version") != "gptgrep.pageindex.capability.v1" or receipt.get("status") != "capability_verified":
        raise ValueError("The live tool-capability probe has not passed")
    if receipt.get("binding") != expected:
        raise ValueError("Capability receipt does not match the current host/adapter/profile/bounds")
    if receipt.get("real_host_invocations", 0) < 2 or receipt.get("completed_host_turns", 0) < 2:
        raise ValueError("Capability receipt lacks the live decision/result roundtrip")
    if 2 not in receipt.get("accessed_physical_pages", []) or receipt.get("answer_correct") is not True:
        raise ValueError("Capability receipt lacks source-page access and the synthetic answer check")
    if receipt.get("answer_withheld_from_initial_request") is not True:
        raise ValueError("Capability fixture answer was not isolated from the first decision")
    sessions = receipt.get("native_sessions", [])
    if len(sessions) < 2:
        raise ValueError("Capability receipt has no live native-session provenance")
    identities = set()
    for session in sessions:
        ordinal = session.get("ordinal")
        if type(ordinal) is not int or not 1 <= ordinal <= receipt["real_host_invocations"]:
            raise ValueError("Invalid capability host-call ordinal")
        response_path = path.parent / "calls" / f"{ordinal:05d}.response.json"
        if file_sha(response_path) != session.get("response_sha256"):
            raise ValueError("Capability host response digest differs")
        response = json.loads(response_path.read_text())
        if response.get("status") != "completed" or response.get("auth_mode") != "chatgpt" or response.get("model_provider") != "openai":
            raise ValueError("Capability host response does not establish the required runtime")
        if response.get("model") != expected["model"] or response.get("requested_reasoning_effort") != expected["reasoning_effort"]:
            raise ValueError("Capability host model/effort differs")
        identity = (response.get("thread_id"), response.get("turn_id"))
        if not all(isinstance(value, str) and value for value in identity) or identity != (session.get("thread_id"), session.get("turn_id")):
            raise ValueError("Capability native-session identity differs")
        identities.add(identity)
    if len(identities) < 2:
        raise ValueError("Capability receipt reused a native turn")
    return {"receipt_sha256": file_sha(path), "status": receipt["status"], "binding": expected}
