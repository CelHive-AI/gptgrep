"""Explicit role settings; source defaults and executed backend adaptations stay distinct."""
from __future__ import annotations

LUNA = "gpt-5.6-luna"
PRESETS = {
    "matched-luna-max": {"chat": {"model": LUNA, "reasoning_effort": "max"}, "judge": {"model": LUNA, "reasoning_effort": "high"}},
    "source-default-chat": {"chat": {"model": LUNA, "reasoning_effort": "high"}, "judge": {"model": LUNA, "reasoning_effort": "high"}},
    "legacy-luna-max-control": {"chat": {"model": LUNA, "reasoning_effort": "max"}, "judge": {"model": LUNA, "reasoning_effort": "max"}},
}


def add_arguments(parser):
    parser.add_argument("--profile", choices=sorted(PRESETS), default="matched-luna-max")
    parser.add_argument("--index-model", default=LUNA)
    parser.add_argument("--index-reasoning-effort", help="Index effort; medium by default, max for the explicitly selected legacy all-max control")
    parser.add_argument("--judge-model")
    parser.add_argument("--judge-reasoning-effort")
    parser.add_argument("--service-tier", choices=["fast", "priority", "flex", "default"], default="fast")


def resolve(args) -> dict:
    preset = PRESETS[args.profile]
    args.service_tier = getattr(args, "service_tier", "fast")
    if args.service_tier not in ("fast", "priority", "flex", "default"):
        raise ValueError("Unsupported requested service tier")
    roles = {
        "index": {"model": args.index_model, "reasoning_effort": args.index_reasoning_effort or (
            "max" if args.profile == "legacy-luna-max-control" else "medium")},
        "chat": {"model": args.model or preset["chat"]["model"], "reasoning_effort": args.reasoning_effort or preset["chat"]["reasoning_effort"]},
        "judge": {"model": args.judge_model or preset["judge"]["model"], "reasoning_effort": args.judge_reasoning_effort or preset["judge"]["reasoning_effort"]},
    }
    for role, values in roles.items():
        values["service_tier"] = args.service_tier
        if not all(isinstance(value, str) and value.strip() for value in values.values()):
            raise ValueError(f"{role} profile has an empty model/effort")
        if values["model"] == "gpt-5.6":
            raise ValueError("Bare gpt-5.6 is not a source profile; select an explicit model ID")
    args.model = roles["chat"]["model"]
    args.reasoning_effort = roles["chat"]["reasoning_effort"]
    return {
        "requested_profile": args.profile, "roles": roles,
        "requested_service_tier": args.service_tier,
        "source_default": {"index": {"model": LUNA, "reasoning_effort": None},
                           "chat": {"model": LUNA, "reasoning_effort": "high"},
                           "judge": {"model": LUNA, "reasoning_effort": "high"}},
        "documented_api_default": {"model": LUNA, "reasoning_effort": "medium",
                                   "source": "https://developers.openai.com/api/docs/models/gpt-5.6-luna"},
        "index_effort_note": "Upstream omits index effort; the corrected adapter explicitly selects the documented API default medium. Codex transport remains an adaptation, not a native-provider reproduction.",
        "historical_reproduction_claimed": False,
    }
