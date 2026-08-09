"""Tests for the Leader's converse function — the conversational endpoint.

`Orchestrator.converse(message)` is the Leader's conversational function (the
same Leader who decomposes/verifies, talking to the operator). It reuses the
tool-loop, persists a per-project conversation thread, and falls back to a
plain acknowledgement offline (no leader chat runner).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import skills, vault
from modulatio.orchestration import Orchestrator
from modulatio.runners import ChatResponse
from modulatio.types import Project, ProjectState


PROJECT_CODE = "CNV"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "converse fixture", "obj")
    return Project(
        code=PROJECT_CODE, name="converse fixture", objective="obj",
        state=ProjectState.ACTIVE, leader_model="stub",
        wiki_path=str(vault.project_dir(PROJECT_CODE)),
    )


def _runners() -> dict:
    return {
        "leader": lambda p: "", "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "", "qc": lambda p: "",
    }


def test_converse_offline_acknowledges_and_persists(project: Project):
    """With no leader chat runner (stub/offline), converse returns a plain
    acknowledgement and persists the thread — never crashes, never silent."""
    orch = Orchestrator(project, _runners())
    reply = orch.converse("hey Leader, can we talk without running a job?")
    assert reply.strip()
    assert "offline" in reply.lower()

    thread = orch._load_conversation()
    assert [t["role"] for t in thread] == ["operator", "leader"]
    assert orch._conversation_path().exists()


def test_reset_conversation_archives_thread_for_a_fresh_start(project: Project):
    """`/new`: reset_conversation renames the thread aside (never deletes) so the
    next turn starts clean, and returns None when there's nothing to archive."""
    orch = Orchestrator(project, _runners())
    assert orch.reset_conversation() is None  # no thread yet

    orch.converse("remember this")
    assert orch._conversation_path().exists()

    archived = orch.reset_conversation()
    assert archived is not None and archived.exists()  # history preserved aside
    assert not orch._conversation_path().exists()      # live thread cleared
    assert orch._load_conversation() == []             # next turn starts fresh


def test_converse_thread_accumulates_across_turns(project: Project):
    orch = Orchestrator(project, _runners())
    orch.converse("first message")
    orch.converse("second message")
    thread = orch._load_conversation()
    assert [t["role"] for t in thread] == [
        "operator", "leader", "operator", "leader",
    ]
    assert thread[0]["content"] == "first message"
    assert thread[2]["content"] == "second message"


def test_converse_runs_the_tool_loop_with_a_chat_runner(project: Project):
    """With a leader chat runner wired, converse runs the real tool-loop:
    the prompt carries the conversational persona + the operator's message,
    and the runner's reply comes back."""
    captured: dict = {}

    def mock_leader(*, messages, tools, tool_choice=None):
        captured["prompt"] = messages[0]["content"]
        return ChatResponse(
            content="Sure — what are we building? (mock leader)", tool_calls=(),
        )

    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": mock_leader},
        chat_runner_models={"leader": "mock-model"},
    )
    reply = orch.converse("let's design a skill — no job yet")
    assert "mock leader" in reply
    # the persona + the operator message reached the model
    assert "Leader of this Modulatio project" in captured["prompt"]
    assert "design a skill" in captured["prompt"]
    # never says "I only run jobs" — that's the whole point
    assert "only run jobs" not in reply.lower()


def test_converse_grants_harness_to_clay_seat(project: Project, monkeypatch):
    """B5 (converse): the converse path grants the HARNESS roots as seat
    extra-grants so a Clay leader's NATIVE file tools see what the litellm
    leader's rebound builtins see — the vault (covering every run's
    deliverables) is home. A litellm leader ignores the hint. The grant is
    restored after the loop."""
    project.run_id = "run-1"
    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": lambda **k: ChatResponse(content="ok", tool_calls=())},
        chat_runner_models={"leader": "mock-model"},
    )
    seen: dict = {}

    def _fake_loop(**kwargs):
        seen["grants"] = getattr(orch._tls, "seat_extra_grants", None)
        return "ok"

    monkeypatch.setattr(orch, "_run_chat_loop", _fake_loop)
    orch.converse("what did the team produce?")

    grants = seen.get("grants") or ()
    assert str(vault.project_dir(PROJECT_CODE)) in grants, (
        "converse must grant the PROJECT dir so Clay's tools reach the runs"
    )
    # Never the VAULT ROOT — Clay's native
    # tools include a shell with no dotfile floor, and the vault root holds
    # the `.env` secret store. The project dir covers every run without it.
    assert str(vault.VAULT_ROOT) not in grants
    assert getattr(orch._tls, "seat_extra_grants", None) is None, "grant must restore"


