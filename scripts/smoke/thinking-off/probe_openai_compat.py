"""Fix #16 spike: which controls actually quiet a reasoning model through an
OpenAI-COMPATIBLE shim? (ollama.com/v1 + glm-5.2 is the live counter-example:
the probe-guarded reasoning_effort="disable" serializes fine and the shim
drops it silently; /no_think is Qwen dialect and inert to GLM.)

Usage:
    OLLAMA_API_KEY=... python probe_openai_compat.py [base_url] [model]
defaults: https://ollama.com/v1  glm-5.2

Emits one evidence row per variant: reasoning-field chars, <think> presence,
completion tokens. Stdlib only; ~7 tiny calls.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://ollama.com/v1"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "glm-5.2"
KEY = os.environ.get("OLLAMA_API_KEY", "")
PROMPT = "What is 2+2? Answer with just the number."

VARIANTS: list[tuple[str, dict]] = [
    ("baseline", {}),
    ("/nothink prefix (GLM dialect)", {"prefix": "/nothink\n\n"}),
    ("/no_think prefix (Qwen dialect)", {"prefix": "/no_think\n\n"}),
    ("chat_template_kwargs enable_thinking=false",
     {"body": {"chat_template_kwargs": {"enable_thinking": False}}}),
    ("reasoning_effort=disable (what we send today)",
     {"body": {"reasoning_effort": "disable"}}),
    ("thinking type=disabled (Zhipu native param)",
     {"body": {"thinking": {"type": "disabled"}}}),
]


def call(variant: dict) -> dict:
    body = {
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": variant.get("prefix", "") + PROMPT,
        }],
        "max_tokens": 2000,
    }
    body.update(variant.get("body", {}))
    req = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {KEY}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def main() -> None:
    if not KEY:
        sys.exit("OLLAMA_API_KEY not set")
    print(f"endpoint={BASE} model={MODEL}\n")
    for name, variant in VARIANTS:
        try:
            r = call(variant)
        except Exception as exc:  # noqa: BLE001 — an HTTP error IS evidence
            print(f"{name:48s} ERROR: {exc}")
            continue
        msg = r["choices"][0]["message"]
        reasoning = (msg.get("reasoning") or msg.get("reasoning_content") or "")
        content = msg.get("content") or ""
        think_tag = "<think>" in content
        toks = (r.get("usage") or {}).get("completion_tokens")
        print(f"{name:48s} reasoning_chars={len(reasoning):5d}  "
              f"think_tag={str(think_tag):5s}  completion_tokens={toks}  "
              f"answer={content.strip()[:40]!r}")


if __name__ == "__main__":
    main()
