# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Durable smoke for Brick 1 of the skill-library arc — offline, no network.

Proves the discover/checkout surface end to end:
  1. the resident index covers the bundled seeds and carries NO bodies,
  2. lexical search finds a skill by capability words,
  3. checkout returns the full body (and an unknown name is empty, not an error),
  4. the search_skills / load_skill / drop_skill builtins are registered and
     return sane producer-facing text.

Run: .venv/bin/python scripts/smoke/skill-library/smoke_skill_library_brick1.py
Exit 0 = all checks pass.
"""

from __future__ import annotations

import sys


def main() -> int:
    from modulatio import skill_library, tools

    failures: list[str] = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("Brick 1 smoke — skill library + checkout builtins")

    # 1. index
    idx = skill_library.build_index()
    names = {e.name for e in idx}
    check("index covers seed skills (web-search, qc, leader)",
          {"web-search", "qc", "leader"} <= names)
    check("index entries carry no body field",
          not any(hasattr(e, "prompt_template") for e in idx))
    check("index has no duplicate names",
          len([e.name for e in idx]) == len(names))

    # 2. search
    hits = skill_library.search_skills("web search discover current sources")
    check("search finds web-search", any(e.name == "web-search" for e in hits))
    check("empty query returns nothing", skill_library.search_skills("  ") == [])

    # 3. checkout
    ws = skill_library.checkout("web-search")
    check("checkout returns full body",
          ws.name == "web-search" and bool(ws.prompt_template.strip()))
    check("checkout unknown is empty, not error",
          skill_library.checkout("no-such-skill").name == "")

    # 4. builtins
    reg = tools.build_registry()
    check("builtins registered",
          all(t in reg for t in ("search_skills", "load_skill", "drop_skill")))
    check("search_skills builtin lists matches",
          "Skills matching" in reg["search_skills"].call(query="web"))
    check("load_skill builtin returns guidance",
          "Skill checked out: web-search" in reg["load_skill"].call(name="web-search"))
    check("load_skill unknown points to search",
          "search_skills" in reg["load_skill"].call(name="nope"))
    check("drop_skill builtin is advisory + reversible",
          "load_skill" in reg["drop_skill"].call(name="web-search"))

    print()
    if failures:
        print(f"SMOKE FAILED — {len(failures)} check(s): {failures}")
        return 1
    print("SMOKE PASSED — discover + checkout work; nothing routes on it yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
