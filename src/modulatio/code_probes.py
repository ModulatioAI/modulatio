# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Probe executor substrate.

The dedicated harness that exercises an assembled CODE deliverable — the
execution digest's muscle. Containment is the empty-root
substrate reused whole: ``build_sandboxed_argv`` with the private scratch
as the ONLY writable bind and the immutable snapshot as a read-only extra
bind. No canonical-artifacts bind, no sibling roots, network unshared.

Hard contracts:

- **Mandatory enforcement** — bypass, ``profile=off``, or a broken/degraded
  sandbox ⇒ ``ENGINE_UNAVAILABLE`` and ZERO producer bytes execute. This
  harness has no soft fallback, ever.
- **Typed status from process facts only** — exit code / signal / timeout;
  captured text is bounded, C0/ANSI-normalized, tainted evidence.
- **Hang-to-escape closed** — a deliverable-controlled timeout is
  ``PRODUCT_FAILED`` (origin ``deliverable``), never unavailable.
- **Snapshot integrity** — content-hashed at materialization (the SAME
  identity as the digest's ``snapshot_hash``), verified after phases.
- **Scratch dies in ``finally``** — ``destroy_scratch`` is idempotent.

The install / wheel / pristine-env / runner phases (steps 4–7)
compose on top of ``run_probe_phase``; this module is their substrate.
"""
from __future__ import annotations

import contextvars
import enum
import hashlib
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from modulatio import sandbox

#: Captured-output ceiling per phase (head+tail excerpt; the raw spool never
#: reaches a model).
OUTPUT_TAIL_CAP = 8_000

#: Default per-phase wall-clock ceiling (seconds) — hard kill, not hope.
DEFAULT_PHASE_TIMEOUT_S = 300

#: Per-PROCESS resource ceilings . Address space,
#: CPU seconds, and file size are per-process and safe to cap absolutely; a
#: memory/CPU/disk bomb in producer code hits these and dies as PRODUCT
#: evidence. Process COUNT (fork bombs) is per-real-UID under rlimit, so an
#: absolute NPROC cap could lock out the operator's other work. Disclosed
#: exactly: the PID namespace isolates process NAMING and strengthens
#: lifecycle cleanup (--die-with-parent, group kill) — it does NOT contain
#: PID/CPU/memory/multi-file-disk exhaustion at the host level. A fork bomb
#: may impair other operator processes until the deadline kill lands; the
#: accepted coding-harness envelope trades that (as real coding harnesses
#: do). Aggregate control wants a cgroup pids.max (follow-up).
_PROBE_RLIMIT_AS = 2 * 1024**3          # 2 GiB address space
_PROBE_RLIMIT_FSIZE = 512 * 1024**2     # 512 MiB single-file write

#: Per-mount cap for the sandbox's writable tmpfs (/tmp /home /run /var) —
#: a hostile hook can't flood engine memory through them (kernel-enforced).
_PROBE_TMPFS_SIZE = 1024 * 1024**2      # 1 GiB per tmpfs mount

#: Overall wall-clock ceiling for a whole run_execution_probes pipeline
#: The multi-goal kill switch a breach maps to
#: ENGINE_UNAVAILABLE, distinct from a per-phase product hang.
DIGEST_DEADLINE_S = 900

#: Absolute monotonic wall for the WHOLE rollup, set by run_execution_probes
#: and read by every phase in run_probe_phase — the ceiling binds mid-phase
#: (each phase runs under min(its own limit, remaining total)) and during
#: wheel build, not just between phase groups. A kill caused by the exhausted
#: TOTAL budget is engine policy (ENGINE_UNAVAILABLE); a phase's own smaller
#: limit stays deliverable-attributed (PRODUCT_FAILED).
_DEADLINE_VAR: "contextvars.ContextVar[float | None]" = contextvars.ContextVar(
    "probe_digest_deadline", default=None)

_CONTROL_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SnapshotError(ValueError):
    """A closure that cannot become an honest snapshot (symlink/special-file
    escape, unsafe path) — an explicit refusal, never a silent skip."""


class ProbeStatus(enum.Enum):
    OK = "ok"
    PRODUCT_FAILED = "product_failed"
    ENGINE_UNAVAILABLE = "engine_unavailable"


@dataclass(frozen=True)
class ProbePhaseResult:
    """One phase's typed outcome. ``status``/``origin``/``phase``/``reason``
    carried explicitly — ``None`` is never overloaded ."""

    status: ProbeStatus
    phase: str
    origin: str            # "deliverable" | "engine"
    reason: str
    returncode: "int | None" = None
    output_tail: str = ""
    duration_s: float = 0.0


@dataclass(frozen=True)
class Snapshot:
    """An immutable materialized closure + its content identity."""

    path: Path
    content_hash: str
    _entries: tuple            # ((relpath, sha256_hex), ...) for verify

    def verify_unchanged(self) -> bool:
        """Exact-tree check after a phase : the snapshot must
        hold precisely the recorded entries — same names, regular files
        only, same bytes. Added, removed, retyped, or rewritten entries all
        mean the sandbox failed its job and the run's evidence is void."""
        expected = dict(self._entries)
        expected_dirs: set[str] = set()
        for rel in expected:
            parts = Path(rel).parts[:-1]
            for i in range(1, len(parts) + 1):
                expected_dirs.add(str(Path(*parts[:i])))
        seen: set[str] = set()
        try:
            for p in self.path.rglob("*"):
                rel = str(p.relative_to(self.path))
                if p.is_symlink():
                    return False
                if p.is_dir():
                    if rel not in expected_dirs:
                        return False                    # planted directory
                    continue
                if not p.is_file():
                    return False                        # special file
                digest = expected.get(rel)
                if digest is None:
                    return False                        # planted file
                if hashlib.sha256(p.read_bytes()).hexdigest() != digest:
                    return False
                seen.add(rel)
        except OSError:
            return False
        return seen == set(expected)                    # nothing removed


def materialize_snapshot(
    units_used: "list[str]", artifacts_root: Path, scratch: Path,
) -> Snapshot:
    """Copy the AUTHORITATIVE unit closure into ``scratch/snapshot`` (step
    1/3): regular files only — a symlink or special file in the closure
    is a ``SnapshotError`` (never silently followed into the host).
    Files are copied then made read-only; the content hash uses the SAME
    recipe as the code digest's ``snapshot_hash``, so the two identities can
    never drift apart."""
    from modulatio import assembly as _assembly

    dest = Path(scratch) / "snapshot"
    dest.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    entries = []
    for name in sorted(_assembly._norm(u) for u in units_used):
        h.update(name.encode())
        h.update(b"\0")
        # Symlink check on the LEXICAL path, before resolution — a resolving
        # _safe_unit_path would either follow it or misattribute the refusal.
        if (Path(artifacts_root) / name).is_symlink():
            raise SnapshotError(f"symlink in closure: {name!r} (refused)")
        src = _assembly._safe_unit_path(name, artifacts_root)
        if src is None:
            raise SnapshotError(f"unsafe unit path in closure: {name!r}")
        if not src.is_file():
            h.update(b"<absent>")
            h.update(b"\0")
            continue
        body = src.read_bytes()
        h.update(body)
        h.update(b"\0")
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        target.chmod(0o444)
        entries.append((name, hashlib.sha256(body).hexdigest()))
    return Snapshot(path=dest, content_hash=f"sha256:{h.hexdigest()}",
                    _entries=tuple(entries))


def _clean_tail(raw: bytes) -> str:
    """Bounded, normalized, secret-scrubbed tainted evidence :
    C0/ANSI stripped, token-shaped secrets redacted via the shared
    ``scrub_secrets``, head+tail excerpt under the cap — the end of the
    spool is where build tools put their verdicts, so the tail survives."""
    text = raw.decode("utf-8", errors="replace")
    text = _CONTROL_RE.sub("", text)
    try:
        from modulatio import logstore
        text = logstore.scrub_secrets(text)
    except Exception:  # noqa: BLE001 — fail CLOSED: unscrubbed producer
        # output must never travel onward; withholding the excerpt is the
        # safe direction for a mandatory secret boundary.
        return "[probe output withheld: redaction unavailable]"
    if len(text) <= OUTPUT_TAIL_CAP:
        return text
    head = text[: OUTPUT_TAIL_CAP // 4]
    tail = text[-(OUTPUT_TAIL_CAP - len(head) - 20):]
    return f"{head}\n... [bounded] ...\n{tail}"


def run_probe_phase(
    exec_argv: "list[str]",
    *,
    phase: str,
    snapshot: Snapshot,
    scratch: Path,
    timeout_s: int = DEFAULT_PHASE_TIMEOUT_S,
    allow_network: bool = False,
    env_extra: "dict[str, str] | None" = None,
    extra_ro: "tuple[Path, ...]" = (),
) -> ProbePhaseResult:
    """Execute ONE probe phase inside the mandatory sandbox.

    Containment: the empty-root mount graph with ``scratch`` as the
    only RW bind (it doubles as the bwrap ``artifacts_root``) and the
    snapshot re-bound read-only. Enforcement is functional or the phase is
    ``ENGINE_UNAVAILABLE`` — producer code NEVER runs unsandboxed here.
    """
    if sandbox.enforcement_state() is not sandbox.EnforcementState.SANDBOXED_FULL:
        return ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase=phase, origin="engine",
            reason="sandbox enforcement not FULL (bypass/off/degraded) — "
                   "probes refuse to run producer code unsandboxed",
        )
    # The exec argv head is ENGINE-authored (python/pip/prlimit): missing on
    # the host means missing inside the sandbox (the runtime is bound RO) —
    # an engine defect, decided before anything spawns.
    if shutil.which(exec_argv[0]) is None:
        return ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase=phase, origin="engine",
            reason=f"engine executable missing: {exec_argv[0]}",
        )
    # The overall digest wall binds HERE, not just between phase groups: the
    # phase runs to an ABSOLUTE monotonic wall = min(its own limit, the
    # rollup deadline), carried as a FLOAT all the way through draining and
    # waiting — never rounded up (0.2s of budget left means 0.2s, not a
    # gifted second), and spawn/setup consume the same wall. Which limit is
    # binding decides attribution on timeout (engine ceiling vs product hang).
    deadline = _DEADLINE_VAR.get()
    wall = time.monotonic() + timeout_s
    budget_bound = False
    if deadline is not None:
        if deadline - time.monotonic() <= 0:
            return ProbePhaseResult(
                status=ProbeStatus.ENGINE_UNAVAILABLE, phase=phase,
                origin="engine",
                reason="overall verification deadline exceeded before phase "
                       "start — engine ceiling, deliverable withheld",
            )
        if deadline < wall:
            wall = deadline
            budget_bound = True
    # RLIMIT_CPU is an independent integer cap — ceil is right THERE; the
    # integer never becomes the wall-clock timeout.
    cpu_s = max(1, math.ceil(wall - time.monotonic()))
    # rlimits ride the PAYLOAD argv (prlimit prefix, inside bwrap's `--`):
    # limits set on the returned Popen pid land on bwrap's MONITOR, and the
    # payload it forks may already exist — per-process limits inherit only
    # at fork. The envelope promises these caps, so no prlimit = refuse.
    prlimit_prefix = _payload_prlimit_prefix(cpu_s)
    if not prlimit_prefix:
        return ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase=phase, origin="engine",
            reason="prlimit unavailable — payload rlimits are part of the "
                   "probe envelope; refusing to run producer code uncapped",
        )
    # Phase-start handshake: bwrap reports the started child on this pipe,
    # so a nonzero exit WITHOUT it is a sandbox setup/bind failure (engine),
    # never mistaken for the payload failing (product).
    status_r, status_w = os.pipe()
    argv, env = sandbox.build_sandboxed_argv(
        prlimit_prefix + list(exec_argv), Path(scratch),
        allow_network=allow_network,
        pass_env=(),
        extra_binds=(snapshot.path, *extra_ro),
        tmpfs_size=_PROBE_TMPFS_SIZE,
        status_fd=status_w,
        # Pinned "standard": the operator's `trusted` posture forces network
        # ON for agent shells and must not widen this evidence gate — probes
        # honor `allow_network` exactly. `off`/bypass is refused above.
        profile="standard",
    )
    if env_extra:
        env = {**env, **env_extra}

    # The wall is rechecked HERE, immediately before Popen: engine-side
    # setup (prefix/pipe/argv construction) consumes the same absolute
    # budget, and a phase never starts past the wall — no bwrap, no race
    # for payload bytes ahead of the drain's first check.
    if time.monotonic() >= wall:
        os.close(status_r)
        os.close(status_w)
        return ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase=phase, origin="engine",
            reason=("overall verification deadline exceeded during engine "
                    "setup — engine ceiling, deliverable withheld"
                    if budget_bound else
                    "phase budget exhausted during engine setup — engine "
                    "setup unavailability, no deliverable byte ran"),
        )
    start = time.monotonic()
    try:
        # stdin=DEVNULL: fd 0 is EOF, never an operator-input/read-and-hang
        # channel. start_new_session: the child leads its own process
        # group so a timeout kills the WHOLE tree, not just bwrap. cwd is the
        # scratch, so a relative read can't reach the engine's launch dir.
        proc = subprocess.Popen(
            argv, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=str(Path(scratch)), start_new_session=True,
            pass_fds=(status_w,),
        )
    except OSError as exc:
        os.close(status_r)
        os.close(status_w)
        sandbox.note_bwrap_exec_failure()
        return ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase=phase, origin="engine",
            reason=f"sandbox invocation failed: {exc}",
            duration_s=time.monotonic() - start,
        )
    # Our copy of the write end closes now so the pipe hits EOF when bwrap
    # exits; bwrap holds the only other copy.
    os.close(status_w)
    # Parent-side clamp on the monitor pid stays as the belt (never
    # preexec_fn — forking rlimit setup from a multithreaded engine is the
    # deadlock); the argv-level prlimit prefix above is
    # what actually reaches the payload.
    _prlimit_child(proc.pid, cpu_s)
    timed_out = False
    try:
        raw = _drain_bounded(proc, wall)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        raw = b""
        sandbox.note_bwrap_exec_failure()
    duration = time.monotonic() - start
    tail = _clean_tail(raw)
    child_started = _status_handshake_seen(status_r)
    if timed_out:
        if budget_bound:
            # Killed at the TOTAL-budget boundary, not the phase's own limit:
            # engine policy ceiling, never a product hang.
            return ProbePhaseResult(
                status=ProbeStatus.ENGINE_UNAVAILABLE, phase=phase,
                origin="engine",
                reason="overall verification deadline exceeded during phase "
                       "(killed at the total-budget wall) — engine ceiling, "
                       "deliverable withheld",
                output_tail=tail, duration_s=duration,
            )
        # Deliverable-controlled hang: PRODUCT evidence (hang-to-escape closed).
        return ProbePhaseResult(
            status=ProbeStatus.PRODUCT_FAILED, phase=phase, origin="deliverable",
            reason=f"phase timeout after {timeout_s}s (deliverable-controlled)",
            output_tail=tail, duration_s=duration,
        )
    if proc.returncode == 0:
        return ProbePhaseResult(
            status=ProbeStatus.OK, phase=phase, origin="deliverable",
            reason="", returncode=0, output_tail=tail, duration_s=duration,
        )
    if not child_started:
        sandbox.note_bwrap_exec_failure()
        return ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase=phase, origin="engine",
            reason="sandbox setup failed before the payload started "
                   f"(no child-start handshake; bwrap exit {proc.returncode})",
            returncode=proc.returncode, output_tail=tail, duration_s=duration,
        )
    return ProbePhaseResult(
        status=ProbeStatus.PRODUCT_FAILED, phase=phase, origin="deliverable",
        reason=f"exit {proc.returncode}", returncode=proc.returncode,
        output_tail=tail, duration_s=duration,
    )


