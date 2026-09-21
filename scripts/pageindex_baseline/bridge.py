"""Bounded local Codex completions with private request/response provenance."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import signal
import threading
import time

import jsonschema

INPUT_CAP = 256 * 1024
OUTPUT_CAP = 128 * 1024


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code, self.status_code = code, status_code


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(value) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def owned_process(arguments: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    """Give the host its own group and a TERM grace period before forced cleanup."""
    process = subprocess.Popen(arguments, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as original:
        cleanup = {"pid": process.pid, "group": process.pid, "term_sent": False, "kill_sent": False}
        try:
            os.killpg(process.pid, signal.SIGTERM)
            cleanup["term_sent"] = True
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                cleanup["kill_sent"] = True
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)
        cleanup["reaped"] = process.returncode is not None
        failure = AdapterError("host_process_timeout", "Local host exceeded its outer deadline; owned process was reaped", 401)
        failure.cleanup = cleanup
        raise failure from original


class LocalCodex:
    def __init__(self, binary: Path, codex_bin: str, codex_home: Path, run_dir: Path,
                 max_calls: int, timeout: int = 180, model: str = "gpt-5.6-luna", effort: str = "max",
                 max_input_bytes: int = INPUT_CAP):
        self.binary, self.codex_bin, self.codex_home = binary, codex_bin, codex_home
        self.run_dir, self.max_calls, self.timeout = run_dir, max_calls, timeout
        self.model, self.effort = model, effort
        if not 1 <= max_input_bytes <= 1024 * 1024:
            raise ValueError("Host input cap must be1..1048576 bytes")
        self.max_input_bytes = max_input_bytes
        self.phase = "unselected"
        self._lock = threading.RLock()
        self.calls = []
        self.rejections = []
        (run_dir / "calls").mkdir(parents=True, exist_ok=True)
        self.ledger = run_dir / "host-calls.jsonl"
        if self.ledger.exists():
            self.calls = [json.loads(line) for line in self.ledger.read_text().splitlines() if line.strip()]
        ordinals = [call.get("ordinal") for call in self.calls]
        if ordinals != list(range(1, len(self.calls) + 1)):
            raise ValueError("Host ledger ordinals are not contiguous")
        # A hard interruption can occur after invocation starts but before its final
        # receipt. Retain that uncertain attempt in the cap; never silently replay it.
        for request_path in sorted((run_dir / "calls").glob("*.request.json")):
            ordinal = int(request_path.name.split(".", 1)[0])
            if ordinal <= len(self.calls):
                continue
            if ordinal != len(self.calls) + 1:
                raise ValueError("Interrupted host request ordinals are not contiguous")
            pending = request_path.with_name(f"{ordinal:05d}.attempt.json")
            receipt = json.loads(pending.read_text()) if pending.exists() else {
                "ordinal": ordinal, "phase": "interrupted_unknown", "host_invoked": None,
                "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
            }
            response_path = request_path.with_name(f"{ordinal:05d}.response.json")
            if response_path.exists():
                receipt["response_sha256"] = hashlib.sha256(response_path.read_bytes()).hexdigest()
            self._append({**receipt, "status": "interrupted", "error_code": "interrupted_before_receipt",
                          "usage": None, "accounting_complete": False,
                          "invocation_or_transmission_confirmed": False})

    def start_attempt(self, receipt: dict) -> None:
        path = self.run_dir / "calls" / f"{receipt['ordinal']:05d}.attempt.json"
        with path.open("xb") as output:
            output.write(json_bytes(receipt))
            output.flush()
            os.fsync(output.fileno())

    def _append(self, receipt: dict) -> None:
        with self.ledger.open("a", encoding="utf-8") as output:
            output.write(json.dumps(receipt, ensure_ascii=False, allow_nan=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        self.calls.append(receipt)

    def complete(self, instructions: str, state, schema: dict) -> tuple[dict, dict]:
        request = {"instructions": instructions, "state": state, "schema": schema}
        payload = json_bytes(request)
        with self._lock:
            if len(payload) > self.max_input_bytes:
                failure = {"phase": self.phase, "code": "input_limit", "input_bytes": len(payload),
                           "input_sha256": hashlib.sha256(payload).hexdigest(), "host_invoked": False}
                self.rejections.append(failure)
                raise AdapterError("input_limit", "Combined completion request exceeds its explicit byte cap; nothing was truncated")
            if len(self.calls) >= self.max_calls:
                self.rejections.append({"phase": self.phase, "code": "call_budget", "host_invoked": False})
                raise AdapterError("call_budget", "Run host-completion budget is exhausted")
            number = len(self.calls) + 1
            request_path = self.run_dir / "calls" / f"{number:05d}.request.json"
            response_path = self.run_dir / "calls" / f"{number:05d}.response.json"
            with request_path.open("xb") as output:
                output.write(payload)
            receipt = {"ordinal": number, "phase": self.phase, "request_sha256": hashlib.sha256(payload).hexdigest(),
                       "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
                       "state_sha256": sha(state), "schema_sha256": sha(schema), "input_bytes": len(payload),
                       "requested_model": self.model, "requested_effort": self.effort, "host_invoked": True}
            self.start_attempt(receipt)
            started = time.perf_counter()
            try:
                process = owned_process(
                    [str(self.binary), "host-complete", "--input", str(request_path),
                     "--codex-bin", self.codex_bin, "--codex-home", str(self.codex_home),
                     "--model", self.model, "--reasoning-effort", self.effort,
                     "--timeout", str(self.timeout), "--max-input-bytes", str(self.max_input_bytes), "--json"],
                    cwd=self.run_dir, timeout=self.timeout + 15,
                )
                if len(process.stdout) > OUTPUT_CAP + 65536:
                    raise AdapterError("output_limit", "Host response exceeds the adapter output limit")
                try:
                    report = json.loads(process.stdout)
                except (ValueError, UnicodeDecodeError) as error:
                    raise AdapterError("invalid_host_json", "Host did not return valid JSON", 401) from error
                with response_path.open("xb") as output:
                    output.write(process.stdout)
                if process.returncode != 0:
                    code = str(report.get("code", report.get("error_code", "host_failure")))
                    raise AdapterError(code, f"Local host failed: {code}", 400 if "limit" in code else 401)
                if report.get("status") != "completed":
                    raise AdapterError("host_status", "Local host did not establish completed status", 401)
                if report.get("model") != self.model or report.get("requested_reasoning_effort") != self.effort:
                    raise AdapterError("host_identity", "Local host changed the selected model/effort", 401)
                if report.get("effective_reasoning_effort") not in (None, self.effort):
                    raise AdapterError("host_effort", "Local host reported a different effective effort", 401)
                if report.get("auth_mode") != "chatgpt":
                    raise AdapterError("host_auth", "Local host did not establish ChatGPT authentication", 401)
                if report.get("model_provider") != "openai" or not report.get("thread_id") or not report.get("turn_id"):
                    raise AdapterError("host_identity", "Local host did not establish provider/thread/turn identity", 401)
                value = report["value"]
                jsonschema.Draft202012Validator(schema).validate(value)
                if len(json_bytes(value)) > OUTPUT_CAP:
                    raise AdapterError("output_limit", "Completion value exceeds128KiB")
                receipt.update(status="completed", model=report["model"], model_provider=report.get("model_provider"),
                               thread_id=report.get("thread_id"), turn_id=report.get("turn_id"),
                               usage=report.get("usage"), effective_reasoning_effort=report.get("effective_reasoning_effort"),
                               reported_host_elapsed_ms=report.get("elapsed_ms"),
                               response_sha256=hashlib.sha256(process.stdout).hexdigest(), value_sha256=sha(value))
                self._append({**receipt, "elapsed_ms": (time.perf_counter() - started) * 1000})
                return value, report
            except Exception as error:
                receipt.update(status="failed", error_code=getattr(error, "code", type(error).__name__),
                               elapsed_ms=(time.perf_counter() - started) * 1000)
                if hasattr(error, "cleanup"):
                    receipt["timeout_cleanup"] = error.cleanup
                self._append(receipt)
                if isinstance(error, AdapterError):
                    raise
                raise AdapterError("host_failure", "Local host completion failed; inspect the private receipt", 401) from error
