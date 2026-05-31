#!/usr/bin/env python3
"""Increment 2a: the code read-toolkit (grep/tail/wc + read-only sed) is allowed
in the passive profile and confined to the artifacts root; every escape / write /
exec form is rejected.

Run:  ~/modulatio/.venv/bin/python smoke_read_toolkit.py
"""
import os, shlex, tempfile
from pathlib import Path
from modulatio.tools import _check_passive

root = Path(tempfile.mkdtemp())
(root / "game.py").write_text("JUMP = -12\n" * 60)
os.chdir(root)
chk = lambda c: _check_passive(shlex.split(c), root)

allow = [
    "grep -n JUMP game.py", "grep -ni jump game.py",
    "tail -n 5 game.py", "wc -l game.py", "sed -n '1,3p' game.py",
]
deny = [
    "grep -r x .",                  # recursive tree walk
    "grep -n root /etc/passwd",     # outside root
    "grep -f patterns.txt game.py", # read-from-file flag
    "grep -n JUMP game.py -",       # bare - = stdin, not a file (Nemo note)
    "sed -i s/x/y/ game.py",        # in-place WRITE
    "sed -n 1e/bin/sh game.py",     # exec via e command
    "tail -n 5 /etc/shadow",        # outside root
    "wc -l /etc/passwd",            # outside root
]
for c in allow:
    assert chk(c), f"should ALLOW: {c}"
for c in deny:
    assert not chk(c), f"should DENY: {c}"
print(f"PASS: {len(allow)} read tools allowed + confined; {len(deny)} dangerous forms rejected")
