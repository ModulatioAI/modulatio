"""Phase 1 evidence-run driver — A/B goal-state compression on vs. off.

beta-readiness: validate the measurement method on the one
dimension the harness can vary today (compression), live, against
the local LM Studio 122b, on the fixed long-horizon workload suite.

Design (all decisions grounded in the + project_execution code):

  - **Isolated vault.** Sets ``MODULATIO_VAULT_ROOT`` + ``vault.VAULT_ROOT``
    to ``<this dir>/.vault`` so the run never touches the user's real
    Obsidian vault.
  - **Local model via the router.** All roles point at the
    ``lmstudio_qwen3_5_122b`` preset (registered idempotently here):
    ``openai/qwen_qwen3.5-122b-a10b`` @ ``http://localhost:1234/v1``,
    ``auth_type=none``. LiteLLM routes; LM Studio runs it on the P40s.
  - **CPU-pinned client.** ``CUDA_VISIBLE_DEVICES=""`` so the Modulatio
    Python process can't grab a GPU and disturb LM Studio's hot model.
    Inference still happens in LM Studio on the GPUs.
  - **Dummy key.** LM Studio ignores the value, but LiteLLM's OpenAI
    provider requires *some* credential, so ``OPENAI_API_KEY`` is set to
    a placeholder. Cloud presets override this with their own keys, so
    it only affects the keyless local calls.
  - **Pinned plan per workload.** The plan body is fixed (identical
    across both arms and all replicates); only ``compression_enabled``
    flips. The replicates average over the model's run-to-run noise.
  - **Real orchestration.** The factory returns ``kickoff_callable=None``
    so ``start_execution`` builds the real Orchestrator path (producers
    draft, QC verifies, leader-reflect compresses) — not a stub.

Usage:

    # Validate the live wiring cheaply first (1 workload, N=1):
    .venv/bin/python scripts/eval/compression-evidence/run_evidence.py --validate --fresh

    # Full Phase 1 run (all 3 workloads, N=3/arm) — long; background it:
    .venv/bin/python scripts/eval/compression-evidence/run_evidence.py --fresh

Each run materializes per-workload experiment artifacts under
``.vault/experiments/<experiment_id>/`` (config, audit, per-replicate
manifests + metrics, arm aggregates, report.json + report.md).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

# ── Environment: set BEFORE importing modulatio. CPU-pin the client
#    (the local ONNX embedder runs CPU-only; cloud agent calls don't
#    touch the GPU), and isolate the vault from the user's real
#    Obsidian projects. ─────────────────────────────────────────────
_BASE = Path(__file__).resolve().parent
_VAULT_ROOT = _BASE / ".vault"
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ["MODULATIO_VAULT_ROOT"] = str(_VAULT_ROOT)
# Keyless local producer (LM Studio, auth_type=none) goes through LiteLLM's
# OpenAI provider, which requires *some* credential. Placeholder only — the
# cloud presets pass their own api_key per call, overriding this env value.
os.environ.setdefault("OPENAI_API_KEY", "lm-studio-local-placeholder")

# The OpenRouter key (for the Claude-Haiku QC preset) isn't in the shell
# env; source it from a local agent's .env. The value is never printed.
if not os.environ.get("OPENROUTER_API_KEY"):
    _local_env = Path.home() / ".local" / ".env"
    if _local_env.exists():
        for _line in _local_env.read_text().splitlines():
            _m = re.match(
                r"\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.+?)\s*$", _line
            )
            if _m:
                os.environ["OPENROUTER_API_KEY"] = (
                    _m.group(1).strip().strip('"').strip("'")
                )
                break

sys.path.insert(0, str(_BASE))

from modulatio import model_presets, plans, roster, vault  # noqa: E402
from modulatio.ab_harness import ABConfig, run_experiment  # noqa: E402
from modulatio.runners import litellm_runner  # noqa: E402
from modulatio.types import Project, ProjectState  # noqa: E402

import workloads  # noqa: E402  (sibling module on sys.path)


# Per-role model assignment — DELIBERATELY DIVERSE roster (2026-05-20) to
# stress-test how the compression fix holds up across heterogeneous models
# and providers, not just the DeepSeek/GLM/Haiku set it was tuned against.
#   - Leader  : ChatGPT 5.5 via OpenRouter — forced-tool-call emit_state.
#   - Producer: local qwen3.5-122b in LM Studio. NB: qwen3.5 is known to
#     spiral in multi-turn tool loops (confirmed in QC/researcher before),
#     so as a producer it stress-tests the system against a flaky local
#     model — expect churn/retries; that's the point of a broad test.
#   - QC      : Kimi K2.6 (ollama cloud) — different mind from the producer
#     so QC catches blind spots rather than sharing them.
ROLE_MODELS = {
    "leader": "openrouter_gpt_5_5",
    "coordinator": "openrouter_gpt_5_5",
    "drafter": "lmstudio_qwen3_5_122b",         # producer (thinking-OFF via LM Studio toggle)
    "specialist": "lmstudio_qwen3_5_122b",
    "researcher": "lmstudio_qwen3_5_122b",
    "qc": "ollama_kimi_k2_6",                  # independent verification
}
ROLES = tuple(ROLE_MODELS)
# Generous single-call timeout — harmless for fast cloud models.
CALL_TIMEOUT = 1800.0


def ensure_presets() -> None:
    """Register the eval presets if absent (GLM + Haiku presets already
    exist in the user config). Idempotent."""
    presets = model_presets.load_presets()
    if "ollama_deepseek_v4_pro" not in presets:
        model_presets.add_preset(
            "ollama_deepseek_v4_pro",
            label="DeepSeek V4 Pro (ollama cloud)",
            base_url="https://ollama.com/v1",
            api_format="openai",
            auth_type="api_key",
            model="deepseek-v4-pro",
            auth_config={"env_var": "OLLAMA_API_KEY"},
        )
    if "xai_grok_4_3" not in presets:
        model_presets.add_preset(
            "xai_grok_4_3",
            label="xAI Grok 4.3",
            base_url="https://api.x.ai/v1",
            api_format="openai",
            auth_type="api_key",
            model="grok-4.3",
            auth_config={"env_var": "XAI_API_KEY"},
        )
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
    if "lmstudio_qwen3_6_35b" not in presets:
        model_presets.add_preset(
            "lmstudio_qwen3_6_35b",
            label="LM Studio — Qwen3.6 35B-A3B (local, P40s)",
            base_url="http://localhost:1234/v1",
            api_format="openai",
            auth_type="none",
            model="qwen_qwen3.6-35b-a3b",
            auth_config={},
        )
    if "lmstudio_gemma_4_31b" not in presets:
        model_presets.add_preset(
            "lmstudio_gemma_4_31b",
            label="LM Studio — Gemma 4 31B dense (local, P40s, 32K ctx)",
            base_url="http://localhost:1234/v1",
            api_format="openai",
            auth_type="none",
            model="google/gemma-4-31b",
            auth_config={},
        )
    if "openrouter_nemotron_3_super" not in presets:
        model_presets.add_preset(
            "openrouter_nemotron_3_super",
            label="Nemotron 3 Super 120B-A12B (OpenRouter, sparse MoE)",
            base_url="https://openrouter.ai/api/v1",
            api_format="openai",
            auth_type="api_key",
            model="nvidia/nemotron-3-super-120b-a12b",
            auth_config={"env_var": "OPENROUTER_API_KEY"},
        )


def build_role_runners() -> dict:
    """One runner per role, each pointed at that role's preset."""
    return {
        role: litellm_runner(model, timeout=CALL_TIMEOUT)
        for role, model in ROLE_MODELS.items()
    }