def test_converse_prompt_carries_operator_context(project: Project):
    """The {operator_context} slot renders — the partnership framing the
    Leader reasons within (present vs autonomous)."""
    present = Orchestrator(project, _runners(), operator_present=True)
    prompt = present._build_converse_prompt([], "hi")
    assert "COLLABORATING" in prompt


def test_converse_prompt_carries_the_constitution(project: Project):
    """The {constitution} slot renders the Leader's values (the seed ships a
    default) into the conversational prompt."""
    orch = Orchestrator(project, _runners())
    block = orch._constitution_block()
    assert block  # the seed default is always present
    assert block in orch._build_converse_prompt([], "hi")


def test_converse_inlines_a_document(project: Project, tmp_path: Path):
    """A document attachment is inlined into the converse prompt so the Leader
    can read it (the text path keeps full tool-use)."""
    from modulatio.attachments import build_attachment

    doc = tmp_path / "notes.md"
    doc.write_text("SECRET DOC CONTENT", encoding="utf-8")
    att = build_attachment(doc, kind="document")
    orch = Orchestrator(project, _runners())
    prompt = orch._build_converse_prompt([], "read this", [att])
    assert "SECRET DOC CONTENT" in prompt


def test_converse_with_image_routes_to_multimodal(
    project: Project, tmp_path: Path, monkeypatch
):
    """An image attachment routes the turn through the multimodal path
    (carrying the image), not the text tool-loop."""
    from modulatio.attachments import build_attachment

    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    att = build_attachment(img, kind="image")

    captured: dict = {}

    def fake_multimodal(*, prompt, attachments, chat_completion, budget_role="leader-decompose"):
        captured["attachments"] = attachments
        captured["budget_role"] = budget_role
        return "I can see the image."

    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": lambda **k: ChatResponse(
            content="unused", tool_calls=())},
    )
    monkeypatch.setattr(orch, "_run_multimodal_leader", fake_multimodal)
    reply = orch.converse("what's in this?", attachments=[att])
    assert reply == "I can see the image."
    # #68: a vision converse turn bills against leader-chat, not leader-decompose.
    assert captured["budget_role"] == "leader-chat"
    assert captured["attachments"][0].kind == "image"


def test_leader_has_the_authoring_tools(project: Project):
    """The leader-converse seed promises create_job_template / create_skill /
    improve_skill (and §4's team_status / read_deliverable) — they must actually
    be in the converse loadout."""
    orch = Orchestrator(project, _runners())
    names = set(orch._leader_function_tools())
    assert {
        "create_job_template", "create_skill", "improve_skill",
        "team_status", "read_deliverable",
    } <= names


def test_create_job_template_tool_writes_and_lists(project: Project):
    """The Leader's create_job_template tool actually writes a template that
    then shows up in list_job_templates (the reported bug: it was promised but
    not wired)."""
    from modulatio import job_templates

    orch = Orchestrator(project, _runners())
    out = orch._leader_function_tools()["create_job_template"].call(
        name="daily-brief", description="A daily brief", interview="Ask the topic.",
        cardinality="one")
    assert "Created" in out
    assert "daily-brief" in job_templates.list_job_templates(PROJECT_CODE)
    # idempotency guard: a second create on the same name is reported, not raised
    again = orch._leader_function_tools()["create_job_template"].call(
        name="daily-brief", description="dup", interview="x", cardinality="one")
    assert "already exists" in again


