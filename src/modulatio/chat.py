# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Per-agent chat dispatch — Prompt-tab chat backend (slice #31b).

Pure dispatch helper: given an ``Agent``, a user message, and the prior
conversation history, build a context-aware prompt, call the agent's
runner, return the response. No Textual, no threading, no side-effects.

The Prompt tab's pane widgets compose this with:
  - Threading (Textual ``@work``) so the LLM call doesn't block the UI.
  - ``memory.agent_memory.add_episodic`` to persist the exchange.
  - ``memory.agent_memory.add_semantic`` for the train-the-agent path
    (Ctrl+M binding) — semantic writes bypass this dispatcher.

A note on prompt construction: we stitch identity + history + new
message into a single prompt rather than building a multi-turn
``messages`` list. The single-shot runner contract from
``runners.litellm_runner`` already targets ``messages=[{"role":"user"}]``
for orchestrator dispatch; reusing it keeps auth-refresh + alert
plumbing consistent. Future slice can promote this to a proper chat
runner if multi-turn fidelity becomes a concern.
"""
from __future__ import annotations

from typing import Any, Callable

from modulatio.attachments import Attachment
from modulatio.roster import Agent

#: Type of the runner factory: takes a model id, returns a single-shot
#: prompt-to-response callable. Matches ``runners.litellm_runner``.
RunnerFactory = Callable[[str], Callable[[str], str]]


def chat_with_agent(
    *,
    agent: Agent,
    message: str,
    history: list[tuple[str, str]],
    runner_factory: RunnerFactory,
    attachments: list[Attachment] | None = None,
    chat_completion: Callable[..., Any] | None = None,
    project_code: str | None = None,
) -> str:
    """Send a chat message to ``agent``'s model and return the response.

    Routes to one of two paths based on attachment kinds:

    - **Single-prompt path** (default): identity + history + message +
      any document attachments are stitched into a single prompt and
      passed to ``runner_factory(agent.model)(prompt)``. Reuses the
      orchestrator's runner contract for auth/refresh consistency.
    - **Multimodal path** (when any image attachment is present): builds
      a LiteLLM-format ``messages`` list with image content blocks
      (base64 data URIs) and calls ``chat_completion`` (defaults to
      ``litellm.completion``).

    The split keeps text-only chats efficient on the existing runner
    while still allowing vision-capable models to actually see image
    bytes when present.

    Args:
        agent: the agent to chat with — ``model``, ``identity``, and
            ``name`` shape the prompt.
        message: the new user message.
        history: prior turns as ``[(role, content), ...]``.
        runner_factory: factory for the single-prompt path.
        attachments: optional per-message attachments.
        chat_completion: factory for the multimodal path (tests inject;
            production defaults to ``litellm.completion``).

    Raises:
        ValueError: when ``agent.model`` is unset.
    """
    if not agent.model:
        raise ValueError(
            f"agent {agent.id!r} has no model configured — "
            "chat dispatch requires an explicit model"
        )
    atts = attachments or []
    if any(a.kind == "image" for a in atts):
        from modulatio.multimodal import chat_with_agent_multimodal
        return chat_with_agent_multimodal(
            agent=agent, message=message, history=history,
            attachments=atts, chat_completion=chat_completion,
        )
    runner = runner_factory(agent.model)
    prompt = _build_prompt(
        agent=agent, message=message, history=history,
        attachments=atts, project_code=project_code,
    )
    return runner(prompt)


def _build_prompt(
    *,
    agent: Agent,
    message: str,
    history: list[tuple[str, str]],
    attachments: list[Attachment],
    project_code: str | None = None,
) -> str:
    """Stitch identity + history + new message into a single prompt.

    Layout:

        # Your identity
        <agent.identity OR "You are <agent.name>.">

        # Available guidance        (omitted when no skills resolve)
        ## Skill: <name>
        <skill body>
        ## Skill: <name>
        <skill body>

        # Conversation so far       (omitted when history is empty)
        **user**: ...
        **assistant**: ...
        ...

        # New user message
        <message>

        Respond concisely and helpfully as your role.

    Skill bodies are loaded from ``agent.skills`` when ``project_code``
    is supplied. Each skill's prompt body is included as available
    guidance — the agent picks which to engage based on the user's
    message, the same way a senior teammate would. No prefix or magic
    keyword routes the choice; intent reading is the agent's job.
    """
    identity = (agent.identity or f"You are {agent.name}.").strip()
    parts: list[str] = [
        "# Your identity",
        identity,
        "",
    ]
    # Pre-V2 Slice B: inject project design-intent into every chat
    # prompt so Leader (and any other agent invoked via the chat
    # surface) sees the project's binding constraints + intentional
    # choices before deciding what to do. Neutral marker when the
    # project hasn't authored a design-intent file yet.
    if project_code:
        from modulatio import design_intent as _design_intent
        intent_block = _design_intent.render_for_prompt(project_code)
        parts.append("# Project design intent")
        parts.append(intent_block)
        parts.append("")
    skill_blocks = _load_agent_skill_bodies(agent, project_code)
    if skill_blocks:
        parts.append("# Available guidance")
        parts.append(
            "You have these skills available. Read the user's message "
            "and decide which (if any) apply — engage the matching one "
            "naturally in your response. Do not require the user to "
            "name a skill or use a prefix; pick from intent."
        )
        parts.append("")
        for skill_name, body in skill_blocks:
            parts.append(f"## Skill: {skill_name}")
            parts.append(body.rstrip())
            parts.append("")
    if history:
        parts.append("# Conversation so far")
        for role, content in history:
            parts.append(f"**{role}**: {content}")
        parts.append("")
    if attachments:
        parts.append("# Attachments")
        for att in attachments:
            if att.kind == "image":
                parts.append(
                    f"- image attached: `{att.name}` "
                    f"(path: {att.path})  "
                    f"[vision content blocks land in a future slice]"
                )
            else:  # document
                content = att.content or ""
                parts.append(f"## Attached document: `{att.name}`")
                parts.append("")
                # Fence with a backtick run longer than any run in the
                # content so a document's own fences can't break out of
                # the wrapper and bleed into instruction context
                # (CommonMark info-string rule). Mirrors multimodal.py's
                # _render_user_text.
                fence = "`" * (_longest_backtick_run(content) + 1)
                if len(fence) < 3:
                    fence = "```"
                parts.append(fence)
                parts.append(content)
                parts.append(fence)
        parts.append("")
    parts.append("# New user message")
    parts.append(message)
    parts.append("")
    parts.append("Respond concisely and helpfully as your role.")
    return "\n".join(parts)


def _longest_backtick_run(text: str) -> int:
    """Return the length of the longest consecutive run of backticks in
    ``text`` (0 when there are none). Used to size the document fence so
    a document's own backtick fences can't break out of the wrapper."""
    longest = 0
    current = 0
    for ch in text:
        if ch == "`":
            current += 1
            if current > longest:
                longest = current
        else:
            current = 0
    return longest


def _load_agent_skill_bodies(
    agent: Agent, project_code: str | None,
) -> list[tuple[str, str]]:
    """Resolve the prompt bodies of skills the agent declares.

    Returns ``[(skill_name, body), ...]`` in declaration order. Skips
    skills that resolve to an empty body (loader fallback path) so the
    "Available guidance" section doesn't get padded with nothing.
    Returns ``[]`` when ``project_code`` is None — the loader needs the
    project for project-local override resolution; without it we'd
    only see shared/seed skills, which is fine for some test paths
    but ambiguous for the production path. Callers that want skill
    injection MUST pass project_code.
    """
    if project_code is None:
        return []
    if not agent.skills:
        return []
    from modulatio import skills as _skills
    blocks: list[tuple[str, str]] = []
    for skill_name in agent.skills:
        try:
            skill = _skills.load_with_metadata(skill_name, project_code=project_code)
        except Exception:
            continue
        body = (skill.prompt_template or "").strip()
        if not body:
            continue
        blocks.append((skill_name, body))
    return blocks


__all__ = ["chat_with_agent", "RunnerFactory"]
