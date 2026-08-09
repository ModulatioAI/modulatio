# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Bubblewrap-based sandbox layer for ``run_shell``.

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
import enum
import logging
import os
import re
import shutil
import subprocess
import sys
import time
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


def canonical_profile(raw: object) -> str:
    """The profile spelling the engine actually acts on.

    THE one canonicalizer: every guard that decides whether a profile value
    is permitted must compare against this, because this is what decides
    what the sandbox does. A guard that canonicalizes differently from the
    consumer is not a guard — ``"OFF"`` and ``" off "`` reach the same
    runtime decision as ``"off"``. Non-string values carry no spelling and
    canonicalize to the empty string (the caller then falls back)."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def current_profile() -> str:
    """Return the active sandbox profile from ``MODULATIO_SANDBOX_PROFILE``.

    Unset / empty → ``standard``. An unrecognized value falls back to
    ``standard`` (fail-safe: an operator typo must NOT silently widen the
    sandbox) and warns once per bad value.
    """
    raw = canonical_profile(os.environ.get(_SANDBOX_PROFILE_ENV, ""))
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
#:
#: #84: ``PWD`` is deliberately NOT forwarded. The child runs with its own
#: ``cwd`` (the caller passes ``cwd=`` to ``subprocess.run`` — the validated
#: artifacts dir), so forwarding the *parent's* ``PWD`` would inject a stale
#: value that disagrees with the real working directory AND leaks the
#: parent's path into the sandbox. POSIX shells regenerate ``$PWD`` on the
#: first ``cd`` and ``getcwd()``-based tools read the kernel cwd directly, so
#: an absent ``PWD`` is correct where a stale one is a bug.
_SAFE_ENV_KEYS: frozenset[str] = frozenset({
    "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
})

#: Pattern-based deny list. Defense in depth: even if a skill's
#: ``pass_env`` mistakenly includes one of these names, the sandbox
#: still strips it. Any env var matching one of these regexes is
#: considered sensitive and never crosses the boundary.
#: The original list was suffix/prefix-specific and missed
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
    # The old rules were exact-suffix (`_API_KEY$` / `_KEY$`)
    # and let secret-shaped names slip through when the `KEY` lacked that exact
    # `_KEY` shape — ``MISTRAL_APIKEY`` (no separator), ``DEPLOY_KEYS`` (plural),
    # ``API-KEY`` (hyphen), ``SOMEKEY`` (bare suffix). Broadened to a
    # word-ending ``KEYS?$`` match plus the hyphen/no-separator ``API[_-]?KEY``
    # shape. Over-denying a non-secret var is the documented safe direction;
    # a prefix ``KEY`` (KEYBOARD_LAYOUT) or mid-word KEY (MONKEYPATCH) is left
    # alone because it isn't a key-suffix shape.
    re.compile(r"API[_-]?KEY", re.IGNORECASE),
    re.compile(r"KEYS?$", re.IGNORECASE),
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
    ``MODULATIO_REQUIRE_SANDBOX=1``.

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


# ── the typed enforcement state ────────────────────────────────────────


class EnforcementState(enum.Enum):
    """The ONE engine-owned sandbox truth . Disclosure and
    dispatch both consume THIS — nothing keys on ``is_sandbox_available()``
    alone. ``profile=off`` / the unsafe bypass never upgrade to FULL; an
    explicit bypass wins over REQUIRE (that env governs only the implicit
    missing-bwrap path — the operator accepted the risk knowingly)."""

    SANDBOXED_FULL = "sandboxed_full"
    DEGRADED_ALLOWLIST = "degraded_allowlist"
    REFUSED = "refused"


#: (probe_ok, probed_at_monotonic) — the SUBSTRATE probe alone, never the
#: policy answer built from it. Probing costs a subprocess, so it is worth
#: caching; the profile, the bypass, and the required-mode flag cost an env
#: read and are re-read for every decision. Caching the ANSWER instead let a
#: state computed while confinement was sealed outlive the setting that made
#: it true — the profile could be switched off, or the bypass set, and the
#: stale answer still reported sealed for the rest of its lifetime.
#: Any bwrap exec failure invalidates immediately.
_POLICY_PROBE_CACHE: "tuple[bool, float] | None" = None
_ENFORCEMENT_TTL_S = 300.0


def _probe_policy_shape() -> bool:
    """Probe the ACTUAL policy shape the empty-root mount uses — tmpfs root,
    runtime ro-binds, the unshare set — never ``bwrap --ro-bind / / true``
    (a host can pass that and still refuse this shape: hardened kernels
    gate mount/namespace combinations independently)."""
    if not is_sandbox_installed():
        return False
    try:
        result = subprocess.run(
            ["bwrap", "--tmpfs", "/", "--ro-bind", "/usr", "/usr",
             "--ro-bind-try", "/bin", "/bin", "--ro-bind-try", "/lib", "/lib",
             "--ro-bind-try", "/lib64", "/lib64",
             "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
             "--unshare-pid", "--unshare-uts", "--unshare-ipc",
             "--unshare-net", "--die-with-parent", "--new-session",
             "--", "/usr/bin/true"],
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def enforcement_state() -> EnforcementState:
    """Compute (or serve cached) the typed enforcement state:

    - ``SANDBOXED_FULL``      — policy probe ok ∧ profile ≠ off ∧ no bypass
    - ``REFUSED``             — sandbox unusable ∧ REQUIRE_SANDBOX (implicit
                                path only; an explicit bypass/off wins)
    - ``DEGRADED_ALLOWLIST``  — everything else (the disclosed soft state)
    """
    global _POLICY_PROBE_CACHE
    # Policy is read fresh; only the probe behind it may be cached.
    explicit_unsafe = is_bypass_requested() or current_profile() == "off"
    if explicit_unsafe:
        return EnforcementState.DEGRADED_ALLOWLIST
    now = time.monotonic()
    if _POLICY_PROBE_CACHE is not None and now - _POLICY_PROBE_CACHE[1] < _ENFORCEMENT_TTL_S:
        probe_ok = _POLICY_PROBE_CACHE[0]
    else:
        probe_ok = _probe_policy_shape()
        _POLICY_PROBE_CACHE = (probe_ok, now)
    if probe_ok:
        return EnforcementState.SANDBOXED_FULL
    return (EnforcementState.REFUSED if is_sandbox_required()
            else EnforcementState.DEGRADED_ALLOWLIST)


def note_bwrap_exec_failure() -> None:
    """A LIVE bwrap invocation failed: drop the cached state so the very
    next read re-probes. The caller must never retry the payload bare —
    it re-reads ``enforcement_state()`` and follows the typed answer."""
    global _POLICY_PROBE_CACHE
    _POLICY_PROBE_CACHE = None


def reset_enforcement_state_cache() -> None:
    """Test seam (mirrors ``reset_sandbox_probe_cache``)."""
    global _POLICY_PROBE_CACHE
    _POLICY_PROBE_CACHE = None


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
            "namespaces."
        )
    else:
        logger.warning(
            "run_shell is running UNSANDBOXED (bwrap not found on PATH). "
            "Producer LLMs have full user-context code execution. Install "
            "bubblewrap (apt: bubblewrap, dnf: bubblewrap, pacman: "
            "bubblewrap) to restore the security boundary."
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


#: /etc entries every exec needs (the dynamic loader + name/UID plumbing).
_ETC_RUNTIME_ALWAYS = (
    "/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d",
    "/etc/alternatives", "/etc/passwd", "/etc/group", "/etc/localtime",
    "/etc/locale.alias",
)
#: /etc entries that only make sense when the child HAS a network — binding
#: them without one would leak host resolver/CA layout for no capability.
_ETC_RUNTIME_NETWORK = (
    "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
    "/etc/ssl", "/etc/ca-certificates", "/etc/pki",
)


def _etc_runtime_binds(allow_network: bool) -> list[str]:
    """The narrow /etc allowlist for the empty-root mount: loader +
    identity files always; resolver/CA files only with network. Everything
    is ``--ro-bind-try`` — absence on a given distro is fine, presence of
    anything NOT listed here is impossible (the root is tmpfs)."""
    entries = list(_ETC_RUNTIME_ALWAYS)
    if allow_network:
        entries += _ETC_RUNTIME_NETWORK
    out: list[str] = []
    for p in entries:
        out += ["--ro-bind-try", p, p]
    return out


def _sized_tmpfs(dest: str, size: "int | None") -> list[str]:
    """A ``--tmpfs DEST`` mount, capped to ``size`` bytes when given
    (``--size BYTES`` precedes the ``--tmpfs`` it sizes in bwrap ≥0.6)."""
    if size is not None:
        return ["--size", str(int(size)), "--tmpfs", dest]
    return ["--tmpfs", dest]


def _rw_root_binds(roots: "tuple[Path, ...]") -> list[str]:
    """``--bind`` (writable) pairs for each operator-granted exec root."""
    out: list[str] = []
    for r in roots:
        rs = str(Path(r).resolve())
        out += ["--bind", rs, rs]
    return out


def build_sandboxed_argv(
    exec_argv: list[str],
    artifacts_root: Path,
    *,
    allow_network: bool | None = None,
    pass_env: tuple[str, ...] | None = None,
    extra_binds: tuple[Path, ...] = (),
    extra_rw_roots: tuple[Path, ...] = (),
    profile: str | None = None,
    tmpfs_size: "int | None" = None,
    status_fd: "int | None" = None,
) -> tuple[list[str], dict[str, str]]:
    """Wrap ``exec_argv`` in a ``bwrap ... -- exec_argv`` invocation.

    ``status_fd``, when given, becomes bwrap's ``--json-status-fd``: bwrap
    reports the started child on it, so the caller can distinguish a
    sandbox SETUP failure (no child ever started — engine) from the payload
    exiting nonzero (product). The caller owns the fd and must pass it
    through ``Popen(pass_fds=...)``.

    Returns ``(wrapped_argv, env_dict)``. Caller passes both to
    ``subprocess.run(argv, env=env_dict, ...)``. The env dict is
    intentionally returned separately so the caller can pass it as
    ``env=`` rather than relying on inheritance.

    ``allow_network`` and ``pass_env`` default to the contextvars set
    by the orchestrator before invoking the tool. Passing explicit
    values overrides the contextvars (useful for tests).

    ``extra_binds`` lets the caller request additional read-only mounts
    inside the sandbox. It is currently an UNUSED hook: the sole production
    caller (``tools.run_shell``) never passes it. pytest already sees the
    venv/site-packages via ``_interpreter_binds(sys.prefix)`` and the project
    test tree via the read-only ``--ro-bind / /``, so no extra mount is
    needed today. The parameter is kept (default ``()``) for a future caller
    that genuinely needs an extra writable/visible path; passing values still
    adds ``--ro-bind-try`` mounts.

    ``profile`` selects the operator posture (see ``current_profile``);
    ``None`` reads the ``MODULATIO_SANDBOX_PROFILE`` env. ``trusted``
    forces network on (an agent that needs to fetch shouldn't be blocked)
    while keeping the secret-stripping floor; ``off`` is handled by the
    caller (no bwrap) and never reaches here. The active interpreter/venv
    is ALWAYS bound read-only so code execs in every profile.
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
        # EMPTY root: the child sees
        # NOTHING of the host it isn't explicitly bound. `--ro-bind / /`
        # exposed /etc, /opt, /mnt, /media, /srv, host /var and /run —
        # pathname sockets, credentials readable by EXECUTED code that the
        # file tools deliberately refuse, service discovery. Gone.
        "--tmpfs", "/",
        # The runtime needed to execute host tools, read-only. /bin, /lib*
        # and /sbin are symlinks into /usr on merged-usr distros — bind-try
        # covers both worlds.
        "--ro-bind", "/usr", "/usr",
        "--ro-bind-try", "/bin", "/bin",
        "--ro-bind-try", "/sbin", "/sbin",
        "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        # Narrowly selected /etc runtime files (never the whole /etc): the
        # loader's cache/conf, the alternatives symlink farm (Debian routes
        # python3 through it), user/group lookup, timezone.
        *_etc_runtime_binds(allow_network),
        # /proc and /dev need to be live
        "--proc", "/proc",
        "--dev", "/dev",
        # Writable tmpfs for /tmp and /home (sandboxed homedir); /run and
        # /var EXIST (tools expect them) but are EMPTY — no host pathname
        # socket or state is visible by default . ``tmpfs_size``
        # (bytes) caps each so a hostile hook can't flood engine memory
        # through the in-sandbox tmpfs (per-mount, kernel-
        # enforced) — None leaves them uncapped (run_shell's posture).
        *_sized_tmpfs("/tmp", tmpfs_size),
        *_sized_tmpfs("/home", tmpfs_size),
        *_sized_tmpfs("/run", tmpfs_size),
        *_sized_tmpfs("/var", tmpfs_size),
        # The producer's only writable host path: the project's
        # artifacts root
        "--bind", artifacts_root_str, artifacts_root_str,
        # Operator-granted exec roots (exec-widen): writable so commands can
        # build/test in a real project folder. The grant is the operator's
        # explicit, sandbox-gated decision; the cheat-guard already refuses a
        # root overlapping the swarm deliverable tree.
        *_rw_root_binds(extra_rw_roots),
        # Namespace isolation + child hygiene (explicit, per the R3 pins)
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-cgroup-try",
        "--cap-drop", "ALL",
        "--die-with-parent",
        "--new-session",
    ]
    if not allow_network:
        bwrap += ["--unshare-net"]
    # #82 fix: bind the active interpreter/venv back in AFTER the tmpfs
    # mounts that mask it, so python3/pytest actually exec. Under the empty
    # root these binds are LOAD-BEARING for any venv outside /usr.
    # Caller-supplied extra_binds (e.g. a project test tree) come after.
    for bind_path in (*_interpreter_binds(), *extra_binds):
        bp = str(Path(bind_path).resolve())
        bwrap += ["--ro-bind-try", bp, bp]
    # Seal the skeleton: the root tmpfs would otherwise be writable (a child
    # could scribble a fake /etc into the throwaway root). Remounted ro LAST
    # so it covers the assembled tree; the explicit RW binds (artifacts,
    # granted roots) and the tmpfs workdirs (/tmp /home /run /var) are their
    # own mounts and stay writable.
    bwrap += ["--remount-ro", "/"]
    if status_fd is not None:
        bwrap += ["--json-status-fd", str(status_fd)]
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
