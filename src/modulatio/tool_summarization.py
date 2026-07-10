# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Tool-loop response summarization + sliding-window prune ().

Lands Layer 1 of the five-layer working-memory architecture: bounds the
tool-result inflow that accumulates inside a single agent call. Wraps
``runners.run_llm_with_tools`` at the tool-result return site.

Core mechanic:
  - Tool result over ``threshold_tokens`` -> small-model summarizer ->
    conversation gets summary + ``call_id`` pointer.
  - Full raw result writes to ``<run_workspace>/tool_calls/<call_id>.txt``.
  - Sliding-window prune (called by Layer 2 /  too) rewrites
    oldest tool-role messages to placeholders when the conversation
    approaches ``prune_at_pct`` of the model's context cap, keeping the
    last ``keep_recent`` verbatim.

Mirrors ``budget.py`` exactly: dataclass + ContextVar + ``current_*``,
``with_*``, ``bind``, ``unbind``. ``start_execution`` binds a config
for the duration of the run; ``runners.run_llm_with_tools`` reads via
``current_config()``; no-op when nothing is bound (preserves stub /
test paths that don't care about summarization).

Token counting goes through ``litellm.token_counter`` so we don't
maintain a per-model tokenizer chart — litellm already does for every
provider it routes to, and falls back to a generic estimator for
unknowns. Threshold-class decisions don't need exact counts.

Local-LLM note: the summarizer model is per-agent config. When the
main model is local (``ollama_chat/...``) point ``summarizer_model``
at a local small model too so the whole pipeline stays free. The
default in ``defaults.json`` is set up for the cloud-main case; the
override path keeps the local-main case free.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass
class ToolSummarizationConfig:
    """Per-agent config for tool-loop summarization + prune.

    Inheritance: project default -> per-agent override -> ``defaults.json``
    fallback. Resolution happens at agent-config-load time; this dataclass
    is what the runner sees.

    Fields:
      enabled            — master switch. ``False`` means runner skips
                          all summarization + prune logic and behaves
                          identically to pre-Slice-2.
      threshold_tokens   — tool result strings counting more than this
                          via litellm get summarized + persisted.
      summarizer_model   — litellm model id used for the summarizer call.
                          Cloud default; override per-agent for local.
      keep_recent        — sliding-window prune leaves at least this many
                          most-recent tool-role messages verbatim.
      prune_at_pct       — fraction of the active model's max_input_tokens
                          at which the prune triggers (0.0-1.0).
      tool_calls_dir     — where raw tool results land. ``None`` means
                          the binder will fill it from the active
                          run-workspace (``<run>/tool_calls/``).
    """

    enabled: bool = True
    threshold_tokens: int = 2000
    summarizer_model: str | None = None
    keep_recent: int = 3
    # Aligned to the context-budget call-boundary prune (0.85) so
    # there's a single coherent "compress at 85%" threshold — the doubled role
    # caps gave the headroom; we don't want the in-loop sliding-window prune
    # trimming tool outputs at 80% while the boundary gate waits for 85%.
    prune_at_pct: float = 0.85
    tool_calls_dir: Path | None = None


_current_config: contextvars.ContextVar["ToolSummarizationConfig | None"] = (
    contextvars.ContextVar("modulatio_tool_summarization_config", default=None)
)


def current_config() -> "ToolSummarizationConfig | None":
    """Return the config bound to the current execution context, or
    ``None`` when no run is active. Runners read this to decide whether
    to apply summarization + prune."""
    return _current_config.get()


@contextmanager
def with_config(config: ToolSummarizationConfig):
    """Bind ``config`` to the ContextVar for the duration of the
    with-block. ``start_execution`` wraps the run loop in this so
    every ``run_llm_with_tools`` call inside reaches the same config."""
    token = _current_config.set(config)
    try:
        yield config
    finally:
        _current_config.reset(token)


def bind(config: ToolSummarizationConfig):
    """Non-context-manager binding. Returns the reset token."""
    return _current_config.set(config)


def unbind(token) -> None:
    """Restore the prior binding. ``token`` must be the value ``bind`` returned."""
    _current_config.reset(token)


def count_tokens(model: str | None, *, text: str | None = None,
                 messages: Sequence[dict] | None = None) -> int:
    """Token count via ``litellm.token_counter``.

    Exactly one of ``text`` / ``messages`` must be provided. ``model``
    is the litellm model id whose tokenizer should drive the count;
    when ``None`` or unknown to litellm, litellm falls back to a
    generic estimator.

    Threshold-purpose accuracy is sufficient — we don't need exact
    counts, we need "is this big or small". Failures (litellm import
    error in a stub-only test path; tokenizer missing) fall back to a
    char-length heuristic so the runner never crashes on a count.
    """
    if (text is None) == (messages is None):
        raise ValueError("count_tokens: pass exactly one of text= or messages=")
    try:
        from litellm import token_counter
    except Exception:
        return _heuristic_count(text=text, messages=messages)
    try:
        if text is not None:
            return int(token_counter(model=model, text=text))
        return int(token_counter(model=model, messages=list(messages or [])))
    except Exception:
        return _heuristic_count(text=text, messages=messages)


def _heuristic_count(*, text: str | None, messages: Sequence[dict] | None) -> int:
    """Char/4 fallback. Crude but deterministic — never raises."""
    if text is not None:
        return max(1, len(text) // 4)
    total = 0
    for m in messages or []:
        total += len(str(m.get("content") or "")) // 4
    return max(1, total)


def persist_raw_result(call_id: str, text: str, tool_calls_dir: Path) -> Path:
    """Write the full raw tool result to ``<tool_calls_dir>/<call_id>.txt``.

    ``call_id`` is the model-supplied opaque correlation id from
    ``ToolCall.id``.  model-supplied path
    material is hostile by default. Validate the bare-id shape (no
    slashes, no ``..``) the same way ``read_tool_result`` and
    ``write_checkpoint`` do, then resolve under ``tool_calls_dir``
    and assert the result actually stays inside that root (catches
    pre-existing symlinks). File is written with ``0o600`` so a
    multi-user host can't sidestep redaction by reading raw payloads
    as another account.

    Returns the file path so the caller can reference it from the
    summarized message.
    """
    import os as _os
    if not call_id or "/" in call_id or "\\" in call_id or ".." in call_id:
        raise ValueError(
            f"invalid call_id {call_id!r} for raw tool result: "
            "must be a bare identifier (no path separators, no '..')."
        )
    tool_calls_dir.mkdir(parents=True, exist_ok=True)
    candidate = (tool_calls_dir / f"{call_id}.txt").resolve()
    root = tool_calls_dir.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"resolved path {candidate} escaped tool_calls_dir {root}: "
            f"{exc}"
        ) from exc
    fd = _os.open(
        candidate, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600,
    )
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        try:
            candidate.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    #  ``os.open(O_CREAT, 0o600)`` only
    # applies the mode on creation. A pre-existing raw-result file
    # at the same call_id (retry, manual edit, earlier build) keeps
    # its original perms after truncate-and-rewrite. ``chmod`` after
    # write closes the existing-file case.
    try:
        _os.chmod(candidate, 0o600)
    except OSError:
        # FAT-on-USB / SMB / NFS mounts may not honor POSIX mode
        # bits. The new-file path already wrote with 0o600 via
        # os.open; only the existing-file repair is lost there.
        pass
    return candidate


def summarize_tool_result(
    text: str,
    *,
    summarizer_model: str,
    chat_runner_factory: Callable[[str], Callable[..., str]],
    max_summary_tokens: int = 256,
) -> str:
    """Call the summarizer model and return a terse summary string.

    Prompt is intentionally narrow: extract direct quote-able fact
    extracts, no inference, no opinion. The agent can re-fetch the
    full text via ``read_tool_result(call_id)`` whenever the summary
    drops something it now needs.

    ``chat_runner_factory(model)`` returns a one-shot runner; the
    caller injects the right factory (in production: ``litellm_runner``;
    in tests: a stub). Keeping this dependency-injected avoids a hard
    couple to the LiteLLM module here.
    """
    runner = chat_runner_factory(summarizer_model)
    prompt = (
        "Summarize the following tool-call result for an agent's "
        "context. Extract only direct quotable facts (numbers, names, "
        "URLs, error strings). No inference, no opinion, no fluff. "
        "If the result is an error, surface the error message verbatim. "
        f"Keep the summary under {max_summary_tokens} tokens.\n\n"
        "----- BEGIN TOOL RESULT -----\n"
        f"{text}\n"
        "----- END TOOL RESULT -----\n"
    )
    return str(runner(prompt))


def format_summarized_message(call_id: str, summary: str) -> str:
    """Compose the conversation-side replacement string. The pointer
    line lets the agent know it can call ``read_tool_result(<call_id>)``
    to recover the verbatim text."""
    return (
        f"[summarized: call_id={call_id}]\n"
        f"Use read_tool_result(call_id={call_id!r}) to retrieve the "
        f"full text on demand.\n\n"
        f"{summary}"
    )


def truncate_tool_result(
    text: str,
    *,
    call_id: str,
    max_tokens: int,
    model: str | None = None,
) -> str:
    """Model-free fallback for when no summarizer is configured: keep the
    HEAD of a large tool result (about ``max_tokens`` worth) and replace the
    tail with a pointer to ``read_tool_result(call_id)``.

    Why: without this, a tool result over ``threshold_tokens`` that can't be
    summarized goes into the agent's context VERBATIM. A producer that fetches
    several sources then accumulates several such results and blows its role
    context budget — which decomposes, and the children re-accumulate, into a
    storm. Truncating on arrival bounds how much a
    single raw fetch can occupy; the full raw is persisted on disk and the
    agent can pull it back with ``read_tool_result`` when it needs more."""
    if count_tokens(model, text=text) <= max_tokens:
        return text
    # The composed return prepends a pointer header (truncation marker +
    # read_tool_result hint) ahead of the kept head. Those header tokens count
    # against the agent's context too, so budget the head against ``max_tokens``
    # MINUS the header cost — otherwise the return systematically overshoots
    # ``max_tokens`` by the un-counted header. The header text
    # is fixed except for ``call_id`` and the ``dropped`` count; the latter only
    # shrinks the head further, so counting the header alone (with a ``dropped``
    # placeholder no shorter than the real value) is a safe upper bound.
    def _header(dropped: int) -> str:
        return (
            f"[truncated: call_id={call_id} — kept ~{max_tokens} tokens of a "
            f"larger result; {dropped} more chars on disk]\n"
            f"Use read_tool_result(call_id={call_id!r}) for the full text.\n\n"
        )

    header_tokens = count_tokens(model, text=_header(len(text)))
    head_budget = max_tokens - header_tokens
    if head_budget <= 0:
        # The header alone meets or exceeds the whole budget — there is no room
        # for any head. Return just the pointer (header keeps its trailing
        # blank line so callers that split on ``\n\n`` still recover an empty
        # head) so the total stays as close to ``max_tokens`` as the
        # (irreducible) header allows.
        return _header(len(text))
    # First cut by the ~4-chars/token heuristic, then tighten until the kept
    # head GENUINELY fits ``head_budget`` — termination is keyed on fit, not on a
    # fixed iteration budget. Dense tokenization (code/markup, CJK, odd byte
    # runs) can pack far more tokens per char than the 4:1 estimate, so a fixed
    # step count could exit while still over budget. The shrink is monotone (the
    # head strictly loses length while it doesn't fit and isn't a lone char), so
    # the loop is bounded by the head length; the ``len(head) <= 1`` guard caps
    # the degenerate single-char case so we never spin.
    head = text[: max(1, head_budget * 4)]
    while head and count_tokens(model, text=head) > head_budget:
        nxt = max(1, int(len(head) * 0.8))
        if nxt >= len(head):
            # int(len*0.8) stops shrinking at len==1 (0.8->0->max(1,0)==1);
            # a single char still over budget means head_budget is too small to
            # ever fit — drop the head entirely rather than loop forever.
            head = ""
            break
        head = head[:nxt]
    dropped = len(text) - len(head)
    return f"{_header(dropped)}{head}"


def prune_messages_sliding_window(
    messages: list[dict],
    *,
    model: str | None,
    max_input_tokens: int,
    prune_at_pct: float = 0.80,
    keep_recent: int = 3,
    tool_calls_dir: Path | None = None,
) -> tuple[list[dict], int]:
    """Rewrite oldest ``role: tool`` messages to summarized placeholders
    when the conversation token count exceeds ``prune_at_pct *
    max_input_tokens``. Always keeps the last ``keep_recent`` tool-role
    messages verbatim so the agent still has fresh context.

    Returns ``(new_messages, pruned_count)``.

    Idempotent: messages already in placeholder shape (containing the
    ``[summarized:`` marker) are not re-pruned. Callers can invoke this
    every iteration without churn.

    Layer 2 (the budget primitive) calls this ad-hoc when its pre-call
    estimate hits the 80-100% band. Layer 1 (this module) calls it from
    inside the tool loop after each tool-result append. Same function,
    two trigger sites.

    Recoverability: the tool loop only persists
    a raw result to ``<tool_calls_dir>/<call_id>.txt`` when it crosses
    ``threshold_tokens``. A SUB-threshold result lands verbatim with no disk
    copy. Pruning such a message to a ``read_tool_result`` promise dangled a
    dead pointer and lost the original text irrecoverably. So:
      - if the message already carries a persist marker (``[summarized:`` /
        ``[truncated:``) OR a ``<call_id>.txt`` already exists, the raw is
        on disk → emit the recovery promise as before;
      - else if a ``tool_calls_dir`` is known (explicit arg, else the bound
        config's), PERSIST-ON-PRUNE the verbatim content first, then emit
        the recovery promise (now resolvable);
      - else (no dir reachable) emit a placeholder that plainly says the raw
        cannot be recovered — never a pointer we can't honor.
    ``tool_calls_dir`` defaults to ``None``; the budget-primitive call site
    doesn't thread it, so we also fall back to ``current_config()``.
    """
    if max_input_tokens <= 0:
        return list(messages), 0
    threshold = int(max_input_tokens * prune_at_pct)
    current = count_tokens(model, messages=messages)
    if current <= threshold:
        return list(messages), 0

    # Resolve where raw results live: explicit arg wins, else the bound
    # config's dir (the budget-primitive call site doesn't pass one).
    if tool_calls_dir is None:
        _cfg = current_config()
        if _cfg is not None:
            tool_calls_dir = _cfg.tool_calls_dir

    # Find tool-role indices in order; protect the last keep_recent.
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= keep_recent:
        return list(messages), 0
    prunable = tool_indices[: len(tool_indices) - keep_recent]

    out = list(messages)
    pruned = 0
    for i in prunable:
        msg = out[i]
        content = str(msg.get("content") or "")
        if content.startswith("[summarized:"):
            continue
        call_id = str(msg.get("tool_call_id") or f"prune-{i}")

        # Is the raw already on disk? Either the tool loop persisted it (the
        # inline content carries a persist marker) or a file already exists.
        # `[summarized:` content already `continue`d above,
        # so `[truncated:` is the only persist marker reachable here.
        already_persisted = content.startswith("[truncated:")
        if not already_persisted and tool_calls_dir is not None:
            try:
                candidate = (Path(tool_calls_dir) / f"{call_id}.txt").resolve()
                root = Path(tool_calls_dir).resolve()
                candidate.relative_to(root)
                already_persisted = candidate.exists()
            except (ValueError, OSError):
                already_persisted = False

        recoverable = already_persisted
        # Never-persisted sub-threshold result + a known dir → persist now so
        # the pointer we're about to write actually resolves.
        if not recoverable and tool_calls_dir is not None and call_id:
            try:
                persist_raw_result(call_id, content, Path(tool_calls_dir))
                recoverable = True
            except (ValueError, OSError):
                # Hostile call_id shape or unwritable dir — fall through to
                # the no-promise placeholder rather than dangle a pointer.
                recoverable = False

        if recoverable:
            new_content = (
                f"[summarized: call_id={call_id} (pruned)]\n"
                f"Original tool result removed by sliding-window prune. "
                f"Use read_tool_result(call_id={call_id!r}) to retrieve."
            )
        else:
            new_content = (
                f"[summarized: call_id={call_id} (pruned)]\n"
                f"Original tool result removed by sliding-window prune and "
                f"cannot be recovered (raw was not persisted to disk)."
            )
        out[i] = {**msg, "content": new_content}
        pruned += 1
        if count_tokens(model, messages=out) <= threshold:
            break
    return out, pruned


__all__ = [
    "ToolSummarizationConfig",
    "bind",
    "count_tokens",
    "current_config",
    "format_summarized_message",
    "truncate_tool_result",
    "persist_raw_result",
    "prune_messages_sliding_window",
    "summarize_tool_result",
    "unbind",
    "with_config",
]