def test_leader_logs_tools_list_and_read(project: Project):
    """#6c: the Leader can list + read diagnostic logs (already redacted) so he can
    triage a crash/error when the operator asks — not blind to the LOGS tab."""
    from modulatio import logstore

    p = logstore.write_error_log("boom in the producer wave", context={"surface": "test"})
    orch = Orchestrator(project, _runners())
    tools_d = orch._leader_function_tools()
    assert "list_logs" in tools_d and "read_log" in tools_d
    listing = tools_d["list_logs"].call()
    assert p.stem in listing
    body = tools_d["read_log"].call(log_id=p.stem)
    assert "boom in the producer wave" in body


def test_create_job_template_captures_cardinality(project: Project):
    """The cardinality-bug fix: the Leader's create_job_template tool now carries
    the output cardinality into the JT, so a multi-unit job is codified as a
    FAN-OUT (fixed:N / per-item) instead of always defaulting to 'one' — which
    collapsed an 8-story anthology into a single task (HRWT 2026-06-05)."""
    from modulatio import job_templates

    orch = Orchestrator(project, _runners())
    tool = orch._leader_function_tools()["create_job_template"]

    out = tool.call(name="anthology", description="8-story book",
                    interview="Ask the stories.", cardinality="fixed:8",
                    artifact_kind="document")
    assert "fixed:8" in out
    jt = job_templates.load_with_metadata("anthology", project_code=PROJECT_CODE)
    assert jt.output_spec.cardinality == "fixed:8"
    assert jt.output_spec.artifact_kind == "document"

    # A bare count is normalized to the engine grammar ("8" -> "fixed:8").
    tool.call(name="bare-count", description="N pieces", interview="x",
              cardinality="8")
    assert job_templates.load_with_metadata(
        "bare-count", project_code=PROJECT_CODE).output_spec.cardinality == "fixed:8"

    # per-item:<param> splits the param out.
    tool.call(name="per-founder", description="one per founder", interview="x",
              cardinality="per-item:founders")
    jt3 = job_templates.load_with_metadata("per-founder", project_code=PROJECT_CODE)
    assert jt3.output_spec.cardinality == "per-item"
    assert jt3.output_spec.per == "founders"


def test_create_job_template_cardinality_validation(project: Project):
    """Cardinality is REQUIRED (no silent default-to-one), is
    normalized CASE-insensitively, and 'per-item' must name its 'per' param —
    otherwise the job has no enforceable output count (the bug class in disguise)."""
    from modulatio import job_templates

    orch = Orchestrator(project, _runners())
    tool = orch._leader_function_tools()["create_job_template"]

    # missing cardinality → rejected, not silently "one"
    assert "required" in tool.call(name="a", description="d", interview="i").lower()
    # per-item with no per binding → rejected
    out = tool.call(name="b", description="d", interview="i", cardinality="per-item")
    assert "per" in out.lower() and "couldn't" in out.lower()
    # case/space-tolerant normalization: "Fixed: 8" → fixed:8
    assert "Created" in tool.call(name="c", description="d", interview="i",
                                  cardinality="Fixed: 8")
    assert job_templates.load_with_metadata(
        "c", project_code=PROJECT_CODE).output_spec.cardinality == "fixed:8"
    # garbage cardinality → rejected
    assert "couldn't" in tool.call(name="e", description="d", interview="i",
                                   cardinality="lots").lower()


def test_pending_approvals_surface_and_decide(project: Project):
    """The Leader sees pending approvals in the prompt and resolves one via the
    decide_approval tool (the conversational-approval path)."""
    from uuid import uuid4

    from modulatio import store
    from modulatio.types import TicketPriority

    run_id = "20260602T000000Z-aaaa"
    vault.init_run(PROJECT_CODE, run_id, "scope")
    project.run_id = run_id
    t = store.create_ticket(
        project_id=uuid4(), project_code=PROJECT_CODE,
        priority=TicketPriority.CRITICAL, title="Approve the budget",
        body="continue?", approval_required=True, run_id=run_id,
    )
    orch = Orchestrator(project, _runners())

    # surfaced in the prompt
    block = orch._pending_approvals_block()
    assert t.id in block and "Approve the budget" in block
    assert "Pending approvals" in orch._build_converse_prompt([], "hi")

    # the Leader carries out the operator's decision via the tool
    tool = orch._leader_function_tools()["decide_approval"]
    out = tool.call(ticket_id=t.id, decision="approved", note="looks good")
    assert "approved" in out.lower()
    updated = store.get_ticket(PROJECT_CODE, t.id, run_id=run_id)
    assert updated is not None
    assert updated.approval_decision == "approved"
    assert updated.approval_decided_by == "operator"
    assert updated.approval_note == "looks good"

    # nothing pending now → the block disappears
    assert orch._pending_approvals_block() == ""


