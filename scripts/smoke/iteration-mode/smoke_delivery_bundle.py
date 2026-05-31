#!/usr/bin/env python3
"""Increment 2b + the companion polish: delivery dedups one file, replaces a
pinned file in place, and ships markdown companions beside code in a bundle.

Run:  ~/modulatio/.venv/bin/python smoke_delivery_bundle.py
"""
import os, tempfile
from pathlib import Path
os.environ["MODULATIO_DELIVERY_DIR"] = tempfile.mkdtemp()
from modulatio import delivery as d


class T:
    def __init__(self, tid, op):
        self.deliverable = True
        self.id = tid
        self.output_path = op
        self.description = "x"


art = Path(tempfile.mkdtemp())

# 1) dedup: three tasks edit one game.py -> ONE deliverable (last writer)
got = d.deliverables_from_tasks(
    [T("T-1", "game.py"), T("T-2", "game.py"), T("T-3", "game.py")], art,
)
assert len(got) == 1 and got[0][0] == "T-3", got

# 2) code bundle: game.py + README.md both ship verbatim, coherent folder
(art / "game.py").write_text("import pygame\n")
(art / "README.md").write_text("# Demo\n\nrun: python game.py\n")
out = d.deliver_finished_products(
    [("T-1", art / "game.py", None), ("T-2", art / "README.md", "readme")],
    project_code="SMK",
)
assert sorted(x.dest.name for x in out) == ["README.md", "game.py"], out

# 3) pinned replace: an improved code file overwrites its prior copy
ddir = Path(os.environ["MODULATIO_DELIVERY_DIR"]) / "SMK2"
ddir.mkdir(parents=True)
(ddir / "game.py").write_text("old version\n")
(art / "game.py").write_text("improved version\n")
dp = d.deliver_product(
    art / "game.py", project_code="SMK2", task_id="T-9", pinned_names={"game.py"},
)
assert dp.dest.name == "game.py" and dp.dest.read_text() == "improved version\n"
print("PASS: dedup->1 file; README.md ships beside game.py; pinned file replaced not duplicated")
