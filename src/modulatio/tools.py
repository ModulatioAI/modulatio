# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Tool registry and builtin tools (slice #9e + run_shell phase 1).

Modulatio's "tool-using producers" are skills whose executor is a
Python callable instead of an LLM. The orchestrator resolves a skill's
declared tool by name from a registry, calls it with the task's
``tool_args``, and uses the returned string as the artifact body. QC
still runs — a tool that returns the wrong content fails verification
the same way a weak LLM draft does.

Business-harness level: no opinion about the business. HTTP fetches,
shell probes, filesystem ops, DB queries — whatever tool a project's
skills declare. This module ships two generic builtins:

- ``http_get`` — smallest-footprint demonstration that a producer can
  succeed without an LLM.
- ``run_shell`` — argv-based, allowlisted shell runner for code-
  verification skills (drafter smoke tests, QC execution checks).

Other tools are added by merging into the registry dict the CLI
passes into :class:`Orchestrator`.

Scope of run_shell phase 1:
- Two profiles (``passive`` / ``full``) selecting an argv allowlist.
- ``shell=False`` argv parsing (no metacharacter expansion).
- cwd confined to the project's artifacts root; dotfile components
  refused; cwd must exist.
- Timeout-bounded; output truncated at a fixed byte cap.
- Tool factory closes over the artifacts root — there is no global
  default. ``build_registry`` only adds ``run_shell`` when a root is
  supplied (project opt-in by construction).

Out of scope (later phases / slices):
- LLM-driven tool selection (OpenAI function-calling schema).
- Multi-step tool chains inside a single task.
- Per-project enabled flag in config (today's surface is opt-in via
  the registry factory; explicit project config gating is phase 2).
- Tool result streaming.
"""

from __future__ import annotations

import html
import ipaddress
import os
import re
import shlex
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from urllib.error import HTTPError


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow 3xx redirects. The SSRF guard on the original
    URL doesn't carry over to a server-supplied ``Location`` header —
    a producer-controlled URL could pass the initial check, then the
    target server can redirect to ``169.254.169.254`` or ``localhost``
    and urllib's default handler would silently follow. Convert every
    redirect into an ``HTTPError`` so the response surfaces as a
    status-coded body, never as a transparent fetch from the redirect
    target.

    Producers that legitimately need redirect-following can register a
    custom tool that validates each Location header before following.
    """

    def http_error_301(self, req, fp, code, msg, headers):  # noqa: D401
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


_no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler())

#: A polite, identifying User-Agent. Without one, courteous sources (Wikipedia,
#: many news/docs sites) reject the request with 403 — which silently starved
#: research and pushed producers back onto stale training-data claims. An
#: identifying UA that names the project + a contact URL is the well-behaved
#: default for a research fetcher.
_HTTP_USER_AGENT = (
    "ModulatioResearchBot/0.2 (+https://modulatio.ai; respects robots) "
    "python-urllib"
)


def _urlopen(url, timeout=None):
    """Production HTTP fetch routed through the no-redirect opener, with a
    polite identifying User-Agent (see :data:`_HTTP_USER_AGENT`). Function name
    preserved (separate from ``urllib.request.urlopen``) so tests can
    monkeypatch this surface to inject canned responses.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _HTTP_USER_AGENT})
    return _no_redirect_opener.open(req, timeout=timeout)


@dataclass(frozen=True)
class Tool:
    """A named callable that produces artifact text.

    - ``name``: registry key. Matches ``Skill.tool_loadout[0]`` for
      MVP single-tool skills.
    - ``description``: human-readable summary for audit + the
      function-calling schema description (Phase 2A).
    - ``call``: the actual implementation. Accepts arbitrary kwargs
      (matches ``Task.tool_args`` for executor=tool skills, or LLM-
      proposed kwargs for executor=llm + tool_loadout skills) and
      returns a string (the artifact body or, in the LLM-with-tools
      path, the tool-result message text).
    - ``params_schema``: optional JSONSchema for the ``call`` kwargs.
      Fed to the LLM via the function-calling schema in Phase 2A.
      Tools without a schema get a permissive ``{type: object}``
      default — the LLM can still pass any args, just without
      documented shape.
    """

    name: str
    description: str
    call: Callable[..., str]
    params_schema: dict | None = None
    #: Cost tier for the metered-tool tier (Part B4). ``None`` / ``"free-local"``
    #: = unmetered, runs freely (the default — ddgs web_search, http_get, run_shell,
    #: every builtin). ``"paid-cloud"`` / ``"premium-cloud"`` = METERED: the engine
    #: gates each call through ``comptroller.authorize_metered_tool`` BEFORE the
    #: call (fail-closed on unknown/missing budget, per-task-capped, idempotent).
    #: A metered tool MUST take narrow params (artifact ids / pinned paths, never an
    #: arbitrary URL/body/endpoint), read only its own key from env, and never
    #: return secrets to the model. See ``comptroller.authorize_metered_tool``.
    cost_class: str | None = None


# ── builtin: http_get ──────────────────────────────────────────────────────

_DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0

# A single fetch must never be able to blow a producer's role context
# budget. Two ceilings, defence-in-depth:
#   * read ceiling — never pull more than this off the socket, so a
#     multi-GB endpoint can't OOM the orchestrator before we even cap.
#   * return ceiling — after HTML→text extraction, the returned body is
#     truncated to this many chars (~8k tokens — half the smallest
#     producer budget). The PIANO discipline: small bounded context; one
#     raw web page (we measured a live 1.24M-char fetch) is not allowed
#     to become 310k tokens in a 16k window.
_HTTP_GET_READ_LIMIT_BYTES = 4 * 1024 * 1024  # 4 MiB socket-read ceiling
_HTTP_GET_MAX_CHARS = 32_000                  # ~8k tokens returned ceiling

# Crude, dependency-free HTML→text (no bs4/lxml/html2text in the venv).
# Not a parser — just enough to turn a markup firehose into the prose a
# producer actually wants, at a fraction of the tokens.
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|noscript|template)\b.*?</\1\s*>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n[ \t]*\n[ \t]*(?:\n[ \t]*)+")


def _html_to_text(body: str) -> str:
    """Strip script/style blocks, drop tags, unescape entities, collapse
    whitespace. Lossy by design — we want readable content, not fidelity."""
    text = _SCRIPT_STYLE_RE.sub(" ", body)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _INLINE_WS_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _looks_like_html(content_type: str, body: str) -> bool:
    """HTML if the Content-Type says so; if a non-HTML type is declared
    (json/plain/xml) trust it and DON'T strip (would corrupt JSON). With
    no declared type, sniff the first bytes for an HTML signature."""
    ct = content_type.lower()
    if "html" in ct:
        return True
    if ct.strip():
        return False
    head = body[:512].lstrip().lower()
    return (
        head.startswith("<!doctype html")
        or head.startswith("<html")
        or "<head" in head
        or "<body" in head
    )


def _cap_http_body(text: str, *, over_read: bool) -> str:
    """Truncate to the return ceiling (chars), noting the dropped amount
    and whether the read itself hit the socket ceiling."""
    if len(text) <= _HTTP_GET_MAX_CHARS and not over_read:
        return text
    dropped = max(0, len(text) - _HTTP_GET_MAX_CHARS)
    note = f"{dropped} more chars" if dropped else "read ceiling reached"
    if over_read and dropped:
        note += "; response exceeded the 4 MiB read ceiling"
    elif over_read:
        note = "response exceeded the 4 MiB read ceiling"
    return text[:_HTTP_GET_MAX_CHARS] + f"\n... [truncated, {note}]"


# Cloud-metadata + private-host names worth blocking by name. IPs in
# private ranges are caught by the IP-classification check below, but
# DNS for these names can also resolve to public IPs that proxy back
# to the metadata service, so name-blocking is belt-and-suspenders.
_BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
    "metadata",
    "instance-data",
    "instance-data.ec2.internal",
})


def _check_url_safe_for_http_get(url: str) -> None:
    """Refuse URLs targeting private / loopback / cloud-metadata endpoints.

    Producer LLMs control the URL passed to ``http_get``; without this
    guard a partially-adversarial producer can reach the cloud metadata
    service (169.254.169.254 — leaks IAM creds), localhost services
    (Ollama / LM Studio with sensitive prompts), or RFC1918 ranges
    (internal HTTP services never meant to be agent-reachable).

    Scope:
    - Reject literal-IP URLs whose IP is loopback / link-local / private /
      multicast / reserved / unspecified.
    - Reject hostnames whose A/AAAA records resolve to any such IP.
    - Reject specific cloud-metadata hostnames by name.
    - DNS failures are NOT a rejection — let urllib surface the real
      error so the producer sees an actionable message.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"http_get requires a hostname; got {url!r}")
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise ValueError(
            f"http_get refuses metadata-style hostname {hostname!r} "
            f"(cloud IAM exfiltration vector)"
        )
    # Literal IP in the hostname slot — short-circuit DNS.
    try:
        direct_ip = ipaddress.ip_address(hostname)
    except ValueError:
        direct_ip = None
    if direct_ip is not None:
        _refuse_if_internal_ip(direct_ip, hostname)
        return
    # Resolve and check every returned address. A hostile name could
    # round-robin to mixed public + private IPs; reject if ANY is
    # internal.
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError):
        return  # unresolvable — let urllib surface the real error
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        if addr in seen:
            continue
        seen.add(addr)
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        _refuse_if_internal_ip(ip, hostname)


def _refuse_if_internal_ip(ip: ipaddress._BaseAddress, hostname: str) -> None:
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise ValueError(
            f"http_get refuses {hostname!r} — resolves to {ip} "
            f"(private/loopback/link-local/metadata range)"
        )