def _stream(msg: str) -> None:
    """Print a progress line, flushed immediately so a backgrounded run
    streams in real time (stdout is block-buffered off a TTY)."""
    print(msg, flush=True)


# ── Eval-only thinking-OFF chat runner ───────────────────────────────────
# Production gap (logged: project_modulatio_reasoning_control_preset_gap): the
# preset/runner layer can't pass provider reasoning-control params, so a
# reasoning-toggle model can't be forced thinking-OFF through the normal path.
# For the sparse-AGENTIC isolation test we need Nemotron-3-super in thinking-
# OFF mode; OpenRouter honors extra_body={"reasoning":{"enabled":False}}
# cleanly (verified — reasoning NONE, tool-calling intact). This thin runner
# mirrors modulatio.runners.litellm_chat_runner.run but injects that body param.
THINKING_OFF_MODELS = {"openrouter_nemotron_3_super"}
_REASONING_OFF_BODY = {"reasoning": {"enabled": False}}


def build_reasoning_off_chat_runner(model_key: str):
    """Chat runner that forces reasoning OFF via OpenRouter's extra_body.
    Returns the same (content, tool_calls) ChatResponse shape the
    Orchestrator expects from a normal chat_runner."""
    import json as _json
    from litellm import completion
    from modulatio.runners import (
        _resolve_model_call_args, _record_call_usage, ChatResponse, ToolCall,
    )
    litellm_model, resolved = _resolve_model_call_args(model_key)
    base_kwargs = {"timeout": CALL_TIMEOUT, **resolved}

    def run(*, messages, tools, tool_choice=None):
        call_kwargs = dict(base_kwargs)
        if tools:
            call_kwargs["tools"] = tools
        if tool_choice is not None:
            call_kwargs["tool_choice"] = tool_choice
        call_kwargs["extra_body"] = _REASONING_OFF_BODY
        resp = completion(model=litellm_model, messages=messages, **call_kwargs)
        try:
            _record_call_usage(resp, litellm_model)
        except Exception:
            pass
        msg = resp.choices[0].message
        parsed = []
        for c in (getattr(msg, "tool_calls", None) or []):
            try:
                fn = c.function if hasattr(c, "function") else c["function"]
                name = fn.name if hasattr(fn, "name") else fn["name"]
                args_raw = (fn.arguments if hasattr(fn, "arguments")
                            else fn.get("arguments", "{}"))
                args = (_json.loads(args_raw) if isinstance(args_raw, str)
                        else dict(args_raw))
                cid = c.id if hasattr(c, "id") else c["id"]
            except Exception as exc:
                parsed.append(ToolCall(id=getattr(c, "id", "malformed"),
                                       name="__malformed__",
                                       args={"error": f"{type(exc).__name__}: {exc}"}))
                continue
            parsed.append(ToolCall(id=cid, name=name, args=args))
        return ChatResponse(content=getattr(msg, "content", None) or "",
                            tool_calls=tuple(parsed))

    return run


