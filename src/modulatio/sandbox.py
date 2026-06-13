# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Bubblewrap-based sandbox layer for ``run_shell`` (SEC-001 + SEC-002).

The argv-allowlist in ``tools.py`` is defense in depth, NOT the trust
boundary. Any argv shape it admits — `python3 -c '<body>'`, `bash
file.sh`, `pytest` — is enough for a producer to read every API key in
``os.environ`` and exfiltrate them. This module wraps those argvs in a
``bwrap`` invocation that:

- Mounts the host filesystem read-only (no writes outside the project
  artifacts root + tmpfs ``/tmp``).
- Strips the parent process env down to a minimal allowlist plus
  whatever the active skill explicitly opts into via ``pass_env``.
- Unshares the network namespace by default; per-skill opt-in via
  ``allow_network=True`` for skills that legitimately fetch (e.g. a
  hypothetical web-research drafter).

Soft-fallback policy: when ``bwrap`` is not on PATH (macOS, CI without
the tool, broken installs), ``run_shell`` logs one warning per process
and runs unsandboxed. This keeps the developer experience working but
the security claim degrades — production deploys MUST install bwrap.
The ``MODULATIO_RUN_SHELL_UNSAFE=1`` env var also forces the unsandboxed
path explicitly (used by the test suite's autouse fixture).

Per-call context flows through Python ``contextvars``:

- ``allow_network_var`` — bool, default False
- ``pass_env_var`` — tuple of env names, default empty

The orchestrator sets these from the active skill's frontmatter
(``needs_network: true``, ``pass_env: ["FOO", "BAR"]``) before invoking
the tool, then resets after.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("modulatio.sandbox")


# ── Sandbox profile ───────────────────────────────────────────────────────

#: Operator-selected posture for run_shell. Controls how much the sandbox
#: gets out of the agents' way. Set via the ``MODULATIO_SANDBOX_PROFILE``
#: env var; default ``standard``. The agents (LLMs) cannot pick this — it's
#: an operator/admin decision, like ``allow_network`` is skill-frontmatter,
#: not a producer tool-arg.
#:
#:   standard  (default, ships safe for any user) — host filesystem
#:             read-only, the project's artifacts root is the ONLY writable
#:             host path, network unshared (per-skill ``needs_network``
#:             opt-in still honored), cloud API keys/secrets stripped from
#:             the child env, ``pip install`` blocked. The interpreter +
#:             venv are bound in so code actually runs (this is the #82 fix
#:             that applies to ALL profiles — a masked venv was never a
#:             policy, just a bug).
#:   trusted   (single-operator / trusted-content boxes) — same confinement
#:             floor PLUS network on by default and ``pip`` unblocked, so an
#:             agent can "do whatever is required" to perform a request.
#:             The ONE floor kept here on purpose: cloud API keys/secrets
#:             are STILL stripped (a skill that genuinely needs a key opts
#:             in per-key via ``pass_env``). Drop to ``off`` only if you
#:             truly want the producers to see every credential in the
#:             parent env.
#:   off       — no bwrap at all: full parent env (secrets included), full
#:             filesystem write, full network. Identical to the long-standing
#:             ``MODULATIO_RUN_SHELL_UNSAFE=1`` bypass. Use only when you own
#:             the box AND trust every model/skill in the loop.
_SANDBOX_PROFILE_ENV = "MODULATIO_SANDBOX_PROFILE"
VALID_SANDBOX_PROFILES: tuple[str, ...] = ("standard", "trusted", "off")
_DEFAULT_SANDBOX_PROFILE = "standard"
_WARNED_BAD_PROFILE: set[str] = set()


def current_profile() -> str:
    """Return the active sandbox profile from ``MODULATIO_SANDBOX_PROFILE``.

    Unset / empty → ``standard``. An unrecognized value falls back to
    ``standard`` (fail-safe: an operator typo must NOT silently widen the
    sandbox) and warns once per bad value.
    """
    raw = os.environ.get(_SANDBOX_PROFILE_ENV, "").strip().lower()
    if not raw:
        return _DEFAULT_SANDBOX_PROFILE
    if raw not in VALID_SANDBOX_PROFILES:
        if raw not in _WARNED_BAD_PROFILE:
            _WARNED_BAD_PROFILE.add(raw)
            logger.warning(
                "unknown %s=%r; expected one of %s. Falling back to %r "
                "(fail-safe — a typo never widens the sandbox).",
                _SANDBOX_PROFILE_ENV, raw,
                ", ".join(VALID_SANDBOX_PROFILES), _DEFAULT_SANDBOX_PROFILE,
            )
        return _DEFAULT_SANDBOX_PROFILE
    return raw


# ── Per-call context ─────────────────────────────────────────────────────

#: Whether the current run_shell call is allowed to reach the network.
#: Default False; the orchestrator flips True only for skills whose
#: frontmatter declares ``needs_network: true``. The LLM cannot set
#: this — it's contextvars-scoped on the orchestrator side, not a
#: tool-arg the producer chooses.
allow_network_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "modulatio_sandbox_allow_network", default=False,
)

