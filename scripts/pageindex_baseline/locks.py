"""Source and dependency checks performed before importing any upstream code."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path):
    def invalid(value):
        raise ValueError(f"Non-finite JSON value: {value}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid)


def verify(upstream: Path, benchmark: Path, judge_source: Path | None = None) -> dict:
    lock = read_json(HERE / "sources.lock.json")
    for label, root in (("pageindex", upstream), ("benchmark", benchmark)):
        revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        if revision != lock[label]["revision"]:
            raise ValueError(f"{label} revision differs from the source lock")
        for name, expected in lock[label]["files"].items():
            if digest(root / name) != expected:
                raise ValueError(f"{label} source file differs: {name}")
    dirty = subprocess.check_output(
        ["git", "-C", str(upstream), "status", "--porcelain", "--untracked-files=all", "--", "pageindex"],
        text=True,
    )
    if dirty.strip():
        raise ValueError("PageIndex package contains local or untracked changes")
    for name, expected in lock["benchmark"]["pdf_sha256"].items():
        if digest(benchmark / "documents" / name) != expected:
            raise ValueError(f"Benchmark PDF differs: {name}")
    if judge_source is not None:
        for name, expected in lock["judge"]["files"].items():
            if digest(judge_source / name) != expected:
                raise ValueError(f"Judge source differs: {name}")
    requirements = HERE / "requirements.lock"
    if digest(requirements) != lock["requirements_sha256"]:
        raise ValueError("Dependency lock has changed without rebinding the source lock")
    if digest(HERE.parents[1] / "evals/pageindex-reference.json") != lock["subset_reference_sha256"]:
        raise ValueError("Public subset reference manifest differs from the lock")
    versions = {}
    for line in requirements.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        package, expected = line.split("==", 1)
        actual = importlib.metadata.version(package)
        if actual != expected:
            raise ValueError(f"Dependency version differs: {package}")
        versions[package] = actual
    return {"source_lock_sha256": digest(HERE / "sources.lock.json"), "dependencies": versions,
            "pageindex_revision": lock["pageindex"]["revision"],
            "benchmark_revision": lock["benchmark"]["revision"], "judge_revision": lock["judge"]["revision"]}


def import_upstream(upstream: Path) -> None:
    if any(name == "pageindex" or name.startswith("pageindex.") for name in sys.modules):
        raise ValueError("PageIndex was imported before the source-lock boundary")
    sys.dont_write_bytecode = True
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    for name in ("OPENAI_API_KEY", "CHATGPT_API_KEY", "OPENROUTER_API_KEY", "PAGEINDEX_API_KEY"):
        os.environ.pop(name, None)
    sys.path.insert(0, str(upstream))
