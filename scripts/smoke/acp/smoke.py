#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Live smoke for `modulatio acp` over real stdio.

Spawns the CLI ACP server as a subprocess (stub mode — no models needed),
drives a real ndjson JSON-RPC conversation (initialize → session/new →
session/prompt), and asserts the responses. Keeps stdin open until the prompt
reply arrives, then closes (EOF) so the server exits cleanly.

    .venv/bin/python scripts/smoke/acp/smoke.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading


def _read_responses(proc, want_id, done):
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        print(f"  <- {line}", file=sys.stderr)
        done.setdefault("by_id", {})[msg.get("id")] = msg
        if msg.get("id") == want_id:
            done["event"].set()
            return


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-m", "modulatio.cli", "acp", "--stub", "--code", "SMK"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )
    done: dict = {"event": threading.Event(), "by_id": {}}
    reader = threading.Thread(target=_read_responses, args=(proc, 3, done), daemon=True)
    reader.start()

    def send(obj):
        line = json.dumps(obj)
        print(f"  -> {line}", file=sys.stderr)
        proc.stdin.write(line + "\n")
        proc.stdin.flush()

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    send({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}})
    send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
          "params": {"sessionId": "sess-1", "prompt": "hello over ACP"}})

    ok = done["event"].wait(timeout=30)
    proc.stdin.close()  # EOF → server exits
    proc.wait(timeout=10)

    by_id = done["by_id"]
    assert ok, "no response to session/prompt within 30s"
    assert by_id[1]["result"]["protocolVersion"] >= 1, "bad initialize"
    assert by_id[2]["result"]["sessionId"] == "sess-1", "bad session/new"
    reply = by_id[3]["result"]["reply"]
    assert by_id[3]["result"]["stopReason"] == "end_turn", "bad stopReason"
    assert reply, "empty reply"
    print(f"OK — ACP stdio round-trip works. Leader reply: {reply!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