#: Names of additional env vars to pass through to the sandboxed
#: process beyond the static allowlist. Populated from the active
#: skill's ``pass_env`` frontmatter.
pass_env_var: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "modulatio_sandbox_pass_env", default=(),
)


# ── Static env policy ─────────────────────────────────────────────────────

#: The minimal allowlist of env vars passed through to the sandboxed
#: process. Anything else is stripped. PATH is constrained to
#: ``/usr/bin:/usr/local/bin`` so an attacker can't inject arbitrary
#: binaries via PATH manipulation in the parent.
_SAFE_ENV_KEYS: frozenset[str] = frozenset({
    "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "PWD",
})

#: Pattern-based deny list. Defense in depth: even if a skill's
#: ``pass_env`` mistakenly includes one of these names, the sandbox
#: still strips it. Any env var matching one of these regexes is
#: considered sensitive and never crosses the boundary.
#: Security audit M1: the original list was suffix/prefix-specific and missed
#: whole classes of secret env var — ``SECRET_KEY`` / ``PRIVATE_KEY`` /
#: ``SSH_*`` keys, ``GH_PAT`` / ``GITHUB_TOKEN`` PATs, ``DATABASE_URL`` /
#: ``*_DSN`` connection strings (which embed creds), ``~/.netrc`` pointers.
#: Broadened to substring-match the generic secret words (token / secret /
#: credential / password / passphrase) case-insensitively and to deny common
#: key/PAT/DSN/SSH/GPG shapes. Over-denying a non-secret var is cheap (it just
#: isn't forwarded — the tool falls back to its default); leaking one is not.
_DENY_ENV_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"TOKEN", re.IGNORECASE),
    re.compile(r"SECRET", re.IGNORECASE),
    re.compile(r"PASSWORD", re.IGNORECASE),
    re.compile(r"PASSWD", re.IGNORECASE),
    re.compile(r"PASSPHRASE", re.IGNORECASE),
    re.compile(r"CREDENTIAL", re.IGNORECASE),
    re.compile(r"_API_KEY$", re.IGNORECASE),
    re.compile(r"_KEY$", re.IGNORECASE),
    re.compile(r"PRIVATE_KEY", re.IGNORECASE),
    re.compile(r"_PAT$", re.IGNORECASE),
    re.compile(r"_DSN$", re.IGNORECASE),
    re.compile(r"DATABASE_URL", re.IGNORECASE),
    re.compile(r"NETRC", re.IGNORECASE),
    re.compile(r"^GH_"),
    re.compile(r"^GITHUB_"),
    re.compile(r"^SSH_"),
    re.compile(r"^GPG_"),
    re.compile(r"^ANTHROPIC_"),
    re.compile(r"^OPENAI_"),
    re.compile(r"^OPENROUTER_"),
    re.compile(r"^XAI_"),
    re.compile(r"^OLLAMA_"),
    re.compile(r"^GOOGLE_"),
    re.compile(r"^GEMINI_"),
    re.compile(r"^AZURE_"),
    re.compile(r"^AWS_"),
)


def _is_safe_env_name(name: str) -> bool:
    """True if ``name`` may cross the sandbox boundary (allowlist + not
    in deny list).
    """
    if not name:
        return False
    for pat in _DENY_ENV_PATTERNS:
        if pat.search(name):
            return False
    return True


# ── Sandbox availability ──────────────────────────────────────────────────

