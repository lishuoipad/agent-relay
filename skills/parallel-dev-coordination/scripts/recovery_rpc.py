"""Bounded JSON-RPC connection to an EXISTING Codex app-server control socket."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time


class RpcError(RuntimeError):
    def __init__(self, code, message="RPC rejected"):
        super().__init__(message)
        self.code = code


class Rpc:
    MAX_LINE = 4 * 1024 * 1024

    def __init__(self, command, timeout=15):
        self.timeout = timeout
        self.events = queue.Queue(maxsize=512)
        self.responses = {}
        self.condition = threading.Condition()
        self.failed = None
        self.serial = 0
        self.proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                line = self.proc.stdout.readline(self.MAX_LINE + 1)
                if not line:
                    raise ConnectionError("control socket closed")
                if len(line) > self.MAX_LINE:
                    raise ConnectionError("RPC response exceeds bounded read limit")
                msg = json.loads(line)
                with self.condition:
                    if "id" in msg and "method" not in msg:
                        self.responses[msg["id"]] = msg
                        self.condition.notify_all()
                    elif "id" in msg:
                        # Never approve tool execution on behalf of the human.
                        raise ConnectionError("server requested approval; return control to user")
                    elif msg.get("method") in {
                        "error", "turn/started", "turn/completed", "thread/status/changed",
                        "thread/goal/updated", "thread/goal/cleared", "thread/closed",
                    }:
                        self.events.put_nowait(msg)
        except Exception:
            with self.condition:
                self.failed = "RPC disconnected, oversized, malformed, or requires attention"
                self.condition.notify_all()

    def _write(self, msg):
        payload = json.dumps(msg, separators=(",", ":")).encode() + b"\n"
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()

    def call(self, method, params):
        with self.condition:
            if self.failed:
                raise ConnectionError(self.failed)
            self.serial += 1
            rid = self.serial
            self._write({"id": rid, "method": method, "params": params})
            deadline = time.monotonic() + self.timeout
            while rid not in self.responses:
                if self.failed:
                    raise ConnectionError(self.failed)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.failed = "RPC timed out; outcome unknown"
                    raise TimeoutError(self.failed)
                self.condition.wait(remaining)
            msg = self.responses.pop(rid)
            if "error" in msg:
                # Do not log raw provider messages or payloads (may contain secrets).
                raise RpcError(msg["error"].get("code"))
            return msg["result"]

    def initialize(self):
        result = self.call("initialize", {
            "clientInfo": {"name": "agent_relay_recovery", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        })
        self._write({"method": "initialized", "params": {}})
        return result

    def wait(self, seconds):
        if self.failed:
            raise ConnectionError(self.failed)
        try:
            return self.events.get(timeout=min(30, max(0, seconds)))
        except queue.Empty:
            return None

    def close(self):
        # Only terminate the proxy created by this object, never the shared daemon.
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        self.reader.join(timeout=1)
        for stream in (self.proc.stdin, self.proc.stdout):
            stream.close()