def http_get(url: str, timeout: float = _DEFAULT_HTTP_TIMEOUT_SECONDS) -> str:
    """Fetch ``url`` via HTTP(S) GET and return the response body as text.

    Safety:
    - Only ``http://`` / ``https://`` schemes pass the scheme gate.
      Mis-specified ``file://`` / ``ftp://`` / etc. raise ValueError
      rather than silently reading local files or hitting unexpected
      backends. Users who need other schemes register a custom tool.
    - SSRF guard: refuses URLs resolving to loopback / link-local /
      private / multicast / metadata-service ranges. See
      ``_check_url_safe_for_http_get`` for full rationale.
    - Default timeout prevents stalled endpoints from hanging the
      orchestrator indefinitely.

    On non-2xx status, the returned body carries the status code and
    reason so QC can detect the failure via the artifact content.
    Network errors propagate as exceptions — the orchestrator's redo
    loop treats them as producer failures (same as any other raised
    exception on a producer call).

    Size: HTML responses are reduced to readable text (script/style/tags
    stripped), and every response is capped at the return ceiling
    (``_HTTP_GET_MAX_CHARS``) with a truncation marker — a single page
    must not be able to blow a producer's role context budget.
    """
    timeout = _clamp_timeout(
        timeout, lo=_HTTP_MIN_TIMEOUT_SECONDS, hi=_HTTP_MAX_TIMEOUT_SECONDS,
        default=_DEFAULT_HTTP_TIMEOUT_SECONDS,
    )  # SEC-04
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"http_get only accepts http(s) URLs; got scheme={parsed.scheme!r}"
        )
    _check_url_safe_for_http_get(url)
    try:
        with _urlopen(url, timeout=timeout) as resp:
            # Bounded read: pull at most the ceiling + 1 byte so we can
            # detect (and flag) an over-large body without buffering it all.
            raw = resp.read(_HTTP_GET_READ_LIMIT_BYTES + 1)
            content_type = ""
            headers = getattr(resp, "headers", None)
            if headers is not None:
                content_type = headers.get("Content-Type", "") or ""
        over_read = len(raw) > _HTTP_GET_READ_LIMIT_BYTES
        body = raw[:_HTTP_GET_READ_LIMIT_BYTES].decode("utf-8", errors="replace")
        if _looks_like_html(content_type, body):
            body = _html_to_text(body)
        return _cap_http_body(body, over_read=over_read)
    except HTTPError as err:
        # Surface the status in the body so QC reads the failure from
        # the artifact rather than treating it as a passing response.
        body = ""
        try:
            body = err.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover — defensive
            pass
        return f"HTTP {err.code} {err.reason}\n\n{_cap_http_body(body, over_read=False)}"


# ── builtin: run_shell (phase 1) ───────────────────────────────────────────
#
# Safety surface for code-verification skills. Two profiles select an
# argv allowlist:
#
# - ``passive``: read-only / parse-only checks a drafter can run on
#   freshly produced code without actually executing it. Imports
#   probe ``module exists``; ``--help`` invokes argparse parsing; ruff
#   / mypy / pyflakes lint without running.
# - ``full``: passive + actual program execution + pytest + bash
#   scripts. The QC profile.
#
# Allowlists check parsed argv rather than regex on raw cmd — easier
# to reason about, and side-steps the "did my regex really anchor"
# class of bugs.

_PYTHON_BINS = ("python", "python3", "python3.11", "python3.12")
_DEFAULT_RUN_SHELL_TIMEOUT_SECONDS = 30.0
_RUN_SHELL_MAX_OUTPUT_BYTES = 8000

# Security audit SEC-04 (Nemo): the model/tool caller supplies `timeout`. Left
# unclamped, a hostile model can pass a huge (or non-finite) value to tie up a
# worker/turn far past the intended bound. Clamp to a sane range and reject
# NaN/inf — the ceilings are generous enough for legitimate QC builds / fetches.
_RUN_SHELL_MIN_TIMEOUT_SECONDS = 0.1
_RUN_SHELL_MAX_TIMEOUT_SECONDS = 600.0
_HTTP_MIN_TIMEOUT_SECONDS = 1.0
_HTTP_MAX_TIMEOUT_SECONDS = 30.0


def _clamp_timeout(value: object, *, lo: float, hi: float, default: float) -> float:
    """Coerce a caller-supplied timeout into ``[lo, hi]``; a non-numeric or
    non-finite value (NaN/inf) falls back to ``default``."""
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):  # NaN / ±inf
        return default
    return max(lo, min(hi, v))

# Security audit H3a — resource ceilings applied to every run_shell child
# (and inherited by its sandboxed grandchildren). bwrap confines the
# filesystem + network but does NOT bound memory / disk / core dumps, so a
# skill that opted into run_shell could exhaust them from inside the sandbox.
# Ceilings are generous (real tools — pandoc, ffmpeg, python probes — fit
# comfortably) and only ever LOWERED, never raised, to avoid EPERM. CPU is
# intentionally bounded by the wall-clock timeout + process-group kill below
# rather than RLIMIT_CPU (which false-kills legitimate parallel builds);
# RLIMIT_NPROC is omitted deliberately — without a user-namespace UID remap it
# is enforced per real-UID and would refuse to fork when the host is already
# busy (a future hardening once the sandbox remaps the UID).
_RUN_SHELL_RLIMIT_AS_BYTES = 4 * 1024**3      # 4 GiB address space (mem bomb)
_RUN_SHELL_RLIMIT_FSIZE_BYTES = 2 * 1024**3   # 2 GiB max single file (disk fill)
# Cap the post-timeout pipe drain so a re-parented double-forking grandchild
# holding the pipes open can't hang run_shell after the process-group kill.
_RUN_SHELL_DRAIN_TIMEOUT = 5.0


def _apply_rlimits_to_pid(pid: int) -> None:
    """Clamp a just-spawned run_shell child's address space / file size / core
    dump FROM THE PARENT via ``prlimit`` (Linux) — NOT a ``preexec_fn``.

    A ``preexec_fn`` runs Python in the forked child between ``fork()`` and
    ``exec()``; in a multithreaded process (the concurrent-wave workers, or any
    caller on a worker thread) a lock another thread holds at fork time can
    deadlock the child — and that wedged the PARENT's own thread creation in
    turn, hanging the process (0.9.0 full-suite hang). ``prlimit`` from the
    parent has no fork hazard, so ``run_shell`` is safe to call from a thread.

    Best-effort + Linux-only: a non-Linux host (no ``resource.prlimit``) or an
    already-exited child simply skips — the bwrap sandbox + the wall-clock
    timeout + process-group kill remain the primary bounds. The cap lands within
    microseconds of spawn (long before a child could allocate gigabytes), so the
    sub-millisecond window before it applies is immaterial for a coarse cap. Each
    limit is only LOWERED (``min(target, current_hard)``) — never raised — so an
    unprivileged target never hits EPERM."""
    try:
        import resource
    except ImportError:
        return
    prlimit = getattr(resource, "prlimit", None)
    if prlimit is None:  # not Linux (macOS/CI dev fallback — already degraded)
        return

    def _clamp(which: int, target: int) -> None:
        try:
            soft, hard = prlimit(pid, which)
        except (OSError, ValueError):  # child gone, EPERM, or unknown resource
            return
        new_hard = target if hard == resource.RLIM_INFINITY else min(hard, target)
        new_soft = new_hard if soft == resource.RLIM_INFINITY else min(soft, new_hard)
        try:
            prlimit(pid, which, (new_soft, new_hard))
        except (OSError, ValueError):
            pass

    _clamp(resource.RLIMIT_AS, _RUN_SHELL_RLIMIT_AS_BYTES)
    _clamp(resource.RLIMIT_FSIZE, _RUN_SHELL_RLIMIT_FSIZE_BYTES)
    _clamp(resource.RLIMIT_CORE, 0)


def _prlimit_wrapper_prefix() -> list[str]:
    """The ``prlimit`` argv prefix that applies the run_shell resource caps
    to the *payload itself* at exec time, or ``[]`` when ``prlimit`` is
    unavailable (non-Linux, util-linux absent).

    H3a re-sweep: the parent-side ``_apply_rlimits_to_pid`` clamps the PID
    that ``Popen`` returns. On the UNSANDBOXED path that is the payload and
    the clamp lands. On the SANDBOXED (production) path that PID is the
    ``bwrap`` MONITOR — bwrap then forks/execs the real payload into its own
    PID namespace as a child, and RLIMIT_AS/FSIZE/CORE (per-process, inherited
    only at fork time) set on the monitor AFTER that fork never reach the
    payload. So a memory/disk bomb inside the sandbox — exactly the posture
    H3a exists to bound — went uncapped.

    Prefixing the payload argv with ``prlimit --as --fsize --core -- <argv>``
    sets the limits in the child process via a single ``execve`` (no Python
    ``preexec_fn`` — that is the fork-from-multithreaded deadlock that hung the
    0.9.0 suite). When the prefix is wrapped BEFORE the bwrap argv it lands
    INSIDE the sandbox (after bwrap's ``--``), so the limits are established on
    the payload's own PID before it can allocate. ``prlimit`` unprivileged can
    only LOWER limits, matching the never-raise contract. ``prlimit`` is bound
    read-only into the sandbox by ``--ro-bind / /``.

    Belt-and-suspenders: the parent-side clamp stays (it still bounds the
    direct child on the unsandboxed path and the monitor on the sandboxed
    one); this prefix is the suspenders that actually reach the payload.
    """
    import shutil

    prlimit_bin = shutil.which("prlimit")
    if prlimit_bin is None:  # non-Linux / util-linux not installed
        return []
    # Single value sets both soft and hard; prlimit lowers only, unprivileged.
    return [
        prlimit_bin,
        f"--as={_RUN_SHELL_RLIMIT_AS_BYTES}",
        f"--fsize={_RUN_SHELL_RLIMIT_FSIZE_BYTES}",
        "--core=0",
        "--",
    ]


def _is_safe_relative_file_arg(arg: str) -> bool:
    """True iff ``arg`` is a relative path with no dotfile components
    and no traversal segments. Used to guard the file arguments to
    ``cat``, ``head``, ``ls``, and ``py_compile`` — the cwd is already
    confined to the artifacts root, but the file arg itself can still
    escape via ``../../etc/passwd`` if not validated here.

    Allows: ``file.py``, ``subdir/file.py``.
    Refuses: absolute paths, ``..``, anything starting with ``.``.
    """
    if not arg:
        return False
    if arg.startswith("/") or arg.startswith("\\"):
        return False
    parts = arg.replace("\\", "/").split("/")
    for p in parts:
        if not p or p.startswith(".") or p == "..":
            return False
    return True


def _is_safe_file_arg(arg: str, root: Path | None, extra_roots=()) -> bool:
    """Same intent as :func:`_is_safe_relative_file_arg` but accepts
    absolute paths IF they resolve under ``root`` (the artifacts dir)
    with no dotfile components.

    Surfaced in an end-to-end test where QC tried ``cat
    <absolute-path-inside-artifacts-root>/file.py`` and got refused.
    Models often write absolute paths when they have one in context —
    there's no safety reason to reject them as long as the resolved
    path is inside the cwd.

    ``root=None`` falls back to relative-only (legacy / tests that
    invoke checks without a root). Production paths always supply
    artifacts_root.
    """
    if not arg:
        return False
    # A bare ``-`` means STDIN to cat/head/tail/grep/wc — never a legitimate
    # file in the confined producer context (stdin is /dev/null there), and it
    # would otherwise slip past as a "file" arg. Reject it (Nemo hull note,
    # 2026-05-31).
    if arg == "-":
        return False
    # A leading dash is a flag, not a file. The passive ls/cat/head/tail
    # branches validate their file args purely through this helper, so a
    # token like ``-R`` / ``--color=always`` / ``-A`` would otherwise slip
    # through as a "file" and re-admit recursive / behavior-changing flags
    # the per-head allowlists are meant to gate. Dedicated flag positions
    # (e.g. ``head -n``, ``head -5``) are checked with their own
    # ``startswith('-')`` guards before this helper sees the FILE arg, so
    # rejecting dash-leading args here costs no legitimate read form.
    if arg.startswith("-"):
        return False
    if arg.startswith("/") or arg.startswith("\\"):
        if root is None and not extra_roots:
            return False
        try:
            resolved = Path(arg).resolve()
        except (ValueError, OSError):
            return False
        # Safe if it resolves under the primary root OR any granted extra_root
        # (exec-widen), with the SAME dotfile secret-floor on every root — a
        # `.env`/`.ssh` BELOW the matched root stays refused (Nemo #2).
        roots = [root] if root is not None else []
        roots += list(extra_roots)
        for r in roots:
            try:
                rel = resolved.relative_to(Path(r).resolve())
            except (ValueError, OSError):
                continue
            if any(p.startswith(".") for p in rel.parts):
                return False  # dotfile component under the matched root
            return True
        return False
    return _is_safe_relative_file_arg(arg)


