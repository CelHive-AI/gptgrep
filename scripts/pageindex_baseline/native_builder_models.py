"""Pure validation/accounting for the explicit raw-document enrichment protocol.

No model invocation, benchmark loading, or inference retry lives in this module.
The builder ledger is counted once across all outer enrichment invocations.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from decimal import Decimal

MAX_PLAN_BYTES = 64 * 1024 * 1024
MAX_LEDGER_BYTES = 256 * 1024 * 1024
MAX_RECORD_BYTES = 32 * 1024
MAX_OVERLAY_BYTES = 4 * 1024 * 1024
TOKEN_FIELDS = ("totalTokens", "inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
                "outputTokens", "reasoningOutputTokens")

QA_COST_THRESHOLD = "0.003607"


def require(condition, message):
    if not condition:
        raise ValueError(message)


class _RawFloat(float):
    def __new__(cls, raw):
        value = super().__new__(cls, raw)
        value.raw = raw
        return value


def encoded(value):
    # Rust serde_json's float spelling can differ from Python (e.g. 1e-6 vs
    # 1e-06). Retained ledger numbers keep their exact original spelling while
    # object keys are sorted as in Rust's Value-based hash preimage.
    def item(current):
        if isinstance(current, _RawFloat):
            return current.raw
        if isinstance(current, dict):
            return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + item(current[key]) for key in sorted(current)) + "}"
        if isinstance(current, list):
            return "[" + ",".join(item(value) for value in current) + "]"
        return json.dumps(current, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return item(value).encode()


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "enrichment_digest_invalid")
    return value


def integer(value, *, maximum=2**64 - 1, positive=False):
    require(type(value) is int and (1 if positive else 0) <= value <= maximum, "enrichment_integer_invalid")
    return value


def label(value):
    require(isinstance(value, str) and value == value.strip() and 0 < len(value.encode()) <= 256
            and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value), "enrichment_label_invalid")
    return value


def regular(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "enrichment_file_missing_or_redirected")
    require(all(not parent.is_symlink() for parent in path.parents), "enrichment_directory_redirected")
    return path


def digest(path):
    value = hashlib.sha256()
    with regular(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "enrichment_duplicate_json_key")
        result[key] = value
    return result


def parse(raw):
    def invalid(_):
        raise ValueError("enrichment_nonfinite_json")
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid, parse_float=_RawFloat)


def read_json(path, cap=MAX_PLAN_BYTES):
    path = regular(path)
    require(path.stat().st_size <= cap, "enrichment_json_byte_limit")
    return parse(path.read_bytes())


def validate_price_card(card):
    require(isinstance(card, dict) and card.get("schema_version") == "gptgrep.api-price-card.v1"
            and card.get("currency") == "USD" and card.get("token_unit") == 1_000_000, "price_card_schema_invalid")
    label(card.get("card_id"))
    require(isinstance(card.get("source_urls"), list) and 0 < len(card["source_urls"]) <= 16
            and all(isinstance(url, str) and len(url) <= 2048 and url.startswith("https://") for url in card["source_urls"]), "price_card_sources_invalid")
    require(isinstance(card.get("models"), dict) and 0 < len(card["models"]) <= 64, "price_card_models_invalid")
    for model, entry in card["models"].items():
        label(model)
        require(isinstance(entry.get("tiers"), dict) and 0 < len(entry["tiers"]) <= 16, "price_card_tiers_invalid")
        aliases = set()
        for tier, values in entry["tiers"].items():
            label(tier)
            require(isinstance(values.get("observed_tier_aliases"), list) and values["observed_tier_aliases"], "price_card_tier_aliases_invalid")
            for alias in values["observed_tier_aliases"]:
                label(alias)
                require(alias not in aliases, "price_card_ambiguous_tier")
                aliases.add(alias)
            bands = values.get("bands")
            require(isinstance(bands, list) and 0 < len(bands) <= 16, "price_card_bands_invalid")
            prior, names = 0, set()
            for index, band in enumerate(bands):
                label(band.get("id"))
                require(band["id"] not in names, "price_card_duplicate_band")
                names.add(band["id"])
                limit = band.get("max_input_tokens")
                if index == len(bands) - 1:
                    require(limit is None, "price_card_final_band_must_be_unbounded")
                else:
                    integer(limit, positive=True)
                    require(limit > prior, "price_card_band_bounds_invalid")
                    prior = limit
                for field in ("input", "cached_input", "cache_write_input", "output"):
                    value = band.get(field)
                    require(value is None and field == "cache_write_input" or isinstance(value, (int, float))
                            and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1_000_000,
                            "price_card_rate_invalid")
    return card


def _price_band(card, model, observed_tier, input_tokens, normalization=None):
    if card is None:
        return None, None, "price_card_unavailable"
    tiers = card["models"].get(model, {}).get("tiers", {})
    if not any(observed_tier in value["observed_tier_aliases"] for value in tiers.values()):
        return None, None, "observed_experiment_tier_unavailable_or_unpriced"
    for tier, value in tiers.items():
        selected = tier == "standard" if normalization == "standard" else observed_tier in value["observed_tier_aliases"]
        if selected:
            bands = value["bands"]
            if len(bands) == 1 or bands[0]["max_input_tokens"] is not None and input_tokens <= bands[0]["max_input_tokens"]:
                return tier, bands[0], None
            rates = [{key: band[key] for key in ("input", "cached_input", "cache_write_input", "output")} for band in bands]
            if all(value == rates[0] for value in rates):
                return tier, {**bands[0], "id": "identically_priced_bands"}, None
            # A logical turn may contain several provider requests. A large
            # aggregate total cannot establish each request's context band.
            return tier, None, "per_request_context_band_unavailable"
    return None, None, "observed_model_or_tier_unpriced"


def check_cli_contract(contract):
    option = contract.get("commands", {}).get("ask", {}).get("options", {}).get("navigation-overlay-sha256")
    enrich = contract.get("commands", {}).get("enrich", {})
    require(isinstance(option, dict) and option.get("type") == "string",
            "enrichment_navigation_cli_contract_unavailable_zero_model_preflight")
    require(enrich.get("plan_only_model_calls") == 0 and enrich.get("model_assisted") is True,
            "enrichment_plan_cli_contract_unavailable_zero_model_preflight")


def navigation_receipt(report, payload):
    expected = payload.get("navigation_overlay")
    if expected is None:
        return None
    nav = report.get("navigation")
    require(isinstance(nav, dict), "enrichment_navigation_report_missing")
    require(nav.get("schema_version") == "gptgrep.navigation-query.v1"
            and nav.get("status") in ("completed", "completed_empty")
            and nav.get("hint_scan_complete") is True, "enrichment_navigation_workflow_incomplete")
    for name in ("artifact_sha256", "generation", "manifest_sha256"):
        require(nav.get(name) == expected[name], "enrichment_navigation_binding_changed")
    require(nav.get("generation") == report.get("generation") and nav.get("document_scope") == payload["document"],
            "enrichment_navigation_scope_changed")
    return {name: nav[name] for name in ("schema_version", "status", "artifact_sha256", "generation",
                                        "manifest_sha256", "document_scope", "hint_scan_complete")}


def validate_plan(plan, expected_binding, document_metadata, canonical):
    require(isinstance(plan, dict) and plan.get("schema_version") == "gptgrep.enrichment-plan.v1",
            "enrichment_plan_schema_invalid")
    require(set(plan) == {"schema_version", "binding", "documents", "units", "unit_count",
                         "worst_case_builder_calls", "worst_case_jev_calls", "plan_sha256"}, "enrichment_plan_fields_invalid")
    binding = plan["binding"]
    require(isinstance(binding, dict), "enrichment_binding_invalid")
    for name, value in expected_binding.items():
        require(binding.get(name) == value, "enrichment_plan_config_or_source_changed")
    require(set(binding) == set(expected_binding) | {"builder_prompt_sha256", "builder_schema_sha256",
             "support_prompt_sha256", "support_schema_sha256"}, "enrichment_plan_binding_fields_invalid")
    for name in ("builder_prompt_sha256", "builder_schema_sha256", "support_prompt_sha256", "support_schema_sha256"):
        sha(binding[name])
    require(isinstance(plan["documents"], list) and isinstance(plan["units"], list), "enrichment_plan_arrays_invalid")
    expected_documents = [document_metadata[name] for name in sorted(document_metadata)]
    require(plan["documents"] == expected_documents, "enrichment_plan_document_coverage_changed")
    require(len(plan["units"]) <= 65536 and plan["unit_count"] == len(plan["units"])
            and plan["worst_case_builder_calls"] == len(plan["units"])
            and plan["worst_case_jev_calls"] == len(plan["units"]), "enrichment_plan_call_counts_invalid")
    require(len(plan["units"]) <= binding["max_builder_calls"] and len(plan["units"]) <= binding["max_jev_calls"],
            "enrichment_worst_case_exceeds_fixed_caps")
    offsets = {name: 0 for name in document_metadata}
    anchors = set()
    ordering = []
    for unit in plan["units"]:
        require(set(unit) == {"document", "anchor", "builder_input_sha256", "builder_input_bytes",
                             "worst_case_jev_template_sha256", "worst_case_jev_request_bytes"}, "enrichment_plan_unit_fields_invalid")
        document, anchor = unit["document"], unit["anchor"]
        name = document.get("path")
        require(name in document_metadata and document == document_metadata[name], "enrichment_plan_unit_document_changed")
        require(set(anchor) == {"anchor_id", "byte_start", "byte_end", "sha256"}, "enrichment_anchor_fields_invalid")
        label(anchor["anchor_id"])
        require(anchor["anchor_id"] not in anchors, "enrichment_duplicate_anchor")
        anchors.add(anchor["anchor_id"])
        start, end = integer(anchor["byte_start"]), integer(anchor["byte_end"])
        require(start == offsets[name] and start < end <= len(canonical[name])
                and end - start <= binding["window_bytes"], "enrichment_plan_window_gap_or_overlap")
        raw = canonical[name][start:end]
        raw.decode("utf-8")
        require(hashlib.sha256(raw).hexdigest() == sha(anchor["sha256"]), "enrichment_plan_raw_window_changed")
        offsets[name] = end
        ordering.append((name, start))
        sha(unit["builder_input_sha256"])
        sha(unit["worst_case_jev_template_sha256"])
        integer(unit["builder_input_bytes"], positive=True, maximum=binding["max_input_bytes"])
        integer(unit["worst_case_jev_request_bytes"], positive=True, maximum=65536)
    require(ordering == sorted(ordering) and all(offsets[name] == len(data) for name, data in canonical.items()),
            "enrichment_plan_incomplete_raw_traversal")
    require(fingerprint({name: plan[name] for name in ("binding", "documents", "units")}) == sha(plan["plan_sha256"]),
            "enrichment_plan_semantic_digest_changed")
    return plan


def _observation(observed, reservation, binding):
    if observed is None:
        return
    require(isinstance(observed, dict), "enrichment_observation_invalid")
    label(observed.get("model"))
    if reservation["kind"] == "builder":
        require(observed.get("model") == reservation["requested_model"], "enrichment_actual_model_changed")
        require(observed.get("provider") == "openai", "enrichment_actual_builder_provider_changed")
        label(observed.get("thread_id"))
        label(observed.get("turn_id"))
        require(observed.get("effective_reasoning_effort") in (None, binding["reasoning_effort"]), "enrichment_actual_effort_changed")
        tier = observed.get("effective_service_tier")
        require(tier is None or ("priority" if tier == "fast" else tier)
                == ("priority" if binding["service_tier"] == "fast" else binding["service_tier"]), "enrichment_actual_tier_changed")
        integer(observed.get("server_retry_notifications"))
    elif observed.get("response_id") is not None:
        label(observed["response_id"])
    usage = observed.get("usage")
    if usage is not None:
        require(isinstance(usage, dict), "enrichment_usage_invalid")
        if reservation["kind"] == "builder":
            require(set(usage) == {"total"} and isinstance(usage["total"], dict)
                    and set(usage["total"]) <= set(TOKEN_FIELDS), "enrichment_builder_usage_scope_invalid")
            for value in usage["total"].values():
                integer(value)
        else:
            require(set(usage) <= {"prompt_tokens", "completion_tokens", "total_tokens", "cost"}, "enrichment_jev_usage_scope_invalid")
            for key, value in usage.items():
                if key == "cost":
                    require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0, "enrichment_jev_cost_invalid")
                else:
                    integer(value)


def summarize_calls(reservations, receipts, kind, *, complete_chain, price_card=None):
    selected = [item for item in reservations if item["kind"] == kind]
    observed = [receipts[item["call_id"]].get("observed") if item["call_id"] < len(receipts) else None for item in selected]
    finished = [receipts[item["call_id"]] for item in selected if item["call_id"] < len(receipts)]
    totals = [((item or {}).get("usage") or {}).get("total", {}).get("totalTokens") if kind == "builder"
              else ((item or {}).get("usage") or {}).get("total_tokens") for item in observed]
    known = [value for value in totals if type(value) is int]
    overflow = sum(known) > 2**64 - 1
    summary = {"attempted_calls": len(selected), "completed_calls": sum(item["error_code"] is None for item in finished),
               "failed_calls": sum(item["error_code"] is not None for item in finished),
               "unobserved_calls": sum(item is None for item in observed),
               "missing_usage_calls": sum(item is None or item.get("usage") is None for item in observed),
               "models": sorted({item["model"] for item in observed if item is not None}),
               "known_total_tokens": sum(known) if known and not overflow else None,
               "missing_total_tokens": len(totals) - len(known), "total_tokens_overflowed": overflow}
    complete = complete_chain and len(finished) == len(selected) and not summary["failed_calls"] and not summary["unobserved_calls"]
    details = {"summary": summary, "accounting_complete": complete and len(known) == len(totals) and not overflow,
               "known_token_subtotal": sum(known), "total_tokens": sum(known) if complete and len(known) == len(totals) and not overflow else None,
               "logical_calls_known_subtotal": len(selected), "logical_calls": len(selected) if complete_chain else None,
               "physical_provider_request_count": None, "provider_billing_usd": None,
               "elapsed_ms_known_subtotal": sum(item["elapsed_ms"] for item in finished)}
    if kind == "builder":
        details["model_turns"] = [{"call_id": call["call_id"], "thread_id": item["thread_id"], "turn_id": item["turn_id"],
                                   "model": item["model"], "usage": item.get("usage")}
                                  for call, item in zip(selected, observed) if item is not None]
        details["token_fields"] = {field: {"known_subtotal": sum(((item or {}).get("usage") or {}).get("total", {}).get(field, 0) for item in observed),
                                             "missing_contributions": sum(field not in ((item or {}).get("usage") or {}).get("total", {}) for item in observed)}
                                   for field in TOKEN_FIELDS}
        price_records = []
        for call, observation in zip(selected, observed):
            receipt = receipts[call["call_id"]] if call["call_id"] < len(receipts) else None
            observation = observation or {}
            price_records.append({"attempt_id": f"builder-{call['call_id']}", "role": "builder",
                "model": observation.get("model"), "requested_model": call["requested_model"],
                "thread_id": observation.get("thread_id"), "turn_id": observation.get("turn_id"),
                "effective_service_tier": observation.get("effective_service_tier"), "usage": observation.get("usage"),
                "accounting_complete": bool(receipt is not None and receipt["error_code"] is None and observation)})
        details["api_price_equivalent"] = price_model_turns(price_records, unknown_groups=0 if complete_chain else 1, price_card=price_card, allow_known_zero=complete_chain and not selected)
        details["standard_normalized_api_price_equivalent"] = price_model_turns(
            price_records, unknown_groups=0 if complete_chain else 1, price_card=price_card, normalization="standard", allow_known_zero=complete_chain and not selected)
    else:
        costs = [((item or {}).get("usage") or {}).get("cost") for item in observed]
        values = [value for value in costs if isinstance(value, (int, float)) and not isinstance(value, bool)]
        details.update(known_cost_subtotal_usd=math.fsum(values) if values else None,
                       missing_cost_calls=len(costs) - len(values),
                       measured_cost_usd=math.fsum(values) if complete and len(values) == len(costs) and values else None)
    return details


def price_model_turns(records, *, unknown_groups=0, price_card=None, normalization=None,
                      expected_roles=None, allow_known_zero=False):
    """Price each actual model turn once; cached tokens are an input subset."""
    seen, steps = {}, []
    require(normalization in (None, "standard"), "price_normalization_invalid")
    subtotal = Decimal(0)
    missing = unknown_groups
    for ordinal, record in enumerate(records):
        identity = (record.get("thread_id"), record.get("turn_id"))
        signature = {key: record.get(key) for key in ("role", "model", "effective_service_tier", "usage", "accounting_complete")}
        if all(identity):
            if identity in seen:
                require(seen[identity] == signature, "price_projection_conflicting_model_turn")
                continue
            seen[identity] = signature
        observed_tier = record.get("effective_service_tier")
        total = (record.get("usage") or {}).get("total") or {}
        values = {key: total.get(key) for key in ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens", "outputTokens")}
        valid = all(type(values[key]) is int and values[key] >= 0 for key in ("inputTokens", "cachedInputTokens", "outputTokens"))
        valid = valid and values["cachedInputTokens"] <= values["inputTokens"]
        tier, rates, reason = _price_band(price_card, record.get("model"), observed_tier, values["inputTokens"], normalization) if valid else (None, None, "token_metering_incomplete_or_invalid")
        amount = None
        if rates is not None and valid:
            writes = values["cacheWriteInputTokens"]
            if rates["cache_write_input"] == rates["input"]:
                writes = 0  # Equal prices make the unobserved partition irrelevant.
            valid_write = type(writes) is int and 0 <= writes <= values["inputTokens"] - values["cachedInputTokens"]
            if valid_write and (writes == 0 or rates["cache_write_input"] is not None):
                amount = ((values["inputTokens"] - values["cachedInputTokens"] - writes) * Decimal(str(rates["input"]))
                          + values["cachedInputTokens"] * Decimal(str(rates["cached_input"]))
                          + writes * Decimal(str(rates["cache_write_input"] or 0))
                          + values["outputTokens"] * Decimal(str(rates["output"]))) / Decimal(price_card["token_unit"])
                subtotal += amount
            else:
                reason = "cache_write_metering_or_rate_unavailable"
        complete = amount is not None and record.get("accounting_complete") is True and all(identity)
        if not complete:
            missing += 1
        steps.append({"record_ordinal": ordinal, "role": record.get("role"), "model": record.get("model"),
                      "effective_service_tier": observed_tier, "rate_tier": tier, "tokens": values,
                      "context_band": rates["id"] if rates is not None else None, "pricing_unavailable_reason": reason,
                      "reported_usage_equivalent_usd": float(amount) if amount is not None else None,
                      "complete": complete})
    role_counts = {role: sum(step["role"] == role for step in steps) for role in set(step["role"] for step in steps)}
    role_coverage = expected_roles is None or role_counts == expected_roles
    nonempty = bool(steps) or allow_known_zero
    complete = missing == 0 and role_coverage and nonempty
    return {"basis": "API price-equivalent estimate; ChatGPT-account billing is unobserved",
            "normalization": "standard_api_prices" if normalization == "standard" else "observed_effective_tier_api_prices",
            "price_card_semantic_sha256": fingerprint(price_card) if price_card is not None else None, "actual_billing_usd": None,
            "input_records": len(records), "distinct_observed_turns": len(seen), "priced_records": len(steps),
            "unknown_accounting_groups": unknown_groups, "incomplete_or_unpriced_records": missing,
            "expected_roles": expected_roles, "observed_role_records": role_counts,
            "expected_role_coverage_complete": role_coverage, "empty_input_is_proven_zero": allow_known_zero and not steps,
            "known_usage_equivalent_subtotal_usd": float(subtotal), "complete": complete,
            "total_usd_equivalent": float(subtotal) if complete else None,
            "total_usd_equivalent_decimal": str(subtotal) if complete else None, "steps": steps,
            "formula": "Partition observed input into uncached, cache-read and cache-write buckets; output includes reasoning; do not add last/context/reasoning subsets"}


def combined_model_jev_cost(model_projection, jev_cost, question_count=None):
    require(type(question_count) is int and question_count > 0 or question_count is None, "price_projection_denominator_invalid")
    jev_total = jev_cost.get("measured_cost_usd")
    jev_complete = (jev_cost.get("accounting_complete") is True and isinstance(jev_total, (int, float))
                    and not isinstance(jev_total, bool) and math.isfinite(jev_total) and jev_total >= 0)
    # No admitted Jev operation is a known zero; absent accounting is not.
    known_zero = (jev_cost.get("attempted_calls") == 0 and jev_cost.get("accounting_complete") is True
                  and jev_total in (None, 0) and jev_cost.get("known_cost_subtotal_usd") in (None, 0))
    if known_zero:
        jev_total, jev_complete = 0, True
    complete = model_projection["complete"] and jev_complete
    total = None
    if complete:
        total = Decimal(model_projection["total_usd_equivalent_decimal"]) + Decimal(str(jev_total))
    return {"complete": complete, "model_api_equivalent": model_projection,
            "jev_observed_usd": jev_total if jev_complete else None,
            "jev_known_subtotal_usd": jev_cost.get("known_cost_subtotal_usd"),
            "jev_cost_basis": "no_admitted_operations" if known_zero else "observed_receipts" if jev_complete else "unavailable",
            "total_usd_equivalent": float(total) if total is not None else None,
            "total_usd_equivalent_decimal": str(total) if total is not None else None,
            "question_denominator": question_count,
            "per_question_usd_equivalent": float(total / question_count) if total is not None and question_count else None,
            "per_question_usd_equivalent_decimal": str(total / question_count) if total is not None and question_count else None,
            "actual_chatgpt_model_billing_usd": None,
            "jev_rate_filled_missing_receipt": False}


def qa_dual_gate_observation(summary, qa_cost):
    full = summary.get("question_denominator") == 62 and summary.get("comparison_eligible") is True
    quality = full and type(summary.get("correct")) is int and summary["correct"] >= 61
    value = qa_cost.get("per_question_usd_equivalent_decimal")
    model = qa_cost.get("model_api_equivalent", {})
    coverage = (model.get("expected_roles") == {"query_planner": 62, "final_reader": 62}
                and model.get("expected_role_coverage_complete") is True and qa_cost.get("question_denominator") == 62
                and model.get("normalization") == "standard_api_prices")
    cost = Decimal(value) < Decimal(QA_COST_THRESHOLD) if coverage and qa_cost.get("complete") is True and value is not None else None
    return {"full62_complete": full, "quality_min_correct": 61, "quality_met": quality,
            "qa_cost_strict_threshold_usd_equivalent": QA_COST_THRESHOLD, "qa_cost_met": cost,
            "qa_model_turn_coverage_complete": coverage,
            "joint_conditions_observed": full and quality and cost is True,
            "cost_scope": "QA reasoning (planner+reader) plus observed ask Jev only; index/enrichment and independent judge excluded",
            "normalization": "Standard API prices for observed GPTgrep model/token usage; not actual Fast experiment billing",
            "baseline_tier_basis": "PageIndex Standard is inferred from original README and absence of Fast declaration, not independently observed billing",
            "baseline_scope": "original PageIndex results.json ask-only; adapted live trace remains a separate reference",
            "acceptance": "quantitative observation only; independent evidence/owner acceptance remains separate"}


def original_index_costs(records, source_paths):
    require(isinstance(records, list) and all(isinstance(record, dict) and isinstance(record.get("doc_id"), str) for record in records),
            "original_index_metadata_shape_invalid")
    by_id = {record["doc_id"]: record for record in records}
    require(len(by_id) == len(records), "original_index_metadata_duplicate_document")
    values = [by_id.get(path, {}).get("flash_index_cost_usd") for path in source_paths]
    known = [Decimal(str(value)) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)
             and math.isfinite(value) and value >= 0]
    subtotal = sum(known, Decimal(0))
    return {"scope": "original PageIndex source-reported indexing cost; separate from its ask-only result and G5",
            "documents": len(values), "known_cost_documents": len(known), "unknown_cost_documents": len(values) - len(known),
            "known_index_cost_subtotal_usd": float(subtotal),
            "complete_index_cost_usd": float(subtotal) if len(known) == len(values) else None,
            "billing_independently_verified": False}


def validate_ledger(path, plan, *, allow_partial=False, price_card=None):
    """Strict full-chain check; failed accounting may retain a verified prefix."""
    path = regular(path)
    require(path.stat().st_size <= min(MAX_LEDGER_BYTES, plan["binding"]["max_ledger_bytes"]), "enrichment_ledger_byte_limit")
    raw = path.read_bytes()
    reservations, receipts, windows = [], [], []
    previous, prepared, publication, terminal, error = None, None, None, None, None
    identities, anchor_units = set(), {unit["anchor"]["anchor_id"]: unit for unit in plan["units"]}
    lines = raw.splitlines(keepends=True)
    for number, line in enumerate(lines):
        try:
            require(line.endswith(b"\n") and len(line) <= MAX_RECORD_BYTES, "enrichment_ledger_torn_or_oversized_record")
            value = parse(line)
            require(set(value) == {"sequence", "previous_sha256", "payload", "sha256"}
                    and value["sequence"] == number and value["previous_sha256"] == previous, "enrichment_ledger_chain_invalid")
            payload = value["payload"]
            require(fingerprint({"sequence": number, "previous_sha256": previous, "payload": payload}) == sha(value["sha256"]), "enrichment_ledger_digest_invalid")
            require(terminal is None, "enrichment_ledger_event_after_terminal")
            event = payload.get("event")
            if number == 0:
                require(payload == {"event": "bound", "binding": plan["binding"], "plan_sha256": plan["plan_sha256"]}, "enrichment_ledger_binding_changed")
            elif event == "call_reserved":
                item = payload["reservation"]
                require(set(item) == {"call_id", "kind", "anchor_id", "requested_model", "request_sha256", "request_bytes"}, "enrichment_reservation_fields_invalid")
                require(prepared is None and item["call_id"] == len(reservations) == len(receipts), "enrichment_reservation_order_invalid")
                require(item["kind"] in ("builder", "jev") and item["anchor_id"] in anchor_units, "enrichment_reservation_identity_invalid")
                unit = anchor_units[item["anchor_id"]]
                key = "builder_model" if item["kind"] == "builder" else "jev_model"
                require(item["requested_model"] == plan["binding"][key], "enrichment_requested_model_changed")
                limit = plan["binding"]["max_builder_calls" if item["kind"] == "builder" else "max_jev_calls"]
                require(sum(call["kind"] == item["kind"] for call in reservations) < limit, "enrichment_cumulative_call_cap_exceeded")
                sha(item["request_sha256"])
                integer(item["request_bytes"], positive=True, maximum=unit["builder_input_bytes"] if item["kind"] == "builder" else unit["worst_case_jev_request_bytes"])
                if item["kind"] == "builder":
                    require(item["request_sha256"] == unit["builder_input_sha256"] and item["request_bytes"] == unit["builder_input_bytes"], "enrichment_builder_input_changed")
                reservations.append(item)
            elif event == "call_finished":
                item = payload["receipt"]
                require(set(item) == {"call_id", "elapsed_ms", "error_code", "observed"} and item["call_id"] == len(receipts)
                        and len(reservations) == len(receipts) + 1, "enrichment_receipt_order_invalid")
                integer(item["elapsed_ms"])
                require(item["error_code"] is None or isinstance(item["error_code"], str), "enrichment_failure_code_invalid")
                reservation = reservations[item["call_id"]]
                _observation(item["observed"], reservation, plan["binding"])
                if reservation["kind"] == "builder" and item["observed"] is not None:
                    identity = item["observed"]["thread_id"], item["observed"]["turn_id"]
                    require(identity not in identities, "enrichment_duplicate_actual_model_turn")
                    identities.add(identity)
                receipts.append(item)
            elif event == "window_completed":
                item = payload["window"]
                require(len(windows) < len(plan["units"]), "enrichment_extra_window")
                unit = plan["units"][len(windows)]
                require(item["document"] == unit["document"] and item["anchor"] == unit["anchor"], "enrichment_completed_window_changed")
                start = sum(1 + (window["jev_call_id"] is not None) for window in windows)
                end = start + 1 + (item["jev_call_id"] is not None)
                require(item["builder_call_id"] == start and item["jev_call_id"] in (None, start + 1)
                        and len(reservations) == len(receipts) == end, "enrichment_window_calls_invalid")
                require(reservations[start]["kind"] == "builder" and (item["jev_call_id"] is None or reservations[start + 1]["kind"] == "jev"), "enrichment_window_call_kinds_invalid")
                require(all(call["anchor_id"] == item["anchor"]["anchor_id"] for call in reservations[start:end])
                        and all(receipt["error_code"] is None and receipt["observed"] is not None for receipt in receipts[start:end]), "enrichment_completed_window_receipt_invalid")
                drafts, support = item["drafts"], item["support"]
                require(isinstance(drafts, list) and len(drafts) <= plan["binding"]["max_hints_per_window"]
                        and isinstance(support, list) and len(support) == len(drafts)
                        and all(value in ("supported", "needs_context", "unsupported") for value in support)
                        and (item["jev_call_id"] is None) == (not drafts), "enrichment_support_coverage_invalid")
                for draft in drafts:
                    require(set(draft) == {"anchor_id", "hint"} and draft["anchor_id"] == item["anchor"]["anchor_id"]
                            and isinstance(draft["hint"], str) and 0 < len(draft["hint"].encode()) <= 1024
                            and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in draft["hint"]), "enrichment_hint_invalid")
                windows.append(item)
            elif event == "checkpoint":
                require(prepared is None and len(reservations) == len(receipts) == sum(1 + (window["jev_call_id"] is not None) for window in windows), "enrichment_checkpoint_pending_work")
            elif event == "failed":
                terminal = "failed"
            elif event == "publication_prepared":
                require(prepared is None and len(windows) == len(plan["units"])
                        and len(reservations) == len(receipts), "enrichment_publication_before_complete_coverage")
                prepared = sha(payload["artifact_sha256"])
            elif event == "published":
                item = payload["publication"]
                require(prepared is not None and item["artifact_sha256"] == prepared
                        and item["schema_version"] == "gptgrep.navigation-overlay.v1"
                        and item["generation"] == plan["binding"]["source"]["generation"]
                        and item["manifest_sha256"] == plan["binding"]["source"]["manifest_sha256"], "enrichment_publication_binding_invalid")
                publication, terminal = item, "published"
            else:
                raise ValueError("enrichment_unknown_ledger_event")
            previous = value["sha256"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as failure:
            if not allow_partial:
                raise ValueError("enrichment_ledger_validation_failed") from failure
            error = "enrichment_ledger_unverified_suffix"
            break
    require(lines and previous is not None, "enrichment_ledger_has_no_verified_binding")
    full = error is None
    safe = full and terminal is None and prepared is None and len(reservations) == len(receipts) == sum(1 + (window["jev_call_id"] is not None) for window in windows)
    return {"ledger_sha256": hashlib.sha256(raw).hexdigest(), "ledger_bytes": len(raw), "validated_chain": full,
            "validation_error": error, "terminal": terminal, "resume_safe": safe, "publication": publication,
            "windows": windows, "builder": summarize_calls(reservations, receipts, "builder", complete_chain=full, price_card=price_card),
            "jev": summarize_calls(reservations, receipts, "jev", complete_chain=full, price_card=price_card),
            "limits": {"raw_model_request_rehash_available": False, "raw_jev_wire_rehash_available": False,
                       "basis": "Hash-bound plan, chained reservation claims and observed receipts; wire bodies are not retained."}}


def validate_enrich_report(report, ledger, plan, ledger_path):
    require(report.get("schema_version") == "gptgrep.enrich.v1" and report.get("status") in ("complete", "incomplete", "failed"), "enrichment_report_schema_invalid")
    require(report.get("source") == plan["binding"]["source"] and report.get("plan_sha256") == plan["plan_sha256"]
            and report.get("ledger_path") == str(ledger_path), "enrichment_report_binding_changed")
    require(ledger["validated_chain"], "enrichment_report_ledger_unverified")
    require(report.get("builder") == ledger["builder"]["summary"] and report.get("jev") == ledger["jev"]["summary"], "enrichment_report_accounting_changed")
    require(report.get("windows_completed") == len(ledger["windows"]), "enrichment_report_window_count_changed")
    integer(report.get("windows_reused"), maximum=len(ledger["windows"]))
    if report["status"] == "complete":
        require(ledger["terminal"] == "published" and len(ledger["windows"]) == plan["unit_count"]
                and report.get("publication") == ledger["publication"] and report.get("publication_state") == "published"
                and report.get("documents_completed") == len(plan["documents"]) and report.get("next_cursor") is None,
                "enrichment_complete_report_unproven")
    elif report["status"] == "incomplete":
        require(report.get("publication") is None and ledger["resume_safe"] and report.get("resume_safe") is True,
                "enrichment_incomplete_checkpoint_unsafe")
    return report


def validate_publication(corpus, report, ledger, plan):
    corpus = Path(corpus)
    current = read_json(corpus / ".gptgrep/CURRENT.json", 4096)
    source = plan["binding"]["source"]
    require(all(current.get(key) == source[key] for key in ("generation", "manifest_sha256")), "enrichment_current_generation_changed")
    pointer = read_json(corpus / ".gptgrep/NAVIGATION.json", 4096)
    require(pointer == report["publication"] == ledger["publication"], "enrichment_overlay_pointer_changed")
    artifact = corpus / ".gptgrep/navigation-overlays" / (sha(pointer["artifact_sha256"]) + ".json")
    require(digest(artifact) == pointer["artifact_sha256"], "enrichment_overlay_artifact_changed")
    overlay = read_json(artifact, MAX_OVERLAY_BYTES)
    require(overlay.get("schema_version") == "gptgrep.navigation-overlay.v1"
            and all(overlay.get(key) == source[key] for key in ("generation", "manifest_sha256")), "enrichment_overlay_base_changed")
    expected = {document["path"]: document for document in plan["documents"]}
    require(isinstance(overlay.get("documents"), list) and len(overlay["documents"]) == len(expected), "enrichment_overlay_document_coverage_changed")
    seen = set()
    hint_count = 0
    for document in overlay["documents"]:
        name = document.get("path")
        require(name in expected and name not in seen and {key: document.get(key) for key in expected[name]} == expected[name], "enrichment_overlay_document_identity_changed")
        seen.add(name)
        require(digest(corpus / name) == expected[name]["source_sha256"], "enrichment_source_changed")
        require(document.get("windows") == [unit["anchor"] for unit in plan["units"] if unit["document"]["path"] == name], "enrichment_overlay_windows_changed")
        supported = {(window["anchor"]["anchor_id"], draft["hint"]) for window in ledger["windows"] if window["document"]["path"] == name
                     for draft, support in zip(window["drafts"], window["support"]) if support == "supported"}
        for hint in document.get("hints", []):
            require(set(hint) == {"origin", "target", "hint", "anchor_ids"} and hint["origin"] == "model_derived_navigation_only"
                    and hint["target"].get("kind") == "chunk" and hint["anchor_ids"] == [hint["target"].get("anchor_id")]
                    and (hint["anchor_ids"][0], hint["hint"]) in supported, "enrichment_overlay_hint_lacks_bound_support")
            hint_count += 1
    coverage = overlay.get("coverage")
    total_bytes = sum(document["text_bytes"] for document in expected.values())
    require(isinstance(coverage, dict) and coverage == report.get("coverage")
            and coverage.get("partial_source_coverage") is False and coverage.get("documents_total") == len(expected)
            and coverage.get("documents_with_complete_windows") == len(expected)
            and coverage.get("covered_text_bytes") == coverage.get("canonical_text_bytes") == total_bytes
            and coverage.get("raw_windows") == plan["unit_count"] and coverage.get("hints") == hint_count,
            "enrichment_overlay_coverage_changed")
    producer = overlay.get("producer", {})
    binding = plan["binding"]
    require(all(producer.get(key) == binding[bound] for key, bound in (("model", "builder_model"), ("reasoning_effort", "reasoning_effort"),
            ("service_tier", "service_tier"), ("prompt_sha256", "builder_prompt_sha256"), ("schema_sha256", "builder_schema_sha256"))), "enrichment_overlay_producer_changed")
    jev = producer.get("jev", {})
    require(jev.get("requested_model") == binding["jev_model"] and jev.get("actual_models") == ledger["jev"]["summary"]["models"]
            and jev.get("logical_calls_attempted") == ledger["jev"]["summary"]["attempted_calls"]
            and jev.get("validated_responses") == ledger["jev"]["summary"]["completed_calls"]
            and jev.get("prompt_sha256") == binding["support_prompt_sha256"] and jev.get("schema_sha256") == binding["support_schema_sha256"],
            "enrichment_overlay_jev_identity_changed")
    return pointer
