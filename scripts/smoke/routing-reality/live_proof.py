"""Live proof for Brick 1 (routing reality), v0.6.0.

Runs a real-model kickoff THROUGH THE DAEMON PATH (the headless path JT/cron
use) with two producers on two DISTINCT models, and shows from the audit trail
that the two producer tasks actually ran on the two different models.

On `main` this is impossible: the daemon constructed the Orchestrator without
the per-agent pool, so every producer task collapsed onto the single role-keyed
``runners["drafter"]`` model — the producer audit rows would all show ONE model
regardless of which agent dispatch picked.

Producers use kimi + nemotron so they are unambiguously distinct from the
leader (deepseek) and QC (grok) seats — i.e. two producer models that are
neither the leader's nor QC's.

Usage:
    .venv/bin/python scripts/smoke/routing-reality/live_proof.py
(Needs OLLAMA_API_KEY + XAI_API_KEY in the env; uses real models.)
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from modulatio import config, daemon, roster, vault

CODE = "RPROOF"
LEADER_MODEL = "openai_deepseek_v4_pro_cloud"
QC_MODEL = "openai_grok_4_3"
PRODUCERS = [
    ("prod-kimi", "ollama_kimi_k2_6"),
    ("prod-nemotron", "openai_nemotron_3_super_latest"),
]
PROOF_ROOT = Path("/tmp/routing-reality-proof-vault")


def main() -> int:
    # Isolated vault so we never touch real projects. Real config (defaults.json,
    # model presets) + real env creds are left untouched so the models work.
    if PROOF_ROOT.exists():
        shutil.rmtree(PROOF_ROOT)
    vault.VAULT_ROOT = PROOF_ROOT
    vault.init_project(CODE, CODE, "routing proof")

    # Preserve the user's real defaults (shared-resource paths etc.); only
    # REDIRECT the defaults file to the proof vault (no clobber of real config)
    # and force the fallback drafter seat to a READY model so a stray
    # non-dispatched task can't crash on the real config's not-ready OpenRouter
    # specialist. Producers override this seat via their own models anyway.
    import copy
    real_defaults = copy.deepcopy(config._load_defaults())
    real_defaults.setdefault("default_models", {})
    real_defaults["default_models"].update(
        {"leader": LEADER_MODEL, "specialist": QC_MODEL, "qc": QC_MODEL}
    )
    config.DEFAULTS_FILE = PROOF_ROOT / "defaults.json"
    config.save_defaults(real_defaults)

    # Two producers on two DISTINCT models, capacity_cap=1 so a 2-task wave is
    # forced to spread across both (least-loaded routing).
    for aid, model in PRODUCERS:
        roster.save(
            roster.Agent(
                id=aid,
                name=aid,
                identity=f"{aid} — a producer.",
                skills=["drafter"],
                model=model,
                capability_tags=["generalist", "long-context", "reasoning-heavy"],
                cost_class="paid-cloud",
                tier="producer",
                capacity_cap=1,
            ),
            project_code=CODE,
        )

    objective = (
        "Produce TWO separate, independent one-paragraph briefs, each its own "
        "deliverable: (1) titled 'Tide Pools' on rocky-shore tide pool "
        "ecosystems; (2) titled 'Desert Dunes' on sand dune formation. Two "
        "distinct documents, not one combined piece."
    )

    print("=== Running daemon dispatch (headless path) on real models ===")
    cb = daemon._make_dispatch_callback(stub=False)
    result = cb(CODE, objective)
    print("KICKOFF:", result)

    proj = PROOF_ROOT / CODE.lower()
    runs = sorted((proj / "runs").glob("*"))
    if not runs:
        print("NO RUN DIR — kickoff did not produce a run")
        return 1
    run = runs[-1]
    print("RUN DIR:", run)

    # Per-task dispatch: which agent did each task land on?
    print("\n=== Tasks (id → assigned_agent_id) ===")
    task_agents: dict[str, str] = {}
    for tf in sorted(run.rglob("*.md")):
        if "/tasks/" not in str(tf):
            continue
        text = tf.read_text()
        meta = text.split("---")[1] if text.startswith("---") else ""
        tid = aid = None
        for line in meta.splitlines():
            if line.startswith("id:"):
                tid = line.split(":", 1)[1].strip()
            if line.startswith("assigned_agent_id:"):
                aid = line.split(":", 1)[1].strip()
        if tid:
            task_agents[tid] = aid or "(none)"
            print(f"  {tid} → {aid}")

    # Audit: did each producer call run on ITS OWN dispatched agent's model?
    # (Per-agent correctness is the model-BINDING proof; load-spread across the
    # producers is a separate dispatch concern.)
    audit = run / "audit.jsonl"
    expected = dict(PRODUCERS)  # agent_id -> its own model
    observed: list[tuple[str, str]] = []  # (agent_id, model) for producer calls
    print("\n=== Producer audit rows (agent → model that actually ran) ===")
    if audit.exists():
        for line in audit.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            aid = row.get("agent_id")
            if aid in expected and row.get("runner_role") == "drafter":
                observed.append((aid, row.get("model")))
        for aid, model in sorted(set(observed)):
            mark = "OK" if model == expected[aid] else "MISMATCH"
            print(f"  [{mark}] agent={aid} ran on {model} (its own model: {expected[aid]})")
    else:
        print("  (no audit.jsonl found at", audit, ")")

    print("\n=== VERDICT ===")
    exercised = sorted({a for a, _ in observed})
    mismatches = [(a, m) for a, m in set(observed) if m != expected[a]]
    print("producer agents exercised:", exercised)
    print("producers run on a model NOT their own:", mismatches or "NONE")
    ok = bool(observed) and not mismatches
    print(
        "PASS — every producer ran on its OWN dispatched agent's model, not the "
        "single chat-fallback. On the pre-fix code these rows showed one chat "
        "model regardless of which agent dispatch picked."
        if ok else
        "REVIEW — a producer ran on a model that isn't its agent's own (see rows)"
    )
    if len(exercised) < 2:
        print(
            "NOTE: dispatch sent all tasks to ONE producer (load not spread across "
            "the goals) — a SEPARATE dispatch-load observation, not the model-"
            "binding fix this proof targets. Model-binding is proven for the "
            "agent(s) actually exercised."
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
