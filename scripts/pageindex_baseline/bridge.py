"""Bounded local Codex completions with private request/response provenance."""
from __future__ import annotations

import hashlib
import asyncio
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import subprocess
import signal
import threading
import time
import uuid

import jsonschema

INPUT_CAP = 256 * 1024
OUTPUT_CAP = 128 * 1024
PROTOCOL_ERROR_KINDS = {"terminal_error", "malformed_error", "identity_mismatch", "failed_turn",
                        "interrupted_turn", "invalid_turn_status"}
CODEX_ERROR_INFOS = {"contextWindowExceeded", "sessionBudgetExceeded", "usageLimitExceeded", "rateLimitExceeded",
                     "serverOverloaded", "cyberPolicy", "misalignmentPolicyViolation", "httpConnectionFailed",
                     "responseStreamConnectionFailed", "internalServerError", "unauthorized", "badRequest",
                     "threadRollbackFailed", "sandboxError", "responseStreamDisconnected",
                     "responseTooManyFailedAttempts", "activeTurnNotSteerable", "other"}


def _unsigned(value) -> bool:
    return type(value) is int and 0 <= value <= 2 ** 64 - 1


def _observed_usage(value):
    """Project known numeric token observations; missing or invalid is not zero."""
    if not isinstance(value, dict):
        return None
    result = {}
    for scope in ("total", "last"):
        tokens = value.get(scope)
        if isinstance(tokens, dict):
            selected = {field: tokens[field] for field in ("totalTokens", "inputTokens", "cachedInputTokens",
                        "cacheWriteInputTokens", "outputTokens", "reasoningOutputTokens")
                        if _unsigned(tokens.get(field))}
            if selected:
                result[scope] = selected
    for field in ("modelContextWindow", "total_tokens", "input_tokens", "cached_input_tokens",
                  "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens"):
        if _unsigned(value.get(field)):
            result[field] = value[field]
    return result or None


def _retain_accounting(receipt: dict, report: dict) -> None:
    receipt["usage"] = _observed_usage(report.get("usage"))
    protocol = report.get("host_protocol")
    if isinstance(protocol, dict):
        # A failed protocol envelope never establishes complete accounting.
        selected = {"accounting_complete": False, "usage": _observed_usage(protocol.get("usage"))}
        for field, allowed in (("kind", PROTOCOL_ERROR_KINDS), ("codex_error_info", CODEX_ERROR_INFOS)):
            value = protocol.get(field)
            if isinstance(value, str) and value in allowed:
                selected[field] = value
            elif value is None:
                selected[field] = None
        retry = protocol.get("will_retry")
        selected["will_retry"] = retry if type(retry) is bool else None
        status = protocol.get("http_status_code")
        selected["http_status_code"] = status if type(status) is int and 100 <= status <= 599 else None
        if _unsigned(protocol.get("server_retry_notifications")):
            selected["server_retry_notifications"] = protocol["server_retry_notifications"]
        receipt["host_protocol"] = selected
        if receipt["usage"] is None:
            receipt["usage"] = selected["usage"]
    if _unsigned(report.get("server_retry_notifications")):
        receipt["server_retry_notifications"] = report["server_retry_notifications"]


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code, self.status_code = code, status_code


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(value) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def owned_process(arguments: list[str], cwd: Path, timeout: float, *, cancelled=None, on_start=None) -> subprocess.CompletedProcess:
    """Give the host its own group and a TERM grace period before forced cleanup."""
    if cancelled and cancelled():
        raise AdapterError("host_cancelled", "Local host invocation cancelled before launch", 401)
    process = subprocess.Popen(arguments, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        if on_start:
            on_start(process.pid)
        deadline = time.monotonic() + timeout
        while True:
            if cancelled and cancelled():
                raise AdapterError("host_cancelled", "Local host invocation cancelled", 401)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining) if cancelled else remaining)
                break
            except subprocess.TimeoutExpired:
                if not cancelled or time.monotonic() >= deadline:
                    raise
        return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
    except BaseException as original:
        cleanup = {"pid": process.pid, "group": process.pid, "term_sent": False, "kill_sent": False}
        try:
            os.killpg(process.pid, signal.SIGTERM)
            cleanup["term_sent"] = True
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                cleanup["kill_sent"] = True
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate(timeout=5)
        cleanup["reaped"] = process.returncode is not None
        failure = (AdapterError("host_process_timeout", "Local host exceeded its outer deadline; owned process was reaped", 401)
                   if isinstance(original, subprocess.TimeoutExpired) else original)
        failure.cleanup = cleanup
        failure.stdout, failure.stderr = stdout, stderr
        if failure is original:
            raise
        raise failure from original