_SANDBOX_BYPASS_ENV = "MODULATIO_RUN_SHELL_UNSAFE"
_WARNED_NO_BWRAP = False
_SANDBOX_PROBE_CACHE: bool | None = None


def is_bypass_requested() -> bool:
    """True if the user explicitly opted out of sandboxing via the
    ``MODULATIO_RUN_SHELL_UNSAFE=1`` env var.
    """
    return os.environ.get(_SANDBOX_BYPASS_ENV, "").strip() == "1"


_SANDBOX_REQUIRED_ENV = "MODULATIO_REQUIRE_SANDBOX"


def is_sandbox_required() -> bool:
    """True if the operator demands a working sandbox via
    ``MODULATIO_REQUIRE_SANDBOX=1`` (security audit H3c).

    The default ``run_shell`` policy *soft-falls* to unsandboxed execution
    when ``bwrap`` is missing or non-functional — deliberate, so macOS/CI
    and single-user dev boxes still run. But a multi-user or otherwise
    untrusted host wants the opposite: if the sandbox can't confine the
    child, **refuse to run it** rather than execute with the parent's full
    env and filesystem. Setting this flag turns the fail-OPEN fallback into
    a fail-CLOSED refusal. An explicit ``MODULATIO_RUN_SHELL_UNSAFE=1`` /
    ``profile=off`` bypass still wins (the operator accepted that risk
    knowingly); this only governs the *implicit* missing-bwrap path.

    See the Multi-user host hardening ops guide.
    """
    return os.environ.get(_SANDBOX_REQUIRED_ENV, "").strip() == "1"


def is_sandbox_installed() -> bool:
    """True if the ``bwrap`` binary is on PATH (regardless of whether
    it can actually create namespaces). Use this when you need to
    distinguish "bubblewrap not installed at all" from "bubblewrap
    installed but unusable on this kernel" — e.g. the doctor command's
    diagnostic output.
    """
    return shutil.which("bwrap") is not None


def _probe_sandbox_functional() -> bool:
    """Run a tiny ``bwrap`` invocation to confirm the kernel allows
    unprivileged user-namespace creation. On hardened distros, in
    containers without `--privileged`, or when ``user.max_user_namespaces``
    is set to 0, ``bwrap`` is on PATH but every invocation fails with
    "No permissions to create new namespace". Catching that here means
    callers don't think sandboxing is live when it isn't.
    """
    if not is_sandbox_installed():
        return False
    try:
        result = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "true"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def is_sandbox_available() -> bool:
    """True iff ``bwrap`` is on PATH AND a probe invocation succeeds.

    the prior check was
    ``shutil.which("bwrap") is not None`` — passes on hosts where bwrap
    is installed but the kernel disallows unprivileged namespaces.
    Modulatio thought the sandbox was live; actual sandboxed calls
    failed at runtime with ``bwrap: No permissions to create new
    namespace``. Now functional: result cached per process so we
    don't pay the probe cost on every ``run_shell`` call.

    Use ``is_sandbox_installed()`` if you only need the PATH check.
    """
    global _SANDBOX_PROBE_CACHE
    if _SANDBOX_PROBE_CACHE is None:
        _SANDBOX_PROBE_CACHE = _probe_sandbox_functional()
    return _SANDBOX_PROBE_CACHE


def reset_sandbox_probe_cache() -> None:
    """Reset the cached probe result. Tests use this to flip between
    "functional" and "broken" states; production code shouldn't.
    """
    global _SANDBOX_PROBE_CACHE
    _SANDBOX_PROBE_CACHE = None


def warn_unsandboxed_once() -> None:
    """Emit a single per-process warning explaining that run_shell is
    running unsandboxed. Called from ``tools.run_shell`` whenever
    ``is_sandbox_available()`` is False and bypass wasn't explicitly
    requested. The message distinguishes "bubblewrap not installed"
    from "bubblewrap installed but unusable" so the user knows what
    to fix.
    """
    global _WARNED_NO_BWRAP
    if _WARNED_NO_BWRAP:
        return
    _WARNED_NO_BWRAP = True
    if is_sandbox_installed():
        logger.warning(
            "run_shell is running UNSANDBOXED (bwrap is installed but "
            "cannot create user namespaces — common on hardened distros, "
            "containers without --privileged, or hosts where "
            "user.max_user_namespaces=0). Producer LLMs have full "
            "user-context code execution. To restore the security "
            "boundary, run on a host that permits unprivileged user "
            "namespaces. See SEC-001 in the security audit."
        )
    else:
        logger.warning(
            "run_shell is running UNSANDBOXED (bwrap not found on PATH). "
            "Producer LLMs have full user-context code execution. Install "
            "bubblewrap (apt: bubblewrap, dnf: bubblewrap, pacman: "
            "bubblewrap) to restore the security boundary. See SEC-001 in "
            "the security audit."
        )