def make_streaming_kickoff(project: Project, runners: dict):
    """Real Orchestrator kickoff (mirrors project_execution._make_default_kickoff)
    with an ``activity_callback`` wired so each GSD phase emits a progress
    line. Returns a per-sub-objective callable for start_execution."""
    from modulatio import roster as _roster, tools as _tools, vault as _vault
    from modulatio.orchestration import Orchestrator
    from modulatio.runners import litellm_runner as _lr, maybe_build_chat_runner

    def _activity(ev) -> None:
        tid = f" {ev.task_id}" if getattr(ev, "task_id", None) else ""
        _stream(f"      · [{ev.role}] {ev.phase}{tid}")

    def _do(sub_objective_text: str, _so) -> object:
        chat_runners: dict = {}
        chat_runner_models: dict = {}
        for agent in _roster.list_agents(project.code):
            if agent.model in THINKING_OFF_MODELS:
                r = build_reasoning_off_chat_runner(agent.model)
            else:
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


def pinned_plan(workload: "workloads.Workload", project: Project):
    """Persist + approve the workload's fixed plan for this run's project."""
    saved = plans.persist(
        workload.plan_body,
        project_code=project.code,
        agent_id="leader",
        source_message=f"eval pinned plan {workload.code}",
    )
    plans.mark_approved(saved.id, project.code, decided_by="user")
    return plans.load(saved.id, project.code)


def make_factory(workload: "workloads.Workload"):
    """Build the per-replicate factory for one workload. Fresh run_id is
    the isolation boundary; compression flips per arm via effective_config."""
    def factory(arm, replicate_index, effective_config):
        run_id = vault.generate_run_id()
        vault.init_run(workload.code, run_id, workload.objective)
        compression_on = bool(effective_config.get("compression_enabled", True))
        project = Project(
            code=workload.code,
            name=workload.name,
            objective=workload.objective,
            state=ProjectState.ACTIVE,
            wiki_path=str(vault.project_dir(workload.code)),
            run_id=run_id,
            leader_model=ROLE_MODELS["leader"],
            agent_models=dict(ROLE_MODELS),
            compression_enabled=compression_on,
        )
        plan = pinned_plan(workload, project)
        runners = build_role_runners()
        reflect_runner = runners["leader"]
        _stream(
            f"  → replicate {arm}{replicate_index} "
            f"(compression {'ON' if compression_on else 'OFF'}, run={run_id})"
        )
        # Real Orchestrator path with streaming activity callback;
        # usage_log_path None → token/cost metrics zero out (fine on
        # local for Phase 1).
        kickoff = make_streaming_kickoff(project, runners)
        return (project, runners, kickoff, plan, reflect_runner, None)
    return factory


