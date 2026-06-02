#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A minimal real ACP client for driving `modulatio acp` by hand.

Spawns the server (REAL mode by default), does the handshake, then sends each
prompt given on argv as a turn. Auto-APPROVES every tool-permission request
(printing it), and prints session/update activity + the Leader's reply.

    .venv/bin/python scripts/smoke/acp/client.py --code ACP "your prompt" "another"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading


class Client:
    def __init__(self, code: str, stub: bool, decision: str) -> None:
        cmd = [sys.executable, "-m", "modulatio.cli", "acp", "--code", code]
        if stub:
            cmd.append("--stub")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.decision = decision  # "allow" or "deny"
        self._resp: dict = {}
        self._events: dict = {}
        self._ids = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("method") == "session/update":
                u = msg.get("params", {}).get("update", {})
                print(f"    · activity: {u.get('role','')}/{u.get('phase','')}",
                      file=sys.stderr)
            elif msg.get("method") == "session/request_permission":
                tc = msg["params"]["toolCall"]
                print(f"    ⚠ permission: {tc['name']}({tc.get('rawInput')}) "
                      f"→ {self.decision}", file=sys.stderr)
                opt = "allow" if self.decision == "allow" else "reject"
                self._send({"jsonrpc": "2.0", "id": msg["id"],
                            "result": {"outcome": {"outcome": "selected",
                                                   "optionId": opt}}})
            elif msg.get("method") == "session/request_input":
                self._send({"jsonrpc": "2.0", "id": msg["id"],
                            "result": {"answer": ""}})
            elif "id" in msg and ("result" in msg or "error" in msg):
                self._resp[msg["id"]] = msg
                ev = self._events.get(msg["id"])
                if ev:
                    ev.set()

    def _send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout=900):
        self._ids += 1
        rid = self._ids
        ev = threading.Event()
        self._events[rid] = ev
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params or {}})
        if not ev.wait(timeout):
            raise TimeoutError(f"{method} timed out after {timeout}s")
        return self._resp[rid]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="ACP")
    ap.add_argument("--stub", action="store_true")
    ap.add_argument("--deny", action="store_true", help="deny tool permissions")
    ap.add_argument("prompts", nargs="+")
    args = ap.parse_args()

    c = Client(args.code, args.stub, "deny" if args.deny else "allow")
    try:
        init = c.request("initialize", {})
        print(f"initialize → protocol v{init['result']['protocolVersion']}")
        sid = c.request("session/new", {})["result"]["sessionId"]
        print(f"session/new → {sid}\n")
        for prompt in args.prompts:
            print(f">>> {prompt}")
            resp = c.request("session/prompt", {"sessionId": sid, "prompt": prompt})
            if "error" in resp:
                print(f"<<< ERROR: {resp['error']}\n")
            else:
                print(f"<<< {resp['result']['reply']}\n")
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
