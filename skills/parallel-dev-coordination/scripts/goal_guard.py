"""Native lifecycle hook: fresh, single-use pause authority and resume verification.

Does not write Codex databases, start turns, call a model, or create Goals.
Requires native review/trust. Unknown pause authority produces an explicit denial.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import quote

MAX_INPUT = 1024 * 1024
MAX_ADVANCE = 4 * 1024 * 1024
PAUSE = re.compile(r"^(?:先)?(?:暂停|暂停一下|暂停开发|暂停任务|暂停目标|停止自动续跑|先别继续|pause|/goal pause)[。.!！\s]*$", re.I)
RESUME = re.compile(r"^(?:继续|继续吧|继续开发|继续执行|恢复|恢复开发|恢复任务|恢复目标|恢复Goal|resume|/goal resume)[。.!！\s]*$", re.I)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_path(value):
    raw = str(value)
    if re.match(r"^\\\\\?\\[A-Za-z]:\\", raw):
        raw = raw[4:]
    return Path(raw).resolve()


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "Agent Relay Goal guard: " + reason}}


def context(event, text):
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


@contextmanager
def locked(path):
    f = path.open("a+b")
    try:
        f.seek(0, 2)
        if not f.tell():
            f.write(b"0"); f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        f.close()


def read_goal(codex_home, session_id):
    p = (codex_home / "goals_1.sqlite").resolve()
    with closing(sqlite3.connect("file:" + quote(p.as_posix(), safe="/:") + "?mode=ro", uri=True, timeout=.25)) as c:
        row = c.execute("SELECT goal_id,status,objective FROM thread_goals WHERE thread_id=?", (session_id,)).fetchone()
    return None if row is None else {"id": row[0], "status": row[1], "objectiveHash": sha(row[2].encode())}


def transcript_mark(value):
    if not value or not Path(value).is_absolute():
        raise ValueError("native transcript path must be absolute")
    p = canonical_path(value or "")
    if not p.is_absolute() or not p.is_file():
        raise ValueError("native transcript path unavailable")
    s = p.stat()
    # A compact/resume or replaced transcript invalidates earlier pause authority.
    return {"path": str(p.resolve()), "size": s.st_size, "device": s.st_dev, "inode": s.st_ino}


def has_new_control(mark, session_id):
    """Inspect only the bounded append since the native user-prompt hook.

    A goal activation or another user message supersedes this pause permission.
    If history has grown beyond the limit, fail closed instead of scanning a GB rollout.
    """
    p = Path(mark["path"])
    s = p.stat()
    if (s.st_dev, s.st_ino) != (mark["device"], mark["inode"]):
        return True
    advance = s.st_size - mark["size"]
    if not 0 <= advance <= MAX_ADVANCE:
        return True
    with p.open("rb") as f:
        f.seek(mark["size"])
        raw = f.read(MAX_ADVANCE + 1)
    own_prompts = 0
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            # Partial append / unknown record must not grant control authority.
            if line.strip():
                return True
            continue
        q = record.get("payload", {})
        if record.get("type") == "event_msg" and q.get("type") == "thread_goal_updated":
            if q.get("threadId") == session_id and (q.get("goal") or {}).get("status") == "active":
                return True
        if q.get("type") == "message" and q.get("role") == "user":
            text = " ".join(x.get("text", "") for x in q.get("content", []) if isinstance(x, dict))
            # The receipt's own prompt is appended after UserPromptSubmit. Match its
            # digest once; never treat quoted text or an automatic Goal prompt as authority.
            if sha(text.strip().encode()) == mark["promptHash"]:
                own_prompts += 1
                if own_prompts > 1:
                    return True
            else:
                if text.startswith("<external_codex_apps_open_page>"):
                    continue
                return True
    return False


class Guard:
    def __init__(self, state_dir, codex_home, clock=time.time, targets=None, transcript=None):
        self.state_dir = Path(state_dir)
        self.codex_home = Path(codex_home)
        self.clock = clock
        self.targets = None if targets is None else frozenset(targets)
        self.transcript = canonical_path(transcript) if transcript else None

    def handle(self, event):
        name = event.get("hook_event_name")
        session = event.get("session_id", "")
        if self.targets is not None and session not in self.targets:
            return {}
        if (event.get("agent_id") or event.get("agent_transcript_path") or
                self.transcript and canonical_path(event.get("transcript_path") or "") != self.transcript):
            return {}
        if name in ("PreToolUse", "PostToolUse"):
            tool = str(event.get("tool_name", "")).split(".")[-1]
            if tool not in ("get_goal", "update_goal"):
                return {}
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session):
            raise ValueError("invalid session identity")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / (session + ".json")
        with locked(path.with_suffix(".lock")):
            state = json.loads(path.read_text("utf-8")) if path.exists() else {"version": 1}
            result = self.decide(event, state)
            state["lastEvent"] = name
            state["lastObservedAt"] = self.clock()
            temp = path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush(); os.fsync(f.fileno())
            os.replace(temp, path)
            return result

    def decide(self, e, state):
        name, session, turn = e["hook_event_name"], e["session_id"], e.get("turn_id")
        tool = str(e.get("tool_name", "")).split(".")[-1]
        args = e.get("tool_input") or {}
        goal = read_goal(self.codex_home, session)
        state["goalObserved"] = goal
        if name in ("SessionStart", "Interrupt"):
            state.pop("permit", None)
            if name == "SessionStart" and goal:
                return context(name, "Agent Relay Goal状态核对：当前为 " + goal["status"] +
                    "。旧检查点中的暂停不能重新执行。新的恢复优先；恢复开发与恢复Goal须分别核实。"
                    "暂停仅响应本轮新的人类暂停指令，收到后立即执行并读回；工具不支持恢复时明确使用原生恢复控件，不能声称Goal已恢复。")
            return {}
        if name == "UserPromptSubmit":
            prompt = str(e.get("prompt", "")).strip()
            is_pause, is_resume = bool(PAUSE.fullmatch(prompt)), bool(RESUME.fullmatch(prompt))
            state.pop("permit", None)
            state["control"] = {"turnId": turn, "promptHash": sha(prompt.encode()),
                                "kind": "pause" if is_pause else "resume" if is_resume else "other",
                                "observedAt": self.clock()}
            if is_pause and goal and goal["status"] == "active" and turn:
                mark = transcript_mark(e.get("transcript_path"))
                mark["promptHash"] = sha(prompt.encode())
                state["permit"] = {"turnId": turn, "goalId": goal["id"], "objectiveHash": goal["objectiveHash"],
                                   "expiresAt": self.clock()+180, "mark": mark, "consumed": False}
                return context(name, "新的人类暂停指令：立即暂停现有Goal并独立读回，再停止工作；不要仅口头说已暂停。此暂停授权只适用于本轮一次操作。")
            if is_resume and goal and goal["status"] != "active":
                return context(name, "新的人类继续指令取代旧暂停，但实际Goal仍为 " + goal["status"] +
                    "。不要重放旧暂停，也不要将本轮运行称为Goal恢复。使用原生恢复入口并独立读回；"
                    "当前工具不支持恢复时告知这一差异，不重建Goal、不重置预算、不直接写数据库。")
            return {}
        if name == "PreToolUse":
            state["lastTool"] = tool
            if tool != "update_goal" or not isinstance(args, dict) or args.get("status") != "paused":
                return {}
            permit = state.get("permit")
            reason = None
            if not permit or permit.get("turnId") != turn:
                reason = "没有绑定本轮的新的人类暂停指令；不要沿用旧暂停。必要时由用户使用Goal暂停控件。"
            elif permit.get("consumed"):
                reason = "这条暂停指令已经处理或结果待核对，禁止重复暂停。"
            elif self.clock() > permit["expiresAt"]:
                reason = "暂停授权已过期，请使用当前原生Goal控件，不重放历史指令。"
            elif not goal or (goal["id"], goal["objectiveHash"], goal["status"]) != (
                    permit["goalId"], permit["objectiveHash"], "active"):
                reason = "实际Goal已改变或已经暂停，禁止覆盖较新状态。"
            elif has_new_control(permit["mark"], session):
                reason = "暂停指令之后出现新控制、恢复或无法完整核对的历史，禁止旧暂停覆盖新状态。"
            if reason:
                state["lastPauseDecision"] = "denied"
                return deny(reason)
            permit["consumed"] = True
            permit["toolUseId"] = e.get("tool_use_id")
            state["lastPauseDecision"] = "permitted_once"
            return {}
        if name == "PostToolUse" and tool == "update_goal":
            permit = state.get("permit")
            if permit and permit.get("consumed") and permit.get("toolUseId") == e.get("tool_use_id"):
                state["pauseReadback"] = "paused" if goal and goal["status"] == "paused" else "not_confirmed"
                if state["pauseReadback"] != "paused":
                    return {"systemMessage": "Agent Relay：暂停写入后读回尚未确认；不得重试覆盖较新状态。"}
        return {}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--codex-home", type=Path, required=True)
    p.add_argument("--code-sha256", required=True)
    p.add_argument("--thread-id", action="append", required=True,
                   help="Only these explicitly installed thread IDs are protected")
    p.add_argument("--transcript-path", type=Path, required=True,
                   help="Exact parent transcript; ignore child and side-session hooks")
    args = p.parse_args()
    event = {}
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT+1)
        if len(raw) > MAX_INPUT:
            raise ValueError("oversized hook input")
        event = json.loads(raw)
        if event.get("session_id") not in args.thread_id:
            print("{}"); return
        if sha(Path(__file__).read_bytes()) != args.code_sha256:
            raise ValueError("hook implementation hash changed; review exact definition again")
        result = Guard(args.state_dir, args.codex_home, targets=args.thread_id,
                       transcript=args.transcript_path).handle(event)
    except Exception:
        # Runtime hook errors may fail open. Emit a valid explicit deny ourselves.
        if (event.get("hook_event_name") == "PreToolUse"
                and str(event.get("tool_name", "")).split(".")[-1] == "update_goal"
                and isinstance(event.get("tool_input"), dict)
                and event["tool_input"].get("status") == "paused"):
            result = deny("保护状态无法核实，拒绝本次Goal控制；请通过原生控件操作。")
        else:
            result = {"systemMessage": "Agent Relay Goal guard 状态无法核实；自动保护未确认，请核对原生Goal状态。"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
