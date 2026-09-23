#!/usr/bin/env python3
"""Plan or explicitly execute append-only pre-reader technical recovery."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "pageindex_baseline"))
from native_recovery import Inputs, analyze, prepare_plan, run_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Freeze automatic metadata-only selection; no model calls")
    for name in ("origin", "seal", "declaration", "judge-binary", "judge-source", "benchmark", "upstream"):
        plan.add_argument("--" + name, required=True, type=Path)
    run = commands.add_parser("run", help="Execute only an explicitly bound sidecar plan")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--plan-sha256", required=True)
    run.add_argument("--execute", action="store_true", required=True, help="Explicitly admit model calls for this exact plan")
    view = commands.add_parser("analyze", help="Read-only origin-plus-sidecar lineage, no inferred verdicts")
    view.add_argument("--origin", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = prepare_plan(Inputs(args.origin, args.seal, args.declaration, args.judge_binary,
                                         args.judge_source, args.benchmark, args.upstream))
        elif args.command == "run":
            result = run_plan(args.plan, args.plan_sha256)
        else:
            result = analyze(args.origin)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as error:
        # Do not echo arbitrary transport/provider/model output.
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__,
                          "message": str(error) if type(error).__name__ == "RecoveryError" else "Recovery preflight or execution failed; preserve all retained artifacts."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