def _is_safe_go_target(arg: str, root: Path | None, extra_roots=()) -> bool:
    """True iff ``arg`` is a Go vet target that cannot leak a file outside
    the artifacts root. Go package patterns (``.``, ``./...``,
    ``./pkg/...``) legitimately begin with ``.`` and address the confined
    cwd, so they are accepted. An absolute path or any ``..`` traversal
    segment can escape ``root`` (go vet quotes offending source lines in
    its diagnostics), so those are refused.
    """
    if not arg:
        return False
    # Flag-style args. A blanket accept here is a hole: ``go vet`` honors
    # ``-vettool=<binary>`` (which makes go vet EXECUTE that binary — a direct
    # violation of the no-execution passive contract) and ``-flag=<path>``
    # shapes whose value is a path that can leak source outside the root.
    if arg.startswith("-"):
        # ``-vettool`` (and any ``-vettool=...``) execs an arbitrary binary —
        # never passive. Refuse outright.
        bare = arg.split("=", 1)[0]
        if bare in ("-vettool", "--vettool"):
            return False
        # ``-flag=value`` — confine the value like a positional target so a
        # path payload (e.g. ``-tags=../../etc``) can't escape the root. A
        # value-less flag (``-json``) carries no path and is accepted.
        if "=" in arg:
            value = arg.split("=", 1)[1]
            return _is_safe_go_target(value, root, extra_roots)
        return True
    if arg.startswith("/") or arg.startswith("\\"):
        # An absolute path is only safe if it resolves under root.
        return _is_safe_file_arg(arg, root, extra_roots)
    parts = arg.replace("\\", "/").split("/")
    # Reject traversal segments; a lone leading ``.`` (the Go package marker)
    # is fine, but ``..`` would climb out of the confined cwd.
    for p in parts:
        if p == "..":
            return False
    return True


def resolve_under_roots(arg: str, roots: "list[Path]") -> "Path | None":
    """Resolve a caller-supplied relative path and return it ONLY if it lands on
    a real FILE inside one of ``roots`` (after following symlinks). Returns
    ``None`` on anything unsafe: empty / ``-`` / absolute / traversal (``..``) /
    dotfile component / symlink escape / missing / not-a-file.

    This is the security choke point for read-only Leader access to its team's
    outputs (§4 ``read_deliverable``). It is deliberately NOT a widening of the
    ``run_shell`` sandbox: a single, unit-tested, file-only, read-only resolver
    over a fixed set of run-scoped roots is a far smaller attack surface than
    threading a second read root through the whole shell-confinement path. The
    ``relative_to`` check runs AFTER ``resolve()`` so a symlink inside a root
    pointing outside it is rejected (its resolved target is not under the root).
    """
    if not arg or arg == "-":
        return None
    if arg.startswith("/") or arg.startswith("\\"):
        return None
    parts = arg.replace("\\", "/").split("/")
    if any((not p) or p.startswith(".") or p == ".." for p in parts):
        return None
    for root in roots:
        try:
            root_r = root.resolve()
            candidate = (root_r / arg).resolve()
            candidate.relative_to(root_r)  # raises if a symlink escaped the root
            # is_file() (an os.stat) must stay inside the guard — an overlong
            # path raises OSError(ENAMETOOLONG), and the contract is "None on
            # anything unsafe", never a raise.
            if candidate.is_file():
                return candidate
        except (ValueError, OSError):
            continue
    return None


def _check_passive(argv: list[str], root: Path | None = None, extra_roots=()) -> bool:
    """True iff ``argv`` is genuinely no-execution for the passive profile.

    "Passive" means: nothing in ``argv`` causes user-controlled code
    to run. The interpreter / runtime itself starting up is fine; what's
    forbidden is anything that imports a user module, runs a user
    script, or invokes a user-package's argparse handlers (whose
    top-level imports execute before the parser sees ``--help``).

    Tightened 2026-05-04 (audit Wave 2, F1) after the security audit's
    review confirmed the prior allowlist let ``python3 -c 'import X'``
    and ``python3 -m <module> --help/--version`` execute import-time
    code despite the comments calling them "no-execution." Same logic
    applies to ``python file.py --help``, ``node file.js --help``, and
    ``ruby file.rb --help`` — the script's top-level runs before the
    flag is honored. All four shapes were dropped.

    What's still passive:

    - ``--version`` / ``-V`` / ``-v`` / ``-h`` probes for
      ``python``/``node``/``npm``/``ruby``/``bundle`` (interpreter
      startup only; no user code).
    - ``python -m py_compile <file>`` — genuine syntax check; the
      stdlib compiler runs but never executes the user file.
    - ``ruby -c <file>`` — same: syntax check, no execution.
    - ``rubocop <file>`` — read-only linter.
    - The non-Python/Ruby read-only utilities (ls, cat, grep, find,
      etc.) handled by ``_is_passive_simple`` below.
    """
    if not argv:
        return False
    head = argv[0]
    if head in _PYTHON_BINS:
        # python3 --version / -V  (interpreter version; no user code)
        if len(argv) == 2 and argv[1] in ("--version", "-V"):
            return True
        # python3 -m py_compile file.py  (canonical syntax check;
        # py_compile parses + emits .pyc without ever executing the
        # user file's top-level code).
        if (
            len(argv) == 4
            and argv[1] == "-m"
            and argv[2] == "py_compile"
            and argv[3].endswith(".py")
            and _is_safe_file_arg(argv[3], root, extra_roots)
        ):
            return True
        # python3 -c <body>, python3 file.py --help, and
        # python3 -m <module> --version/--help all execute user-
        # controlled top-level code. They belong in `full`, not
        # `passive`. (See module docstring for the F1 rationale.)
    if head == "node":
        # node --version / -v
        if len(argv) == 2 and argv[1] in ("--version", "-v"):
            return True
        # node file.js --help runs file.js's top-level — not passive.
    if head == "npm":
        # npm --version  (no install, no execution; just version probe)
        if len(argv) == 2 and argv[1] in ("--version", "-v"):
            return True
    # ── Ruby ─────────────────────────────────────────────────────────
    if head == "ruby":
        # ruby --version  (interpreter version; no user code)
        if len(argv) == 2 and argv[1] in ("--version", "-v"):
            return True
        # ruby -c file.rb  (canonical syntax check; parses but does not run)
        if (
            len(argv) == 3
            and argv[1] == "-c"
            and argv[2].endswith(".rb")
            and _is_safe_file_arg(argv[2], root, extra_roots)
        ):
            return True
        # ruby file.rb --help runs file.rb's top-level — not passive.
    if head == "bundle":
        # bundle --version  (gemset metadata probe; no install)
        if len(argv) == 2 and argv[1] in ("--version", "-v"):
            return True
    if head == "rubocop":
        # rubocop file.rb [more files]  (linter; read-only)
        if len(argv) >= 2 and all(
            a.endswith(".rb") and _is_safe_file_arg(a, root, extra_roots) for a in argv[1:]
        ):
            return True
    # ── Go ───────────────────────────────────────────────────────────
    if head == "go":
        # go version  (subcommand style — Go uses bare `version`, not --version)
        if len(argv) == 2 and argv[1] in ("version", "--version", "-version"):
            return True
        # go vet [pkg-or-file ...]  (static analyzer, read-only).
        # go vet echoes offending source lines in its diagnostics, so an
        # unconfined path arg is an arbitrary-file read — confine every arg
        # like the other linters (gofmt/ruff/mypy). Go package patterns
        # (``./...``, ``.``, ``./pkg/...``) legitimately start with ``.`` and
        # so are accepted explicitly; only absolute paths and ``..`` traversal
        # (which can escape the artifacts root) are refused.
        if len(argv) >= 2 and argv[1] == "vet":
            if all(_is_safe_go_target(a, root, extra_roots) for a in argv[2:]):
                return True
    if head == "gofmt":
        # gofmt -l <file>.go  (lists files needing reformat; no modification)
        # gofmt -d <file>.go  (shows diff; no modification)
        if len(argv) >= 3 and argv[1] in ("-l", "-d"):
            if all(
                a.endswith(".go") and _is_safe_file_arg(a, root, extra_roots)
                for a in argv[2:]
            ):
                return True
    if head == "ruff":
        # ruff check  /  ruff check <path>...  — read-only lint only. Reject any
        # flag (leading '-'): the passive tier must NOT admit mutating flags like
        # --fix / --fix-only / --add-noqa, which REWRITE files in place (a flag
        # also slips past _is_safe_file_arg, which treats '--fix' as a filename).
        # Path args are confined to the artifacts root so lint error output can't
        # leak arbitrary source; bare `ruff check` lints the (confined) cwd.
        if len(argv) >= 2 and argv[1] == "check":
            rest = argv[2:]
            if all(not a.startswith("-") and _is_safe_file_arg(a, root, extra_roots) for a in rest):
                return True
    if head == "mypy":
        # mypy <file.py>  or  mypy <file.py> <file.py>...  — confine each path
        # (a lint tool echoes offending source lines, so an unconfined path is
        # an arbitrary-file read).
        if len(argv) >= 2 and all(
            a.endswith(".py") and _is_safe_file_arg(a, root, extra_roots) for a in argv[1:]
        ):
            return True
    if head == "pyflakes":
        if len(argv) >= 2 and all(
            a.endswith(".py") and _is_safe_file_arg(a, root, extra_roots) for a in argv[1:]
        ):
            return True
    # ── Read-only filesystem inspection (cwd already confined) ──────
    # The agent often wants to confirm what's in the workspace before
    # writing. These are read-only, the cwd guard prevents escape, and
    # path-arg validation refuses traversal + dotfiles.
    if head == "ls":
        # ls (no args), ls -l/-la/-a, ls <file>, ls -la <file>
        if len(argv) == 1:
            return True
        if all(a in ("-l", "-la", "-al", "-a") or _is_safe_file_arg(a, root, extra_roots) for a in argv[1:]):
            return True
    if head == "cat":
        # cat <file> [<file> ...] — read-only file inspection
        if len(argv) >= 2 and all(_is_safe_file_arg(a, root, extra_roots) for a in argv[1:]):
            return True
    if head == "head":
        # head <file>, head -<N> <file>, head -n <N> <file>
        if len(argv) == 2 and _is_safe_file_arg(argv[1], root, extra_roots):
            return True
        if (
            len(argv) == 3
            and argv[1].startswith("-")
            and argv[1][1:].isdigit()
            and _is_safe_file_arg(argv[2], root, extra_roots)
        ):
            return True
        if (
            len(argv) == 4
            and argv[1] == "-n"
            and argv[2].isdigit()
            and _is_safe_file_arg(argv[3], root, extra_roots)
        ):
            return True
    if head == "tail":
        # tail <file>, tail -<N> <file>, tail -n <N> <file>  (head's twin)
        if len(argv) == 2 and _is_safe_file_arg(argv[1], root, extra_roots):
            return True
        if (
            len(argv) == 3
            and argv[1].startswith("-")
            and argv[1][1:].isdigit()
            and _is_safe_file_arg(argv[2], root, extra_roots)
        ):
            return True
        if (
            len(argv) == 4
            and argv[1] == "-n"
            and argv[2].isdigit()
            and _is_safe_file_arg(argv[3], root, extra_roots)
        ):
            return True
    if head == "wc":
        # wc [-lwcmL...] <file> [<file>...] — read-only line/word/byte counts.
        rest = argv[1:]
        flags = [a for a in rest if a.startswith("-") and a != "-"]
        files = [a for a in rest if not (a.startswith("-") and a != "-")]
        if (
            files
            and all(set(f[1:]) <= set("lwcmL") for f in flags)
            and all(_is_safe_file_arg(a, root, extra_roots) for a in files)
        ):
            return True
    if head == "grep":
        # grep [search-flags] PATTERN <file> [<file>...] — read-only search.
        # The PATTERN is a string grep never executes; cwd guard +
        # _is_safe_file_arg confine the reads. Reject recursive / read-from-
        # file / dir-walk forms (-r/-R/-f/-d/-Z and any long --option) and
        # require an explicit confined file arg (so it can't be pointed at a
        # tree or outside the artifacts root). Code iteration's #1 ask:
        # "find the JUMP constant" in one call vs re-cat'ing 300 lines.
        rest = argv[1:]
        flags = [a for a in rest if a.startswith("-") and a != "-"]
        nonflags = [a for a in rest if not (a.startswith("-") and a != "-")]
        safe_flags = all(
            not a.startswith("--") and set(a[1:]) <= set("niEcwoHhsvxF")
            for a in flags
        )
        if safe_flags and len(nonflags) >= 2:  # PATTERN + >=1 file
            files = nonflags[1:]
            if all(_is_safe_file_arg(f, root, extra_roots) for f in files):
                return True
    if head == "sed":
        # ONLY the read-only line-range print form: ``sed -n 'A,Bp' <file>``.
        # Raw sed is a footgun (-i writes in place; the `e`/`w`/`r` commands
        # execute shell / touch files), so we match the print form EXACTLY
        # and reject everything else. Gives surgical line-range reads with no
        # write/exec surface — the safe replacement for "let them use sed".
        if (
            len(argv) == 4
            and argv[1] == "-n"
            and re.fullmatch(r"\d+,\d+p", argv[2])
            and _is_safe_file_arg(argv[3], root, extra_roots)
        ):
            return True
    return False


