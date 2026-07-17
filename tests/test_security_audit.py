# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Security-audit follow-up regression tests (post-v0.8.8).

Each test reproduces a vector confirmed in the full-codebase audit and
proves the engine now *binds* the invariant (prose can't, per the
"engine binds it" rule):

- **H1 — name traversal.** A skill / job-template name carrying a path
  separator, ``..``, an absolute prefix, or a control character must not
  escape its registry root. Left open, a Leader (or a poisoned upstream
  artifact driving the Leader) could write ``../<other-project>/skills/qc``
  and poison another project's library, or read a file outside the root.
- **H2 — front-matter injection.** A Leader-supplied scalar field (e.g.
  ``description``) containing a newline must not forge an extra front-matter
  line. Left open, ``create_skill`` could ship a skill whose body looks
  benign but whose injected ``needs_network: true`` / ``pass_env:
  OPENAI_API_KEY`` self-grants the sandbox network + a secret env var.

H1 + H2 compose into a privilege escalation (write a poisoned skill
cross-project that grants itself network + secrets + run_shell), so both
are closed at the engine chokepoints, not in prompt prose.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from modulatio import job_templates, sandbox, skills, tools
from modulatio import vault


# --------------------------------------------------------------------------
# H1 — registry-name traversal (skills + job templates)
# --------------------------------------------------------------------------

_UNSAFE_NAMES = [
    "../evil",
    "../../etc/passwd",
    "/tmp/evil",
    "foo/bar",
    "a/../../b",
    "..",
    ".",
    "",
    "-leading-dash-ok?-no",  # leading '-' is not allowed (must start alnum)
    ".hidden",
    "with space",
    "semi;colon",
    "x\x00y",
    "新しい",
    "a" * 65,  # 65 chars — one past the 64 ceiling
]


@pytest.mark.parametrize("evil", _UNSAFE_NAMES)
def test_validate_registry_name_rejects_unsafe(evil):
    with pytest.raises(ValueError):
        vault.validate_registry_name(evil)


@pytest.mark.parametrize(
    "ok", ["qc", "code-review", "win-codify", "my_skill-2", "A1", "x" * 64]
)
def test_validate_registry_name_accepts_legit(ok):
    assert vault.validate_registry_name(ok) == ok


@pytest.mark.parametrize("evil", _UNSAFE_NAMES)
def test_create_skill_rejects_unsafe_name(evil, tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path)
    with pytest.raises(ValueError):
        skills.create_skill(name=evil, description="d", prompt_template="p")
    # nothing escaped the registry root
    assert not list(tmp_path.parent.glob("evil*"))
    assert not list(tmp_path.parent.glob("*passwd*"))


def test_create_skill_accepts_valid_slug(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path)
    sk = skills.create_skill(name="my-skill_2", description="d", prompt_template="p")
    assert sk.name == "my-skill_2"
    assert (tmp_path / "my-skill_2.md").exists()


def test_save_skill_rejects_unsafe_name(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path)
    poisoned = skills.Skill(
        name="../../poison", description="d", prompt_template="p"
    )
    with pytest.raises(ValueError):
        skills.save(poisoned)
    assert not list(tmp_path.parent.glob("poison*"))


def test_load_with_metadata_rejects_traversal_name(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path)
    # A traversal name resolves to the empty skill (safe not-found), never a
    # read outside the root.
    out = skills.load_with_metadata("../../../etc/hostname")
    assert out.name == ""
    assert out.prompt_template == ""


@pytest.mark.parametrize("evil", _UNSAFE_NAMES)
def test_create_job_template_rejects_unsafe_name(evil, tmp_path, monkeypatch):
    monkeypatch.setattr(job_templates, "_JT_ROOT", tmp_path)
    with pytest.raises(ValueError):
        job_templates.create_job_template(
            name=evil, description="d", interview_body="body",
        )
    assert not list(tmp_path.parent.glob("evil*"))


# --------------------------------------------------------------------------
# H2 — front-matter injection via a newline in a scalar field
# --------------------------------------------------------------------------

def test_save_blocks_skill_frontmatter_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path)
    # A benign-looking description that tries to forge two privilege-granting
    # front-matter keys.
    evil_desc = "harmless summary\nneeds_network: true\npass_env: OPENAI_API_KEY"
    sk = skills.Skill(name="poison", description=evil_desc, prompt_template="body")
    skills.save(sk)

    reloaded = skills.load_with_metadata("poison")
    # The injection did NOT take — no self-granted network or secret.
    assert reloaded.needs_network is False
    assert reloaded.pass_env == ()
    # The forged keys never became their own front-matter lines.
    assert "\n" not in reloaded.description


