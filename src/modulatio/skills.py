# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Skill registry — Research-First pattern applied to agent capabilities.

A **skill** is a named capability an agent can hold. Each skill file
declares the routing surface for that capability: what tools it needs
(narrow loadout keeps LLM-request tokens low — the core value prop
Modulatio has over swiss-army-agent designs), what model tier runs it,
what cost class it sits in, and which capability axes it claims. Body
of the file is the prompt template the reasoner uses.

Files live in two locations, searched in order:

1. **Shared defaults** at ``<shared_resources>/skills/<name>.md`` —
   baseline skill definitions shipped with Modulatio. The shared
   resources path is resolved via ``modulatio.config``.
2. **Per-project overrides** at ``<project_vault>/skills/<name>.md`` —
   a team's tuned version of the skill for their product. When present,
   the project-local file **fully replaces** the shared one (unlike
   standards, which stack). A skill is a coherent whole — appending team
   overrides to a shared prompt template would produce jagged output.

Slice #6a registers the schema and the loader. Orchestrator still uses
hardcoded prompt templates in ``orchestration.py`` until slice #6b wires
the registry into dispatch.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from modulatio import config
from modulatio.vault import project_dir, validate_registry_name

logger = logging.getLogger("modulatio.skills")

#: Override hook for the shared skills directory. ``None`` (the default)
#: means resolve from ``config.get_shared_resources_path()`` at CALL time, so a
#: relocated shared-resources path (e.g. after a config reload) is honored —
#: matching standards.py / constitution.py / qc_persona.py, which never freeze
#: the path at import. Set to a concrete ``Path`` only to pin the directory
#: (tests do this via ``monkeypatch.setattr(skills, "_SKILLS_ROOT", ...)``).
#: Was frozen at import — diverged from the sibling
#: resolvers and went stale after a config reload.
_SKILLS_ROOT: Path | None = None


def _skills_root() -> Path:
    """The user's shared skills directory, resolved at call time so a
    relocated shared-resources path / test pin is honored (no import-time
    freeze) — mirrors ``standards._standards_root()``."""
    if _SKILLS_ROOT is not None:
        return _SKILLS_ROOT
    return config.get_shared_resources_path() / "skills"

#: Built-in canonical skills bundled with the package. Read-only seed
#: copies of skills that the team_template + roster reference by name —
#: e.g. ``coding`` (engineer agent) and ``code-review`` (QC agent).
#: A fresh install with no user-edited copies still resolves these
#: names through this directory; the user's shared-resources skills
#: directory takes precedence when present (so any edit they make
#: there overrides the seed).
_SEED_SKILLS_ROOT = Path(__file__).parent / "_seed_skills"

_OWN_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _fm(value: object) -> str:
    """Collapse any CR/LF in a front-matter SCALAR value to a single space so
    a value can never forge an extra front-matter line.

    The front-matter parser is line-based (``key: value`` per line), so a
    Leader-supplied ``description`` containing ``"\\nneeds_network: true"``
    would otherwise self-grant the sandbox network (and ``pass_env`` a secret).
    Stripping the newline at the single serialization chokepoint binds the
    invariant deterministically, regardless of how the ``Skill`` was built.
    """
    return str(value).replace("\r", " ").replace("\n", " ")