def _check_full(argv: list[str], root: Path | None = None, extra_roots=()) -> bool:
    if _check_passive(argv, root, extra_roots):
        return True
    if not argv:
        return False
    head = argv[0]
    if head in _PYTHON_BINS:
        # python3 file.py [args...]  — actual program execution.
        # File arg must resolve under artifacts root with no dotfile
        # components — otherwise a producer can read arbitrary
        # user-readable files via Python's traceback-on-syntax-error
        # leak (e.g. python3 /home/user/.ssh/id_rsa.py).
        if (
            len(argv) >= 2
            and argv[1].endswith(".py")
            and _is_safe_file_arg(argv[1], root, extra_roots)
        ):
            return True
        # python3 -c '<any body>'  — full profile authorizes execution,
        # so any -c body is fair game (the model needs to run code to
        # verify behavior, not just probe imports).
        if len(argv) == 3 and argv[1] == "-c":
            return True
        # python3 -m <module> [<any args>]  — full -m execution shape
        if len(argv) >= 3 and argv[1] == "-m":
            return True
    if head == "node":
        # node file.js — same file-arg containment rule as python.
        if (
            len(argv) >= 2
            and argv[1].endswith(".js")
            and _is_safe_file_arg(argv[1], root, extra_roots)
        ):
            return True
    if head == "npm":
        # npm <subcommand> [args] — install, test, run, etc. Full
        # because these write node_modules / execute scripts.
        if len(argv) >= 2:
            return True
    if head == "npx":
        # npx <tool> [args] — fetches+runs a binary; full territory.
        if len(argv) >= 2:
            return True
    # ── Ruby (full) ─────────────────────────────────────────────────
    if head == "ruby":
        # ruby file.rb [args]  — execution. Same file-arg containment
        # rule as python/node.
        if (
            len(argv) >= 2
            and argv[1].endswith(".rb")
            and _is_safe_file_arg(argv[1], root, extra_roots)
        ):
            return True
    if head == "bundle":
        # bundle <subcommand> [args] — install, exec, etc. Subcommand
        # restricted to a known-safe set so arbitrary tokens don't
        # creep through (no flags, no paths in argv[1]).
        if len(argv) >= 2 and argv[1] in (
            "exec", "install", "test", "config", "list",
            "show", "outdated", "update", "lock",
        ):
            return True
    if head == "rspec":
        # rspec [args]  — Ruby's pytest equivalent
        return True
    if head == "rake":
        # rake [task] [args]  — Ruby's make equivalent
        return True
    # ── Go (full) ───────────────────────────────────────────────────
    if head == "go":
        # go <subcommand> [args]  — known-safe subcommand set
        if len(argv) >= 2 and argv[1] in (
            "build", "run", "test", "install", "mod",
            "get", "generate", "doc", "env",
        ):
            return True
    if head == "gofmt":
        # gofmt -w file.go  — write reformatted output back to disk
        if len(argv) >= 3 and argv[1] == "-w":
            return True
    if head == "pytest":
        return True
    if head == "bash":
        # bash file.sh — same file-arg containment rule as the other
        # script-execution heads. Without this, a producer can run
        # arbitrary host scripts via bash <abs-path>.
        if (
            len(argv) == 2
            and argv[1].endswith(".sh")
            and _is_safe_file_arg(argv[1], root, extra_roots)
        ):
            return True
    return False


_PROFILE_CHECKS: dict[str, Callable[[list[str], Path | None], bool]] = {
    "passive": _check_passive,
    "full": _check_full,
}

#: Tools where ``argv[0] = name`` should be rewritten to invoke them
#: through the running Python interpreter via ``-m``. Production-grade
#: testing requires that pytest / mypy / pyflakes use the same Python
#: that has the project's packages installed — system ``python3`` on
#: the user's PATH may not have pytest at all (the STR end-to-end
#: failure mode), or may have a different version than the venv
#: Modulatio is running from.
_PYTHON_DASH_M_TOOLS = ("pytest", "mypy", "pyflakes")


def _rewrite_argv_to_running_python(argv: list[str]) -> list[str]:
    """Rewrite python / pytest / mypy / pyflakes invocations to use
    ``sys.executable`` (the running Python's path).

    Why: subprocess.run inherits PATH from the parent. If ``python3``
    or ``pytest`` aren't on PATH but ARE importable via the running
    interpreter, the rewrite makes them succeed. The allowlist runs
    BEFORE this rewrite so the user-visible cmd shape is what the
    safety surface enforces; the rewrite is a transparent execution
    helper.

    Cases:

    - ``python`` / ``python3`` / ``python3.X`` → ``sys.executable``.
      The user wrote the canonical name; the venv's Python actually
      runs. Necessary so ``import pytest`` resolves against the
      venv's site-packages.
    - ``pytest`` / ``mypy`` / ``pyflakes`` → ``sys.executable -m <tool>``.
      These projects all support the ``-m`` invocation. Same
      rationale: use the venv's installed copy regardless of PATH.

    Other heads (``ls``, ``cat``, ``head``, ``node``, ``ruff``, ``bash``)
    pass through unchanged — they're either pure system tools or
    standalone binaries.
    """
    if not argv:
        return argv
    head = argv[0]
    if head in _PYTHON_BINS:
        return [sys.executable] + argv[1:]
    if head in _PYTHON_DASH_M_TOOLS:
        return [sys.executable, "-m", head] + argv[1:]
    return argv


def _validate_run_shell_cwd(cwd_str: str, artifacts_root: Path, extra_roots=()) -> Path:
    """Resolve ``cwd_str`` under ``artifacts_root`` OR a granted ``extra_root``
    (exec-widen) and return the absolute Path. Empty string → root itself.
    Raises ``ValueError`` on traversal escape (under no allowed root), dotfile
    component, or missing dir. The dotfile secret-floor applies under EVERY
    allowed root (Nemo #2 — both run_shell confinement helpers share the rule).
    """
    root_resolved = artifacts_root.resolve()
    if not cwd_str:
        return root_resolved
    candidate = (artifacts_root / cwd_str).resolve()
    roots = [root_resolved, *[Path(r).resolve() for r in extra_roots]]
    matched = next((r for r in roots if candidate == r or r in candidate.parents), None)
    if matched is None:
        raise ValueError(f"cwd escapes artifacts root: {cwd_str!r}")
    for part in candidate.relative_to(matched).parts:
        if part.startswith("."):
            raise ValueError(
                f"cwd contains dotfile component {part!r}; phase 1 refuses "
                f"dotted dirs to keep the safety surface conservative"
            )
    if not candidate.exists():
        raise ValueError(f"cwd does not exist: {cwd_str!r}")
    if not candidate.is_dir():
        raise ValueError(f"cwd is not a directory: {cwd_str!r}")
    return candidate