def test_save_blocks_skill_injection_via_list_element(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path)
    sk = skills.Skill(
        name="poison2",
        description="d",
        prompt_template="body",
        capability_tags=("ok", "evil\nneeds_network: true"),
    )
    skills.save(sk)
    reloaded = skills.load_with_metadata("poison2")
    assert reloaded.needs_network is False


def test_save_blocks_jt_frontmatter_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(job_templates, "_JT_ROOT", tmp_path)
    evil_desc = "harmless\nversion: 99\nlast_verified_at: 2099-01-01"
    job_templates.create_job_template(
        name="poison-jt", description=evil_desc, interview_body="body",
    )
    reloaded = job_templates.load_with_metadata("poison-jt")
    # No forged version line took.
    assert reloaded.version != "99"
    assert "\n" not in reloaded.description


# --------------------------------------------------------------------------
# H3 — run_shell containment: resource limits, orphan reaping, fail-closed
# --------------------------------------------------------------------------

def _artifacts(tmp_path):
    art = tmp_path / "artifacts"
    art.mkdir()
    return art


def test_run_shell_applies_resource_limits(tmp_path):
    """H3a: the child (and any sandboxed grandchild) runs under a bounded
    address space + zero core-dump size, so a memory bomb is contained even
    though bwrap doesn't cgroup-limit memory.

    The caps are now applied from the PARENT via ``prlimit`` right after spawn
    (not a fork-hazardous ``preexec_fn``), so the child sleeps briefly before
    reading its own limits to let the microsecond-scale apply land first."""
    art = _artifacts(tmp_path)
    (art / "limits.py").write_text(
        "import resource, time\n"
        "time.sleep(0.3)  # let the parent's prlimit land\n"
        "soft_as, _ = resource.getrlimit(resource.RLIMIT_AS)\n"
        "soft_core, _ = resource.getrlimit(resource.RLIMIT_CORE)\n"
        "print(f'AS={soft_as} CORE={soft_core}')\n"
    )
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 limits.py", profile="full", timeout=10)
    # parse the reported soft limits
    line = [ln for ln in out.splitlines() if ln.startswith("AS=")][0]
    as_val = int(line.split("AS=")[1].split(" ")[0])
    core_val = int(line.split("CORE=")[1])
    # address space is now a finite ceiling (not RLIM_INFINITY == -1) and at
    # or below our 4 GiB clamp; core dumps are disabled.
    assert as_val != -1
    assert as_val <= 4 * 1024**3
    assert core_val == 0


def test_run_shell_is_fork_safe_from_worker_threads(tmp_path):
    """0.9.0 hang regression: run_shell must be safe to call from worker
    threads. The old ``preexec_fn`` ran Python between fork() and exec() in a
    multithreaded process — a lock another thread held at fork time could
    deadlock the child and wedge the parent's own thread creation, hanging the
    whole suite. The cap is now applied from the parent via ``prlimit``, which
    has no fork hazard. Drive several concurrent run_shell calls off worker
    threads and require they all complete promptly."""
    art = _artifacts(tmp_path)
    (art / "echo.py").write_text("print('ok')\n")
    rs = tools.make_run_shell(art)

    def _call(_i):
        return rs(cmd="python3 echo.py", profile="full", timeout=10)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_call, i) for i in range(8)]
        outs = [f.result(timeout=30) for f in futures]
    assert all("ok" in o for o in outs)


def test_run_shell_reaps_orphaned_background_child_on_timeout(tmp_path):
    """H3b: a command that spawns a background child which outlives the
    parent must NOT survive the wall-clock timeout — the whole process group
    is killed (or the sandbox PID namespace is torn down). Proven by a marker
    a surviving child would write ~2s in: after a 1s timeout it must never
    appear."""
    art = _artifacts(tmp_path)
    (art / "orphan.py").write_text(
        "import subprocess, sys, time, os\n"
        "child = (\n"
        "    'import time, pathlib; time.sleep(2); "
        "pathlib.Path(\"orphan_marker\").write_text(\"survived\")'\n"
        ")\n"
        "subprocess.Popen([sys.executable, '-c', child], cwd=os.getcwd())\n"
        "time.sleep(60)\n"
    )
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 orphan.py", profile="full", timeout=1)
    assert "TIMEOUT" in out
    # give a surviving orphan well past its 2s write window
    time.sleep(3.5)
    assert not (art / "orphan_marker").exists(), (
        "orphaned background child survived the timeout — process group not reaped"
    )


