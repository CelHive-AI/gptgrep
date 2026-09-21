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
    parser.add_argument("--index-reasoning-effort", default="max")
    parser.add_argument("--judge-model")
    parser.add_argument("--judge-reasoning-effort")


def resolve(args) -> dict:
    preset = PRESETS[args.profile]
    roles = {
        "index": {"model": args.index_model, "reasoning_effort": args.index_reasoning_effort},
        "chat": {"model": args.model or preset["chat"]["model"], "reasoning_effort": args.reasoning_effort or preset["chat"]["reasoning_effort"]},
        "judge": {"model": args.judge_model or preset["judge"]["model"], "reasoning_effort": args.judge_reasoning_effort or preset["judge"]["reasoning_effort"]},
    }
    for role, values in roles.items():
        if not all(isinstance(value, str) and value.strip() for value in values.values()):
            raise ValueError(f"{role} profile has an empty model/effort")
        if values["model"] == "gpt-5.6":
            raise ValueError("Bare gpt-5.6 is not a source profile; select an explicit model ID")
    args.model = roles["chat"]["model"]
    args.reasoning_effort = roles["chat"]["reasoning_effort"]
    return {
        "requested_profile": args.profile, "roles": roles,
        "source_default": {"index": {"model": LUNA, "reasoning_effort": None},
                           "chat": {"model": LUNA, "reasoning_effort": "high"},
                           "judge": {"model": LUNA, "reasoning_effort": "high"}},
        "index_effort_note": "Upstream index effort is unspecified; executed local index effort is an explicit backend adaptation.",
        "historical_reproduction_claimed": False,
    }

