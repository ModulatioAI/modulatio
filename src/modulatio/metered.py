# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Metered-tool tier — the engine-side spend authorizer (Part B4).

The free-local default (ddgs ``web_search``, ``http_get``, ``run_shell``) runs
unmetered. A tool that costs real money per call sets ``Tool.cost_class`` to
``paid-cloud`` / ``premium-cloud``; the engine then gates every such call BEFORE it
spends. This module builds the gate the producer tool-loop calls
(``runners.run_llm_with_tools(metered_authorizer=...)``), enforcing the contract the
Part B review required (Nemo #6/#7):

  1. **Narrow params.** A metered tool's args may name pinned artifacts + options —
     never an arbitrary URL / endpoint / body / headers (no LLM-controlled SSRF or
     spend target).
  2. **Ledger-pinned inputs.** It only ever runs on QC-passed artifacts whose bytes
     are unchanged (``review_ledger.verify_unit``) — never pay to process a drifted
     or unverified input.
  3. **Idempotency.** The spend key is a hash of the pinned-input checksums + the
     tool + the caller's options, so a retry of the identical call isn't re-charged.
  4. **Fail-closed authorization.** ``comptroller.authorize_metered_tool`` denies on
     unknown/missing cost_class, missing budget config, per-task cap, or daily cap.

This module holds NO provider/key — the first real paid adapter (its own ``Tool``
with a ``call`` that reads its own key from env) lands when a genuine need appears.
The mechanism is proven here by a test double.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Callable

from modulatio import comptroller, review_ledger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from modulatio.types import Task


#: Arg keys a metered tool must NOT accept — they would let the model choose the
#: network target / payload (SSRF + arbitrary spend). The contract is narrow params
#: (pinned artifact ids + bounded options) only. Checked EXACTLY (case-insensitive)
#: AND by substring token (so ``input_url`` / ``base_url`` / ``callback_url`` /
#: ``hostname`` are caught too), RECURSIVELY through nested dicts/lists, with a
#: URL-like string-VALUE backstop so an endpoint can't ride in under any key name.
FORBIDDEN_ARG_KEYS = frozenset(
    {"url", "endpoint", "uri", "host", "hostname", "body", "headers",
     "data", "payload", "method", "request", "target", "link", "addr",
     "server", "proxy", "base_url", "api_base", "webhook", "redirect", "callback"}
)

#: Substring tokens: any key CONTAINING one of these is treated as a network param.
_FORBIDDEN_KEY_TOKENS = ("url", "uri", "endpoint", "host", "webhook", "proxy")

#: A string value that looks like an endpoint (``scheme://...``). The strong
#: backstop: the model can't smuggle a target through an innocuous key name without
#: the value itself looking like a URL.
_URL_LIKE_RE = re.compile(r"^\s*[a-z][a-z0-9+.\-]*://", re.IGNORECASE)

#: Max nesting depth the scan will descend through LLM-supplied tool-call args.
#: The args object is model-controlled, so a pathologically deep nest would blow the
#: Python recursion limit. We bound the descent and treat over-depth as FORBIDDEN
#: (deny) — consistent with the narrow-param/fail-closed contract: legitimate metered
#: args are pinned ids + bounded options, never a deeply nested blob.
_MAX_SCAN_DEPTH = 32


def _scan_for_network_params(obj: object, path: str = "", _depth: int = 0) -> "list[str]":
    """Recursively find forbidden network keys or URL-like string values. Returns
    a list of offending dotted paths (empty = clean). Descent is depth-bounded
    (``_MAX_SCAN_DEPTH``): args nested past the bound are denied rather than risking a
    RecursionError on model-controlled input."""
    if _depth > _MAX_SCAN_DEPTH:
        return [f"{path}<nested past max depth {_MAX_SCAN_DEPTH}>"]
    bad: list[str] = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            kl = str(key).lower()
            if kl in FORBIDDEN_ARG_KEYS or any(tok in kl for tok in _FORBIDDEN_KEY_TOKENS):
                bad.append(f"{path}{key}")
            bad.extend(_scan_for_network_params(val, f"{path}{key}.", _depth + 1))
    elif isinstance(obj, (list, tuple)):
        for i, val in enumerate(obj):
            bad.extend(_scan_for_network_params(val, f"{path}[{i}].", _depth + 1))
    elif isinstance(obj, str) and _URL_LIKE_RE.match(obj):
        bad.append(f"{path}<url-like value>")
    return bad


def metered_idempotency_key(
    tool_name: str, pinned_checksums: "list[str]", options: dict
) -> str:
    """A stable spend key over the pinned-input checksums + tool + options. Same
    inputs + same options → same key → the Comptroller treats a retry as
    idempotent (not re-charged)."""
    h = hashlib.sha256()
    h.update(tool_name.encode())
    for c in sorted(pinned_checksums):
        h.update(b"\x00")
        h.update(c.encode())
    h.update(b"\x00opts\x00")
    h.update(json.dumps(options, sort_keys=True, default=str).encode())
    return h.hexdigest()[:32]


def build_metered_authorizer(
    *,
    project_code: str,
    cost_class: str | None,
    tool_name: str,
    task_id: str,
    agent_id: str,
    pinned_units: "list[Task]",
    artifacts_root: "Path",
    per_task_cap: int = 1,
) -> "Callable[[str, dict], tuple[bool, str]]":
    """Return the ``(tool_name, args) -> (allowed, reason)`` callback the producer
    tool-loop calls before a metered tool spends. Enforces the full contract above,
    then defers the go/no-go to ``comptroller.authorize_metered_tool`` (fail-closed).
    """

    def authorize(called_name: str, args: dict) -> "tuple[bool, str]":
        # 0. Name guard (Nemo B4 #3): this authorizer is bound to ONE tool's
        # cost_class/accounting. If the same authorizer is wired across a loadout
        # with another metered tool, a different tool must NOT be billed as this one.
        if called_name != tool_name:
            return (
                False,
                f"metered authorizer for {tool_name!r} cannot authorize a different "
                f"tool {called_name!r} — wire one authorizer per metered tool",
            )
        # 1. Narrow-param contract (Nemo B4 #4) — no arbitrary network target/payload,
        # checked recursively + by token + by URL-like value (not just top-level keys).
        bad = _scan_for_network_params(args)
        if bad:
            return (
                False,
                f"metered tool {called_name!r}: forbidden network param(s) "
                f"{sorted(set(bad))} — narrow params (pinned artifacts + bounded "
                "options) only; a real adapter should allowlist its options by schema",
            )
        # 2. Ledger-pinned-input validation — only QC-passed, unchanged inputs.
        checksums: list[str] = []
        for unit in pinned_units:
            verdict = review_ledger.verify_unit(unit, artifacts_root)
            if not verdict.ok:
                return (
                    False,
                    f"metered tool {called_name!r}: input "
                    f"{unit.output_path!r} is not pinned/QC-passed ({verdict.reason}) "
                    "— refusing to spend on an unverified input",
                )
            checksums.append(verdict.on_disk_checksum or "")
        # 3. Idempotency key over pinned inputs + tool + options (args is clean —
        # the scan above denied on any network param).
        key = metered_idempotency_key(tool_name, checksums, dict(args))
        # 4. Comptroller fail-closed authorization (budget / per-task cap / idempotent).
        auth = comptroller.authorize_metered_tool(
            project_code, cost_class, tool_name, task_id, key, agent_id,
            per_task_cap=per_task_cap,
        )
        return auth.allowed, auth.reason

    return authorize


__all__ = ["FORBIDDEN_ARG_KEYS", "build_metered_authorizer", "metered_idempotency_key"]