def test_run_shell_fails_closed_when_sandbox_required_but_unavailable(
    tmp_path, monkeypatch
):
    """H3c: with MODULATIO_REQUIRE_SANDBOX=1 and no working bwrap (and no
    explicit bypass), run_shell REFUSES rather than running unconfined."""
    art = _artifacts(tmp_path)
    (art / "demo.py").write_text("print('should not run')\n")
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: False)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "standard")
    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: False)
    monkeypatch.setattr(sandbox, "is_sandbox_required", lambda: True)
    rs = tools.make_run_shell(art)
    with pytest.raises(RuntimeError, match="refused"):
        rs(cmd="python3 demo.py", profile="full", timeout=5)


def test_run_shell_soft_falls_when_sandbox_not_required(tmp_path, monkeypatch):
    """The default (no MODULATIO_REQUIRE_SANDBOX) still soft-falls to
    unsandboxed execution when bwrap is missing — dev/CI must keep working."""
    art = _artifacts(tmp_path)
    (art / "demo.py").write_text("print('ran unsandboxed')\n")
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: False)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "standard")
    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: False)
    monkeypatch.setattr(sandbox, "is_sandbox_required", lambda: False)
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 demo.py", profile="full", timeout=5)
    assert "ran unsandboxed" in out


# --------------------------------------------------------------------------
# M1 — broaden the sandbox env deny-list (secret exfil via pass_env)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "SECRET_KEY", "PRIVATE_KEY", "MYAPP_KEY", "GH_PAT", "GITHUB_TOKEN",
    "DATABASE_URL", "REDIS_DSN", "SSH_PRIVATE_KEY", "GPG_KEY", "MY_PASSPHRASE",
    "DB_PASSWORD", "SOME_CREDENTIAL", "GEMINI_API_KEY", "SESSION_SECRET",
    "ACCESS_TOKEN", "github_token",
])
def test_sandbox_denies_secret_env_names(name):
    """M1: the deny-list now catches generic secret shapes the original
    suffix/prefix list missed (key / PAT / DSN / DB URL / ssh / gpg)."""
    assert sandbox._is_safe_env_name(name) is False


@pytest.mark.parametrize("name", [
    "MY_CONFIG_PATH", "JAVA_HOME", "NODE_ENV", "HTTP_PROXY", "FOO",
])
def test_sandbox_allows_nonsecret_env_names(name):
    """Non-secret config vars are still forwardable via pass_env."""
    assert sandbox._is_safe_env_name(name) is True


# --------------------------------------------------------------------------
# M2 — redact secrets from surfaced auth-error alerts
# --------------------------------------------------------------------------

def test_redact_secrets_covers_provider_key_shapes():
    """M2: the shared redactor now catches xAI / GitHub / Google key shapes
    in addition to the OpenAI/Anthropic/OAuth set."""
    from modulatio.oauth_refresh import _redact_secrets

    raw = (
        "auth failed for sk-ant-ABC123DEF456GHI789 / "
        "xai-ABC123DEF456GHI789 / ghp_ABCDEF0123456789ABCDEF / "
        "AIzaABCDEF0123456789ABCD / Bearer ABCDEF0123456789ABCDEF"
    )
    out = _redact_secrets(raw)
    for secret in ("sk-ant-ABC123", "xai-ABC123", "ghp_ABCDEF0123", "AIzaABCDEF0", "Bearer ABCDEF0123"):
        assert secret not in out
    assert "<redacted>" in out


def test_fire_auth_alert_redacts_key_before_surfacing(monkeypatch):
    """The auth-alert chokepoint must not surface a raw provider key that the
    AuthenticationError echoed back."""
    from modulatio import auth_alerts, runners

    captured = {}

    def _fake_raise_alert(alert_id, *, error_message, auth_type, auth_config):
        captured["msg"] = error_message

    monkeypatch.setattr(auth_alerts, "raise_alert", _fake_raise_alert)
    runners._fire_auth_alert(
        "gpt-4",
        "401 Unauthorized: key sk-ant-LEAKED12345678901234 rejected",
        None,
    )
    assert "sk-ant-LEAKED" not in captured["msg"]
    assert "<redacted>" in captured["msg"]