def _truncate_output(text: str, max_bytes: int = _RUN_SHELL_MAX_OUTPUT_BYTES) -> str:
    """Cap ``text`` at ``max_bytes`` UTF-8 bytes, appending a marker
    when truncation occurred. Bytes (not chars) is the right axis —
    log-flood scripts emit ASCII; QC's review prompt is bounded."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    head = encoded[:max_bytes].decode("utf-8", errors="replace")
    return head + f"\n... [truncated, {len(encoded) - max_bytes} more bytes]"


def _format_run_shell_result(returncode: int, stdout: str, stderr: str) -> str:
    return (
        f"exit_code: {returncode}\n"
        f"stdout:\n{_truncate_output(stdout)}\n"
        f"stderr:\n{_truncate_output(stderr)}"
    )


def _resolve_payload_binary(head: str) -> str | None:
    """Return the resolved executable path for ``head`` (the first real
    payload token), or ``None`` when it isn't installed on this host.

    re-sweep (prlimit-masks-INFO): the ``prlimit`` wrapper prefix means
    ``Popen`` execs ``prlimit`` (which exists) for a standalone payload like
    ``ruby``/``go``/``node``, so a missing payload no longer raises
    ``FileNotFoundError`` — ``prlimit`` runs and exits 127, and the friendly
    ``[INFO] tool 'X' not installed`` body never fires. We therefore pre-check
    the payload binary BEFORE building the wrapper. The sandbox ``--ro-bind /
    /``s the host root, so host-PATH resolution is the right authority on both
    the sandboxed and unsandboxed paths.

    An absolute/relative path that points at an existing executable file is
    treated as present (``shutil.which`` only searches PATH for bare names).
    """
    import shutil

    if not head:
        return None
    if os.sep in head or (os.altsep and os.altsep in head):
        # Explicit path form: present iff it's an existing executable file.
        return head if os.path.isfile(head) and os.access(head, os.X_OK) else None
    return shutil.which(head)


def _not_installed_body(missing: str, detail: str = "") -> str:
    """The friendly ``[INFO] tool '<missing>' not installed`` body."""
    suffix = f" Underlying error: {detail}" if detail else ""
    return (
        f"[INFO] tool {missing!r} not installed in this "
        f"environment. If a probe relied on it, treat as 'not "
        f"configured' and skip — do not retry.{suffix}"
    )


def make_run_shell(artifacts_root: Path, extra_roots=()) -> Callable[..., str]:
    """Return a ``run_shell`` callable bound to ``artifacts_root`` (plus any
    operator-granted ``extra_roots`` for exec-widen — cwd + file-args may resolve
    under a granted root, and widened-root execution is sandbox-REQUIRED).

    Closing over the root rather than accepting it per-call keeps the
    skill-authored ``tool_args`` minimal AND makes path confinement a
    construction-time invariant — a skill cannot "pass a different
    root" to escape its project's directory.

    Returned callable signature::

        run_shell(cmd: str, profile: str = "passive",
                  cwd: str = "", timeout: float = 30.0) -> str

    Returns a string with shape::

        exit_code: <int>
        stdout:
        <captured + truncated stdout>
        stderr:
        <captured + truncated stderr>

    QC reads the body and grades the result the same way it grades any
    artifact. Non-zero exit codes, TIMEOUT markers, and traceback
    fragments all surface as text.

    Raises ``ValueError`` for any safety-surface violation: unknown
    profile, command outside the profile allowlist, cwd escape, dotfile
    cwd component, missing cwd.

    When the resolved binary isn't installed (``FileNotFoundError`` from
    subprocess), returns a friendly ``[INFO] tool 'X' not installed in
    this environment`` body instead of propagating — gives the model
    diagnostic text it can read and adapt to (skip-the-probe semantics)
    rather than a raw exception that crashes the chat loop.
    """
    artifacts_root = Path(artifacts_root)

    def run_shell(
        cmd: str,
        profile: str = "passive",
        cwd: str = "",
        timeout: float = _DEFAULT_RUN_SHELL_TIMEOUT_SECONDS,
    ) -> str:
        timeout = _clamp_timeout(
            timeout, lo=_RUN_SHELL_MIN_TIMEOUT_SECONDS,
            hi=_RUN_SHELL_MAX_TIMEOUT_SECONDS,
            default=_DEFAULT_RUN_SHELL_TIMEOUT_SECONDS,
        )  # SEC-04
        check = _PROFILE_CHECKS.get(profile)
        if check is None:
            raise ValueError(
                f"unknown profile {profile!r}; expected one of "
                f"{sorted(_PROFILE_CHECKS)!r}"
            )
        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            raise ValueError(f"unparseable cmd: {exc}") from exc
        if not check(argv, artifacts_root, extra_roots):
            raise ValueError(
                f"command not allowed by profile {profile!r}: {cmd!r}"
            )
        wd = _validate_run_shell_cwd(cwd, artifacts_root, extra_roots)
        # Rewrite happens AFTER the allowlist check so the safety
        # surface enforces user-visible cmd shapes; the rewrite is
        # a transparent execution helper that lets ``pytest`` /
        # ``python3`` find their package home.
        exec_argv = _rewrite_argv_to_running_python(argv)
        # re-sweep (prlimit-masks-INFO): pre-check the real payload binary
        # BEFORE wrapping it in the prlimit prefix. Once exec_argv is wrapped,
        # Popen execs `prlimit` (which exists) and a missing standalone payload
        # (ruby/go/node/bash/rubocop/...) makes prlimit exit 127 rather than
        # raising FileNotFoundError — so the friendly [INFO] body below would
        # never fire on the production prlimit path. Resolve against the host
        # PATH (the sandbox ro-binds `/`), and return the [INFO] body directly
        # when the payload isn't installed. The FileNotFoundError handler stays
        # as a backstop (e.g. prlimit-absent hosts, races).
        if exec_argv and _resolve_payload_binary(exec_argv[0]) is None:
            return _format_run_shell_result(
                -1, "", _not_installed_body(exec_argv[0])
            )
        # SEC-001 + SEC-002: sandbox the child process.
        # The argv allowlist above is defense in depth; the trust
        # boundary is here. Three paths:
        #   1. Bypass requested explicitly (test suite, CI, opted-out
        #      dev) — run as-is, no sandbox, no env stripping. Caller
        #      accepted the risk.
        #   2. bwrap available — wrap argv in a confined namespace +
        #      stripped env (per-skill pass_env honored via contextvar).
        #   3. bwrap missing — soft-fall to unsandboxed, log a one-time
        #      warning, run with parent env. Production must install
        #      bubblewrap; this branch keeps macOS/CI alive.
        from modulatio import sandbox as _sandbox

        run_env: dict[str, str] | None = None
        # H3a re-sweep: wrap the PAYLOAD argv in a prlimit prefix so the
        # mem/disk/core caps are established on the payload's own PID at exec
        # time. On the sandboxed path this prefix is built BEFORE bwrap so it
        # lands INSIDE the sandbox (after `--`) and clamps the real payload —
        # the parent-side _apply_rlimits_to_pid below only ever sees the bwrap
        # MONITOR pid there, which forks the payload AFTER and so never inherits
        # the caps. Keep exec_argv un-wrapped for the FileNotFoundError message
        # (it reports the real missing binary, not "prlimit").
        _payload_argv = _prlimit_wrapper_prefix() + list(exec_argv)
        run_argv = _payload_argv
        _profile = _sandbox.current_profile()
        # exec-widen HIGH-3 (Wild Bill): a WIDENED-root run requires a FUNCTIONAL
        # sandbox, fail-closed — checked BEFORE the bypass/off branch, so the
        # global dev/test bypass (MODULATIO_RUN_SHELL_UNSAFE / profile=off) does
        # NOT reach widened exec (that would leak the parent env + provider keys
        # into a real project folder). The workspace path keeps its bypass/soft-
        # fallback below (the Leader's own confined home, lower risk).
        _art_resolved = artifacts_root.resolve()

        def _under(p: Path, root: Path) -> bool:
            return p == root or root in p.parents

        _wd_widened = not _under(wd, _art_resolved)
        # exec-widen HIGH-3, code-review r1 BLOCK (Wild Bill): widening also
        # enters via a path-bearing ARGV token — e.g. `python3 /granted/script.py`
        # with a WORKSPACE cwd. The cwd-only check above misses that, so the
        # global bypass below would run it UNSANDBOXED with the parent env,
        # leaking provider keys into a command operating on a real project
        # folder. Treat the run as widened if ANY accepted argv token resolves
        # under a granted extra_root rather than the artifacts root — then the
        # same fail-closed guard applies. (argv already passed the profile
        # allowlist + _is_safe_file_arg, so a token under an extra_root is an
        # accepted widened path, not an escape attempt.)
        _extra_resolved = [Path(r).resolve() for r in extra_roots]
        _argv_widened = False
        for _tok in argv:
            if not _tok or _tok.startswith("-"):
                continue
            try:
                _tp = (Path(_tok) if Path(_tok).is_absolute() else wd / _tok).resolve()
            except (ValueError, OSError):
                continue
            if any(_under(_tp, er) for er in _extra_resolved) and not _under(
                _tp, _art_resolved
            ):
                _argv_widened = True
                break
        _widened = _wd_widened or _argv_widened
        if _widened and not _sandbox.is_sandbox_available():
            raise RuntimeError(
                "widened exec refused: bwrap sandbox unavailable. Running "
                "commands in an operator-granted folder requires a functional "
                "sandbox (no bypass). Install/repair bubblewrap."
            )
        if (_sandbox.is_bypass_requested() or _profile == "off") and not _widened:
            # Explicit opt-out (UNSAFE env or profile=off), run as-is — but ONLY
            # for a WORKSPACE run. The global dev/test bypass must NEVER apply to
            # a widened run (Wild Bill HIGH-3): a widened run reaching here has a
            # functional sandbox (the raise above caught the no-sandbox case), so
            # it falls through to the sandboxed branch and is confined, not run
            # unsandboxed with the parent env.
            pass
        elif _sandbox.is_sandbox_available():
            run_argv, run_env = _sandbox.build_sandboxed_argv(
                _payload_argv, artifacts_root, profile=_profile,
                extra_rw_roots=tuple(Path(r) for r in extra_roots),
            )
        elif _sandbox.is_sandbox_required():
            # H3c: operator demanded a working sandbox (MODULATIO_REQUIRE_SANDBOX=1)
            # but bwrap is missing/non-functional and no explicit bypass was set.
            # Fail CLOSED — refuse to run unconfined rather than fall open.
            raise RuntimeError(
                "run_shell refused: MODULATIO_REQUIRE_SANDBOX=1 but the bwrap "
                "sandbox is unavailable on this host. Install/repair bubblewrap, "
                "or set MODULATIO_RUN_SHELL_UNSAFE=1 to accept unsandboxed "
                "execution explicitly."
            )
        else:
            _sandbox.warn_unsandboxed_once()
        try:
            # H3a/H3b: own process group (start_new_session) + child rlimits so a
            # wall-clock timeout reaps the WHOLE tree (orphaned background
            # grandchildren included), and a memory/disk bomb is bounded. We use
            # Popen+communicate rather than subprocess.run because run() SIGKILLs
            # only the direct child on timeout, leaving an unsandboxed
            # background process alive.
            import signal as _signal

            proc = subprocess.Popen(
                run_argv,
                cwd=str(wd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # errors="replace": a child can emit non-UTF-8/binary bytes on
                # stdout/stderr (a probe that dumps a binary, a tool that writes
                # latin-1 diagnostics). With text=True the default 'strict'
                # decoder would raise UnicodeDecodeError inside communicate() —
                # which is caught by NEITHER the TimeoutExpired nor the
                # FileNotFoundError handler, so it would propagate and discard
                # ALL captured output (and crash the chat loop). Substitute the
                # undecodable bytes so the model still sees a best-effort body.
                errors="replace",
                shell=False,
                env=run_env,
                start_new_session=True,
            )
            # Apply the resource caps from the PARENT (fork-safe), not a
            # preexec_fn (fork-from-multithreaded deadlock hazard — the 0.9.0 hang).
            _apply_rlimits_to_pid(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Kill the entire process group, then drain. killpg targets the
                # session we created above (start_new_session). For an
                # UNSANDBOXED child this is what reaps grandchildren. Under the
                # bwrap sandbox the payload runs in its own PID namespace
                # (--unshare-pid) whose grandchildren are NOT in our session, so
                # killpg reaps the bwrap process itself and the kernel then tears
                # down the whole PID namespace; --die-with-parent is the belt
                # that also kills the sandbox if we are reaped first. The
                # bounded drain below covers a double-forking grandchild that
                # re-parented away and kept the pipes open.
                try:
                    os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                # Bounded drain: a double-forking grandchild that re-parented
                # away from our process group can keep the stdout/stderr pipes
                # open after the killpg, so an unbounded communicate() would
                # hang run_shell forever. Cap the drain; on a second timeout,
                # kill again and give up on the leftover output.
                try:
                    stdout, stderr = proc.communicate(timeout=_RUN_SHELL_DRAIN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        stdout, stderr = proc.communicate(timeout=_RUN_SHELL_DRAIN_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        # A grandchild that re-parented away still holds the
                        # pipes open; the final communicate() never closed them,
                        # so close our pipe ends explicitly to avoid leaking fds
                        # across repeated timeouts (Popen.__del__ is not
                        # guaranteed to run promptly under the chat loop's GC).
                        for _pipe in (proc.stdout, proc.stderr):
                            if _pipe is not None:
                                try:
                                    _pipe.close()
                                except OSError:
                                    pass
                        # re-sweep: killpg already SIGKILL'd the group, so the
                        # child is dead — but nothing here reaped it, leaving a
                        # zombie until Popen.__del__ runs under the chat loop's
                        # GC. Reap it now with a non-blocking poll() (the group
                        # is gone, so this returns immediately) rather than
                        # leaking a defunct PID per give-up under wave load.
                        # Best-effort: a reap failure must never crash the chat
                        # loop on this already-pathological path.
                        try:
                            proc.poll()
                        except Exception:  # noqa: BLE001 - best-effort reap
                            pass
                        stdout, stderr = "", ""
                stderr = (stderr or "") + f"\n[TIMEOUT after {timeout}s]"
                return _format_run_shell_result(-1, stdout or "", stderr)
            return _format_run_shell_result(
                proc.returncode,
                stdout or "",
                stderr or "",
            )
        except FileNotFoundError as exc:
            # Binary not on PATH (and, for python -m <tool> shapes, the
            # rewrite already used sys.executable so this is genuinely
            # "the tool isn't installed"). Return diagnostic text so
            # the model can read [INFO] and skip the probe rather than
            # raising and crashing the chat loop.
            missing = exec_argv[0] if exec_argv else "(unknown)"
            return _format_run_shell_result(
                -1, "", _not_installed_body(missing, str(exc))
            )

    return run_shell


# ── builtin: write_artifact ────────────────────────────────────────────────
#
# Surfaced 2026-04-28 in the FIN/STR end-to-end tests: engineer LLMs
# repeatedly tried ``cat > add.py << 'EOF'`` to write files iteratively
# during the chat loop. shell=False in run_shell rejected the redirect
# (correctly — that's the safety surface). But the engineer's INTENT —
# write a file early so they can probe it with run_shell — is reasonable.
#
# ``write_artifact`` is the safe channel for that intent. cwd-confined,
# size-bounded, refuses traversal + dotfiles + tool_calls/, parent dir
# auto-created. Returns a confirmation string with the relative path
# and byte count so the model knows what landed where.

_WRITE_ARTIFACT_MAX_BYTES = 1_048_576  # 1 MiB

_WRITE_ARTIFACT_BLOCKED_PREFIXES = ("tool_calls/", "tool_calls\\")


def make_write_artifact(
    artifacts_root: Path,
    on_write: "Callable[[Path], None] | None" = None,
) -> Callable[..., str]:
    """Return a ``write_artifact`` callable bound to ``artifacts_root``.

    ``on_write`` (optional): a callback invoked with the absolute target Path
    after a successful write. The concurrent-wave orchestrator passes
    ``_record_artifact_write`` here so a tool-written file in the per-task
    staging tree is recorded for the merge — without it, a producer's
    ``write_artifact`` sidecar is written to staging, passes QC there, then
    deleted with staging and never copied to the shared tree (Nemo R2 HIGH).
    None (the sequential path / CLI) makes it a no-op; those writes already land
    directly in the shared artifacts root.

    Returned callable signature::

        write_artifact(path: str, content: str) -> str

    Writes ``content`` to ``<artifacts_root>/<path>`` and returns a
    confirmation string. Raises ``ValueError`` for any safety
    violation: absolute path, ``..`` traversal, dotfile component,
    write into ``tool_calls/`` (audit-only), content size beyond
    1 MiB.

    ``content`` is written verbatim as UTF-8. Parent directories are
    created on demand (e.g., ``write_artifact("src/main.py", ...)``
    creates ``<artifacts_root>/src/`` if needed).

    The orchestrator still writes the producer's final response to
    the task's output_path AFTER the loop ends. If the producer's
    final response matches what they wrote via ``write_artifact``,
    the file is canonical and idempotent. If they differ, the final
    response wins — the producer is responsible for keeping them
    consistent (skill prose covers this).
    """
    artifacts_root = Path(artifacts_root)

    def write_artifact(path: str, content: str) -> str:
        if not isinstance(path, str) or not path:
            raise ValueError("write_artifact: path must be a non-empty string")
        if not isinstance(content, str):
            raise ValueError(
                f"write_artifact: content must be a string, got "
                f"{type(content).__name__}"
            )
        # Path-arg safety mirrors run_shell's relative-only check: no
        # absolute paths, no traversal, no dotfile components, no
        # writes into tool_calls/ (audit log).
        if not _is_safe_relative_file_arg(path):
            raise ValueError(
                f"write_artifact: path {path!r} not safe — must be relative, "
                f"with no traversal segments or dotfile components"
            )
        normalized = path.replace("\\", "/")
        for blocked in _WRITE_ARTIFACT_BLOCKED_PREFIXES:
            if normalized.startswith(blocked.replace("\\", "/")):
                raise ValueError(
                    f"write_artifact: path {path!r} writes into "
                    f"tool_calls/ — that subdir is the audit transcript "
                    f"log, not a writable artifact location"
                )
        # Size cap. 1 MiB is generous for code / docs / config; binary
        # artifacts that would exceed this aren't write_artifact's job
        # (and the tool only accepts str content anyway).
        encoded = content.encode("utf-8", errors="replace")
        if len(encoded) > _WRITE_ARTIFACT_MAX_BYTES:
            raise ValueError(
                f"write_artifact: content size {len(encoded)} exceeds "
                f"{_WRITE_ARTIFACT_MAX_BYTES}-byte cap"
            )
        # Resolve under artifacts_root and verify no escape (defensive
        # — _is_safe_relative_file_arg already rejected obvious cases,
        # but symlinks could in theory still escape).
        target = (artifacts_root / path).resolve()
        try:
            target.relative_to(artifacts_root.resolve())
        except ValueError as exc:
            raise ValueError(
                f"write_artifact: resolved path escapes artifacts root: {path!r}"
            ) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if on_write is not None:
            # Record the write for the concurrent-wave merge (no-op safe).
            on_write(target)
        rel = target.relative_to(artifacts_root.resolve())
        return (
            f"[OK] wrote {len(encoded)} bytes to artifacts/{rel}"
        )

    return write_artifact


# ── builtins: read_file / edit_file (the Leader's solo-coding pen-set) ───────
#
# write_artifact already covers "write a file" (root-bound). The genuinely
# missing fluent capability for operator-guided standalone coding is READING a
# file and SURGICALLY EDITING one — doing those through run_shell's argv
# allowlist (sed/cat gymnastics) is clumsy. These are TOOLS (registry builtins),
# NOT skills: the conversational Leader gets them via the whole-registry loadout,
# producers get them only if a skill's tool_loadout names them. The coding
# KNOW-HOW stays a library skill (``coding.md``).
#
# SECURITY: unlike run_shell (bwrap subprocess), these run IN-PROCESS — so the
# path check in ``_resolve_file_under_root`` IS their confinement boundary.

_READ_FILE_MAX_BYTES = 1_048_576  # 1 MiB — keep a huge file from blowing context


def _resolve_file_under_root(path: str, root: Path, extra_roots=()) -> Path:
    """Resolve ``path`` under ``root`` (or a granted ``extra_root``) and refuse
    any escape. The in-process file tools are NOT wrapped by bwrap, so THIS is
    their confinement.

    - A RELATIVE path must be safe (no ``..`` / dotfile components) and resolve
      under the PRIMARY ``root`` (the Leader's own workspace).
    - An ABSOLUTE path is allowed ONLY if it resolves under ``root`` or one of
      the operator-granted ``extra_roots`` — and even then the SECRET FLOOR holds:
      no dotfile component BELOW the matched root (so ``.env`` / ``.ssh`` inside a
      granted project stay unreadable). With no ``extra_roots``, absolute paths
      are refused (fail-closed default confinement)."""
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    roots = [Path(root).resolve(), *[Path(r).resolve() for r in extra_roots]]
    if os.path.isabs(path):
        target = Path(path).resolve()
        matched = next((r for r in roots if target == r or r in target.parents), None)
        if matched is None:
            raise ValueError(f"path {path!r} is outside the confined/granted roots")
        rel_parts = [p for p in target.relative_to(matched).parts if p != "."]
        if any(p.startswith(".") for p in rel_parts):
            raise ValueError(f"path {path!r} has a dotfile component (refused — secret floor)")
        return target
    if not _is_safe_relative_file_arg(path):
        raise ValueError(
            f"path {path!r} not safe — must be relative, with no traversal "
            f"segments or dotfile components"
        )
    primary = roots[0]
    target = (primary / path).resolve()
    if target != primary and primary not in target.parents:
        raise ValueError(f"path {path!r} escapes the confined root")
    return target


def make_read_file(artifacts_root: Path, extra_roots=()) -> "Callable[..., str]":
    """Return a ``read_file`` callable bound to ``artifacts_root`` (+ any granted
    ``extra_roots`` reachable via absolute path). Returns the file's UTF-8 text,
    truncated to 1 MiB. Raises ``ValueError`` for a safety violation or missing
    file::

        read_file(path: str) -> str
    """
    root = Path(artifacts_root)

    def read_file(path: str) -> str:
        target = _resolve_file_under_root(path, root, extra_roots)
        if not target.is_file():
            raise ValueError(f"read_file: {path!r} does not exist")
        data = target.read_bytes()
        text = data[:_READ_FILE_MAX_BYTES].decode("utf-8", "replace")
        if len(data) > _READ_FILE_MAX_BYTES:
            text += f"\n[...truncated at {_READ_FILE_MAX_BYTES} bytes]"
        return text

    return read_file


def make_edit_file(artifacts_root: Path, extra_roots=()) -> "Callable[..., str]":
    """Return an ``edit_file`` callable bound to ``artifacts_root``.

    Surgical string-replace, confined to the root. ``old`` must match EXACTLY
    ONCE (unique-match, like a careful editor) — a zero or ambiguous match
    raises ``ValueError`` rather than guess. Returns a confirmation::

        edit_file(path: str, old: str, new: str) -> str
    """
    root = Path(artifacts_root)

    def edit_file(path: str, old: str, new: str) -> str:
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("edit_file: 'old' and 'new' must be strings")
        if old == "":
            raise ValueError("edit_file: 'old' must be a non-empty string")
        target = _resolve_file_under_root(path, root, extra_roots)
        if not target.is_file():
            raise ValueError(f"edit_file: {path!r} does not exist")
        text = target.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise ValueError(f"edit_file: 'old' not found in {path!r}")
        if count > 1:
            raise ValueError(
                f"edit_file: 'old' matches {count} times in {path!r} — make it "
                f"unique by including surrounding context"
            )
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"edited {path}: replaced 1 occurrence"

    return edit_file


# ── builtin: web_search (DuckDuckGo, no API key) ────────────────────────────

#: Caps for the search tool. A handful of ranked hits is enough for an agent to
#: pick the right sources to ``http_get``; the return-text cap keeps it inside a
#: reasonable context slice.
_WEB_SEARCH_DEFAULT_RESULTS = 6
_WEB_SEARCH_MAX_RESULTS = 12
_WEB_SEARCH_RETURN_CHARS = 8_000

#: Low-credibility domains — AI-content-farms / unvetted wikis that fabricate
#: plausible-looking "facts" (esp. current events). We FLAG, never drop: the
#: producer + audit still see what was found and why it's suspect, and the
#: producer is told (web-search / rigorous-sourcing skills) not to cite a
#: flagged hit on its own.
#:
#: This seed set WILL bit-rot — content farms proliferate faster than code
#: ships (Nemo hull note, 2026-05-31). It is a COMMUNITY-MAINTAINED SURFACE:
#: extend it here via PR, OR per-deployment without a code change by setting
#: ``MODULATIO_LOW_CREDIBILITY_DOMAINS`` (comma-separated) — merged in at call
#: time by :func:`_low_credibility_domains`.
_LOW_CREDIBILITY_DOMAINS: frozenset[str] = frozenset({
    "grokipedia.com",
    "kennelbiscotti.com",
})

_LOW_CREDIBILITY_ENV = "MODULATIO_LOW_CREDIBILITY_DOMAINS"


def _low_credibility_domains() -> frozenset[str]:
    """The active low-credibility domain set: the built-in seed plus any from
    the ``MODULATIO_LOW_CREDIBILITY_DOMAINS`` env var (comma-separated). Read at
    call time so a deployment can extend it without a code change."""
    extra = os.environ.get(_LOW_CREDIBILITY_ENV, "")
    if not extra.strip():
        return _LOW_CREDIBILITY_DOMAINS
    add = {d.strip().lower().lstrip(".") for d in extra.split(",") if d.strip()}
    return _LOW_CREDIBILITY_DOMAINS | add


def _is_low_credibility(href: str) -> bool:
    """True iff ``href``'s host is (or is a subdomain of) a known low-trust
    content-farm domain (seed set + env extension)."""
    try:
        host = (urllib.parse.urlsplit(href).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    return any(
        host == d or host.endswith("." + d) for d in _low_credibility_domains()
    )


def web_search(query: str, max_results: int = _WEB_SEARCH_DEFAULT_RESULTS) -> str:
    """Search the web (DuckDuckGo, no API key) and return ranked results as
    text — title, URL, and snippet per hit.

    This is how an agent **discovers** current sources instead of guessing a URL
    or leaning on stale training data: search a query, read the ranked hits,
    then ``http_get`` the promising URLs to read them in full. URLs are never
    hard-coded — they're found.
    """
    q = (query or "").strip()
    if not q:
        return "web_search requires a non-empty query string."
    try:
        from ddgs import DDGS
    except ImportError:  # pragma: no cover - dependency guard
        return (
            "web_search is unavailable: the 'ddgs' package is not installed. "
            "Install it (`pip install ddgs`) to enable web search."
        )
    try:
        n = int(max_results)
    except (TypeError, ValueError):
        n = _WEB_SEARCH_DEFAULT_RESULTS
    n = max(1, min(n, _WEB_SEARCH_MAX_RESULTS))
    try:
        results = list(DDGS().text(q, max_results=n))
    except Exception as exc:  # network / rate-limit / backend change
        return f"web_search error for {q!r}: {type(exc).__name__}: {exc}"
    if not results:
        return f"web_search: no results for {q!r}."
    # Source-credibility pass: flag known content-farm/low-trust hits and sink
    # them below credible ones (stable within each group). FLAG, don't drop —
    # the producer sees everything and is told not to cite a flagged hit alone.
    normalized = []
    for r in results:
        href = (r.get("href") or r.get("url") or "").strip()
        normalized.append((
            (r.get("title") or "").strip(),
            href,
            " ".join((r.get("body") or "").split()),
            _is_low_credibility(href),
        ))
    normalized.sort(key=lambda t: t[3])  # credible (False) first; stable
    lines = [f"Web search results for: {q}", ""]
    for i, (title, href, body, low) in enumerate(normalized, 1):
        flag = "  [LOW-CREDIBILITY SOURCE — verify independently before citing]" if low else ""
        lines.append(f"{i}. {title}{flag}\n   {href}\n   {body}")
    return "\n".join(lines)[:_WEB_SEARCH_RETURN_CHARS]


# ── builtins: skill library (search / load / drop) ──────────────────────────
# Brick 1 of the skill-library arc. These let a producer DISCOVER a skill it
# doesn't already hold (search_skills), CHECK IT OUT to read its full guidance
# (load_skill), and release it (drop_skill) — drawn from the shared pool via
# modulatio.skill_library. v1 load is body-injection only: the checked-out
# skill's guidance enters the conversation; its tools are pre-authorized via
# the task's candidate-set tool union, so the frozen-loadout chat loop is
# unchanged. Every call is auto-audited through the existing on_tool_call hook.

def make_search_skills(project_code: str | None):
    def search_skills(query: str, limit: int = 8) -> str:
        """Search the skill library and return matching skills (name +
        one-line description + capability tags) — bodies excluded."""
        from modulatio import skill_library
        q = (query or "").strip()
        if not q:
            return "search_skills requires a non-empty query."
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 8
        entries = skill_library.search_skills(q, project_code=project_code, limit=n)
        if not entries:
            return f"search_skills: no skills match {q!r}."
        lines = [f"Skills matching {q!r}:", ""]
        for e in entries:
            tags = f"  [{', '.join(e.capability_tags)}]" if e.capability_tags else ""
            lines.append(f"- {e.name}: {e.description}{tags}")
        lines.append("")
        lines.append("Use load_skill(name) to check one out and read its full guidance.")
        return "\n".join(lines)

    return search_skills


def make_load_skill(project_code: str | None):
    def load_skill(name: str) -> str:
        """Check a skill out of the library and return its full guidance body."""
        from modulatio import skill_library
        nm = (name or "").strip()
        if not nm:
            return "load_skill requires a skill name."
        skill = skill_library.checkout(nm, project_code=project_code)
        if not skill.name:
            return (
                f"load_skill: no skill named {nm!r} in the library. "
                f"Try search_skills(...) to find the right name."
            )
        body = skill.prompt_template.strip()
        if not body:
            return f"load_skill: skill {nm!r} has no guidance body."
        return f"## Skill checked out: {skill.name}\n\n{body}"

    return load_skill


def make_drop_skill(project_code: str | None):
    def drop_skill(name: str) -> str:
        """Release a checked-out skill. v1 is advisory — the existing
        tool-result summarization reclaims the window; this records the
        intent (audited) and is reversible via load_skill at any time."""
        nm = (name or "").strip()
        if not nm:
            return "drop_skill requires a skill name."
        return (
            f"Dropped skill {nm!r} (guidance released; re-load it any time "
            f"with load_skill)."
        )

    return drop_skill


# ── registry ────────────────────────────────────────────────────────────────

def build_registry(
    artifacts_root: Path | None = None,
    tool_calls_dir: Path | None = None,
    project_code: str | None = None,
    on_artifact_write: "Callable[[Path], None] | None" = None,
    extra_roots=(),
    run_shell_extra_roots=(),
    extra_read_roots=(),
) -> dict[str, Tool]:
    """Return a fresh dict of builtin tools. Callers (CLI / tests)
    merge their own tools in and pass the result into the
    :class:`Orchestrator`.

    ``extra_read_roots`` (the FOLDERS ro split) widens READ-class access
    only: read_file resolves under it, but edit_file keeps ``extra_roots``
    and run_shell never sees it (run_shell's extra_roots double as its
    writable bwrap binds — a ro folder must never become one).

    When ``artifacts_root`` is supplied, the registry includes
    ``run_shell`` bound to that root. Without a root, ``run_shell`` is
    omitted — code-verification is opt-in at the project level. The
    legacy slice-#9e callsite (no kwargs) keeps its old shape: just
    ``http_get``.

    When ``tool_calls_dir`` is supplied, the registry includes
    ``read_tool_result`` bound to that directory ().
    Agents whose tool loadout includes ``read_tool_result`` can recover
    the verbatim text of a previously-summarized tool result. Without
    it, the tool is omitted — same opt-in shape as ``run_shell``.
    """
    registry: dict[str, Tool] = {
        "http_get": Tool(
            name="http_get",
            description=(
                "HTTP(S) GET a URL and return the response body as text."
            ),
            call=http_get,
            params_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute http:// or https:// URL.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Seconds before giving up. Default 10.",
                        "minimum": _HTTP_MIN_TIMEOUT_SECONDS,
                        "maximum": _HTTP_MAX_TIMEOUT_SECONDS,
                    },
                },
                "required": ["url"],
            },
        ),
        "web_search": Tool(
            name="web_search",
            description=(
                "Search the web (DuckDuckGo) and return ranked results — "
                "title, URL, snippet. Use it to DISCOVER current sources, then "
                "http_get the URLs you choose to read them. Never assume a URL."
            ),
            call=web_search,
            params_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (plain keywords).",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "How many hits to return (1-12, default 6).",
                    },
                },
                "required": ["query"],
            },
        ),
        "search_skills": Tool(
            name="search_skills",
            description=(
                "Search the skill library and return matching skills (name, "
                "one-line description, capability tags) — bodies excluded. Use "
                "it to DISCOVER a capability you don't already have loaded, "
                "then load_skill the one you want."
            ),
            call=make_search_skills(project_code),
            params_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What capability you're looking for (plain keywords).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max skills to return (default 8).",
                    },
                },
                "required": ["query"],
            },
        ),
        "load_skill": Tool(
            name="load_skill",
            description=(
                "Check a skill out of the library and read its full guidance. "
                "Your task's required_skills are pre-authorized — load them as "
                "needed. If you need something beyond them, search_skills first, "
                "then load_skill it (the checkout is logged)."
            ),
            call=make_load_skill(project_code),
            params_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact skill name (from search_skills or your required_skills).",
                    },
                },
                "required": ["name"],
            },
        ),
        "drop_skill": Tool(
            name="drop_skill",
            description=(
                "Release a checked-out skill you no longer need, to reclaim "
                "context. Reversible — load_skill it again any time."
            ),
            call=make_drop_skill(project_code),
            params_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name to drop.",
                    },
                },
                "required": ["name"],
            },
        ),
    }
    if artifacts_root is not None:
        registry["run_shell"] = Tool(
            name="run_shell",
            description=(
                "Run a shell command from a profile-restricted allowlist "
                "inside the project's artifacts dir. shell=False (no "
                "metacharacter expansion: pipes, &&, ;, $(), heredocs are "
                "all literal arg tokens — they will fail).\n"
                "\n"
                "All paths are interpreted relative to the artifacts root "
                "(your cwd). Absolute paths work IF they resolve under the "
                "artifacts root — e.g. cat /full/path/to/<artifacts>/x.py "
                "is fine; cat /etc/passwd is refused.\n"
                "\n"
                "Profile 'passive' (read-only, no user-controlled "
                "code execution) accepts EXACTLY:\n"
                "  Python:\n"
                "  • python3 --version / -V (interpreter version)\n"
                "  • python3 -m py_compile file.py (canonical syntax "
                "check — stdlib compiler runs but never executes the "
                "user file's top-level)\n"
                "  • ruff check [path], mypy file.py, pyflakes file.py\n"
                "  Node:\n"
                "  • node --version / -v\n"
                "  • npm --version\n"
                "  Ruby:\n"
                "  • ruby --version / -v\n"
                "  • ruby -c file.rb (syntax check, no execution)\n"
                "  • bundle --version, rubocop file.rb [more.rb ...]\n"
                "  Go:\n"
                "  • go version\n"
                "  • go vet [args] (static analysis)\n"
                "  • gofmt -l <file>.go, gofmt -d <file>.go (no rewrite)\n"
                "  Filesystem inspection:\n"
                "  • ls, ls -la, ls <file/dir>\n"
                "  • cat <file>, head <file>, head -N <file>, head -n N <file>\n"
                "\n"
                "NOT passive (these execute user-controlled code at "
                "import or top-level — must use profile='full'):\n"
                "  • python3 -c '<body>' / 'import X' / 'from X import Y'\n"
                "  • python3 file.py --help / [args] (top-level runs "
                "before --help is honored)\n"
                "  • python3 -m <module> --help / --version / [args] "
                "(module's __init__.py imports before argparse)\n"
                "  • node file.js --help / [args]\n"
                "  • ruby file.rb --help / [args]\n"
                "Use profile='full' for those — see audit Wave 2 "
                "F1 for the security rationale.\n"
                "\n"
                "Profile 'full' (passive + actual execution) ALSO accepts:\n"
                "  Python:\n"
                "  • python3 file.py [args...]\n"
                "  • python3 -c '<any body>' (full code execution; "
                "smoke-test imports go here, NOT in passive)\n"
                "  • python3 -m <module> [<any args>] (including "
                "--help / --version)\n"
                "  • pytest [args...]\n"
                "  Node:\n"
                "  • node file.js [args...]\n"
                "  • npm <subcommand> [args] (install/test/run/etc.)\n"
                "  • npx <tool> [args]\n"
                "  Ruby:\n"
                "  • ruby file.rb [args...]\n"
                "  • bundle <subcommand> [args]  (exec/install/test/...)\n"
                "  • rspec [args], rake [task]\n"
                "  Go:\n"
                "  • go <subcommand> [args] (build/run/test/install/mod/get/...)\n"
                "  • gofmt -w <file>.go (rewrite)\n"
                "  Shell:\n"
                "  • bash file.sh\n"
                "\n"
                "Anything outside these patterns raises ValueError. If a "
                "probe is rejected, do NOT keep retrying variants — pick a "
                "shape from the list above OR stop calling the tool and "
                "produce your final answer.\n"
                "\n"
                "Returns text: 'exit_code: N\\nstdout:...\\nstderr:...'. "
                "Non-zero exit codes and stderr tracebacks are evidence, "
                "not noise — read them."
            ),
            call=make_run_shell(artifacts_root, run_shell_extra_roots),
            params_schema={
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "description": (
                            "Command to execute. Parsed via shlex; shell=False "
                            "(no metacharacter expansion)."
                        ),
                    },
                    "profile": {
                        "type": "string",
                        "enum": ["passive", "full"],
                        "description": (
                            "Allowlist profile. 'passive' for read-only "
                            "checks; 'full' for actual execution. Default "
                            "'passive'."
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Working directory relative to the project's "
                            "artifacts dir. Empty string = artifacts root. "
                            "Dotfile components refused."
                        ),
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Seconds before kill. Default 30.",
                        "minimum": _RUN_SHELL_MIN_TIMEOUT_SECONDS,
                        "maximum": _RUN_SHELL_MAX_TIMEOUT_SECONDS,
                    },
                },
                "required": ["cmd"],
            },
        )
        registry["write_artifact"] = Tool(
            name="write_artifact",
            description=(
                "Write a file to the project's artifacts directory. "
                "Use this for iterative file-writing during the chat "
                "loop — write code via this tool, then probe it via "
                "run_shell. Args: path (relative path under artifacts/, "
                "no traversal, no dotfiles), content (UTF-8 text, max "
                "1 MiB).\n"
                "\n"
                "Path safety: relative paths only. ``add.py``, "
                "``src/main.py``, ``tests/test_x.py`` work. Absolute "
                "paths, ``..`` traversal, dotfile components, and "
                "writes into ``tool_calls/`` (the audit log) all "
                "raise ValueError.\n"
                "\n"
                "NOTE: the orchestrator writes your FINAL response to "
                "the task's output_path AFTER the loop ends. Whatever "
                "you write here is canonical only if your final response "
                "matches. If you write add.py via this tool then make "
                "your final response prose like 'I wrote add.py', the "
                "orchestrator will overwrite add.py with that prose. "
                "Best practice: write_artifact for probing, AND emit "
                "the same content as your final response."
            ),
            call=make_write_artifact(artifacts_root, on_write=on_artifact_write),
            params_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path under artifacts/. e.g. "
                            "``add.py``, ``src/main.py``."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "File contents (UTF-8, max 1 MiB).",
                    },
                },
                "required": ["path", "content"],
            },
        )
        registry["read_file"] = Tool(
            name="read_file",
            description=(
                "Read a text file from the working root and return its "
                "contents. Path is relative to the root; absolute paths, "
                "``..`` traversal, and dotfiles are refused. Read a file "
                "before you edit it."
            ),
            call=make_read_file(artifacts_root, (*extra_roots, *extra_read_roots)),
            params_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path under the working root.",
                    },
                },
                "required": ["path"],
            },
        )
        registry["edit_file"] = Tool(
            name="edit_file",
            description=(
                "Make a surgical edit to a file under the working root: "
                "replace an exact, UNIQUE ``old`` string with ``new``. "
                "``old`` must occur exactly once — include enough surrounding "
                "context to be unique; a zero or ambiguous match is refused "
                "rather than guessed. Confined to the root; traversal and "
                "dotfiles refused."
            ),
            call=make_edit_file(artifacts_root, extra_roots),
            params_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path under the working root.",
                    },
                    "old": {
                        "type": "string",
                        "description": "Exact text to replace; must be unique in the file.",
                    },
                    "new": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                },
                "required": ["path", "old", "new"],
            },
        )
    if tool_calls_dir is not None:
        registry["read_tool_result"] = Tool(
            name="read_tool_result",
            description=(
                "Recover the verbatim text of a previously-summarized "
                "tool result. When a tool result was large enough to "
                "trip Slice 2's summarization threshold, the conversation "
                "shows a ``[summarized: call_id=...]`` placeholder; pass "
                "that ``call_id`` to this tool to read the full text "
                "from disk. Returns an explicit error string if no "
                "persisted result exists for the given id."
            ),
            call=make_read_tool_result(tool_calls_dir),
            params_schema={
                "type": "object",
                "properties": {
                    "call_id": {
                        "type": "string",
                        "description": (
                            "The opaque correlation id from the "
                            "``[summarized: call_id=...]`` marker."
                        ),
                    },
                },
                "required": ["call_id"],
            },
        )
    # Service-API pool (spec 2026-07-05): capability tools + api_call for
    # operator-configured outside services. Lazy import — service_tools
    # imports tools at module level (Tool/_urlopen), so the reverse edge
    # must stay inside the function body.
    from modulatio import service_tools as _service_tools
    registry.update(_service_tools.build_service_tools(
        artifacts_root=artifacts_root,
        on_artifact_write=on_artifact_write,
    ))
    return registry


# ── builtin: read_tool_result () ────────────────────────

def make_read_tool_result(tool_calls_dir: Path) -> Callable[..., str]:
    """Build a ``read_tool_result(call_id)`` tool bound to a directory.

    Slice 2 persists raw tool results that exceed the summarization
    threshold to ``<tool_calls_dir>/<call_id>.txt``. The summarized
    conversation message points at this tool, letting the agent recover
    verbatim text on demand without paying the full cost of keeping
    every result inline.

    Refuses path-traversal: ``call_id`` is filename-fragment-only (no
    slashes, no '..'). Missing files return an explicit error string
    (mirrors http_get's error-as-result convention) so the model can
    self-correct rather than crash the loop.
    """
    root = Path(tool_calls_dir).resolve()

    def read_tool_result(call_id: str) -> str:
        if not call_id or "/" in call_id or "\\" in call_id or ".." in call_id:
            return (
                f"ERROR: invalid call_id {call_id!r}. "
                "call_id must be a bare identifier (no path separators, no '..')."
            )
        path = (root / f"{call_id}.txt").resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return f"ERROR: call_id {call_id!r} resolved outside tool_calls dir."
        if not path.exists():
            return (
                f"ERROR: no persisted tool result for call_id={call_id!r}. "
                "Either the result was below the summarization threshold "
                "(no raw was saved) or the run-workspace was cleaned up."
            )
        try:
            return path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"ERROR: could not read call_id={call_id!r}: {exc}"

    return read_tool_result


__all__ = [
    "Tool",
    "build_registry",
    "http_get",
    "make_edit_file",
    "make_read_file",
    "make_read_tool_result",
    "make_run_shell",
    "make_write_artifact",
]