def _status_handshake_seen(status_r: int) -> bool:
    """Whether bwrap's ``--json-status-fd`` reported a started child.
    Called after the process is gone, so the pipe holds whatever bwrap
    wrote and hits EOF (our write-end copy closed at spawn). Bounded,
    non-blocking, and always closes the fd."""
    try:
        os.set_blocking(status_r, False)
        buf = b""
        while len(buf) < 65536:
            try:
                chunk = os.read(status_r, 4096)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            buf += chunk
        return b'"child-pid"' in buf
    finally:
        os.close(status_r)


def _payload_prlimit_prefix(timeout_s: int) -> "list[str]":
    """The ``prlimit`` argv prefix that establishes the probe caps on the
    payload itself at exec time (wrapped BEFORE bwrap so it lands after
    bwrap's ``--``). ``[]`` when ``prlimit`` is unavailable — the caller
    refuses rather than running uncapped."""
    from modulatio.tools import _prlimit_wrapper_prefix
    return _prlimit_wrapper_prefix(
        as_bytes=_PROBE_RLIMIT_AS, fsize_bytes=_PROBE_RLIMIT_FSIZE,
        cpu_s=min(timeout_s + 5, 3600))


def _prlimit_child(pid: int, timeout_s: int) -> None:
    """Apply per-process rlimits to an already-spawned child (prlimit, not
    preexec_fn). Best-effort — never raises into the caller."""
    import resource
    cpu = min(timeout_s + 5, 3600)
    for res, limit in (
        (resource.RLIMIT_AS, _PROBE_RLIMIT_AS),
        (resource.RLIMIT_FSIZE, _PROBE_RLIMIT_FSIZE),
        (resource.RLIMIT_CPU, cpu),
    ):
        try:
            resource.prlimit(pid, res, (limit, limit))
        except (ValueError, OSError, ProcessLookupError):
            pass