# --------------------------------------------------------------------------
# SEC-01 (HIGH) — tool-call authorization bypass: dispatch must check
# the skill's tool_loadout, not just registry membership.
# --------------------------------------------------------------------------

def test_run_llm_with_tools_denies_tool_outside_loadout(tmp_path):
    """A model that returns a tool call NOT in the skill's declared
    tool_loadout must be refused — even when that tool exists in the
    registry. Mirrors the exploit shape: a web-only skill must not reach
    run_shell."""
    from modulatio import tools
    from modulatio.runners import ChatResponse, ToolCall, run_llm_with_tools

    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "pwn.py").write_text("print('BYPASS_EXECUTED')\n")
    reg = tools.build_registry(
        artifacts_root=art, tool_calls_dir=art / "tool_calls"
    )
    assert "run_shell" in reg  # the privileged tool is in the registry
    seen_schema = []
    tool_results = []

    def runner(*, messages, tools):
        seen_schema.append([t["function"]["name"] for t in tools])
        if len(seen_schema) == 1:
            # hostile/injected model emits an unlisted privileged tool call
            return ChatResponse(
                content="",
                tool_calls=(ToolCall(
                    id="tc1", name="run_shell",
                    args={"cmd": "python3 pwn.py", "profile": "full", "timeout": 5},
                ),),
            )
        # capture the tool-role result fed back after dispatch
        tool_results.append(messages[-1]["content"])
        return ChatResponse(content="done", tool_calls=())

    run_llm_with_tools(
        chat_runner=runner, prompt="x",
        tool_loadout=("http_get",), tool_registry=reg,
    )
    # the model only ever saw http_get
    assert seen_schema[0] == ["http_get"]
    # the unlisted run_shell was REFUSED, not executed
    assert "BYPASS_EXECUTED" not in tool_results[0]
    assert "run_shell" in tool_results[0]  # named in the deny message
    assert not (art / "BYPASS").exists()


# --------------------------------------------------------------------------
# SEC-04 (LOW) — clamp caller-supplied timeouts
# --------------------------------------------------------------------------

def test_clamp_timeout_bounds_and_nonfinite():
    from modulatio.tools import _clamp_timeout

    assert _clamp_timeout(5, lo=1, hi=30, default=10) == 5
    assert _clamp_timeout(1e9, lo=1, hi=30, default=10) == 30      # over -> hi
    assert _clamp_timeout(0.0001, lo=1, hi=30, default=10) == 1    # under -> lo
    assert _clamp_timeout(float("nan"), lo=1, hi=30, default=10) == 10
    assert _clamp_timeout(float("inf"), lo=1, hi=30, default=10) == 10
    assert _clamp_timeout("not-a-number", lo=1, hi=30, default=10) == 10


def test_run_shell_sanitizes_nonfinite_timeout(tmp_path):
    """A NaN timeout would otherwise reach subprocess and misbehave; the clamp
    coerces it to the default so the command runs normally."""
    from modulatio import tools

    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "demo.py").write_text("print('ok')\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 demo.py", profile="full", timeout=float("nan"))
    assert "ok" in out
    assert "exit_code: 0" in out


# --------------------------------------------------------------------------
# SEC-02 (MEDIUM) — ACP attachments confined to an allowed root
# --------------------------------------------------------------------------

def test_acp_attachment_path_rejects_outside_root(tmp_path, monkeypatch):
    from modulatio.acp import server

    monkeypatch.setenv("MODULATIO_ACP_ATTACHMENT_ROOTS", str(tmp_path))
    # a legit file under the root resolves
    good = tmp_path / "doc.txt"
    good.write_text("hi")
    assert server._validate_attachment_path(str(good)) == good.resolve()
    # outside the root → refused
    with pytest.raises(ValueError):
        server._validate_attachment_path("/etc/hostname")
    # a dotfile/secret under the root → refused
    secret = tmp_path / ".env"
    secret.write_text("OPENAI_API_KEY=sk-xxx")
    with pytest.raises(ValueError):
        server._validate_attachment_path(str(secret))
    # traversal that escapes the root → refused
    with pytest.raises(ValueError):
        server._validate_attachment_path(str(tmp_path / ".." / "etc" / "passwd"))