def seed_explicit_roster(code: str) -> None:
    """Seed a controlled roster directly, bypassing the wizard
    team_template (which would otherwise override per-role models).

    Producer-collapse: leader / producer / qc only — NO separate
    researcher AGENT (the research-first identity never enters the team).
    But the producer HOLDS the researcher skill as a *capability*: a
    producer produces research when a task needs it, as a producer. This
    is the "skills are capabilities, not identities" principle — the
    producer's identity is producer; research is just one thing it can
    do. Holding the skill means research-flavored tasks route to the
    producer instead of gapping / mis-dispatching to qc.
    """
    specs = [
        ("leader",
         ("leader", "leader-verify", "leader-plan",
          "leader-plan-approve", "leader-reflect"),
         "leader", ROLE_MODELS["leader"]),
        ("producer", ("drafter", "drafter-edit", "researcher"),
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
            template_origin="eval-explicit",
        )
        roster.save(agent, code)


def run_workload(workload, *, output_root: Path, replicates: int):
    """Seed the workload's roster + run the compression A/B for it."""
    vault.init_project(workload.code, workload.name, workload.objective,
                       exist_ok=True)
    seed_explicit_roster(workload.code)
    cfg = ABConfig(
        base_project_config={
            "code": workload.code,
            "leader_model": ROLE_MODELS["leader"],
        },
        varied_dimension="compression",
        arm_a_value=True,    # compression ON  (structured)
        arm_b_value=False,   # compression OFF (free-markdown)
        replicates=replicates,
        mode="live",
    )
    return run_experiment(
        cfg,
        output_root=output_root,
        allow_paid_live=True,
        replicate_factory=make_factory(workload),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Compression A/B evidence run")
    ap.add_argument("--validate", action="store_true",
                    help="cheap wiring check: RESEARCH workload only, N=1/arm")
    ap.add_argument("--replicates", type=int, default=3,
                    help="replicates per arm for the full run (default 3)")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe the eval vault before running")
    ap.add_argument("--reasoning-on", action="store_true",
                    help="run the Nemotron producer with reasoning ON "
                         "(the one-variable control for the agentic test: "
                         "same model, only the reasoning toggle flipped)")
    args = ap.parse_args()

    # --reasoning-on: stop injecting OpenRouter reasoning:{enabled:false} for
    # the Nemotron producer, so it falls back to the normal chat runner (which
    # leaves reasoning ON, Nemotron's default). Mutates the shared set in place
    # so make_streaming_kickoff's call-time check sees it.
    if args.reasoning_on:
        THINKING_OFF_MODELS.discard("openrouter_nemotron_3_super")

    if args.fresh and _VAULT_ROOT.exists():
        resolved = _VAULT_ROOT.resolve()
        if not resolved.is_relative_to(_BASE):
            raise RuntimeError(f"refusing to rmtree {resolved} (escapes {_BASE})")
        shutil.rmtree(resolved)
    _VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    vault.VAULT_ROOT = _VAULT_ROOT  # belt-and-suspenders alongside the env var

    ensure_presets()
    output_root = _VAULT_ROOT / "experiments"

    if args.validate:
        suite = (workloads.DOC,)   # lightest construction workload
        replicates = 1
    else:
        suite = workloads.SUITE
        replicates = args.replicates

    print(f"Vault:       {_VAULT_ROOT}")
    print(f"Output:      {output_root}")
    print("Models:      leader/coord=openrouter_gpt_5_5, "
          "producer=lmstudio_qwen3_5_122b [thinking-OFF via LM Studio toggle], "
          "qc=ollama_kimi_k2_6")
    print(f"Workloads:   {[w.code for w in suite]}  @ N={replicates}/arm")
    print()

    failures = []
    for w in suite:
        print(f"=== {w.code} — {w.name}  (stresses: {', '.join(w.stresses)}) ===")
        try:
            report = run_workload(w, output_root=output_root, replicates=replicates)
        except Exception as exc:  # noqa: BLE001 — one workload failing shouldn't sink the rest
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            failures.append((w.code, f"{type(exc).__name__}: {exc}"))
            continue
        a, b = report.arm_a, report.arm_b
        print(f"  experiment_id: {report.experiment_id}")
        print(f"  arm A (compression ON):  {a.n_successful}/{a.n_attempted}"
              f"  underpowered={a.underpowered}")
        print(f"  arm B (compression OFF): {b.n_successful}/{b.n_attempted}"
              f"  underpowered={b.underpowered}")
        print()

    print("=== summary ===")
    if failures:
        for code, err in failures:
            print(f"  {code}: {err}")
        return 1
    print("  all workloads completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