# ── Sandbox argv construction ─────────────────────────────────────────────

def _interpreter_binds() -> tuple[Path, ...]:
    """Read-only host paths that MUST be visible inside the sandbox for the
    active Python interpreter to exec.

    The #82 bug: ``--tmpfs /home`` masks the whole home tree to hide the
    operator's secrets, but a project venv lives under home
    (``/home/<user>/<proj>/.venv``). ``run_shell`` rewrites ``python3`` /
    ``pytest`` to ``sys.executable`` (the venv interpreter), so without
    binding the venv back in, ``bwrap`` dies with ``execvp ... .venv ...``.
    Binding ``sys.prefix`` (the venv root) read-only restores the
    interpreter + its site-packages WITHOUT un-masking the rest of home.
    ``sys.base_prefix`` is included for the rare layout where the base
    stdlib also sits under a masked mount; when it's under ``/usr`` (the
    norm) the ``--ro-bind / /`` already covers it and the extra try-bind is
    a harmless no-op.
    """
    binds: list[Path] = []
    seen: set[str] = set()
    for raw in (sys.prefix, sys.base_prefix):
        if not raw:
            continue
        try:
            rp = Path(raw).resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        key = str(rp)
        if key in seen:
            continue
        seen.add(key)
        binds.append(rp)
    return tuple(binds)