def test_leader_does_not_self_start_jobs(project: Project):
    """The Leader has NO ``run_job`` tool — a job is launched ONLY by the operator's
    ``/kickoff … /end`` brackets, never by the Leader self-starting from a chat turn
    (which made every conversational message spawn a job). He keeps his other
    functions (job-template management, etc.)."""
    offered: dict = {}

    def mock_leader(*, messages, tools, tool_choice=None):
        offered["names"] = [t["function"]["name"] for t in tools]
        return ChatResponse(content="will do", tool_calls=())

    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": mock_leader},
        chat_runner_models={"leader": "mock-model"},
    )
    orch.converse("run the weekly brief for me")
    assert "run_job" not in offered["names"]  # he cannot self-start a job
    assert "list_job_templates" in offered["names"]

    # the tool itself is gone from the Leader's function set
    lft = orch._leader_function_tools()
    assert "run_job" not in lft
    assert "list_job_templates" in lft


# --- Two-lane resolution, option 3 (2026-07-06): converse home = the harness ---
# The conversational Leader is Claude-Code-like over the modulatio harness:
# standing read AND write over the vault, the shared resources, and the config
# dir — no gate, no blocked subtrees. The swarm lanes (decompose/producers)
# keep their sandboxes; the tools-layer secret floor still hides dotfiles
# (.env) below every root.


def test_converse_leader_home_is_the_whole_harness(
    project: Project, tmp_path: Path, monkeypatch
):
    from modulatio import config

    shared = tmp_path / "shared-res"
    (shared / "skills").mkdir(parents=True)
    skill_md = shared / "skills" / "some-skill.md"
    skill_md.write_text("---\nname: some-skill\n---\nold body\n", encoding="utf-8")
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: shared)

    deliverable = vault.runs_dir(PROJECT_CODE) / "r1" / "artifacts" / "draft.md"
    deliverable.parent.mkdir(parents=True)
    deliverable.write_text("swarm draft", encoding="utf-8")

    orch = Orchestrator(project, _runners())
    reg = orch._leader_tool_registry()

    # EYES: standing read over the runs tree and the shared library — no gate.
    assert "swarm draft" in reg["read_file"].call(path=str(deliverable))
    assert "old body" in reg["read_file"].call(path=str(skill_md))
    # HANDS: standing write over the harness — the library AND the runs tree.
    reg["edit_file"].call(path=str(skill_md), old="old body", new="new body")
    assert "new body" in skill_md.read_text(encoding="utf-8")
    reg["edit_file"].call(path=str(deliverable), old="swarm draft", new="leader touch")
    assert "leader touch" in deliverable.read_text(encoding="utf-8")


def test_converse_leader_gate_has_no_blocked_subtrees(project: Project):
    """The BLOCK-1 runs/artifacts/delivery fence is retired for the Leader's
    gate: inside modulatio he doesn't need a widen at all, and a widen he does
    request (outside world) has no harness subtree to refuse."""
    orch = Orchestrator(project, _runners())
    assert orch.leader_gate()._blocked_subtrees == ()


def test_create_skill_tool_carries_the_full_contract(
    project: Project, tmp_path: Path, monkeypatch
):
    """The Leader can author a COMPLETE skill: tool_loadout (what the producer
    gets granted at checkout) and capability_tags (what routes it) reach the
    library — not silently swallowed (the incomplete-ocr-space bug)."""
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "lib")
    orch = Orchestrator(project, _runners())
    out = orch._leader_function_tools()["create_skill"].call(
        name="ocr-space", description="OCR via the OCR.space service",
        prompt="Call the service through api_call.",
        tool_loadout=["api_call"], capability_tags=["ocr"],
    )
    assert "Created" in out
    skill = skills.load_with_metadata("ocr-space")
    assert skill.tool_loadout == ("api_call",)
    assert skill.capability_tags == ("ocr",)