@dataclass(frozen=True)
class Skill:
    """A loaded skill definition — routing schema + prompt template body.

    Fields that drive slice #6c routing:

    - ``tool_loadout`` — explicit narrow tool list. Minimizing slack here
      is the core reason skill-based dispatch matters: a one-tool
      a single-purpose skill ships ~500 tokens of schema vs ~10K for a swiss-army
      agent. Routing prefers the narrowest loadout that still covers the
      task's needs.
    - ``model_tier`` — which LLM tier serves this skill
      (strategic / tactical / generalist / reasoning-heavy / tool-using /
      budget). Matches ``Agent.model_tier`` at dispatch time.
    - ``cost_class`` — free-local / paid-cloud / premium-cloud. Routing
      prefers the cheapest qualifying tier unless a task override bumps
      it, following the existing cron-agent policy.
    - ``capability_tags`` — free-form taxonomy (e.g. "web-research",
      "contrarian-argument", "python-coding"). Advertises what this
      skill brings to the agent holding it. Consumed by slice #9a
      capability-match routing.
    - ``required_capabilities`` — capability tags the executing agent
      MUST have for this skill to run. Distinct from ``capability_tags``
      (advertised) — a floor on the agent, not a promise by the skill.
      Slice #9b dispatch unions these with the task's own
      ``required_capabilities`` and filters the roster by the union.
      A ``shell-runner`` skill, for example, can declare
      ``required_capabilities = ("shell-access",)`` so an agent without
      shell access is never routed to tasks that need it, even when
      the planning step forgets to declare the capability.
    - ``standards_domain`` — fixed domain for skills that always pull a
      specific ``standards/<kind>.md`` (e.g. Leader's ``leader-scope``).
      ``None`` for kind-agnostic skills (drafter, QC) that follow the
      task's ``artifact_kind`` dynamically.
    - ``executor`` — which producer path runs this skill (slice #9e).
      ``"llm"`` (default) routes through the usual LLM runner; ``"tool"``
      routes through the tool registry, invoking ``tool_loadout[0]``
      with ``Task.tool_args``. Back-compat: every pre-#9e skill loads
      as ``"llm"`` with no behavior change.

    ``freshness_class`` and ``last_verified_at`` are Research-First
    metadata — signal for humans + QC about whether this skill definition
    is still current.
    """

    name: str
    description: str
    prompt_template: str
    tool_loadout: tuple[str, ...] = ()
    standards_domain: str | None = None
    model_tier: str | None = None
    cost_class: str | None = None
    capability_tags: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    executor: str = "llm"
    #: Skill version (Brick 4 self-codification). ``None`` for the bundled
    #: seeds; an autonomously-codified skill starts at ``"1"`` and an
    #: improvement bumps it (``"2"``, ``"3"`` …). The bumped file overwrites
    #: in place — the FULL history is kept by git (the skill library is a
    #: git repo), so nothing earned at a real token cost is ever lost.
    version: str | None = None
    #: Codification provenance (task #84): the content hash of the bundled
    #: SEED this codification was derived from, stamped by ``save()`` when a
    #: seed exists. ``None`` for seeds themselves and for user-authored
    #: overrides. ``load_with_metadata`` uses it to detect a STALE
    #: codification — if the package later ships a newer seed (hash
    #: mismatch), the seed supersedes the codification instead of being
    #: permanently shadowed. A codification with no ``base_seed_hash``
    #: (pre-#84 / legacy) is preserved as-is; it gets stamped the next time
    #: it's re-codified.
    base_seed_hash: str | None = None
    #: Explicit "this is a HUMAN override, keep it sacred" marker. The
    #: seed→supersede rule treats any skill
    #: carrying a ``version``/``base_seed_hash`` as machine-codified and may
    #: supersede it on a seed change. But a human who EDITS a codified shared
    #: skill and leaves that frontmatter would get silently superseded. Setting
    #: ``user_override: true`` makes the skill a user override — ALWAYS honored,
    #: never superseded — regardless of leftover codification stamps. (Removing
    #: ``version``/``base_seed_hash`` does the same; this is the explicit way.)
    user_override: bool = False
    #: Codification provenance: ``"fail"`` (codified from repeated QC
    #: rejections — independently validated), ``"win"`` (codified from a QC-authored
    #: RECOVERY — the same mind judged + wrote the fix, so NON-independent and most
    #: worth a spot-check), or ``"user"``. ``None`` for seeds. Load-bearing for
    #: honesty: it lets a reader tell an independently-validated lesson from a
    #: non-independent one — without it, codifying from a QC fix would launder the
    #: source.
    provenance: str | None = None
    #: The recovery cluster SIGNATURES already codified into this skill. The
    #: win loop appends a signature here as it codifies; before
    #: appending a ``## Learned (from recovery)`` block it checks this list and SKIPS
    #: if the signature is present — so a replay after a consume-after-commit failure
    #: is idempotent (the applied-signature guard). Empty for seeds / fail-only skills.
    learned_from: tuple[str, ...] = ()
    freshness_class: str | None = None
    last_verified_at: str | None = None
    #: Sandbox: skill explicitly opts in to network access for its
    #: ``run_shell`` calls (e.g. a web-research drafter that fetches
    #: real URLs). Default False — the sandbox's network namespace is
    #: unshared. Frontmatter key: ``needs_network: true``.
    needs_network: bool = False
    #: Sandbox: extra NON-SECRET env var names this skill needs forwarded
    #: into the sandboxed process beyond the static allowlist (e.g. a config
    #: path like ``MYAPP_CONFIG_DIR`` or ``TOKENIZERS_PARALLELISM``). Secrets
    #: are categorically refused: any name matching the deny-pattern regex
    #: (token / secret / key / password / credential / DSN / DB URL / ssh /
    #: gpg / a provider prefix) is dropped even when listed here — pass_env is
    #: for configuration, never credentials. A tool that genuinely needs a
    #: credential belongs behind its own registered tool, not run_shell.
    #: Frontmatter key: ``pass_env: [FOO, BAR]``.
    pass_env: tuple[str, ...] = ()
    sources: tuple[str, ...] = field(default_factory=tuple)


