"""Install narrowly scoped Goal hooks; never grant native hook trust.

Requires preinstalled sibling goal_guard.py, an existing parent thread transcript,
and explicit local output paths. Only merges this installer's own hook handlers.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import time

LABEL = "Agent Relay Goal guard"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_path(p):
    raw = str(p)
    if re.match(r"^\\\\\?\\[A-Za-z]:\\", raw):
        raw = raw[4:]
    if not Path(raw).is_absolute():
        raise ValueError("Hook command paths must be absolute")
    value = Path(raw).resolve().as_posix()
    # Native shell selection varies by host. This installer supports only the
    # shell-neutral paths used in this deployment, and refuses interpolation.
    if not re.fullmatch(r"[A-Za-z0-9_./:\\-]+", value):
        raise ValueError("Hook command paths must contain no spaces or shell metacharacters")
    return value


def build_handlers(python, script, codex_home, state_dir, thread_id, transcript):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", thread_id):
        raise ValueError("invalid thread identity")
    for p in (python, script, transcript):
        if not Path(p).is_file():
            raise ValueError("required installed executable, helper or transcript is absent")
    command = (f"{safe_path(python)} -X utf8 -B {safe_path(script)}"
               f" --codex-home {safe_path(codex_home)} --state-dir {safe_path(state_dir)}"
               f" --code-sha256 {sha(Path(script).read_bytes())} --thread-id {thread_id}"
               f" --transcript-path {safe_path(transcript)}")
    out = {}
    for event in ("SessionStart", "UserPromptSubmit", "Interrupt", "PreToolUse", "PostToolUse"):
        handler = {"type":"command", "command":command, "timeout":3,
                   "statusMessage":LABEL+" ["+thread_id+"]"}
        if event in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
            handler["additionalContextLimit"] = 500
        group = {"hooks":[handler]}
        if event in ("PreToolUse", "PostToolUse"):
            group["matcher"] = r"^(?:.*\.)?(?:get_goal|update_goal)$"
        out[event] = [group]
    return out


def merge(existing, handlers):
    result = copy.deepcopy(existing)
    labels = {h["statusMessage"] for groups in handlers.values() for g in groups for h in g["hooks"]}
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("existing hooks must be an object")
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            new = copy.deepcopy(group)
            new["hooks"] = [h for h in group["hooks"] if not (
                h.get("statusMessage") in labels and "/goal_guard.py " in h.get("command", ""))]
            if new["hooks"]:
                kept.append(new)
        hooks[event] = kept
    for event, groups in handlers.items():
        hooks.setdefault(event, []).extend(groups)
    return result


def install(codex_home, backup_dir, handlers):
    target = Path(codex_home)/"hooks.json"
    before = target.read_bytes() if target.exists() else None
    existing = json.loads(before.decode("utf-8-sig")) if before is not None else {}
    merged = merge(existing, handlers)
    if merged == existing:
        return {"changed":False,"path":str(target),"sha256":sha(before),"trust":"not_modified"}
    raw = (json.dumps(merged,ensure_ascii=False,indent=2)+"\n").encode()
    backup = Path(backup_dir)
    backup.mkdir(parents=True,exist_ok=True)
    receipt_path = backup/("goal-hook-install-"+str(time.time_ns())+".json")
    saved = None
    if before is not None:
        saved = receipt_path.with_suffix(".before.json")
        saved.write_bytes(before)
    if (target.read_bytes() if target.exists() else None) != before:
        raise RuntimeError("hooks changed during preparation; nothing installed")
    temp = target.with_name(".hooks-agent-relay-"+str(time.time_ns())+".tmp")
    with temp.open("xb") as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())
    os.replace(temp,target)
    receipt = {"changed":True,"path":str(target),"sha256":sha(raw),
               "backup":str(saved) if saved else None,"previouslyExisted":before is not None,
               "trust":"requires_native_review","events":list(handlers)}
    receipt_path.write_text(json.dumps(receipt,indent=2)+"\n",encoding="utf-8")
    return receipt


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("python","codex-home","state-dir","backup-dir","transcript-path"):
        p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--thread-id",required=True)
    a=p.parse_args()
    handlers=build_handlers(a.python,Path(__file__).with_name("goal_guard.py"),a.codex_home,
                            a.state_dir,a.thread_id,a.transcript_path)
    print(json.dumps(install(a.codex_home,a.backup_dir,handlers),ensure_ascii=False))


if __name__=="__main__":main()