def test_improve_skill_tool_can_repair_frontmatter(
    project: Project, tmp_path: Path, monkeypatch
):
    """improve_skill can SET tool_loadout/capability_tags on an existing skill —
    so the Leader can repair a skill born bare, not only append prose."""
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "lib")
    orch = Orchestrator(project, _runners())
    tools_d = orch._leader_function_tools()
    tools_d["create_skill"].call(name="ocr-space", description="d", prompt="p")
    out = tools_d["improve_skill"].call(
        name="ocr-space", guidance="Name the tool in the loadout.",
        tool_loadout=["api_call"], capability_tags=["ocr"],
    )
    assert "v2" in out
    skill = skills.load_with_metadata("ocr-space")
    assert skill.tool_loadout == ("api_call",)
    assert skill.capability_tags == ("ocr",)
    assert "Name the tool in the loadout." in skill.prompt_template


def test_converse_prompt_states_the_harness_addresses(
    project: Project, tmp_path: Path, monkeypatch
):
    """Live-fire 2026-07-06: the Leader had the GRANT to edit the library but
    not its ADDRESS — file tools take absolute paths for anything beyond his
    workspace, and nothing told him where his home is on disk. The converse
    prompt now states the harness addresses as engine-rendered facts."""
    from modulatio import config

    shared = tmp_path / "shared-res"
    (shared / "skills").mkdir(parents=True)
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: shared)
    orch = Orchestrator(project, _runners())
    prompt = orch._build_converse_prompt([], "hi")
    assert str(shared / "skills") in prompt      # the library's real path
    assert str(vault.VAULT_ROOT) in prompt       # the vault's real path


def test_run_shell_roots_exclude_the_vault_secret_store(
    project: Project, tmp_path: Path, monkeypatch
):
    """The dotfile floor checks path ARGS and
    cwd COMPONENTS, but arbitrary code (python3 -c) reads `.env` BY NAME from
    inside a bound shell root — so the VAULT ROOT (the secret store's home)
    must never be a run_shell root. File tools keep the vault (their
    in-process floor holds); shell keeps the workspace
    (primary) + shared resources."""
    from modulatio import config
    from modulatio import tools as tools_mod

    shared = tmp_path / "shared-res"
    shared.mkdir()
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: shared)
    seen: dict = {}
    real = tools_mod.build_registry

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(tools_mod, "build_registry", spy)
    orch = Orchestrator(project, _runners())
    orch._leader_tool_registry()
    shell_roots = [str(r) for r in seen["run_shell_extra_roots"]]
    file_roots = [str(r) for r in seen["extra_roots"]]
    assert str(vault.VAULT_ROOT) not in shell_roots  # the BLOCK, engine-bound
    assert str(shared) in shell_roots                # shared keeps shell
    assert str(vault.VAULT_ROOT) in file_roots       # file tools keep vault
    # And the floor still guards the file lane (his verdict's confirmed half):
    (vault.VAULT_ROOT / ".env").write_text("SECRET=sk-live\n", encoding="utf-8")
    reg = orch._leader_tool_registry()
    with pytest.raises(ValueError):
        reg["read_file"].call(path=str(vault.VAULT_ROOT / ".env"))


def test_converse_leader_gate_carries_standing_harness_roots(project: Project):
    """The harness roots (vault / shared resources / config dir) ride into the
    gate as STANDING roots — a config read silent-allows instead of dying at
    the dotfile refusal floor (the 'I couldn't read the config files' defect:
    the registry granted the roots but the gate refused the ask)."""
    from modulatio import config as config_mod
    from modulatio import leader_gate as lg
    from modulatio import leader_permissions as lp

    orch = Orchestrator(project, _runners())
    gate = orch.leader_gate()
    assert gate._standing_roots  # wired, not empty
    req = lg.SecurityRequest(
        action="read",
        resource=str(config_mod.CONFIG_DIR / "model_presets.json"),
        request_class=lp.REQUEST_CLASS_PATH, why="t")
    assert gate.is_granted(req) is True
    d = gate.decide(req, prompt_fn=lambda r: pytest.fail("must not prompt"))
    assert d.granted_via == "standing"