_EMPTY_SKILL = Skill(name="", description="", prompt_template="")


def _parse_csv(raw: str) -> tuple[str, ...]:
    """Turn ``"fs, web, http"`` (or ``"[fs, web, http]"``) into a tuple.

    Naive front-matter parser — good enough for the short lists skill
    files carry. A real YAML parser is a slice #6b-or-later upgrade.
    """
    s = raw.strip()
    if not s:
        return ()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return tuple(p.strip() for p in s.split(",") if p.strip())


def _parse_file(path: Path) -> Skill:
    # Resilience: skill files are human-edited artifacts in the shared/project
    # vault. A stray non-UTF-8 byte (Latin-1/CP1252 save, truncated write) must
    # NOT raise UnicodeDecodeError out of the dispatch/wave-floor callbacks and
    # crash the whole run — a single bad skill file degrades to "missing", never
    # bricks the read path. ``errors="replace"`` tolerates bad bytes; an
    # unreadable file (perms/IO) degrades to the empty skill, honoring the
    # documented "missing → empty, not an error" contract.
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _EMPTY_SKILL
    m = _OWN_FRONTMATTER_RE.match(raw)
    meta: dict[str, str] = {}
    body = raw
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        body = raw[m.end():].lstrip()
    else:
        body = raw.lstrip()

    return Skill(
        name=meta.get("name") or path.stem,
        description=meta.get("description") or "",
        prompt_template=body,
        tool_loadout=_parse_csv(meta.get("tool_loadout", "")),
        standards_domain=meta.get("standards_domain") or None,
        model_tier=meta.get("model_tier") or None,
        cost_class=meta.get("cost_class") or None,
        capability_tags=_parse_csv(meta.get("capability_tags", "")),
        required_capabilities=_parse_csv(meta.get("required_capabilities", "")),
        executor=meta.get("executor") or "llm",
        version=meta.get("version") or None,
        base_seed_hash=meta.get("base_seed_hash") or None,
        user_override=str(meta.get("user_override", "")).strip().lower() in ("true", "yes", "1"),
        provenance=meta.get("provenance") or None,
        learned_from=_parse_csv(meta.get("learned_from", "")),
        freshness_class=meta.get("freshness_class") or None,
        last_verified_at=meta.get("last_verified_at") or None,
        needs_network=str(meta.get("needs_network", "")).strip().lower() in ("true", "yes", "1"),
        pass_env=_parse_csv(meta.get("pass_env", "")),
        sources=(str(path),),
    )


def seed_content_hash(name: str) -> str | None:
    """Stable short content hash of the bundled SEED skill ``name``, or
    ``None`` if no seed exists. Used to stamp codifications with the seed
    they derive from and to detect when a codification has gone stale
    against a newer bundled seed (task #84)."""
    seed = _SEED_SKILLS_ROOT / f"{name}.md"
    if not seed.exists():
        return None
    return hashlib.sha256(seed.read_bytes()).hexdigest()[:16]


def _is_codified(skill: Skill) -> bool:
    """A shared skill is machine-codified (vs a user-authored override) when it
    carries a ``version`` (the Alfred loop stamps one) or a ``base_seed_hash``.
    User overrides carry neither and are always honored. An explicit
    ``user_override: true`` (task #90) forces the override reading even when
    codification stamps are still present — so a human who edits a codified
    shared skill can keep it sacred without scrubbing the frontmatter."""
    if skill.user_override:
        return False
    return skill.version is not None or skill.base_seed_hash is not None


#: Warn once per (name) when a stale codification is superseded by its seed.
_WARNED_SUPERSEDED: set[str] = set()