def _build_env(pass_env: tuple[str, ...], *, profile: str = "standard") -> dict[str, str]:
    """Construct the env dict to forward into the sandboxed process.
    Starts from the static allowlist plus any safe per-skill pass-through
    names; values are read from the parent's environment. ``profile``
    selects how much capability the child gets (see ``current_profile``);
    the secret deny-list is enforced in EVERY profile that reaches here
    (``off`` never calls this — it runs with the raw parent env).
    """
    out: dict[str, str] = {}
    # Forward allowlisted base names if present in the parent.
    for key in _SAFE_ENV_KEYS:
        val = os.environ.get(key)
        if val is not None:
            out[key] = val
    # Constrain PATH to a safe minimum (don't inherit arbitrary paths).
    out["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    # Defensive Python flags.
    out["PYTHONNOUSERSITE"] = "1"
    # 'standard' blocks accidental installs by poisoning the index; 'trusted'
    # leaves pip functional (network is on there) so an agent can pull a
    # package it legitimately needs — into a writable target, NOT the RO venv.
    if profile != "trusted":
        out["PIP_INDEX_URL"] = "file:///dev/null"
    # Per-skill opt-in pass-through, filtered against the deny patterns.
    for name in pass_env:
        if not _is_safe_env_name(name):
            logger.warning(
                "sandbox: skill requested pass_env=%r but it matches the "
                "deny pattern; dropping for safety.", name,
            )
            continue
        val = os.environ.get(name)
        if val is not None:
            out[name] = val
    return out


def build_sandboxed_argv(
    exec_argv: list[str],
    artifacts_root: Path,
    *,
    allow_network: bool | None = None,
    pass_env: tuple[str, ...] | None = None,
    extra_binds: tuple[Path, ...] = (),
    profile: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Wrap ``exec_argv`` in a ``bwrap ... -- exec_argv`` invocation.

    Returns ``(wrapped_argv, env_dict)``. Caller passes both to
    ``subprocess.run(argv, env=env_dict, ...)``. The env dict is
    intentionally returned separately so the caller can pass it as
    ``env=`` rather than relying on inheritance.

    ``allow_network`` and ``pass_env`` default to the contextvars set
    by the orchestrator before invoking the tool. Passing explicit
    values overrides the contextvars (useful for tests).

    ``extra_binds`` lets the caller request additional read-only mounts
    inside the sandbox — used to give pytest access to the project's
    test tree and venv site-packages.

    ``profile`` selects the operator posture (see ``current_profile``);
    ``None`` reads the ``MODULATIO_SANDBOX_PROFILE`` env. ``trusted``
    forces network on (an agent that needs to fetch shouldn't be blocked)
    while keeping the secret-stripping floor; ``off`` is handled by the
    caller (no bwrap) and never reaches here. The active interpreter/venv
    is ALWAYS bound read-only so code execs in every profile (the #82 fix).
    """
    if profile is None:
        profile = current_profile()
    if allow_network is None:
        allow_network = allow_network_var.get()
    if pass_env is None:
        pass_env = pass_env_var.get()
    # 'trusted' = don't hamper the agents: network on regardless of the
    # per-skill flag. The secret floor (_build_env deny-list) still holds.
    if profile == "trusted":
        allow_network = True

    artifacts_root_str = str(artifacts_root.resolve())

    bwrap: list[str] = [
        "bwrap",
        # Read-only host filesystem
        "--ro-bind", "/", "/",
        # /proc and /dev need to be live
        "--proc", "/proc",
        "--dev", "/dev",
        # Writable tmpfs for /tmp and /home (sandboxed homedir)
        "--tmpfs", "/tmp",
        "--tmpfs", "/home",
        # The producer's only writable host path: the project's
        # artifacts root
        "--bind", artifacts_root_str, artifacts_root_str,
        # Namespace isolation
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-cgroup-try",
        "--die-with-parent",
        "--new-session",
    ]
    if not allow_network:
        bwrap += ["--unshare-net"]
    # #82 fix: bind the active interpreter/venv back in AFTER the --tmpfs
    # /home that masks it, so python3/pytest actually exec. ro-bind-try is
    # idempotent + safe when the path is already covered by --ro-bind / /.
    # Caller-supplied extra_binds (e.g. a project test tree) come after.
    for bind_path in (*_interpreter_binds(), *extra_binds):
        bp = str(Path(bind_path).resolve())
        bwrap += ["--ro-bind-try", bp, bp]
    # End of bwrap flags; everything after `--` is the child argv.
    bwrap += ["--"]
    bwrap += list(exec_argv)
    env = _build_env(pass_env, profile=profile)
    return bwrap, env


# ── Context manager helpers (orchestrator-side) ───────────────────────────


class _SkillContextScope:
    """Set ``allow_network_var`` + ``pass_env_var`` for the duration of
    a skill's tool calls. Resets on exit even if the call raises.

    Use as a context manager in the orchestrator's chat-loop wrapper::

        with sandbox.skill_context(needs_network=skill.needs_network,
                                    pass_env=skill.pass_env):
            run_chat_loop(...)
    """

    def __init__(
        self,
        *,
        needs_network: bool = False,
        pass_env: tuple[str, ...] = (),
    ):
        self._needs_network = needs_network
        self._pass_env = pass_env
        self._tokens: tuple[contextvars.Token[bool], contextvars.Token[tuple[str, ...]]] | None = None

    def __enter__(self) -> "_SkillContextScope":
        net_token = allow_network_var.set(self._needs_network)
        env_token = pass_env_var.set(self._pass_env)
        self._tokens = (net_token, env_token)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._tokens is not None:
            net_token, env_token = self._tokens
            allow_network_var.reset(net_token)
            pass_env_var.reset(env_token)
            self._tokens = None


def skill_context(
    *,
    needs_network: bool = False,
    pass_env: tuple[str, ...] = (),
) -> _SkillContextScope:
    """Context manager binding sandbox settings for the duration of a
    skill's tool calls. See ``_SkillContextScope`` for usage.
    """
    return _SkillContextScope(needs_network=needs_network, pass_env=pass_env)


__all__ = [
    "allow_network_var",
    "pass_env_var",
    "build_sandboxed_argv",
    "current_profile",
    "is_bypass_requested",
    "is_sandbox_available",
    "is_sandbox_installed",
    "is_sandbox_required",
    "reset_sandbox_probe_cache",
    "skill_context",
    "warn_unsandboxed_once",
    "VALID_SANDBOX_PROFILES",
]