def test_converse_answers_in_lane_when_the_turn_crashes(
    project: Project, monkeypatch
):
    """The no-block invariant: a model/tool failure mid-turn becomes an honest
    in-lane reply (persisted like any turn), never an exception the surface
    turns into a 500 (web) or a dead TUI worker."""
    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": lambda **k: ChatResponse(content="ok", tool_calls=())},
        chat_runner_models={"leader": "mock-model"},
    )

    def _boom(**kwargs):
        raise RuntimeError("input exceeds the context window of this model")

    monkeypatch.setattr(orch, "_run_chat_loop", _boom)
    reply = orch.converse("read that huge file for me")
    assert "turn failed before I could reply" in reply
    assert "context window" in reply
    thread = orch._load_conversation()
    assert thread[-1]["role"] == "leader"
    assert "turn failed" in thread[-1]["content"]


def test_mid_turn_grant_reaches_the_very_call_that_prompted(
    project: Project, tmp_path: Path
):
    """The stale-split defect, end-to-end: the Leader read_files a path
    outside every root, the operator grants Session at the modal — and the
    SAME tool call must succeed (the registry's fence reads the gate live,
    not a tuple frozen at turn start)."""
    from modulatio import runners as mod_runners

    outside = tmp_path.parent / f"outside-{project.code}"
    outside.mkdir(exist_ok=True)
    secret = outside / "notes.txt"
    secret.write_text("the owl flies at midnight\n", encoding="utf-8")

    calls: list = []

    def mock_leader(*, messages, tools, tool_choice=None):
        calls.append(list(messages))
        if len(calls) == 1:
            return ChatResponse(content="", tool_calls=(
                mod_runners.ToolCall(
                    id="c1", name="read_file", args={"path": str(secret)}),
            ))
        return ChatResponse(content="done reading", tool_calls=())

    prompts: list = []

    def grant_session(request):
        from modulatio import leader_gate as lg
        prompts.append(request)
        return lg.ScopedDecision(scope="session")

    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": mock_leader},
        chat_runner_models={"leader": "mock-model"},
    )
    reply = orch.converse("read my notes", prompt_fn=grant_session)
    assert reply == "done reading"
    assert len(prompts) == 1, "the out-of-root read must prompt exactly once"
    # The tool result the model saw carries the file content — the granted
    # call succeeded first try, no refusal round-trip.
    fed_back = str(calls[1])
    assert "the owl flies at midnight" in fed_back
    assert "outside the confined/granted roots" not in fed_back


def test_no_block_belt_survives_broken_persist_and_activity(
    project: Project, monkeypatch
):
    """WB F2 pin: the belt's own side effects (failure-turn persist, activity
    emit) are best-effort — a disk fault in either must not resurrect the 500
    the belt exists to prevent."""
    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": lambda **k: ChatResponse(content="ok", tool_calls=())},
        chat_runner_models={"leader": "mock-model"},
    )

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(orch, "_run_chat_loop", _boom)
    real_append = orch._append_conversation

    def flaky_append(role, content, **kw):
        if "turn failed" in content:
            raise OSError("disk full")
        return real_append(role, content, **kw)

    real_emit = orch._emit_activity

    def flaky_emit(**kw):
        if kw.get("phase") == "leader_answered":
            raise OSError("activity sink down")
        return real_emit(**kw)

    monkeypatch.setattr(orch, "_append_conversation", flaky_append)
    monkeypatch.setattr(orch, "_emit_activity", flaky_emit)
    reply = orch.converse("read something")
    assert "turn failed before I could reply: boom" in reply


def test_converse_prompt_names_the_delivery_folder(project: Project):
    """A grant without an address is unreachable. The Leader can write outside
    its workspace through the operator gate, but only to a path it can name —
    so the place finished work is collected has to be in the prompt, like every
    other address it is expected to reach."""
    from modulatio import delivery

    orch = Orchestrator(project, _runners())
    prompt = orch._build_converse_prompt([], "hi")

    assert str(delivery.project_delivery_dir(project.code)) in prompt
    # And it is framed as a handover, so work is not left on the desk as done.
    lowered = prompt.lower()
    assert "delivery folder" in lowered
    assert "workspace" in lowered