def load_with_metadata(name: str, project_code: str | None = None) -> Skill:
    """Load a skill by name. Resolution chain (highest precedence first):

    1. Project-local override at ``<project>/skills/<name>.md``
    2. User-curated shared skills at ``<shared_resources>/skills/<name>.md``
    3. Package-bundled seed at ``modulatio/_seed_skills/<name>.md``

    Missing in all three → empty skill (not an error).

    The seed-dir fallback is what makes the team_template's
    ``coding`` / ``code-review`` references work on a fresh install
    where the user hasn't manually populated the shared dir.

    Freshness gate (task #84): a USER-authored shared/project override is
    always honored — sacred. But a MACHINE-codified shared skill (Alfred
    loop) is honored only while it's based on the CURRENT seed. If the
    package ships a newer seed (its ``base_seed_hash`` no longer matches),
    the seed SUPERSEDES the codification instead of permanently shadowing the
    improvement. A legacy codification with no ``base_seed_hash`` is
    preserved as-is (we can't prove it stale); it gets stamped the next time
    it's re-codified.
    """
    # A traversal name resolves to the empty skill (safe not-found) rather
    # than a read outside the registry root. Trusted internal names (the task
    # graph) always pass; only a hostile name is short-circuited.
    try:
        validate_registry_name(name)
    except ValueError:
        return _EMPTY_SKILL
    seed_path = _SEED_SKILLS_ROOT / f"{name}.md"
    if project_code is not None:
        local = project_dir(project_code) / "skills" / f"{name}.md"
        if local.exists():
            return _maybe_supersede(name, _parse_file(local), seed_path)
    shared = _skills_root() / f"{name}.md"
    if shared.exists():
        return _maybe_supersede(name, _parse_file(shared), seed_path)
    if seed_path.exists():
        return _parse_file(seed_path)
    return _EMPTY_SKILL


def _maybe_supersede(name: str, override: Skill, seed_path: Path) -> Skill:
    """Return ``override`` unless it is a STALE codification — a machine
    codification whose stamped ``base_seed_hash`` no longer matches the
    current bundled seed — in which case return the seed (task #84).

    A WIN/USER codification (``provenance in ('win', 'user')``) is
    EXEMPT: it carries genuinely-learned content (a ``## Learned (from
    recovery)`` technique block) appended on top of the seed, so a seed bump
    must never discard it. Only a mechanical FAIL codification is refreshable
    by a newer seed."""
    if not seed_path.exists():
        return override
    if not _is_codified(override) or override.base_seed_hash is None:
        # User override, or a legacy codification with no provenance stamp:
        # honor it. (Legacy ones can't be proven stale; preserve learning.)
        return override
    if override.provenance in ("win", "user"):
        # A WIN codification appends a genuinely-learned recovery technique
        # ('## Learned (from recovery)') on top of the seed body — it is NOT a
        # mechanical fail-codification the seed can refresh. Superseding it on a
        # seed change would silently discard learned work (the same money the
        # win loop spent earning it). Treat win/user provenance as sacred, like
        # an explicit ``user_override`` — a seed bump never destroys it.
        return override
    current = seed_content_hash(name)
    if override.base_seed_hash == current:
        return override
    if name not in _WARNED_SUPERSEDED:
        _WARNED_SUPERSEDED.add(name)
        logger.warning(
            "skill %r: codified copy (base_seed_hash=%s) is stale against the "
            "current seed (%s) — using the seed. Re-codify to refresh it.",
            name, override.base_seed_hash, current,
        )
    return _parse_file(seed_path)


def load(name: str, project_code: str | None = None) -> str:
    """Return the skill's prompt template body (empty string if missing)."""
    return load_with_metadata(name, project_code).prompt_template


def list_skills(project_code: str | None = None) -> list[str]:
    """Return the sorted union of skill names visible to a project.

    Combines (a) package-bundled seeds, (b) user-curated shared
    skills, (c) project-local skills. The Leader's slice-#6b
    skill-gap detection consumes this once per decomposition to know
    what the roster can dispatch against — bundled seeds count.
    """
    names: set[str] = set()
    if _SEED_SKILLS_ROOT.exists():
        names.update(p.stem for p in _SEED_SKILLS_ROOT.glob("*.md"))
    shared_root = _skills_root()
    if shared_root.exists():
        names.update(p.stem for p in shared_root.glob("*.md"))
    if project_code is not None:
        local_dir = project_dir(project_code) / "skills"
        if local_dir.exists():
            names.update(p.stem for p in local_dir.glob("*.md"))
    return sorted(names)