class ExternalAttempt:
    """One reserved native invocation; its caller owns bounded report validation."""
    def __init__(self, host, receipt, request_path):
        self.host, self.receipt, self.request_path = host, receipt, request_path
        self._ran = False

    def run(self, arguments, timeout):
        if self._ran:
            raise ValueError("A reserved native attempt cannot be replayed")
        self._ran = True
        try:
            return owned_process(arguments, self.host.run_dir, timeout,
                                 cancelled=self.host._cancelled.is_set,
                                 on_start=lambda pid: self.host._process_started(self.receipt, pid))
        finally:
            self.host._process_finished(self.receipt)


class LocalCodex:
    def __init__(self, binary: Path, codex_bin: str, codex_home: Path, run_dir: Path,
                 max_calls: int, timeout: int = 180, model: str = "gpt-5.6-luna", effort: str = "max",
                 max_input_bytes: int = INPUT_CAP, *, host_concurrency: int = 1, service_tier: str = "fast",
                 reader_concurrency: int = 1, judge_concurrency: int = 1):
        self.binary, self.codex_bin, self.codex_home = binary, codex_bin, codex_home
        self.run_dir, self.max_calls, self.timeout = run_dir, max_calls, timeout
        self.model, self.effort = model, effort
        if service_tier not in ("fast", "priority", "flex", "default"):
            raise ValueError("Unsupported requested service tier")
        self.service_tier = service_tier
        if not 1 <= max_input_bytes <= 1024 * 1024:
            raise ValueError("Host input cap must be1..1048576 bytes")
        self.max_input_bytes = max_input_bytes
        self.phase = "unselected"
        self._lock = threading.RLock()
        if any(not 1 <= value <= 64 for value in (host_concurrency, reader_concurrency, judge_concurrency)) or max_calls < 0:
            raise ValueError("Host concurrency must be1..64 and the cumulative call cap nonnegative")
        self.host_concurrency = host_concurrency
        self.role_concurrency = {"index": host_concurrency, "chat": reader_concurrency, "judge": judge_concurrency, "other": 1}
        self.total_concurrency = max(self.role_concurrency.values())
        self._slots = threading.BoundedSemaphore(self.total_concurrency)
        self._role_slots = {role: threading.BoundedSemaphore(limit) for role, limit in self.role_concurrency.items()}
        self._cancelled = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=self.total_concurrency, thread_name_prefix="gptgrep-host")
        self._futures = set()
        self._active = {}
        self._measurement_id = uuid.uuid4().hex
        self._next_ordinal = 1
        self._finished = set()
        self.calls = []
        self.rejections = []
        (run_dir / "calls").mkdir(parents=True, exist_ok=True)
        self.ledger = run_dir / "host-calls.jsonl"
        if self.ledger.exists():
            self.calls = [json.loads(line) for line in self.ledger.read_text().splitlines() if line.strip()]
        ordinals = [call.get("ordinal") for call in self.calls]
        if any(type(number) is not int or number < 1 for number in ordinals) or len(set(ordinals)) != len(ordinals):
            raise ValueError("Host ledger ordinals are invalid or duplicated")
        self._finished = set(ordinals)
        self.calls.sort(key=lambda item: item["ordinal"])
        # A hard interruption can occur after invocation starts but before its final
        # receipt. Retain that uncertain attempt in the cap; never silently replay it.
        started = {int(path.name.split(".", 1)[0])
                   for pattern in ("*.request.json", "*.attempt.json") for path in (run_dir / "calls").glob(pattern)}
        all_ordinals = self._finished | started
        if sorted(all_ordinals) != list(range(1, max(all_ordinals, default=0) + 1)):
            raise ValueError("Host request/receipt ordinals are not contiguous")
        self._next_ordinal = max(all_ordinals, default=0) + 1
        for ordinal in sorted(started):
            if ordinal in self._finished:
                continue
            request_path = run_dir / "calls" / f"{ordinal:05d}.request.json"
            pending = request_path.with_name(f"{ordinal:05d}.attempt.json")
            receipt = json.loads(pending.read_text()) if pending.exists() else {
                "ordinal": ordinal, "phase": "interrupted_unknown", "host_invoked": None,
                "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest() if request_path.exists() else None,
            }
            if receipt.get("ordinal") != ordinal:
                raise ValueError("Interrupted host journal ordinal differs from its filename")
            process_start = request_path.with_name(f"{ordinal:05d}.process-start.json")
            if process_start.exists():
                receipt.update(json.loads(process_start.read_text()))
            response_path = request_path.with_name(f"{ordinal:05d}.response.json")
            if response_path.exists():
                raw = response_path.read_bytes()
                receipt["response_sha256"] = hashlib.sha256(raw).hexdigest()
                try:
                    report = json.loads(raw)
                    if isinstance(report, dict):
                        _retain_accounting(receipt, report)
                except (ValueError, UnicodeDecodeError):
                    pass
            self._append({**receipt, "status": "interrupted", "error_code": "interrupted_before_receipt",
                          "usage": receipt.get("usage"), "accounting_complete": False,
                          "invocation_or_transmission_confirmed": False})

    def start_attempt(self, receipt: dict) -> None:
        with self._lock:
            number = receipt["ordinal"]
            if type(number) is not int or number < 1 or number in self._finished:
                raise ValueError("Invalid or already completed host ordinal")
            path = self.run_dir / "calls" / f"{number:05d}.attempt.json"
            with path.open("xb") as output:
                output.write(json_bytes(receipt))
                output.flush()
                os.fsync(output.fileno())
            # Also advances for the separate serial native runner's direct asks.
            self._next_ordinal = max(self._next_ordinal, number + 1)

    def _append(self, receipt: dict) -> None:
        if receipt.get("protocol_trace_requested"):
            trace = self.run_dir / "calls" / f"{receipt['ordinal']:05d}.trace.jsonl"
            try:
                descriptor = os.open(trace, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    raw = stream.read(64 * 1024 + 1)
                if len(raw) > 64 * 1024:
                    receipt["protocol_trace_status"] = "oversized"
                else:
                    receipt.update(protocol_trace_status="available", protocol_trace_bytes=len(raw),
                                   protocol_trace_sha256=hashlib.sha256(raw).hexdigest())
            except FileNotFoundError:
                receipt["protocol_trace_status"] = "unavailable"
            except OSError:
                receipt["protocol_trace_status"] = "unreadable_or_redirected"
        with self._lock:
            number = receipt["ordinal"]
            if number in self._finished:
                raise ValueError("Duplicate final host receipt")
            with self.ledger.open("a", encoding="utf-8") as output:
                output.write(json.dumps(receipt, ensure_ascii=False, allow_nan=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
            self.calls.append(receipt)
            self.calls.sort(key=lambda item: item["ordinal"])
            self._finished.add(number)
            self._next_ordinal = max(self._next_ordinal, number + 1)

    def _bindings(self, model=None, effort=None, phase=None, role=None, service_tier=None) -> dict:
        with self._lock:
            selected_phase = self.phase if phase is None else phase
            selected_role = role or selected_phase.split(":", 1)[0]
            selected_role = "chat" if selected_role in ("answer", "reader") else selected_role
            return {"model": self.model if model is None else model,
                    "effort": self.effort if effort is None else effort, "phase": selected_phase,
                    "service_tier": self.service_tier if service_tier is None else service_tier,
                    "role": selected_role if selected_role in self._role_slots else "other"}

    def calls_for_phase(self, phase: str) -> list[dict]:
        with self._lock:
            return [dict(call) for call in self.calls if call.get("phase") == phase]

    def call_by_ordinal(self, ordinal: int):
        with self._lock:
            return next((dict(call) for call in self.calls if call["ordinal"] == ordinal), None)

    @contextmanager
    def external_attempt(self, payload: dict, *, phase, model, effort, service_tier, role="chat", operation="native_ask"):
        """Reserve before launch; role slots and paid-attempt accounting survive errors."""
        binding = self._bindings(model, effort, phase, role, service_tier)
        if binding["service_tier"] not in ("fast", "priority", "flex", "default"):
            raise AdapterError("service_tier", "Unsupported requested service tier", 401)
        raw = json_bytes(payload)
        if len(raw) > self.max_input_bytes:
            with self._lock:
                self.rejections.append({"phase": phase, "code": "input_limit", "input_bytes": len(raw), "host_invoked": False})
            raise AdapterError("input_limit", "Native request exceeds its explicit byte cap")
        acquired = []
        try:
            for gate in (self._role_slots[binding["role"]], self._slots):
                while not gate.acquire(timeout=0.1):
                    if self._cancelled.is_set():
                        raise AdapterError("host_cancelled", "Native host cancelled while queued", 401)
                acquired.append(gate)
            with self._lock:
                if self._cancelled.is_set():
                    raise AdapterError("host_cancelled", "Native host cancelled before reservation", 401)
                if self._next_ordinal > self.max_calls:
                    self.rejections.append({"phase": phase, "code": "call_budget", "host_invoked": False})
                    raise AdapterError("call_budget", "Global native-host invocation budget exhausted")
                number = self._next_ordinal
                request_path = self.run_dir / "calls" / f"{number:05d}.request.json"
                with request_path.open("xb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                receipt = {"ordinal": number, "phase": phase, "role": binding["role"], "operation": operation,
                           "request_sha256": hashlib.sha256(raw).hexdigest(), "input_bytes": len(raw),
                           "requested_model": binding["model"], "requested_effort": binding["effort"],
                           "requested_service_tier": binding["service_tier"], "host_invoked": False,
                           "host_process_started": False, "measurement_id": self._measurement_id}
                self.start_attempt(receipt)
            started = time.perf_counter()
            try:
                yield ExternalAttempt(self, receipt, request_path)
                if receipt.get("status") not in ("completed", "failed", "interrupted"):
                    raise AdapterError("external_attempt_unfinished", "Native caller did not validate a final status", 401)
            except BaseException as error:
                receipt.update(status="interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) or getattr(error, "code", None) == "host_cancelled" else "failed",
                               error_code=getattr(error, "code", type(error).__name__), accounting_complete=False)
                if hasattr(error, "cleanup"):
                    receipt["timeout_cleanup"] = error.cleanup
                raise
            finally:
                self._process_finished(receipt)
                if receipt.get("status") != "completed":
                    receipt["accounting_complete"] = False
                receipt.setdefault("usage", None)
                receipt["elapsed_ms"] = (time.perf_counter() - started) * 1000
                self._append(receipt)
        finally:
            for gate in reversed(acquired):
                gate.release()

    def _process_started(self, receipt: dict, pid: int) -> None:
        with self._lock:
            number, role = receipt["ordinal"], receipt["role"]
            self._active[number] = role
            receipt.update(host_invoked=True, host_process_started=True, host_pid=pid,
                           process_started_unix_ns=time.time_ns(), process_started_monotonic_ns=time.monotonic_ns(),
                           active_host_processes_at_start=len(self._active),
                           active_role_processes_at_start=sum(value == role for value in self._active.values()))
            path = self.run_dir / "calls" / f"{number:05d}.process-start.json"
            with path.open("xb") as output:
                output.write(json_bytes(receipt))
                output.flush()
                os.fsync(output.fileno())

    def _process_finished(self, receipt: dict) -> None:
        with self._lock:
            if receipt["ordinal"] in self._active:
                receipt.update(process_finished_unix_ns=time.time_ns(), process_finished_monotonic_ns=time.monotonic_ns())
                del self._active[receipt["ordinal"]]

    def _save_response(self, receipt: dict, raw: bytes) -> dict:
        receipt["response_sha256"] = hashlib.sha256(raw).hexdigest()
        if len(raw) > OUTPUT_CAP + 65536:
            raise AdapterError("output_limit", "Host response exceeds the adapter output limit")
        path = self.run_dir / "calls" / f"{receipt['ordinal']:05d}.response.json"
        with path.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        try:
            report = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise AdapterError("invalid_host_json", "Host did not return valid JSON", 401) from error
        if not isinstance(report, dict):
            raise AdapterError("invalid_host_json", "Host response must be an object", 401)
        # Retain observed usage even when later status, identity or schema checks fail.
        _retain_accounting(receipt, report)
        receipt.update(model=report.get("model"), model_provider=report.get("model_provider"),
                       thread_id=report.get("thread_id"), turn_id=report.get("turn_id"),
                       effective_reasoning_effort=report.get("effective_reasoning_effort"),
                       reported_host_elapsed_ms=report.get("elapsed_ms"),
                       reported_requested_service_tier=report.get("requested_service_tier"),
                       effective_service_tier=report.get("effective_service_tier"))
        return report

    async def acomplete(self, instructions: str, state, schema: dict, *, model=None, effort=None, phase=None, role=None, service_tier=None,
                        on_reserved=None):
        # Snapshot before dispatch; neither caller mutation nor later role phases
        # can change a queued request. This executor is never asyncio's default pool.
        request = json.loads(json_bytes({"instructions": instructions, "state": state, "schema": schema}))
        binding = self._bindings(model, effort, phase, role, service_tier)
        cancelled = threading.Event()
        with self._lock:
            if self._cancelled.is_set():
                raise AdapterError("host_cancelled", "Local host is closed", 401)
            future = self._executor.submit(self.complete, request["instructions"], request["state"], request["schema"],
                                           **binding, on_reserved=on_reserved, _cancel_event=cancelled)
            self._futures.add(future)
            future.add_done_callback(self._forget_future)
        wrapped = asyncio.wrap_future(future)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            cancelled.set()
            # Drain the paid attempt and its final receipt before SDK teardown.
            try:
                await asyncio.shield(wrapped)
            except (Exception, asyncio.CancelledError):
                pass
            raise

    def _forget_future(self, future) -> None:
        with self._lock:
            self._futures.discard(future)

    def drain(self) -> None:
        with self._lock:
            futures = list(self._futures)
        if futures:
            wait(futures)
        with self._lock:
            self._futures.difference_update(future for future in futures if future.done())

    def close(self, *, cancel: bool = False) -> None:
        if cancel:
            self._cancelled.set()
        self.drain()
        self._cancelled.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def cancel(self) -> None:
        """Request cleanup only for this instance's owned host invocations."""
        self._cancelled.set()

    def concurrency_report(self, calls=None) -> dict:
        with self._lock:
            selected = list(self.calls if calls is None else calls)
        def observed(items):
            complete = [item for item in items if "process_started_monotonic_ns" in item and "process_finished_monotonic_ns" in item]
            peak, busy_ns, summed_ns = 0, 0, 0
            for measurement in {item.get("measurement_id") for item in complete}:
                events = []
                for item in complete:
                    if item.get("measurement_id") != measurement:
                        continue
                    start, end = item["process_started_monotonic_ns"], item["process_finished_monotonic_ns"]
                    summed_ns += end - start
                    events.extend(((start, 1), (end, -1)))
                active, previous = 0, None
                for instant, delta in sorted(events):
                    if previous is not None and active:
                        busy_ns += instant - previous
                    active += delta
                    peak = max(peak, active)
                    previous = instant
            return {"completed_intervals": len(complete), "calls_without_complete_interval": len(items) - len(complete),
                    "measured_peak": peak if complete else None, "busy_ms": busy_ns / 1e6 if complete else None,
                    "summed_process_ms": summed_ns / 1e6 if complete else None,
                    "overlap_ms": (summed_ns - busy_ns) / 1e6 if complete else None}
        return {"configured_host_ceiling": self.host_concurrency,
                "configured_role_ceilings": {role: self.role_concurrency[role] for role in ("index", "chat", "judge")},
                "configured_total_ceiling": self.total_concurrency,
                "measurement": "Observed owned-host process intervals; not provider request concurrency or inferred speedup",
                **observed(selected), "by_role": {role: observed([item for item in selected if item.get("role") == role])
                                                   for role in ("index", "chat", "judge")}}

    def complete(self, instructions: str, state, schema: dict, *, model=None, effort=None, phase=None, role=None, service_tier=None,
                 on_reserved=None, _cancel_event=None) -> tuple[dict, dict]:
        request = {"instructions": instructions, "state": state, "schema": schema}
        payload = json_bytes(request)
        request = json.loads(payload)
        binding = self._bindings(model, effort, phase, role, service_tier)
        if binding["service_tier"] not in ("fast", "priority", "flex", "default"):
            raise AdapterError("service_tier", "Unsupported requested service tier", 401)
        cancelled = lambda: self._cancelled.is_set() or (_cancel_event is not None and _cancel_event.is_set())
        with self._lock:
            if len(payload) > self.max_input_bytes:
                failure = {"phase": binding["phase"], "code": "input_limit", "input_bytes": len(payload),
                           "input_sha256": hashlib.sha256(payload).hexdigest(), "host_invoked": False}
                self.rejections.append(failure)
                raise AdapterError("input_limit", "Combined completion request exceeds its explicit byte cap; nothing was truncated")
        acquired = []
        receipt = None
        try:
            for gate in (self._role_slots[binding["role"]], self._slots):
                while not gate.acquire(timeout=0.1):
                    if cancelled():
                        raise AdapterError("host_cancelled", "Local host invocation cancelled while queued", 401)
                acquired.append(gate)
            with self._lock:
                if cancelled():
                    raise AdapterError("host_cancelled", "Local host invocation cancelled before reservation", 401)
                if self._next_ordinal > self.max_calls:
                    self.rejections.append({"phase": binding["phase"], "code": "call_budget", "host_invoked": False})
                    raise AdapterError("call_budget", "Run host-completion budget is exhausted")
                number = self._next_ordinal
                request_path = self.run_dir / "calls" / f"{number:05d}.request.json"
                with request_path.open("xb") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                receipt = {"ordinal": number, "phase": binding["phase"], "role": binding["role"],
                           "request_sha256": hashlib.sha256(payload).hexdigest(),
                           "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
                           "state_sha256": sha(request["state"]), "schema_sha256": sha(request["schema"]), "input_bytes": len(payload),
                           "requested_model": binding["model"], "requested_effort": binding["effort"],
                           "requested_service_tier": binding["service_tier"],
                           "host_invoked": False, "host_process_started": False, "measurement_id": self._measurement_id,
                           "protocol_trace_requested": True}
                self.start_attempt(receipt)
            started = time.perf_counter()
            try:
                if on_reserved is not None:
                    on_reserved(dict(receipt))
                process = owned_process(
                    [str(self.binary), "host-complete", "--input", str(request_path),
                     "--codex-bin", self.codex_bin, "--codex-home", str(self.codex_home),
                     "--model", binding["model"], "--reasoning-effort", binding["effort"],
                     "--service-tier", binding["service_tier"],
                     "--timeout", str(self.timeout), "--max-input-bytes", str(self.max_input_bytes),
                     "--protocol-trace", str(self.run_dir / "calls" / f"{number:05d}.trace.jsonl"), "--json"],
                    cwd=self.run_dir, timeout=self.timeout + 15, cancelled=cancelled,
                    on_start=lambda pid: self._process_started(receipt, pid),
                )
                self._process_finished(receipt)
                report = self._save_response(receipt, process.stdout)
                if process.returncode != 0:
                    code = str(report.get("code", report.get("error_code", "host_failure")))
                    raise AdapterError(code, f"Local host failed: {code}", 400 if "limit" in code else 401)
                if report.get("status") != "completed":
                    raise AdapterError("host_status", "Local host did not establish completed status", 401)
                if report.get("model") != binding["model"] or report.get("requested_reasoning_effort") != binding["effort"]:
                    raise AdapterError("host_identity", "Local host changed the selected model/effort", 401)
                if report.get("effective_reasoning_effort") not in (None, binding["effort"]):
                    raise AdapterError("host_effort", "Local host reported a different effective effort", 401)
                if report.get("requested_service_tier") not in (None, binding["service_tier"]):
                    raise AdapterError("host_service_tier", "Local host changed the requested service tier", 401)
                effective_tier = report.get("effective_service_tier")
                normalize_tier = lambda value: "priority" if value in ("fast", "priority") else value
                if effective_tier is not None and normalize_tier(effective_tier) != normalize_tier(binding["service_tier"]):
                    raise AdapterError("host_service_tier", "Local host reported a non-equivalent service tier", 401)
                if report.get("auth_mode") != "chatgpt":
                    raise AdapterError("host_auth", "Local host did not establish ChatGPT authentication", 401)
                if report.get("model_provider") != "openai" or not report.get("thread_id") or not report.get("turn_id"):
                    raise AdapterError("host_identity", "Local host did not establish provider/thread/turn identity", 401)
                value = report["value"]
                jsonschema.Draft202012Validator(request["schema"]).validate(value)
                if len(json_bytes(value)) > OUTPUT_CAP:
                    raise AdapterError("output_limit", "Completion value exceeds128KiB")
                receipt.update(status="completed", model=report["model"], model_provider=report.get("model_provider"),
                               thread_id=report.get("thread_id"), turn_id=report.get("turn_id"),
                               effective_reasoning_effort=report.get("effective_reasoning_effort"),
                               reported_host_elapsed_ms=report.get("elapsed_ms"),
                               response_sha256=hashlib.sha256(process.stdout).hexdigest(), value_sha256=sha(value))
            except BaseException as error:
                self._process_finished(receipt)
                if getattr(error, "stdout", None) and "response_sha256" not in receipt:
                    try:
                        self._save_response(receipt, error.stdout)
                    except AdapterError:
                        pass
                receipt.update(status="interrupted" if isinstance(error, KeyboardInterrupt) or getattr(error, "code", None) == "host_cancelled" else "failed",
                               error_code=getattr(error, "code", type(error).__name__), accounting_complete=False,
                               elapsed_ms=(time.perf_counter() - started) * 1000)
                receipt.setdefault("usage", None)
                if hasattr(error, "cleanup"):
                    receipt["timeout_cleanup"] = error.cleanup
                self._append(receipt)
                if isinstance(error, (AdapterError, KeyboardInterrupt, SystemExit)):
                    raise
                raise AdapterError("host_failure", "Local host completion failed; inspect the private receipt", 401) from error
            else:
                # An interruption after this durable append must not create a
                # second final receipt for the same paid attempt.
                self._append({**receipt, "elapsed_ms": (time.perf_counter() - started) * 1000})
                return value, report
        finally:
            for gate in reversed(acquired):
                gate.release()
