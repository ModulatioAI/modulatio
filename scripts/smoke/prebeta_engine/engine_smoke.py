"""Pre-beta engine regression smoke — one end-to-end run on the clean lineup.

Goal: confirm the pre-beta e2e-debug changes did NOT regress the engine on a
real, heterogeneous model lineup. The at-risk changes this exercises:

  - per-task artifact staging + deterministic main-thread merge (Blocker 2);
  - the planning terminology sweep across the seed prompts (leader,
    leader-verify, qc, task-plan, consolidation, coding-diff, researcher);
  - the QC-as-fixer / reasoning-control wiring.

Lineup (exactly as requested):

  - Leader  : ChatGPT 5.5            (openrouter_gpt_5_5)
  - Producer: Nemotron 3 Super 120B  (openrouter_nemotron_3_super) — reasoning
              OFF via the preset's ``default_params`` (the divert's
              reasoning-control path through ``_resolve_model_call_args`` —
              NOT the eval-only chat-runner hack), so it applies on BOTH the
              single-shot and tool-loop paths.
  - QC      : Kimi K2.6              (ollama_kimi_k2_6)

Runs the WD1 "Beacon operator's guide" workload (5 sub-objectives, cross-unit
continuity + a consolidation appendix) through the REAL Orchestrator. Default
(sequential) path is what beta ships; pass ``--concurrent`` to additionally
exercise the new staging+merge under ``MODULATIO_CONCURRENT_WAVES``.

Each GSD phase streams a progress line (stdout flushed) so a backgrounded run
is watchable in real time.

Usage:
    .venv/bin/python scripts/smoke/prebeta_engine/engine_smoke.py
    .venv/bin/python scripts/smoke/prebeta_engine/engine_smoke.py --concurrent
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

# ── Environment: set BEFORE importing modulatio. Isolate the vault from the
#    user's real Obsidian projects; CPU-pin the client (all 3 models are
#    cloud, so the GPU is irrelevant — harmless belt-and-suspenders). ──────
_BASE = Path(__file__).resolve().parent
_VAULT_ROOT = _BASE / ".vault"
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ["MODULATIO_VAULT_ROOT"] = str(_VAULT_ROOT)

# OPENROUTER_API_KEY (gpt-5.5 + nemotron) may not be in the shell;
# OLLAMA_API_KEY (kimi) is typically already in the shell. Source the former
# from a local env file pointed at by MODULATIO_EVAL_ENV_FILE if absent.
# Values are never printed.
if not os.environ.get("OPENROUTER_API_KEY"):
    _ef = os.environ.get("MODULATIO_EVAL_ENV_FILE")
    _extra_env = Path(_ef) if _ef else None
    if _extra_env and _extra_env.exists():
        for _line in _extra_env.read_text().splitlines():
            _m = re.match(
                r"\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.+?)\s*$", _line
            )
            if _m:
                os.environ["OPENROUTER_API_KEY"] = (
                    _m.group(1).strip().strip('"').strip("'")
                )
                break

# Import the canonical WD1 workload (pinned plan + objective) from the eval
# suite rather than duplicating it, so the smoke can't drift from it.
_EVAL_DIR = _BASE.parents[1] / "eval" / "compression-evidence"
sys.path.insert(0, str(_EVAL_DIR))

from modulatio import model_presets, plans, roster, vault  # noqa: E402
from modulatio.project_execution import start_execution  # noqa: E402
from modulatio.runners import (  # noqa: E402
    litellm_runner, maybe_build_chat_runner,
)
from modulatio.types import Project, ProjectState  # noqa: E402

import workloads  # noqa: E402  (sibling on sys.path from the eval dir)


ROLE_MODELS = {
    "leader": "openrouter_gpt_5_5",
    "drafter": "openrouter_nemotron_3_super",     # producer — reasoning OFF
    "specialist": "openrouter_nemotron_3_super",
    "researcher": "openrouter_nemotron_3_super",
    "qc": "ollama_kimi_k2_6",                      # independent verification
}
CALL_TIMEOUT = 1800.0
WORKLOAD = workloads.DOC  # WD1 — Beacon operator's guide


def ensure_presets() -> None:
    """Register the three lineup presets if absent. Idempotent.

    Nemotron carries ``default_params`` forcing reasoning OFF via OpenRouter
    — applied through ``_resolve_model_call_args`` on every call path, so the
    producer is genuinely non-thinking without any eval-only shim.
    """
    presets = model_presets.load_presets()
    if "openrouter_gpt_5_5" not in presets:
        model_presets.add_preset(
            "openrouter_gpt_5_5",
            label="ChatGPT 5.5 (OpenRouter)",
            base_url="https://openrouter.ai/api/v1",
            api_format="openai",
            auth_type="api_key",
            model="openai/gpt-5.5",
            auth_config={"env_var": "OPENROUTER_API_KEY"},
        )
    if "openrouter_gpt_5_4" not in presets:
        model_presets.add_preset(
            "openrouter_gpt_5_4",
            label="ChatGPT 5.4 (OpenRouter) — higher rate-limit headroom",
            base_url="https://openrouter.ai/api/v1",
            api_format="openai",
            auth_type="api_key",
            model="openai/gpt-5.4",
            auth_config={"env_var": "OPENROUTER_API_KEY"},
        )
    # Nemotron must carry reasoning-OFF default_params. A prior eval run may
    # have registered a BARE version (no default_params) — so add it fresh if
    # absent, else update the existing entry to guarantee reasoning is OFF.
    _nemotron_off = {"extra_body": {"reasoning": {"enabled": False}}}
    if "openrouter_nemotron_3_super" not in presets:
        model_presets.add_preset(
            "openrouter_nemotron_3_super",
            label="Nemotron 3 Super 120B-A12B (OpenRouter, reasoning OFF)",
            base_url="https://openrouter.ai/api/v1",
            api_format="openai",
            auth_type="api_key",
            model="nvidia/nemotron-3-super-120b-a12b",
            auth_config={"env_var": "OPENROUTER_API_KEY"},
            default_params=_nemotron_off,
        )
    else:
        model_presets.update_preset(
            "openrouter_nemotron_3_super", default_params=_nemotron_off,
        )
    if "ollama_kimi_k2_6" not in presets:
        model_presets.add_preset(
            "ollama_kimi_k2_6",
            label="Kimi K2.6 (ollama cloud)",
            base_url="https://ollama.com/v1",
            api_format="openai",
            auth_type="api_key",
            model="kimi-k2.6",
            auth_config={"env_var": "OLLAMA_API_KEY"},
        )


def _stream(msg: str) -> None:
    print(msg, flush=True)


def seed_roster(code: str) -> None:
    """Producer-collapse roster: leader / producer / qc. The producer HOLDS
    the researcher skill as a capability (not a separate identity), so
    research-flavored tasks route to it as a producer."""
    specs = [
        ("leader",
         ("leader", "leader-verify", "leader-plan",
          "leader-plan-approve", "leader-reflect"),
         "leader", ROLE_MODELS["leader"]),
        ("producer",
         ("drafter", "drafter-edit", "researcher",
          "continuity-check", "consolidation"),
         "producer", ROLE_MODELS["drafter"]),
        ("qc", ("qc",), "qc", ROLE_MODELS["qc"]),
    ]
    for aid, skills, tier, model in specs:
        caps, model_tier, cost_class = roster._derive_agent_caps(
            list(skills), code,
        )
        agent = roster.Agent(
            id=aid, name=aid, identity=roster._DEFAULT_IDENTITY,
            skills=list(skills), model=model, model_tier=model_tier,
            cost_class=cost_class, capability_tags=caps, tier=tier,
            template_origin="smoke-explicit",
        )
        roster.save(agent, code)


def make_streaming_kickoff(project: Project, runners: dict):
    """Real Orchestrator kickoff with an ``activity_callback`` that streams
    each GSD phase. Mirrors project_execution._make_default_kickoff."""
    from modulatio import roster as _roster, tools as _tools, vault as _vault
    from modulatio.orchestration import Orchestrator
    from modulatio.runners import litellm_runner as _lr

    def _activity(ev) -> None:
        tid = f" {ev.task_id}" if getattr(ev, "task_id", None) else ""
        _stream(f"      · [{ev.role}] {ev.phase}{tid}")

    def _do(sub_objective_text: str, _so) -> object:
        chat_runners: dict = {}
        chat_runner_models: dict = {}
        for agent in _roster.list_agents(project.code):
            r = maybe_build_chat_runner(agent.model)
            if r is not None:
                chat_runners[agent.id] = r
                chat_runner_models[agent.id] = agent.model
        run_dir = _vault.run_dir(project.code, project.run_id)
        tool_registry = _tools.build_registry(
            artifacts_root=run_dir / "artifacts",
            tool_calls_dir=run_dir / "tool_calls",
        )
        orch = Orchestrator(
            project, runners,
            tool_registry=tool_registry,
            chat_runners=chat_runners,
            chat_runner_models=chat_runner_models,
            summarizer_chat_runner_factory=_lr,
            activity_callback=_activity,
        )
        return orch.kickoff(sub_objective_text)

    return _do


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-beta engine regression smoke")
    ap.add_argument("--concurrent", action="store_true",
                    help="run with MODULATIO_CONCURRENT_WAVES=1 to exercise the "
                         "new per-task staging + deterministic merge path")
    ap.add_argument("--keep", action="store_true",
                    help="keep an existing .vault (default wipes for a clean run)")
    ap.add_argument("--leader-model", default="openrouter_gpt_5_5",
                    help="preset key for the Leader role (default gpt-5.5; "
                         "use openrouter_gpt_5_4 for more rate-limit headroom)")
    ap.add_argument("--force-qc-reject", action="store_true",
                    help="force every QC verdict to reject so producers exhaust "
                         "retries and QC-as-fixer fires (live test of the fixer)")
    args = ap.parse_args()

    # Opt-in gate. This is a LIVE, real-model, multi-objective orchestration
    # that legitimately runs for many minutes (I/O-bound on cloud models, so
    # it sits at ~0% CPU and looks hung). It must NOT run inside a blind
    # `find scripts/smoke -name '*.py' | python` loop — doing so wedges the
    # whole sweep (it stalled two separate reviewer sweeps for 40-77 min
    # before being killed). Require an explicit opt-in so a glob skips it
    # instantly with a clean exit 0; a human who wants it runs it on purpose.
    if os.environ.get("MODULATIO_ENGINE_SMOKE") != "1":
        print(
            "SKIP: live engine smoke — intentionally excluded from blind smoke "
            "globs.\n"
            "      It runs a full multi-objective workload on REAL cloud models "
            "(minutes to hours).\n"
            "      To run it: MODULATIO_ENGINE_SMOKE=1 .venv/bin/python "
            "scripts/smoke/prebeta_engine/engine_smoke.py\n"
            "      (needs OPENROUTER_API_KEY + OLLAMA_API_KEY in the env)."
        )
        return 0

    # Leader override (e.g. gpt-5.4 for rate-limit headroom). Mutate the
    # shared map BEFORE roster/Project/runners are built off it.
    ROLE_MODELS["leader"] = args.leader_model

    if args.concurrent:
        os.environ["MODULATIO_CONCURRENT_WAVES"] = "1"

    if not args.keep and _VAULT_ROOT.exists():
        resolved = _VAULT_ROOT.resolve()
        if not resolved.is_relative_to(_BASE):
            raise RuntimeError(f"refusing to rmtree {resolved} (escapes {_BASE})")
        shutil.rmtree(resolved)
    _VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    vault.VAULT_ROOT = _VAULT_ROOT

    # Fail fast + clearly if a credential is missing (no values printed).
    missing = [k for k in ("OPENROUTER_API_KEY", "OLLAMA_API_KEY")
               if not os.environ.get(k)]
    if missing:
        print(f"MISSING CREDENTIALS: {', '.join(missing)} — cannot run.")
        return 2

    ensure_presets()

    code = WORKLOAD.code
    print("=== Pre-beta engine regression smoke ===")
    print(f"Vault:     {_VAULT_ROOT}")
    print(f"Lineup:    leader={ROLE_MODELS['leader']}  "
          "producer=openrouter_nemotron_3_super [reasoning OFF]  "
          "qc=ollama_kimi_k2_6")
    print(f"Workload:  {code} — {WORKLOAD.name} "
          f"({len(plans.extract_sub_objectives(WORKLOAD.plan_body))} sub-objectives)")
    print(f"Path:      {'CONCURRENT (staging+merge)' if args.concurrent else 'sequential (default / beta-shipping)'}")
    print()

    run_id = vault.generate_run_id()
    vault.init_project(code, WORKLOAD.name, WORKLOAD.objective, exist_ok=True)
    vault.init_run(code, run_id, WORKLOAD.objective)
    seed_roster(code)

    project = Project(
        code=code,
        name=WORKLOAD.name,
        objective=WORKLOAD.objective,
        state=ProjectState.ACTIVE,
        wiki_path=str(vault.project_dir(code)),
        run_id=run_id,
        leader_model=ROLE_MODELS["leader"],
        agent_models=dict(ROLE_MODELS),
    )

    saved = plans.persist(
        WORKLOAD.plan_body, project_code=code, agent_id="leader",
        source_message=f"smoke pinned plan {code}",
    )
    plans.mark_approved(saved.id, code, decided_by="user")

    runners = {role: litellm_runner(model, timeout=CALL_TIMEOUT)
               for role, model in ROLE_MODELS.items()}

    # --force-qc-reject: deterministically exercise QC-as-fixer live. The qc
    # role-runner serves BOTH the verdict call and the qc-patch call; force
    # every VERDICT to reject (→ producer exhausts retries → fixer fires) but
    # let the PATCH call through to the real QC model (identified by the
    # "LAST-RESORT rescue" marker in _QC_PATCH_PROMPT) so QC authors a genuine
    # fix. Watch for the `qc_authored_fix` event + the task completing.
    if args.force_qc_reject:
        _real_qc = runners["qc"]

        def _force_reject_qc(prompt: str) -> str:
            if "LAST-RESORT rescue" in prompt:        # the qc-patch prompt
                return _real_qc(prompt)
            return ('```json\n'
                    '{"check": "forced reject (QC-fixer live-test)", '
                    '"passed": false, "defect_type": "substantive"}\n```')

        runners["qc"] = _force_reject_qc
        print("FORCE-QC-REJECT: every QC verdict fails → exercises QC-as-fixer\n")

    kickoff = make_streaming_kickoff(project, runners)

    _stream(f"  → executing plan {saved.id} (run={run_id})")
    result = start_execution(
        saved.id, project,
        runners=runners,
        reflect_runner=runners["leader"],
        kickoff_callable=kickoff,
    )

    # ── Report ───────────────────────────────────────────────────────────
    from modulatio import store

    run_dir = vault.run_dir(code, run_id)
    artifacts = run_dir / "artifacts"
    print()
    print("=== result ===")
    print(f"  final_status:        {result.final_status}")
    print(f"  sub-objectives:      {result.sub_objectives_completed}/"
          f"{result.sub_objectives_total} completed")
    if result.error:
        print(f"  error:               {result.error}")
    if result.abort_summary:
        print(f"  abort_summary:       {result.abort_summary}")
    if result.paused_ticket_id:
        print(f"  paused_ticket_id:    {result.paused_ticket_id}")

    tasks = store.list_tasks(code, run_id=run_id)
    by_status: dict[str, int] = {}
    for t in tasks:
        by_status[t.status.value] = by_status.get(t.status.value, 0) + 1
    completed = by_status.get("completed", 0)
    print(f"  tasks:               {completed}/{len(tasks)} completed  "
          f"(breakdown: {by_status})")
    for t in tasks:
        if t.status.value != "completed":
            print(f"      ! {t.id} [{t.status.value}] {t.description[:70]}")

    draft_files = [
        p for p in sorted(artifacts.rglob("*"))
        if p.is_file() and "tool_calls" not in p.parts
    ] if artifacts.exists() else []
    print(f"  artifacts:           {len(draft_files)} files under {artifacts}")
    for p in draft_files:
        print(f"      - {p.relative_to(artifacts)}  ({p.stat().st_size} B)")

    leftover_staging = list(run_dir.glob(".staging/*")) if run_dir.exists() else []
    if leftover_staging:
        print(f"  ⚠ STAGING NOT TORN DOWN: {[str(s) for s in leftover_staging]}")

    ok = (
        result.final_status == "done"
        and result.sub_objectives_completed == result.sub_objectives_total
        and len(tasks) > 0
        and completed == len(tasks)
        and not leftover_staging
    )
    print()
    print("  VERDICT:", "PASS — engine intact on the clean lineup" if ok
          else "REVIEW — see status/errors above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