def save(skill: Skill, project_code: str | None = None) -> Path:
    """Write a skill to shared (no project_code) or the project roster.

    Routing fields are serialized as comma-separated values in
    front-matter. Body is the prompt template. Returns the written path.
    """
    validate_registry_name(skill.name)  # no path-traversal write
    if project_code is not None:
        root = project_dir(project_code) / "skills"
    else:
        root = _skills_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{skill.name}.md"

    # Every interpolated value is newline-collapsed (``_fm``) so a scalar
    # field can't forge a privilege-granting front-matter key.
    fm_lines: list[str] = [
        f"name: {skill.name}",
        f"description: {_fm(skill.description)}",
        f"tool_loadout: {_fm(', '.join(skill.tool_loadout))}",
        f"standards_domain: {_fm(skill.standards_domain or '')}",
        f"model_tier: {_fm(skill.model_tier or '')}",
        f"cost_class: {_fm(skill.cost_class or '')}",
        f"capability_tags: {_fm(', '.join(skill.capability_tags))}",
        f"required_capabilities: {_fm(', '.join(skill.required_capabilities))}",
        f"executor: {_fm(skill.executor)}",
    ]
    if skill.version is not None:
        fm_lines.append(f"version: {_fm(skill.version)}")
    # task #84: stamp the seed this codification derives from so a later seed
    # improvement can supersede it instead of being shadowed. Auto-derive when
    # the skill is codified (has a version), a seed exists, and the caller
    # didn't pin one explicitly. User overrides (no version) get no stamp.
    base_seed_hash = skill.base_seed_hash
    if base_seed_hash is None and skill.version is not None:
        base_seed_hash = seed_content_hash(skill.name)
    if base_seed_hash is not None:
        fm_lines.append(f"base_seed_hash: {_fm(base_seed_hash)}")
    if skill.user_override:  # task #90: persist the sacred-override marker
        fm_lines.append("user_override: true")
    if skill.provenance is not None:  # #81: fail | win | user provenance
        fm_lines.append(f"provenance: {_fm(skill.provenance)}")
    if skill.learned_from:  # #81: the recovery cluster signatures already codified
        fm_lines.append(f"learned_from: {_fm(', '.join(skill.learned_from))}")
    if skill.freshness_class is not None:
        fm_lines.append(f"freshness_class: {_fm(skill.freshness_class)}")
    if skill.last_verified_at is not None:
        fm_lines.append(f"last_verified_at: {_fm(skill.last_verified_at)}")
    if skill.needs_network:
        fm_lines.append("needs_network: true")
    if skill.pass_env:
        fm_lines.append(f"pass_env: {_fm(', '.join(skill.pass_env))}")

    content = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + skill.prompt_template.rstrip() + "\n"
    path.write_text(content, encoding="utf-8")
    return path


def delete_skill(name: str, project_code: str | None = None) -> bool:
    """Delete a user/project skill by name. Returns True if a writable copy
    existed and was removed, False otherwise.

    Only touches the writable roots (shared ``_skills_root()`` or the project's
    ``skills/`` dir) — bundled SEED skills live in ``_SEED_SKILLS_ROOT`` and are
    never deletable here, so a codification can be removed but the seed it
    derives from survives. The name is path-validated (no traversal) and a
    symlinked skill file is refused rather than followed."""
    validate_registry_name(name)  # no path-traversal delete
    if project_code is not None:
        root = project_dir(project_code) / "skills"
    else:
        root = _skills_root()
    path = root / f"{name}.md"
    if path.is_symlink():
        raise ValueError(f"skill {name!r} is a symlink — refusing to delete")
    if not path.exists():
        return False
    path.unlink()
    return True


def create_skill(
    *,
    name: str,
    description: str,
    prompt_template: str,
    tool_loadout: tuple[str, ...] = (),
    standards_domain: str | None = None,
    model_tier: str | None = None,
    cost_class: str | None = None,
    capability_tags: tuple[str, ...] = (),
    required_capabilities: tuple[str, ...] = (),
    executor: str = "llm",
    version: str | None = None,
    provenance: str | None = None,
    learned_from: tuple[str, ...] = (),
    project_code: str | None = None,
) -> Skill:
    """Create a new skill file, shared or project-local.

    Slice #18: wizard-facing API for the TUI's Skills tab. ``project_code``
    is None for shared skills (written to the modulatio/skills/ vault
    root); supplying a project code writes under that project's
    ``skills/`` override directory.

    Idempotency: raises ``FileExistsError`` if a skill with the same
    name already exists at the target scope — wizards should validate
    against ``list_skills`` before calling.
    """
    validate_registry_name(name)  # no path-traversal write
    if project_code is not None:
        root = project_dir(project_code) / "skills"
    else:
        root = _skills_root()
    target = root / f"{name}.md"
    if target.exists():
        raise FileExistsError(
            f"skill already exists at {target}; pick a different name "
            f"or edit the existing file"
        )

    skill = Skill(
        name=name,
        description=description,
        prompt_template=prompt_template,
        tool_loadout=tool_loadout,
        standards_domain=standards_domain,
        model_tier=model_tier,
        cost_class=cost_class,
        capability_tags=capability_tags,
        required_capabilities=required_capabilities,
        executor=executor,
        version=version,
        provenance=provenance,
        learned_from=learned_from,
    )
    save(skill, project_code)
    return skill


__all__ = [
    "Skill", "create_skill", "delete_skill", "list_skills", "load",
    "load_with_metadata", "save",
]