def _drain_bounded(proc, wall: float) -> bytes:
    """Read the child's merged output into a BOUNDED head+tail buffer while
    it runs — never hold the whole stream in engine memory (a flood
    can't OOM the engine). ``wall`` is an ABSOLUTE monotonic deadline,
    honored as a float through draining AND the post-EOF wait — a payload
    that closes stdout and lingers gains no rounding floor. Raises
    ``TimeoutExpired`` when the wall passes."""
    import os
    import select

    head = bytearray()          # first OUTPUT_TAIL_CAP bytes
    tail = bytearray()          # last OUTPUT_TAIL_CAP bytes
    total = 0
    t0 = time.monotonic()
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    while True:
        remaining = wall - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, time.monotonic() - t0)
        # select with a bounded wait so a SILENT hang (no output) still hits
        # the deadline — a plain blocking read would sleep past it forever.
        ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            continue
        if not chunk:
            break                                   # EOF: child closed stdout
        total += len(chunk)
        if len(head) < OUTPUT_TAIL_CAP:
            head += chunk[: OUTPUT_TAIL_CAP - len(head)]
        tail += chunk
        if len(tail) > OUTPUT_TAIL_CAP:
            del tail[:-OUTPUT_TAIL_CAP]
    remaining = wall - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(proc.args, time.monotonic() - t0)
    proc.wait(timeout=remaining)
    if total <= OUTPUT_TAIL_CAP:
        return bytes(head)                          # head holds all of it
    half = OUTPUT_TAIL_CAP // 2
    return bytes(head[:half]) + b"\n... [bounded] ...\n" + bytes(tail[-half:])


def _kill_group(proc) -> None:
    """Kill the child's whole process group (start_new_session made it a
    group leader) so no forked descendant survives the timeout."""
    import os
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            break
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


def destroy_scratch(scratch: Path) -> None:
    """Remove the private scratch (``finally``): idempotent,
    never raises — read-only snapshot files are force-unlinked."""
    scratch = Path(scratch)
    if not scratch.exists():
        return

    def _chmod_retry(func, path, _exc):
        try:
            Path(path).chmod(0o700)
            func(path)
        except OSError:
            pass

    shutil.rmtree(scratch, onerror=_chmod_retry)


# ── pipeline steps 4-6 ─────────────────────────────────────────────────
#
# The execution split: BUILDING a wheel executes arbitrary
# PEP 517 / setup.py hooks → mandatory sandbox. INSTALLING a built wheel
# executes nothing (pip unzips + writes scripts; --no-compile keeps even
# bytecompilation out) → engine-side, still hermetic. Entry-point, import,
# and test probes EXECUTE producer code → back through run_probe_phase.

_WHEELHOUSE_ENV = "MODULATIO_WHEELHOUSE"