def test_acp_parse_prompt_drops_unauthorized_attachment(tmp_path, monkeypatch):
    """An ACP client supplying an out-of-root absolute path gets the attachment
    dropped, not read into context."""
    from modulatio.acp import server

    monkeypatch.setenv("MODULATIO_ACP_ATTACHMENT_ROOTS", str(tmp_path))
    text, atts = server._parse_prompt(
        {"prompt": [{"type": "resource", "path": "/etc/hostname"}]}
    )
    assert atts == []


# --------------------------------------------------------------------------
# SEC-03 (MEDIUM) — persistence redaction gaps
# --------------------------------------------------------------------------

def test_checkpoint_redacts_assistant_prose(tmp_path):
    """SEC-03a: a secret echoed into assistant PROSE (not just tool args) must
    not persist verbatim in a checkpoint."""
    import json as _json

    from modulatio.context_budget import write_checkpoint

    path = write_checkpoint(
        "ck1",
        [
            {"role": "assistant",
             "content": "the key it gave back was sk-ant-LEAKED1234567890ABCD ok"},
            {"role": "user", "content": "here is Bearer ABCDEF0123456789ABCDEF"},
        ],
        model="m", estimated_tokens=999, max_input_tokens=1,
        checkpoints_dir=tmp_path, redact_secrets=True,
    )
    data = _json.loads(path.read_text())
    blob = _json.dumps(data["messages"])
    assert "sk-ant-LEAKED" not in blob
    assert "Bearer ABCDEF0123456789ABCDEF" not in blob
    assert "<redacted>" in blob
    # prose around the secret is preserved (Leader's reasoning shape intact)
    assert "the key it gave back was" in blob
    assert "assistant.content" in data["redaction_policy"]
    assert "user.content" in data["redaction_policy"]


def test_leader_conversation_is_0600_and_redacted(tmp_path, monkeypatch):
    """SEC-03b: the Leader↔operator log is owner-only and token-redacted."""
    from modulatio import vault
    from modulatio.orchestration import Orchestrator
    from modulatio.types import Project, ProjectState

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("SEC", "sec fixture", "obj")
    project = Project(
        code="SEC", name="sec fixture", objective="obj",
        state=ProjectState.ACTIVE, leader_model="stub",
        wiki_path=str(vault.project_dir("SEC")),
    )
    runners = {
        "leader": lambda p: "", "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "", "qc": lambda p: "",
    }
    orch = Orchestrator(project, runners)
    orch._append_conversation("operator", "my token is sk-ant-LEAKED1234567890ABCD")

    path = orch._conversation_path()
    import stat as _stat
    mode = _stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    body = path.read_text()
    assert "sk-ant-LEAKED" not in body
    assert "<redacted>" in body


# --------------------------------------------------------------------------
# SEC-03 follow-up — AWS credential shapes must be redacted
# --------------------------------------------------------------------------

def test_redact_secrets_covers_aws_credentials():
    """SEC-03 follow-up: AWS access-key IDs and secret-access-key values
    pasted/echoed into prose were persisting verbatim."""
    from modulatio.oauth_refresh import _redact_secrets

    raw = (
        "aws_access_key_id=AKIAIOSFODNN7EXAMPLE "
        "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    )
    out = _redact_secrets(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY" not in out
    assert "<redacted>" in out
    # a bare temporary (ASIA) key id is also caught
    assert "ASIAY34FZKBOKMUTVV7A" not in _redact_secrets("token ASIAY34FZKBOKMUTVV7A here")
    # SEC-03: a labeled value with non-alphanumeric / abbreviated chars
    # must not leave the AKIA prefix behind, nor a quoted value its content.
    assert "AKIA" not in _redact_secrets("aws_access_key_id=AKIAIO...MPLE")
    assert "AKIA" not in _redact_secrets('aws_access_key_id="AKIAIOSFODNN7EXAMPLE"')
    assert "sk_live_" not in _redact_secrets("stripe sk_live_ABCDEF0123456789 here")


def test_checkpoint_redacts_aws_prose(tmp_path):
    """The checkpoint path now strips AWS creds from assistant/user prose."""
    import json as _json

    from modulatio.context_budget import write_checkpoint

    path = write_checkpoint(
        "ckaws",
        [{"role": "user",
          "content": "creds: aws_secret_access_key=wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY and id AKIAIOSFODNN7EXAMPLE"}],
        model="m", estimated_tokens=9, max_input_tokens=1,
        checkpoints_dir=tmp_path, redact_secrets=True,
    )
    blob = _json.dumps(_json.loads(path.read_text())["messages"])
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    assert "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY" not in blob