def test_a_loaded_file_rides_one_same_turn_redispatch(project: Project, monkeypatch):
    """``load_document`` stages bytes for the turn, and the turn ends with ONE
    multimodal completion carrying them — the text loop cannot hold image
    content blocks, so the look happens as the same single call an
    operator-attached image turn uses. The loop's own reply rides along so the
    model continues instead of starting over."""
    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": lambda **k: ChatResponse(content="x", tool_calls=())},
        chat_runner_models={"leader": "mock-model"},
    )
    workspace = orch._leader_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    def _loop_that_loads(**kwargs):
        tool = orch._leader_converse_registry()["load_document"]
        result = tool.call(path="shot.png")
        assert "Loaded shot.png" in result, result
        return "let me look at that"

    seen = {}

    def _mm(*, prompt, attachments, **kw):
        seen["attachments"] = list(attachments)
        seen["prompt"] = prompt
        return "a red button on a grey dialog"

    monkeypatch.setattr(orch, "_run_chat_loop", _loop_that_loads)
    monkeypatch.setattr(orch, "_run_multimodal_leader", _mm)

    reply = orch.converse("what's in ~/shot.png?")

    assert reply == "a red button on a grey dialog"
    assert [a.name for a in seen["attachments"]] == ["shot.png"]
    assert seen["attachments"][0].kind == "image"
    # The interim reply rides the redispatch prompt.
    assert "let me look at that" in seen["prompt"]
    # Nothing loaded survives the turn: queue cleared, staged bytes gone.
    assert getattr(orch._tls, "loaded_items", None) is None
    staged = seen["attachments"][0].staged_path
    assert staged is not None and not staged.exists()


def test_loaded_items_clear_even_when_the_loop_dies(project: Project, monkeypatch):
    """The queue and its staged bytes clear on EVERY exit — a model error must
    not leave this turn's bytes waiting to ride a future turn."""
    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": lambda **k: ChatResponse(content="x", tool_calls=())},
        chat_runner_models={"leader": "mock-model"},
    )
    workspace = orch._leader_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.md").write_text("body\n")
    staged_seen = {}

    def _loop_loads_then_dies(**kwargs):
        orch._leader_converse_registry()["load_document"].call(path="notes.md")
        staged_seen["path"] = orch._tls.loaded_items[0].staged_path
        raise RuntimeError("model fell over")

    monkeypatch.setattr(orch, "_run_chat_loop", _loop_loads_then_dies)
    reply = orch.converse("read my notes")

    assert "failed" in reply  # the in-lane belt answered, not a raise
    assert getattr(orch._tls, "loaded_items", None) is None
    assert not staged_seen["path"].exists()


def test_load_document_refuses_an_outside_path_and_names_the_remedy(
        project: Project, monkeypatch):
    """An ungranted absolute path is refused by the same fence read_file uses,
    and the refusal says how to proceed — asking for the folder IS the flow."""
    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": lambda **k: ChatResponse(content="x", tool_calls=())},
        chat_runner_models={"leader": "mock-model"},
    )
    replies = {}

    def _loop(**kwargs):
        tool = orch._leader_converse_registry()["load_document"]
        replies["outside"] = tool.call(path="/etc/passwd")
        replies["count"] = len(orch._tls.loaded_items)
        return "done"

    monkeypatch.setattr(orch, "_run_chat_loop", _loop)
    orch.converse("load /etc/passwd")

    assert "Can't load" in replies["outside"]
    assert "grant" in replies["outside"]
    assert replies["count"] == 0


def test_load_document_is_gated_as_a_read(project: Project):
    """The gate sees a load exactly as it sees a read: same class, same
    action, so an outside folder prompts with the read wording and a grant
    covers both tools alike."""
    from modulatio import leader_gate as lg

    reqs = lg.extract_tool_requests(
        "load_document", {"path": "/outside/shot.png"},
        root=vault.project_dir(PROJECT_CODE))
    assert len(reqs) == 1
    assert reqs[0].action == "read"
    assert reqs[0].request_class == "path"
    assert reqs[0].resource == "/outside/shot.png"