def wheelhouse_path() -> "Path | None":
    """The operator's approved local wheel source .
    ``MODULATIO_WHEELHOUSE`` wins; else ``<CONFIG_DIR>/wheelhouse`` when it
    exists (populate: ``pip download pytest hatchling setuptools wheel -d
    ~/.config/modulatio/wheelhouse``). None when absent — phases needing
    external bytes then report ENGINE_UNAVAILABLE(dependency_source), never
    a product failure and never a silent green."""
    raw = (os.environ.get(_WHEELHOUSE_ENV) or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_dir() else None
    from modulatio import config as _config

    p = Path(_config.CONFIG_DIR) / "wheelhouse"
    return p if p.is_dir() else None


#: Wheel-parse ceilings: an attacker-produced zip is bounded before
#: any member is read, so a zip bomb can't exhaust the engine.
_WHEEL_MAX_MEMBERS = 20_000
_WHEEL_MAX_TOTAL_UNCOMPRESSED = 512 * 1024**2   # 512 MiB
_WHEEL_MAX_METADATA_BYTES = 4 * 1024**2         # 4 MiB per metadata member
#: Count ceilings (same class): every console script mints an entry-point
#: PHASE and every module joins the import smoke — unbounded counts grow the
#: facts, the memo, and the verify prompt with them.
_WHEEL_MAX_CONSOLE_SCRIPTS = 32
_WHEEL_MAX_MODULES = 512


class WheelInspectError(ValueError):
    """A wheel that can't be parsed within the ceilings — the caller maps it
    to PRODUCT_FAILED (a malformed wheel is product evidence)."""


def wheelhouse_fingerprint() -> str:
    """A CONTENT fingerprint of the wheel source: streamed SHA-256 over
    every wheel's bytes, joined with its normalized name. Any byte change —
    including a same-size swap with the original timestamps restored —
    changes this, so the facts memo invalidates instead of reusing a
    stale-green result (a stat triple is spoofable; bytes are not). The
    wheelhouse is operator-approved and small, so the read cost is
    appropriate. Empty string when absent."""
    wh = wheelhouse_path()
    if wh is None:
        return ""
    h = hashlib.sha256()
    for whl in sorted(wh.glob("*.whl")):
        try:
            h.update(whl.name.encode())
            h.update(b"\x00")
            with open(whl, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            h.update(b"\x00")
        except OSError:
            continue
    return h.hexdigest()


def build_wheel_phase(
    snapshot: Snapshot, scratch: Path, *,
    build_target: "Path | None" = None,
    timeout_s: int = DEFAULT_PHASE_TIMEOUT_S,
) -> ProbePhaseResult:
    """Step 4: build ONE wheel from the snapshot, inside the sandbox
    (build hooks are arbitrary code), backend resolved ONLY from the local
    wheelhouse. ``build_target`` is the SELECTED packaging root (defaults to
    the snapshot root; a nested pyproject is built where it lives).
    No wheelhouse → ENGINE_UNAVAILABLE(dependency_source), named."""
    wh = wheelhouse_path()
    if wh is None:
        return ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase="wheel", origin="engine",
            reason="no approved local wheelhouse configured "
                   f"(${_WHEELHOUSE_ENV}) — cannot provision a build backend "
                   "hermetically (dependency_source)",
        )
    wheels_dir = Path(scratch) / "wheels"
    wheels_dir.mkdir(parents=True, exist_ok=True)
    # Build from a WRITABLE copy, not from the read-only snapshot bind.
    # setuptools' build_meta writes an .egg-info directory INTO the source
    # tree while resolving build requirements, so a read-only source fails
    # before the backend runs — and the failure reads as the deliverable's
    # fault when it is the mount that refused. The snapshot itself stays
    # pristine, so its content hash still verifies once the phases finish.
    src = Path(build_target) if build_target is not None else snapshot.path
    build_dir = Path(scratch) / "build"
    shutil.rmtree(build_dir, ignore_errors=True)
    try:
        shutil.copytree(src, build_dir)
    except OSError as exc:
        return ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase="wheel", origin="engine",
            reason=f"could not stage a writable build tree: {exc}"[:300],
        )
    target = str(build_dir)
    return run_probe_phase(
        [sys.executable, "-m", "pip", "wheel", "--no-index",
         "--find-links", str(wh), "--no-deps", "--no-cache-dir",
         "--wheel-dir", str(wheels_dir), target],
        phase="wheel", snapshot=snapshot, scratch=Path(scratch),
        timeout_s=timeout_s,
        # /home is tmpfs inside the mount — the wheel source must be an
        # explicit RO bind or the isolated build env sees an empty path.
        extra_ro=(wh,),
    )


def _validated_wheel_metadata(wheels: "list[Path]"):
    """Exactly-one-wheel guard + bounded parse: return the metadata
    dict, or a ``ProbePhaseResult`` (PRODUCT_FAILED) when zero/many wheels or
    a malformed/oversized one is present — never raise into the rollup."""
    if len(wheels) == 0:
        return ProbePhaseResult(
            status=ProbeStatus.PRODUCT_FAILED, phase="wheel_inspect",
            origin="deliverable", reason="build produced no wheel")
    if len(wheels) > 1:
        return ProbePhaseResult(
            status=ProbeStatus.PRODUCT_FAILED, phase="wheel_inspect",
            origin="deliverable",
            reason=f"build produced {len(wheels)} wheels (expected exactly one) "
                   "— a build hook may have planted extra artifacts")
    try:
        return inspect_wheel_metadata(wheels[0])
    except (WheelInspectError, Exception) as exc:  # noqa: BLE001 — any parse
        return ProbePhaseResult(
            status=ProbeStatus.PRODUCT_FAILED, phase="wheel_inspect",
            origin="deliverable", reason=f"malformed wheel: {exc}"[:300])


def inspect_wheel_metadata(wheel: Path) -> dict:
    """Step 5a: wheel facts WITHOUT importing (stdlib zip + email
    parse) — name, version, declared requirements, console scripts, and the
    module set. Bounded before any read: member count, total
    uncompressed size, and per-metadata-member size are capped, so a zip
    bomb raises ``WheelInspectError`` instead of exhausting the engine."""
    import email
    import zipfile

    with zipfile.ZipFile(wheel) as z:
        infos = z.infolist()
        if len(infos) > _WHEEL_MAX_MEMBERS:
            raise WheelInspectError(f"{len(infos)} members > {_WHEEL_MAX_MEMBERS}")
        total = sum(i.file_size for i in infos)
        if total > _WHEEL_MAX_TOTAL_UNCOMPRESSED:
            raise WheelInspectError(f"uncompressed {total}B over cap")
        names = [i.filename for i in infos]
        dist_hits = [n for n in names if n.endswith("/METADATA")]
        if not dist_hits:
            raise WheelInspectError("no METADATA member")
        dist = dist_hits[0].split("/")[0]

        def _read_capped(member: str) -> bytes:
            info = z.getinfo(member)
            if info.file_size > _WHEEL_MAX_METADATA_BYTES:
                raise WheelInspectError(f"{member} {info.file_size}B over cap")
            return z.read(member)

        md = email.message_from_bytes(_read_capped(f"{dist}/METADATA"))
        scripts: dict[str, str] = {}
        ep_name = f"{dist}/entry_points.txt"
        if ep_name in names:
            section = None
            for line in _read_capped(ep_name).decode(errors="replace").splitlines():
                line = line.strip()
                if line.startswith("["):
                    section = line.strip("[]")
                elif "=" in line and section == "console_scripts":
                    k, _, v = line.partition("=")
                    scripts[k.strip()] = v.strip()
        modules: set[str] = set()
        for n in names:
            if n.endswith(".py") and not n.startswith(dist):
                parts = n[:-3].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if parts:
                    modules.add(".".join(parts))
    if len(scripts) > _WHEEL_MAX_CONSOLE_SCRIPTS:
        raise WheelInspectError(
            f"{len(scripts)} console scripts > {_WHEEL_MAX_CONSOLE_SCRIPTS}")
    if len(modules) > _WHEEL_MAX_MODULES:
        raise WheelInspectError(
            f"{len(modules)} modules > {_WHEEL_MAX_MODULES}")
    return {
        "name": md.get("Name", ""),
        "version": md.get("Version", ""),
        "requires_dist": md.get_all("Requires-Dist") or [],
        "extras": sorted(md.get_all("Provides-Extra") or []),
        "console_scripts": scripts,
        "modules": sorted(modules),
    }


