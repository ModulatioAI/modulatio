# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Durable smoke for Brick 2 of the skill-library arc — offline, no network.

Proves a producer's capabilities now come from its MODEL:
  1. inference gives known model families sensible (tier, cost, caps),
  2. an unknown model is neutral (not an error), a local endpoint is free-local,
  3. the model-preset schema stores explicit capability tags,
  4. roster resolves caps from the model — explicit tag wins, else inference,
  5. the emitted tier vocabulary is one dispatch knows how to rank.

Run: .venv/bin/python scripts/smoke/skill-library/smoke_model_capabilities_brick2.py
Exit 0 = all checks pass.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def main() -> int:
    from modulatio import dispatch, model_capabilities as mc, model_presets, roster

    failures: list[str] = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("Brick 2 smoke — capabilities from the model")

    # 1. inference
    tier, cost, caps = mc.infer("claude-opus-4-8")
    check("opus → strategic / premium-cloud / vision",
          tier == "strategic" and cost == "premium-cloud" and "vision" in caps)
    tier, cost, caps = mc.infer("grok-4.3-latest")
    check("grok → reasoning-heavy + web-search",
          tier == "reasoning-heavy" and "web-search" in caps)

    # 2. unknown + local
    tier, cost, caps = mc.infer("frobnicator-9000")
    check("unknown model is neutral, not error",
          tier == "generalist" and cost is None and caps == ())
    _, cost, _ = mc.infer("qwen3.5:122b", base_url="http://localhost:11434")
    check("local endpoint forces free-local", cost == "free-local")

    # 3 + 4. preset schema + roster resolution (isolated config + vault)
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        model_presets.PRESETS_FILE = tdp / "presets.json"
        from modulatio import vault
        vault.VAULT_ROOT = tdp

        entry = model_presets.add_preset(
            "opus", label="Opus", base_url="https://api.anthropic.com",
            api_format="anthropic", auth_type="api_key", model="claude-opus-4-8",
            model_tier="strategic", capability_tags=["reasoning-heavy", "vision"],
        )
        check("preset stores explicit capability tags",
              entry.get("capability_tags") == ["reasoning-heavy", "vision"])

        model_presets.add_preset(
            "localq", label="Local Qwen", base_url="http://localhost:11434",
            api_format="openai", auth_type="none", model="qwen3.5:122b",
        )
        caps, tier, cost = roster._caps_from_model("localq")
        check("untagged local preset → inferred free-local", cost == "free-local")

        caps, tier, cost = roster._caps_from_model("opus")
        check("explicit tag wins (strategic/vision)",
              tier == "strategic" and "vision" in caps)

        # A skill-less producer bound to the model is dispatch-visible.
        agents = vault.project_dir("smokeproj") / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "p1.md").write_text(
            "---\nid: p1\nname: P1\ntier: producer\nskills: \nmodel: opus\n"
            "model_tier: \ncost_class: \ncapability_tags: \n---\n\nendpoint\n"
        )
        agent = roster.load("p1", "smokeproj")
        check("skill-less producer draws caps from model",
              agent is not None and agent.skills == []
              and agent.covers_capabilities(["reasoning-heavy"]))

    # 5. vocabulary aligns with dispatch
    check("emitted tiers are all dispatch-rankable",
          set(mc.MODEL_TIERS) == set(dispatch._TIER_RANK))

    print()
    if failures:
        print(f"SMOKE FAILED — {len(failures)} check(s): {failures}")
        return 1
    print("SMOKE PASSED — producer capabilities come from the model.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
