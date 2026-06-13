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
    though bwrap doesn't cgroup-limit memory."""
    art = _artifacts(tmp_path)
    (art / "limits.py").write_text(
        "import resource\n"
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