#: Env that neutralizes ambient pip/user config INSIDE the sandbox :
#: no operator pip.conf, no user site-packages, no cache — the install is
#: reproducible and can't be steered by host configuration.
_HERMETIC_PIP_ENV = {
    "PIP_CONFIG_FILE": "/dev/null",
    "PYTHONNOUSERSITE": "1",
    "PIP_NO_INPUT": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}

#: The system interpreter inside the empty-root mount (/usr is bound RO);
#: venv creation + the engine pip frontend run as THIS, never the live venv.
_SANDBOX_PYTHON = "/usr/bin/python3"


def create_pristine_env(
    scratch: Path, *, snapshot: "Snapshot | None" = None,
    timeout_s: int = 120,
) -> "tuple[Path, ProbePhaseResult]":
    """Step 5b: the pristine judged environment — a fresh venv built
    INSIDE the sandbox (provisioning is contained, not host-side),
    ``--without-pip`` so the engine frontend never joins the judged env. Its
    base interpreter is the sandbox's ``/usr`` python, valid in every
    subsequent sandboxed phase. Returns (env_path, result); a venv failure
    is ENGINE provisioning, not product evidence."""
    env = Path(scratch) / "envs" / "pristine"
    snap = snapshot or _null_snapshot(scratch)
    res = run_probe_phase(
        [_SANDBOX_PYTHON, "-m", "venv", "--without-pip", str(env)],
        phase="mkenv", snapshot=snap, scratch=Path(scratch), timeout_s=timeout_s)
    if res.status is not ProbeStatus.OK:
        # A venv that won't build is the engine's provisioning failure.
        res = ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase="mkenv", origin="engine",
            reason="pristine venv creation failed", output_tail=res.output_tail,
            duration_s=res.duration_s)
    return env, res


def _null_snapshot(scratch: Path) -> "Snapshot":
    """A trivial empty snapshot for provisioning phases that don't read the
    deliverable (venv create / clone) — keeps the RO bind uniform."""
    p = Path(scratch) / "snapshot"
    p.mkdir(parents=True, exist_ok=True)
    return Snapshot(path=p, content_hash="sha256:", _entries=())


_NO_DIST_RE = re.compile(
    r"No matching distribution found for ([A-Za-z0-9._-]+)")

#: The explicit test-extra selection set: ONLY these declared extras install
#: for the test phase — docs/gpu/dev extras never ride along.
_SELECTED_TEST_EXTRAS = ("test", "tests", "testing")


def _selected_test_extras(extras: "list[str]") -> "list[str]":
    """The subset of a wheel's declared extras that the engine installs for
    testing (recorded in the facts; empty selection adds nothing)."""
    return [e for e in extras if e in _SELECTED_TEST_EXTRAS]


_PEP508_ENV_CACHE: "dict | None" = None


def _pep508_env() -> "dict | None":
    """PEP 508 marker environment of the TARGET (sandbox) interpreter —
    dumped by running it, never assumed from the engine's own venv (their
    versions can differ). Process-static; ``None`` when the dump fails
    (callers then treat direct references conservatively as active)."""
    global _PEP508_ENV_CACHE
    if _PEP508_ENV_CACHE is None:
        import json as _json
        prog = (
            "import json, os, platform, sys\n"
            "print(json.dumps({\n"
            " 'implementation_name': sys.implementation.name,\n"
            " 'implementation_version':"
            " '{}.{}.{}'.format(*sys.implementation.version[:3]),\n"
            " 'os_name': os.name,\n"
            " 'platform_machine': platform.machine(),\n"
            " 'platform_python_implementation':"
            " platform.python_implementation(),\n"
            " 'platform_release': platform.release(),\n"
            " 'platform_system': platform.system(),\n"
            " 'platform_version': platform.version(),\n"
            " 'python_full_version': platform.python_version(),\n"
            " 'python_version':"
            " '.'.join(platform.python_version_tuple()[:2]),\n"
            " 'sys_platform': sys.platform}))\n")
        try:
            out = subprocess.run([_SANDBOX_PYTHON, "-c", prog],
                                 capture_output=True, text=True, timeout=30)
            _PEP508_ENV_CACHE = (
                _json.loads(out.stdout) if out.returncode == 0 else {})
        except Exception:  # noqa: BLE001 — dump failure = conservative mode
            _PEP508_ENV_CACHE = {}
    return _PEP508_ENV_CACHE or None


def _direct_ref_violation(
    requires_dist: "list[str]", active_extras: "tuple[str, ...]",
) -> "str | None":
    """The first ACTIVE PEP 508 direct reference ("name @ scheme://…", incl.
    VCS forms) in ``requires_dist``, or ``None``. Decided by requirement
    inspection BEFORE any spawn — one exact named state, never whichever
    text pip prints. Markers are evaluated against the TARGET interpreter
    environment plus each active extra: an INACTIVE optional extra's URL
    must not manufacture a withhold. Malformed metadata raises
    ``ValueError`` (product failure at the caller)."""
    from packaging.requirements import InvalidRequirement, Requirement

    env = _pep508_env()
    contexts = [{"extra": e} for e in ("", *active_extras)]
    for raw in requires_dist:
        try:
            req = Requirement(raw)
        except InvalidRequirement as exc:
            raise ValueError(f"malformed requirement {raw!r}: {exc}") from exc
        if req.url is None:
            continue
        if req.marker is None or env is None:
            return raw          # unconditional, or no target env: active
        for ctx in contexts:
            try:
                if req.marker.evaluate({**env, **ctx}):
                    return raw
            except Exception:  # noqa: BLE001 — unevaluable marker: active
                return raw
    return None


