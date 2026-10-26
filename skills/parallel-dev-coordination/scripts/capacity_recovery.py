"""Finite, opt-in capacity recovery. No model, daemon installation, or token handling.

Only attach to an existing native app-server via its supported stdio proxy.
Default mode is observation; --execute requires per-thread authorization.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import threading
import time
import uuid

from recovery_rpc import Rpc


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    p = Path(path)
    if not p.is_absolute() or p.stat().st_size > 1024 * 1024:
        raise ValueError("handoff must be an absolute path to a file <=1 MiB")
    return hashlib.sha256(p.read_bytes()).hexdigest()


def capacity_error(error):
    """Only trusted protocol error fields; never infer capacity from HTTP 429/503 alone."""
    if not isinstance(error, dict):
        return False
    info = error.get("codexErrorInfo")
    code = error.get("code", "")
    if isinstance(info, str) and info not in ("serverOverloaded", "other"):
        return False
    if code and code not in ("server_overloaded", "server_is_overloaded", "model_overloaded"):
        return False
    if info == "serverOverloaded" or code:
        return True
    # Older clients wrap overload in generic stream/HTTP errors. Be conservative.
    text = str(error.get("message", "")).lower()[:4096]
    if any(x in text for x in ("quota", "usage limit", "rate limit", "unauthorized",
                               "context window", "额度", "限额", "上下文")):
        return False
    return any(x in text for x in ("model is temporarily overloaded", "model is overloaded",
                                   "model is at capacity", "model is busy",
                                   "模型繁忙", "模型容量不足"))


def retry_after(error, now):
    value = error.get("retryAfterSeconds")
    if value is None:
        details = str(error.get("additionalDetails") or "")[:8192]
        match = re.search(r"(?im)^retry-after:\s*([^\r\n]+)", details)
        value = match[1].strip() if match else None
    if value is None:
        return 0
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - now
        except (ValueError, TypeError, OverflowError):
            return math.inf  # Unparseable explicit instruction: do not retry early.
    return max(0, seconds) if math.isfinite(seconds) else math.inf


class Lock:
    """OS-backed lock survives stale files; process death releases ownership."""
    def __init__(self, path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.file = open(self.path, "a+b")
        try:
            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("another recovery owner holds this thread") from None
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {"version": 1, "threads": {}}
        if self.data.get("version") != 1:
            raise ValueError("unsupported recovery state")

    def save(self):
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.path)


def load_config(path):
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise ValueError("config exceeds 64 KiB")
    config = json.loads(raw)
    if config.get("version") != 1:
        raise ValueError("config version must be 1")
    targets = config.get("threads", [])
    if not 1 <= len(targets) <= 8:
        raise ValueError("select 1..8 explicitly authorized independent threads")
    ids = set()
    for t in targets:
        tid = t["threadId"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", tid) or tid in ids:
            raise ValueError("invalid or duplicate threadId")
        ids.add(tid)
        if t.get("authorized") is not True or t.get("exclusiveRecoveryOwner") is not True:
            raise ValueError("explicit recovery/message authority and exclusive dispatch ownership required")
        if not t.get("model") or t.get("effort") not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            raise ValueError("explicit model and effort required")
        if not isinstance(t.get("modelLocked"), bool):
            raise ValueError("explicit modelLocked required")
        if t.get("fallbackModels", []) not in ([], ["gpt-6.1-sol"]):
            raise ValueError("only the approved gpt-6.1-sol fallback is allowed")
        if t["modelLocked"] and t.get("fallbackModels"):
            raise ValueError("locked model cannot have fallbacks")
        if not t.get("batchId") or file_hash(t["handoffPath"]) != t["handoffSha256"]:
            raise ValueError("current batch and frozen handoff hash required")
        bound = t.get("nativeFailureAttemptUpperBound")
        if type(bound) is not int or not 1 <= bound <= 1000:
            raise ValueError("verified finite nativeFailureAttemptUpperBound required")
        if not t.get("nativeRetryEvidence"):
            raise ValueError("record how the native retry upper bound was verified")
        gate = t.get("reconcileCommand")
        if gate is not None and (not isinstance(gate, list) or not gate or
                                 not all(isinstance(x, str) for x in gate) or not Path(gate[0]).is_absolute()):
            raise ValueError("reconcileCommand must be a trusted absolute executable argv")
    policy = {"maxRecoveryTurns": 3, "maxFailureAttemptAllowance": 120, "deadlineSeconds": 900,
              "baseDelaySeconds": 60, "maxDelaySeconds": 300, "cooldownSeconds": 1800,
              "pollSeconds": 60}
    overrides = config.get("policy", {})
    if set(overrides) - set(policy):
        raise ValueError("unknown policy key")
    policy.update(overrides)
    if any(type(x) is not int or x <= 0 for x in policy.values()):
        raise ValueError("policy values must be positive integers")
    if policy["maxRecoveryTurns"] > 5 or policy["deadlineSeconds"] > 3600 or policy["pollSeconds"] < 30:
        raise ValueError("recovery limits: <=5 turns, <=1 hour, polling >=30 seconds")
    config["policy"] = policy
    return config, hashlib.sha256(raw).hexdigest()


class NativeHost:
    def __init__(self, rpc):
        self.rpc = rpc
        self.models = None

    def snapshot(self, tid):
        # No transcript, image, raw tool output, credentials or model request bodies.
        thread = self.rpc.call("thread/read", {"threadId": tid})["thread"]
        turns = self.rpc.call("thread/turns/list", {"threadId": tid, "limit": 1,
                              "sortDirection": "desc", "itemsView": "summary"})["data"]
        goal = self.rpc.call("thread/goal/get", {"threadId": tid}).get("goal")
        pending = self.rpc.call("thread/queue/list", {"threadId": tid, "limit": 1})
        terminals = self.rpc.call("thread/backgroundTerminals/list", {"threadId": tid, "limit": 1})
        if self.models is None:
            models = self.rpc.call("model/list", {"limit": 100})
            self.models = {m["model"]: [e["reasoningEffort"] for e in m["supportedReasoningEfforts"]]
                           for m in models["data"]}
        return {"thread": thread, "turn": turns[0] if turns else None, "goal": goal,
                "queued": bool(pending["data"] or pending.get("nextCursor")),
                "terminals": bool(terminals["data"] or terminals.get("nextCursor")),
                "models": self.models}

    def attach(self, tid):
        return self.rpc.call("thread/resume", {"threadId": tid, "excludeTurns": True})

    def send(self, tid, model, effort, prompt, message_id):
        # Preserve sandbox, approval, Goal, workspace, ownership, and service tier.
        return self.rpc.call("turn/start", {"threadId": tid, "model": model, "effort": effort,
            "clientUserMessageId": message_id, "input": [{"type": "text", "text": prompt}]})


SAFE_ITEMS = {"userMessage", "agentMessage", "reasoning", "plan"}


class Recovery:
    def __init__(self, host, store, config, notify, execute=False, clock=time.time, jitter=random.uniform,
                 cancelled=lambda _tid: False):
        self.host, self.store, self.config, self.notify = host, store, config, notify
        self.execute, self.clock, self.jitter = execute, clock, jitter
        self.policy = config["policy"]
        signals = self.store.data.get("signals", {})
        self.native_retrying = {(tid, v["turnId"]) for tid, v in signals.items() if v.get("willRetry") is True}
        self.retry_not_before = {(tid, v["turnId"]): v.get("notBefore", 0) for tid, v in signals.items()}
        self.cancelled = cancelled

    def event(self, msg):
        p = msg.get("params", {})
        if p.get("threadId") not in {t["threadId"] for t in self.config["threads"]}:
            return
        key = (p.get("threadId"), p.get("turnId"))
        if msg.get("method") == "error":
            error = p.get("error", {})
            if capacity_error(error):
                delay = retry_after(error, self.clock())
                self.retry_not_before[key] = self.clock() + (delay if math.isfinite(delay) else 315360000)
            if p.get("willRetry") is True:
                self.native_retrying.add(key)
            elif p.get("willRetry") is False:
                self.native_retrying.discard(key)
            self.store.data.setdefault("signals", {})[key[0]] = {
                "turnId": key[1], "willRetry": p.get("willRetry"),
                "notBefore": self.retry_not_before.get(key, 0)}
            self.store.save()
        if msg.get("method") == "turn/completed":
            key = (p.get("threadId"), p.get("turn", {}).get("id"))
            self.native_retrying.discard(key)
            self.store.data.setdefault("signals", {})[key[0]] = {
                "turnId": key[1], "willRetry": False, "notBefore": self.retry_not_before.get(key, 0)}
            self.store.save()

    def record(self, t, state, reason, announce=True, **extra):
        old = self.store.data["threads"].get(t["threadId"], {})
        changed = (old.get("state"), old.get("reason")) != (state, reason)
        old.update(state=state, reason=reason, **extra)
        self.store.data["threads"][t["threadId"]] = old
        self.store.save()
        if changed and announce:
            self.notify({"threadId": t["threadId"], "state": state, "reason": reason,
                         "at": self.clock()})
        return old

    def gate(self, t, s):
        turn = s["turn"]
        if turn.get("itemsView") not in ("summary", "full") or "items" not in turn:
            return False
        action_items = [i for i in turn["items"] if i.get("type") not in SAFE_ITEMS]
        if not action_items:
            return True
        command = t.get("reconcileCommand")
        if not command:
            return False
        request = {"threadId": t["threadId"], "turnId": turn["id"], "batchId": t["batchId"],
                   "handoffSha256": t["handoffSha256"],
                   "actions": [{"id": i.get("id"), "type": i.get("type"), "status": i.get("status")}
                               for i in action_items]}
        # An explicitly configured project adapter checks real file/test/input-lease state.
        # Never feed arbitrary tool commands from a transcript to a shell.
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, shell=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        output = []
        reader = threading.Thread(target=lambda: output.append(proc.stdout.read(65537)), daemon=True)
        try:
            proc.stdin.write(json.dumps(request).encode())
            proc.stdin.close()
            reader.start()
            reader.join(timeout=10)
            if reader.is_alive() or not output or len(output[0]) > 65536:
                return False
            if proc.wait(timeout=1):
                return False
            result = json.loads(output[0])
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=2)
            reader.join(timeout=1)
            proc.stdout.close()
        return (result.get("safe") is True and result.get("turnId") == turn["id"]
                and result.get("batchId") == t["batchId"]
                and result.get("handoffSha256") == t["handoffSha256"]
                and result.get("runningOperations") == [] and result.get("pendingApprovals") == []
                and result.get("ownershipValid") is True and result.get("changesReconciled") is True)

    @staticmethod
    def fingerprint(s):
        return digest({"turn": s["turn"], "status": s["thread"]["status"],
                       "goal": s["goal"], "model": s["thread"].get("model"),
                       "effort": s["thread"].get("reasoningEffort"),
                       "queued": s["queued"], "terminals": s["terminals"]})

    def step(self, t):
        tid, now = t["threadId"], self.clock()
        old = self.store.data["threads"].get(tid, {})
        contract = digest({"thread": t, "policy": self.policy})
        if self.cancelled(tid):
            return self.record(t, "suppressed", "user_stop_or_config_changed", contract=contract)
        if old and old.get("contract") != contract:
            return self.record(t, "attention", "contract_changed")
        if old.get("state") == "uncertain" and old.get("reason") == "dispatch_intent":
            return self.record(t, "uncertain", "prior_dispatch_outcome_unknown")
        if old.get("state") in ("uncertain", "attention", "suppressed", "done", "cooldown"):
            return old  # Durable circuit: never reopen the same incident automatically.
        s = self.host.snapshot(tid)
        turn = s.get("turn")
        if not turn:
            return None
        thread = s["thread"]
        # Never recover subagents, unloaded threads, paused/limited Goals or queued user input.
        if thread.get("parentThreadId") or not thread.get("canAcceptDirectInput", False):
            return self.record(t, "suppressed", "not_independent_direct_input_thread", contract=contract)
        if s["goal"] and s["goal"].get("status") != "active":
            return self.record(t, "suppressed", "goal_not_active", contract=contract)
        if s["queued"]:
            return self.record(t, "suppressed", "user_input_pending", contract=contract)
        if old.get("root") and digest(s["goal"]) != old["goalHash"]:
            return self.record(t, "suppressed", "goal_changed")
        if old.get("state") == "sent":
            if turn["id"] != old["sentTurn"]:
                return self.record(t, "suppressed", "newer_turn_exists")
            if thread.get("model") != old["selectedModel"] or thread.get("reasoningEffort") != t["effort"]:
                return self.record(t, "attention", "effective_model_or_effort_mismatch")
            if turn["status"] == "completed":
                return self.record(t, "done", "recovery_turn_completed")
            if turn["status"] == "interrupted":
                return self.record(t, "suppressed", "turn_interrupted")
        elif old.get("observedTurn") and turn["id"] != old["observedTurn"]:
            return self.record(t, "suppressed", "newer_turn_exists")
        if thread["status"]["type"] not in ("idle", "systemError") or turn["status"] == "inProgress":
            return old or None
        if (tid, turn["id"]) in self.native_retrying:
            return old or None
        if turn["status"] != "failed" or not capacity_error(turn.get("error")):
            if old:
                return self.record(t, "suppressed", "not_a_capacity_failure")
            return None
        if s["terminals"]:
            return self.record(t, "attention", "background_operation_running", contract=contract)
        if file_hash(t["handoffPath"]) != t["handoffSha256"]:
            return self.record(t, "attention", "handoff_changed", contract=contract)
        attempts = old.get("attempts", 0)
        expected_model = old.get("selectedModel", t["model"])
        if thread.get("model") != expected_model or thread.get("reasoningEffort") != t["effort"]:
            return self.record(t, "attention", "runtime_settings_changed", contract=contract)
        p = self.policy
        if not old.get("root"):
            completed = turn.get("completedAt")
            # Don't wake an old abandoned failure when a watcher starts days later.
            if not isinstance(completed, (int, float)) or not 0 <= now - completed <= p["deadlineSeconds"]:
                return self.record(t, "suppressed", "stale_or_unknown_failure_time", contract=contract)
            old = self.record(t, "waiting", "capacity_failure", contract=contract, root=turn["id"],
                observedTurn=turn["id"], goalHash=digest(s["goal"]), attempts=0,
                reservedFailureAttempts=t["nativeFailureAttemptUpperBound"], deadline=completed+p["deadlineSeconds"])
        if old.get("scheduledTurn") != turn["id"]:
            delay = max(retry_after(turn["error"], now), self.retry_not_before.get((tid, turn["id"]), now)-now,
                        min(p["maxDelaySeconds"], p["baseDelaySeconds"] * 2 ** attempts)
                        * self.jitter(1.0, 1.2))
            if not math.isfinite(delay) or now + delay > old["deadline"]:
                return self.record(t, "cooldown", "retry_after_exceeds_deadline", cooldownUntil=now+p["cooldownSeconds"])
            old = self.record(t, "waiting", "backoff", scheduledTurn=turn["id"],
                              observedTurn=turn["id"], due=now+delay)
        latest_hint = self.retry_not_before.get((tid, turn["id"]), 0)
        if latest_hint > old["due"]:
            old = self.record(t, "waiting", "backoff", due=latest_hint)
        if old["due"] > old["deadline"]:
            return self.record(t, "cooldown", "retry_after_exceeds_deadline", cooldownUntil=now+p["cooldownSeconds"])
        if now >= old["deadline"] or attempts >= p["maxRecoveryTurns"] or (
                old["reservedFailureAttempts"] + t["nativeFailureAttemptUpperBound"] > p["maxFailureAttemptAllowance"]):
            return self.record(t, "cooldown", "retry_budget_exhausted", cooldownUntil=now+p["cooldownSeconds"])
        if now < old["due"]:
            return old
        choices = [t["model"]] + ([] if t["modelLocked"] else t.get("fallbackModels", []))
        choices = list(dict.fromkeys(choices))
        model = choices[min(attempts, len(choices)-1)]
        if t["effort"] not in s["models"].get(model, []):
            return self.record(t, "attention", "approved_model_or_effort_unavailable")
        if not self.gate(t, s):
            return self.record(t, "attention", "side_effects_need_reconciliation")
        if not self.execute:
            return self.record(t, "observed", "eligible_no_turn_sent", selectedModel=model)
        # Final bounded read immediately before writing; no API CAS exists for turn/start.
        # Explicit exclusive dispatch ownership is therefore required by the config.
        if self.fingerprint(self.host.snapshot(tid)) != self.fingerprint(s):
            return self.record(t, "suppressed", "changed_during_preflight")
        if file_hash(t["handoffPath"]) != t["handoffSha256"]:
            return self.record(t, "attention", "handoff_changed")
        if self.cancelled(tid):
            return self.record(t, "suppressed", "user_stop_or_config_changed")
        message_id = str(uuid.uuid4())
        prompt = ("Agent Relay capacity recovery: continue only the unfinished portion of the existing "
                  "authorized batch. First verify the current Goal, standing rules, ownership, changed "
                  "files and running operations against the handoff. Preserve completed actions; do not "
                  "replay commands or tests without checking their actual results. Do not create or "
                  "replace Goals, repeat external sends, or change scope. If paused, superseded, or "
                  "uncertain, stop and report. Reuse valid evidence and upload only bounded summaries. "
                  f"Batch: {t['batchId']}; handoff: {t['handoffPath']}; SHA256: {t['handoffSha256']}.")
        # Write-ahead intent: after crash/timeout never resend an ambiguous turn/start.
        self.record(t, "uncertain", "dispatch_intent", announce=False, messageId=message_id,
                    attempts=attempts+1, reservedFailureAttempts=old["reservedFailureAttempts"]+t["nativeFailureAttemptUpperBound"],
                    selectedModel=model)
        try:
            result = self.host.send(tid, model, t["effort"], prompt, message_id)
            sent = result["turn"]
            if not sent.get("id") or sent["id"] == turn["id"]:
                raise ValueError("turn/start may have steered an existing turn")
        except Exception:
            return self.record(t, "uncertain", "dispatch_outcome_unknown")
        return self.record(t, "sent", "recovery_requested_not_yet_verified", sentTurn=sent["id"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--sock", type=Path, required=True, help="existing app-server control socket")
    parser.add_argument("--execute", action="store_true", help="send bounded recovery turns to authorized targets")
    parser.add_argument("--duration-seconds", type=int, default=900)
    parser.add_argument("--check", action="store_true", help="read-only capability preflight; never resume or send")
    args = parser.parse_args(argv)
    config, config_hash = load_config(args.config)
    if not args.state_dir.is_absolute() or not args.codex.is_absolute() or not args.sock.is_absolute():
        parser.error("state, codex and socket paths must be absolute")
    if not 1 <= args.duration_seconds <= 21600:
        parser.error("duration must be 1..21600 seconds")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    targets = config["threads"]
    # Stable per-user registry prevents duplicate owners using different output directories.
    lock_root = Path.home() / ".codex" / "agent-relay-recovery-locks"
    lock_root.mkdir(parents=True, exist_ok=True)

    def emit(event):
        # Only state transitions. No payloads, raw failures, tokens or transcript copies.
        print(json.dumps(event, ensure_ascii=False), flush=True)
        with (args.state_dir / "notifications.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    with ExitStack() as stack:
        # All processes must use this canonical state directory; no parallel owners.
        stack.enter_context(Lock(args.state_dir / "owner.lock"))
        for t in sorted(targets, key=lambda x: x["threadId"]):
            identity = digest([os.path.normcase(str(args.sock.resolve())), t["threadId"]])
            stack.enter_context(Lock(lock_root / (identity + ".lock")))
        rpc = Rpc([str(args.codex), "app-server", "proxy", "--sock", str(args.sock)])
        stack.callback(rpc.close)
        rpc.initialize()
        host = NativeHost(rpc)
        for t in targets:
            s = host.snapshot(t["threadId"])
            if s["thread"].get("parentThreadId") or not s["thread"].get("canAcceptDirectInput", False):
                raise RuntimeError("target is not an independent direct-input conversation")
            if s["thread"]["status"]["type"] == "notLoaded":
                raise RuntimeError("target is not loaded in this daemon; no separate app-server may be started")
            if args.check:
                emit({"threadId": t["threadId"], "state": "capabilities_read", "model": s["thread"].get("model"),
                      "effort": s["thread"].get("reasoningEffort"), "status": s["thread"]["status"]["type"]})
            elif args.execute:
                # Subscribe to existing loaded runtime, with no settings overrides or turns.
                host.attach(t["threadId"])
        if args.check:
            return 0
        store = Store(args.state_dir / "state.json")
        def cancelled(tid):
            return ((args.state_dir / "stop.request").exists()
                    or (args.state_dir / (tid + ".pause")).exists()
                    or hashlib.sha256(args.config.read_bytes()).hexdigest() != config_hash)
        recovery = Recovery(host, store, config, emit, args.execute, cancelled=cancelled)
        end = time.monotonic() + args.duration_seconds
        next_check = 0.0
        while time.monotonic() < end:
            if (args.state_dir / "stop.request").exists():
                emit({"state": "stopped", "reason": "user_stop"})
                break
            if hashlib.sha256(args.config.read_bytes()).hexdigest() != config_hash:
                emit({"state": "stopped", "reason": "config_changed"})
                break
            if time.monotonic() >= next_check:
                for t in targets:
                    if (args.state_dir / (t["threadId"] + ".pause")).exists():
                        recovery.record(t, "suppressed", "user_pause", contract=digest({"thread": t, "policy": config["policy"]}))
                    else:
                        recovery.step(t)
                next_check = time.monotonic() + config["policy"]["pollSeconds"]
            event = rpc.wait(min(5, next_check-time.monotonic(), end-time.monotonic()))
            if event:
                recovery.event(event)
                # Events are signals, never authorization. Read fresh state at bounded intervals.
        emit({"state": "watch_ended", "reason": "finite_run_no_automatic_restart"})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(json.dumps({"state": "attention", "reason": type(exc).__name__}), flush=True)
        raise SystemExit(2)