def install_wheels_phase(
    env: Path, wheels: "list[Path]", *, wheelhouse: "Path | None",
    snapshot: "Snapshot | None" = None, scratch: "Path | None" = None,
    timeout_s: int = DEFAULT_PHASE_TIMEOUT_S,
) -> ProbePhaseResult:
    """Step 5c: hermetic BASE install of the built wheel(s) + their
    DECLARED dependencies into the pristine env — no extras ever (selected
    test extras are test-environment input via ``provision_test_env``) —
    INSIDE the sandbox with the network unshared (a built wheel's
    ``dep @ http://…`` direct-URL or VCS requirement CANNOT connect; ambient
    pip config is neutralized). Attribution is mechanical : an ACTIVE
    declared direct-URL/VCS requirement or a missing DECLARED distribution
    → ENGINE_UNAVAILABLE(dependency_source) named; any other nonzero →
    product."""
    scratch = Path(scratch) if scratch is not None else env.parent.parent
    for w in wheels:
        try:
            reqs = inspect_wheel_metadata(w)["requires_dist"]
        except Exception as exc:  # noqa: BLE001 — malformed = product
            return ProbePhaseResult(
                status=ProbeStatus.PRODUCT_FAILED, phase="install",
                origin="deliverable",
                reason=f"malformed wheel {w.name}: {exc}"[:300])
        try:
            bad = _direct_ref_violation(reqs, active_extras=())
        except ValueError as exc:
            return ProbePhaseResult(
                status=ProbeStatus.PRODUCT_FAILED, phase="install",
                origin="deliverable",
                reason=f"malformed requirement metadata: {exc}"[:300])
        if bad:
            return ProbePhaseResult(
                status=ProbeStatus.ENGINE_UNAVAILABLE, phase="install",
                origin="engine",
                reason=(f"declared direct-URL/VCS requirement {bad!r} "
                        "cannot be satisfied from the approved local "
                        "source under the no-network policy "
                        "(dependency_source)")[:300])
    snap = snapshot or _null_snapshot(scratch)
    argv = [_SANDBOX_PYTHON, "-m", "pip", "--python", str(env / "bin" / "python"),
            "install", "--no-index", "--no-cache-dir", "--no-compile"]
    if wheelhouse is not None:
        argv += ["--find-links", str(wheelhouse)]
    argv += [str(w) for w in wheels]
    res = run_probe_phase(
        argv, phase="install", snapshot=snap, scratch=scratch,
        timeout_s=timeout_s, allow_network=False, env_extra=_HERMETIC_PIP_ENV,
        extra_ro=(wheelhouse,) if wheelhouse is not None else ())
    if res.status is ProbeStatus.OK:
        return res
    missing = _NO_DIST_RE.search(res.output_tail)
    if missing:
        return ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase="install",
            origin="engine",
            reason=f"declared dependency {missing.group(1)!r} absent from the "
                   "approved local source (dependency_source)",
            returncode=res.returncode, output_tail=res.output_tail,
            duration_s=res.duration_s,
        )
    return ProbePhaseResult(
        status=ProbeStatus.PRODUCT_FAILED, phase="install",
        origin="deliverable", reason=f"install exit {res.returncode}",
        returncode=res.returncode, output_tail=res.output_tail,
        duration_s=res.duration_s,
    )


def entry_point_probe(
    env: Path, script: str, *, snapshot: Snapshot, scratch: Path,
    timeout_s: int = 60,
) -> ProbePhaseResult:
    """Step 6: every declared console script resolves and answers
    ``--help`` without a traceback — producer code, so sandboxed."""
    return run_probe_phase(
        [str(env / "bin" / "python"), str(env / "bin" / script), "--help"],
        phase="entry_point", snapshot=snapshot, scratch=Path(scratch),
        timeout_s=timeout_s,
    )


def import_smoke_phase(
    env: Path, modules: "list[str]", *, snapshot: Snapshot, scratch: Path,
    timeout_s: int = 120,
) -> ProbePhaseResult:
    """Step 6: per-module import in the PRISTINE env (never a
    sys.path shim — undeclared deps must fail here by name, the missing-dependency class). ONE sandbox, a FRESH interpreter per module:
    an early module mutating sys.modules/sys.path/env/import hooks cannot
    green a later one. -P on parent and child keeps the cwd off sys.path
    (source must never shadow the installed wheel)."""
    prog = (
        "import subprocess, sys\n"
        "failed = []\n"
        f"for m in {modules!r}:\n"
        "    r = subprocess.run(\n"
        "        [sys.executable, '-P', '-c',\n"
        "         'import importlib, sys; importlib.import_module(sys.argv[1])', m],\n"
        "        capture_output=True, text=True)\n"
        "    if r.returncode != 0:\n"
        "        line = (r.stderr.strip().splitlines() or ['?'])[-1]\n"
        "        failed.append(f'{m}: {line}')\n"
        "if failed:\n"
        "    print('IMPORT FAILURES:'); [print(f) for f in failed]\n"
        "    sys.exit(1)\n"
    )
    res = run_probe_phase(
        [str(env / "bin" / "python"), "-P", "-c", prog],
        phase="import_smoke", snapshot=snapshot, scratch=Path(scratch),
        timeout_s=timeout_s, env_extra={"PYTHONPATH": ""},
    )
    if res.status is ProbeStatus.PRODUCT_FAILED and "IMPORT FAILURES" in res.output_tail:
        failed = [ln.split(":")[0].strip()
                  for ln in res.output_tail.splitlines()
                  if ln and ":" in ln and not ln.startswith("IMPORT")]
        return ProbePhaseResult(
            status=res.status, phase=res.phase, origin=res.origin,
            reason="import-dead modules: " + ", ".join(failed[:8]),
            returncode=res.returncode, output_tail=res.output_tail,
            duration_s=res.duration_s,
        )
    return res


def provision_test_env(
    env: Path, wheels: "list[Path]", *, extras: "tuple[str, ...]" = (),
    snapshot: "Snapshot | None" = None, scratch: "Path | None" = None,
    timeout_s: int = DEFAULT_PHASE_TIMEOUT_S,
) -> "tuple[Path, ProbePhaseResult]":
    """The DISPOSABLE runner environment (the two-env split): clone the
    proven pristine env, then seed the engine's runner (pytest) and the
    policy-selected test extras — TEST-ENVIRONMENT input only, the pristine
    product closure never sees them. Everything resolves from the approved
    local wheel bundle; there is no fallback to the live venv's pytest
    (that environment is too contaminated for packaging judgment). Returns
    (test_env, result); every failure is ENGINE provisioning."""
    scratch = Path(scratch) if scratch is not None else env.parent.parent
    snap = snapshot or _null_snapshot(scratch)
    test_env = scratch / "envs" / "test"
    wh = wheelhouse_path()
    if wh is None or not any(wh.glob("pytest-*.whl")):
        return test_env, ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase="test_env",
            origin="engine",
            reason="engine runner bundle unavailable — no pytest wheel in the "
                   "approved local source; never falling back to the live venv",
        )
    if extras:
        # Selecting an extra ACTIVATES its markers: a direct-URL requirement
        # behind the selected extra is the same named pre-spawn refusal the
        # base install applies (the base scan ran with no extras active).
        for w in wheels:
            try:
                reqs = inspect_wheel_metadata(w)["requires_dist"]
                bad = _direct_ref_violation(reqs, active_extras=tuple(extras))
            except Exception as exc:  # noqa: BLE001 — malformed = product
                return test_env, ProbePhaseResult(
                    status=ProbeStatus.PRODUCT_FAILED, phase="test_env",
                    origin="deliverable",
                    reason=f"malformed requirement metadata: {exc}"[:300])
            if bad:
                return test_env, ProbePhaseResult(
                    status=ProbeStatus.ENGINE_UNAVAILABLE, phase="test_env",
                    origin="engine",
                    reason=(f"declared direct-URL/VCS requirement {bad!r} "
                            "(active under selected extras) cannot be "
                            "satisfied from the approved local source under "
                            "the no-network policy (dependency_source)")[:300])
    clone = run_probe_phase(
        ["/bin/cp", "-a", str(env), str(test_env)],
        phase="test_env", snapshot=snap, scratch=scratch, timeout_s=120)
    if clone.status is not ProbeStatus.OK:
        return test_env, ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase="test_env",
            origin="engine",
            reason="test-env clone failed", output_tail=clone.output_tail)
    # Selected extras ride the requirement form so their declared deps
    # resolve from the wheelhouse under the same gates; empty selection
    # adds nothing beyond the runner.
    suffix = f"[{','.join(extras)}]" if extras else ""
    targets = ["pytest"] + ([f"{w}{suffix}" for w in wheels] if extras else [])
    seed = run_probe_phase(
        [_SANDBOX_PYTHON, "-m", "pip", "--python", str(test_env / "bin" / "python"),
         "install", "--no-index", "--no-cache-dir", "--no-compile",
         "--find-links", str(wh), *targets],
        phase="test_env", snapshot=snap, scratch=scratch, timeout_s=timeout_s,
        allow_network=False, env_extra=_HERMETIC_PIP_ENV, extra_ro=(wh,))
    if seed.status is not ProbeStatus.OK:
        return test_env, ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase="test_env",
            origin="engine",
            reason="runner/extras seeding failed", output_tail=seed.output_tail)
    return test_env, seed


def run_tests_phase(
    test_env: Path, *, snapshot: Snapshot, scratch: Path,
    timeout_s: int = DEFAULT_PHASE_TIMEOUT_S,
) -> ProbePhaseResult:
    """Step 7: the deliverable's OWN suite under the ENGINE'S runner,
    in the env ``provision_test_env`` built. Plugin autoload is disabled.
    Tests execute FROM THE SNAPSHOT (suites aren't installed), sandboxed."""
    scratch = Path(scratch)
    # -P + importlib import mode + empty PYTHONPATH: the WHEEL is the judged
    # artifact. Default prepend mode walks a packaged tests/ tree up to the
    # snapshot root and puts it on sys.path — product SOURCE then shadows
    # the installed wheel and a test can green a module the wheel omitted.
    res = run_probe_phase(
        [str(test_env / "bin" / "python"), "-P", "-m", "pytest",
         "--import-mode=importlib",
         str(snapshot.path), "-q", "-p", "no:cacheprovider"],
        phase="test", snapshot=snapshot, scratch=scratch, timeout_s=timeout_s,
        env_extra={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": "",
                   **_HERMETIC_PIP_ENV},
    )
    if res.returncode == 5:
        # pytest exit 5: no tests collected — "no green evidence" (the same
        # verdict the goal pytest gate renders), product-attributed.
        return ProbePhaseResult(
            status=ProbeStatus.PRODUCT_FAILED, phase="test",
            origin="deliverable", reason="no tests collected (no green evidence)",
            returncode=5, output_tail=res.output_tail, duration_s=res.duration_s,
        )
    return res


def pip_check_phase(
    env: Path, *, snapshot: Snapshot, scratch: Path, timeout_s: int = 60,
) -> ProbePhaseResult:
    """A `pip check` on the pristine judged env : a broken/inconsistent
    declared-dependency tree is product evidence, distinct from a bad import."""
    res = run_probe_phase(
        [_SANDBOX_PYTHON, "-m", "pip", "--python", str(env / "bin" / "python"),
         "check"],
        phase="pip_check", snapshot=snapshot, scratch=Path(scratch),
        timeout_s=timeout_s, env_extra=_HERMETIC_PIP_ENV)
    return res


#: Explicit ceiling for the dedicated manifest result file — exceeding it is
#: a TYPED unavailable outcome, never a spliced "complete" manifest.
_MANIFEST_FILE_CAP = 512 * 1024


def env_manifest(
    env: Path, *, snapshot: Snapshot, scratch: Path, phase: str = "manifest",
) -> "tuple[list[str], ProbePhaseResult]":
    """The installed-distribution manifest of ``env`` (product and runner
    manifests are recorded separately), TYPED: returns (manifest, phase
    result). Evidence travels through a DEDICATED result file in the
    scratch, not the bounded output excerpt — a large listing through the
    head+tail spool would silently lose its middle while presenting as
    complete. Over the explicit cap, or any collection failure, is a
    recorded engine phase — a green digest must never silently lack its
    claimed manifest evidence."""
    out_path = Path(scratch) / f"{phase}.freeze.txt"
    prog = (
        "import subprocess, sys\n"
        f"out = open({str(out_path)!r}, 'w')\n"
        "r = subprocess.run(\n"
        f"    [sys.executable, '-m', 'pip', '--python',\n"
        f"     {str(env / 'bin' / 'python')!r}, 'freeze', '--all'],\n"
        "    stdout=out, stderr=subprocess.STDOUT)\n"
        "sys.exit(r.returncode)\n")
    res = run_probe_phase(
        [_SANDBOX_PYTHON, "-c", prog],
        phase=phase, snapshot=snapshot, scratch=Path(scratch),
        timeout_s=60, env_extra=_HERMETIC_PIP_ENV)
    if res.status is not ProbeStatus.OK:
        # Collection failed: engine evidence tooling, disclosed as such.
        return [], ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase=phase,
            origin="engine",
            reason="manifest collection failed — the digest cannot carry "
                   "its claimed installed-distribution evidence",
            returncode=res.returncode, output_tail=res.output_tail,
            duration_s=res.duration_s)
    try:
        if out_path.stat().st_size > _MANIFEST_FILE_CAP:
            return [], ProbePhaseResult(
                status=ProbeStatus.ENGINE_UNAVAILABLE, phase=phase,
                origin="engine",
                reason=f"manifest exceeds the {_MANIFEST_FILE_CAP}-byte "
                       "evidence cap — typed truncated, never a spliced "
                       "excerpt presented as complete")
        text = out_path.read_text(encoding="utf-8", errors="replace")
        out_path.unlink()
    except OSError:
        return [], ProbePhaseResult(
            status=ProbeStatus.ENGINE_UNAVAILABLE, phase=phase,
            origin="engine",
            reason="manifest result file unreadable — evidence unavailable")
    # pip freeze emits "name==ver" for indexed installs and "name @ file://…"
    # for wheel installs — keep both requirement forms, drop banners/blanks.
    manifest = sorted(
        ln.strip() for ln in text.splitlines()
        if ln.strip() and ("==" in ln or " @ " in ln) and not ln.startswith("#"))
    return manifest, res


def run_execution_probes(
    units_used: "list[str]", artifacts_root: Path, *, scratch_root: Path,
) -> dict:
    """Step 8's engine: the fixed order (snapshot → wheel → metadata →
    pristine env → install → entry points → import smoke → tests) as ONE
    call, returning the execution-digest FACTS. Scratch dies in
    ``finally``; the snapshot hash is verified after the phases; overall
    status is mechanical: any ENGINE_UNAVAILABLE dominates (the gate could
    not measure), else any PRODUCT_FAILED, else ok."""
    from modulatio import assembly as _assembly

    pk = _assembly._packaging_facts(units_used, artifacts_root)
    if pk["root"] is None:
        return {
            "status": "not_applicable",
            "reason": "no supported Python packaging shape detected"
            if not pk["candidates"] else
            "ambiguous packaging roots — layout hard issue rules first",
            "packaging": pk,
        }
    scratch = Path(scratch_root) / "probe"
    phases: list[ProbePhaseResult] = []
    facts: dict = {"install_mode": "hermetic", "test_extras": [],
                   "packaging": pk,
                   # The accepted provenance envelope, disclosed on the
                   # digest surface: complete undeclared-dependency
                   # verification is NOT claimed.
                   "import_provenance": (
                       "eager imports audited in the pristine product "
                       "environment; lazy product imports exercised only "
                       "after runner installation are not provenance "
                       "audited and may be satisfied by the runner closure")}
    start = time.monotonic()
    # The hard wall every phase reads through _DEADLINE_VAR (a phase never
    # starts past it and runs under min(own limit, remaining) — the ceiling
    # binds mid-phase and during wheel build).
    _deadline_token = _DEADLINE_VAR.set(start + DIGEST_DEADLINE_S)

    def _over_deadline() -> bool:
        # Loop/short-circuit checkpoint between phase groups: the SUM of
        # phases exceeding the overall ceiling is an ENGINE policy limit,
        # distinct from a per-phase product hang. Skips the record when the
        # wall already stamped one (a phase killed/refused at the boundary).
        if time.monotonic() - start > DIGEST_DEADLINE_S:
            if not (phases
                    and phases[-1].status is ProbeStatus.ENGINE_UNAVAILABLE
                    and "deadline" in phases[-1].reason):
                phases.append(ProbePhaseResult(
                    status=ProbeStatus.ENGINE_UNAVAILABLE, phase="deadline",
                    origin="engine",
                    reason="overall verification deadline "
                           f"({DIGEST_DEADLINE_S}s) exceeded — engine "
                           "ceiling, deliverable withheld",
                    duration_s=time.monotonic() - start))
            return True
        return False

    try:
        try:
            snap = materialize_snapshot(units_used, artifacts_root, scratch)
        except SnapshotError as exc:
            return {**facts, "status": "product_failed",
                    "reason": f"snapshot refused: {exc}", "phases": []}
        facts["snapshot_hash"] = snap.content_hash
        # Build the SELECTED packaging root (a nested pyproject must be
        # targeted where it lives, not the closure root).
        build_target = snap.path if pk["root"] in (".", None) else snap.path / pk["root"]

        built = build_wheel_phase(snap, scratch, build_target=build_target)
        phases.append(built)
        if built.status is ProbeStatus.OK and not _over_deadline():
            wheels = sorted((scratch / "wheels").glob("*.whl"))
            md_or_fail = _validated_wheel_metadata(wheels)
            if isinstance(md_or_fail, ProbePhaseResult):
                phases.append(md_or_fail)               # zero/many/corrupt wheel
            else:
                md = md_or_fail
                facts["wheel"] = md
                # Explicit selected test extras: of the wheel's declared
                # extras, ONLY the test-shaped set — and it is
                # TEST-ENVIRONMENT input, never pristine input (the two-env
                # split would otherwise be false at install time: a runtime
                # import satisfied by a test extra's closure would false-
                # green the eager smoke). Recorded as a fact; a pure
                # function of the wheel = of the snapshot, so the memo key
                # already encodes it.
                selected_extras = _selected_test_extras(md.get("extras", []))
                facts["test_extras"] = selected_extras
                env, mkenv = create_pristine_env(scratch, snapshot=snap)
                phases.append(mkenv)
                if mkenv.status is ProbeStatus.OK:
                    inst = install_wheels_phase(
                        env, wheels, wheelhouse=wheelhouse_path(),
                        snapshot=snap, scratch=scratch)
                    phases.append(inst)
                    if inst.status is ProbeStatus.OK:
                        # product manifest = the pristine (judged) env,
                        # BEFORE any runner byte — typed: a failed
                        # collection is a recorded phase, never a silent [].
                        facts["product_manifest"], man_res = env_manifest(
                            env, snapshot=snap, scratch=scratch)
                        phases.append(man_res)
                        phases.append(pip_check_phase(
                            env, snapshot=snap, scratch=scratch))
                        for script in md["console_scripts"]:
                            if _over_deadline():
                                break
                            phases.append(entry_point_probe(
                                env, script, snapshot=snap, scratch=scratch))
                        if md["modules"] and not _over_deadline():
                            phases.append(import_smoke_phase(
                                env, md["modules"], snapshot=snap, scratch=scratch))
                        if not _over_deadline():
                            test_env, prov = provision_test_env(
                                env, wheels, extras=tuple(selected_extras),
                                snapshot=snap, scratch=scratch)
                            phases.append(prov)
                            if prov.status is ProbeStatus.OK:
                                # Runner manifest: recorded AFTER
                                # provisioning, BEFORE any product test
                                # byte runs — untrusted test code cannot
                                # forge the claimed runner closure.
                                facts["runner_manifest"], rman = env_manifest(
                                    test_env, snapshot=snap, scratch=scratch,
                                    phase="runner_manifest")
                                phases.append(rman)
                                if (rman.status is ProbeStatus.OK
                                        and not _over_deadline()):
                                    phases.append(run_tests_phase(
                                        test_env, snapshot=snap,
                                        scratch=scratch))
        if not snap.verify_unchanged():
            phases.append(ProbePhaseResult(
                status=ProbeStatus.ENGINE_UNAVAILABLE, phase="integrity",
                origin="engine",
                reason="snapshot mutated during probes — containment failed; "
                       "evidence void"))
    finally:
        _DEADLINE_VAR.reset(_deadline_token)
        # Own the whole tree we were handed: the mkdtemp parent, not just
        # <root>/probe.
        destroy_scratch(Path(scratch_root))
    facts["phases"] = [
        {"phase": r.phase, "status": r.status.value, "origin": r.origin,
         "reason": r.reason, "returncode": r.returncode,
         "output_tail": r.output_tail, "duration_s": round(r.duration_s, 2)}
        for r in phases
    ]
    if any(r.status is ProbeStatus.ENGINE_UNAVAILABLE for r in phases):
        facts["status"] = "engine_unavailable"
        facts["reason"] = next(r.reason for r in phases
                               if r.status is ProbeStatus.ENGINE_UNAVAILABLE)
    elif any(r.status is ProbeStatus.PRODUCT_FAILED for r in phases):
        facts["status"] = "product_failed"
        facts["reason"] = "; ".join(
            f"{r.phase}: {r.reason}" for r in phases
            if r.status is ProbeStatus.PRODUCT_FAILED)[:500]
    else:
        facts["status"] = "ok"
        facts["reason"] = ""
    return facts
