# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The shipped service catalog — known vendors the SERVICES tab offers.

Mirrors ``provider_catalog``'s role for LLM providers. Content is each
vendor's GENERAL current offering (Modulatio ships for every user, never one
user's setup). ``beta=True`` = the API shape is catalog-documented but not
yet live-verified; the TUI surfaces the flag. The catalog earns entries over
time — the custom lane (api_call) covers the long tail from day one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from modulatio.services import Service


@dataclass(frozen=True)
class CatalogEntry:
    service: Service
    beta: bool = True
    notes: str = ""


_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        service=Service(
            id="openai-images", name="OpenAI Images", kind="catalog",
            capabilities=("image",), env_var="OPENAI_API_KEY",
            base_url="https://api.openai.com", auth_shape="bearer",
            docs_url="https://platform.openai.com/docs/guides/images",
        ),
        beta=True, notes="gpt-image-1 via /v1/images/generations (b64 result)",
    ),
    CatalogEntry(
        service=Service(
            id="tavily", name="Tavily Search", kind="catalog",
            capabilities=("research",), env_var="TAVILY_API_KEY",
            base_url="https://api.tavily.com", auth_shape="bearer",
            docs_url="https://docs.tavily.com",
            per_task_cap=3,
        ),
        beta=True, notes="POST /search — ranked results with content snippets",
    ),
    CatalogEntry(
        service=Service(
            id="elevenlabs", name="ElevenLabs", kind="catalog",
            capabilities=("speech",), env_var="ELEVENLABS_API_KEY",
            base_url="https://api.elevenlabs.io",
            auth_shape="header:xi-api-key",
            docs_url="https://elevenlabs.io/docs",
        ),
        beta=True, notes="POST /v1/text-to-speech/<voice_id> (mp3 bytes)",
    ),
    CatalogEntry(
        service=Service(
            id="luma", name="Luma Dream Machine", kind="catalog",
            capabilities=("video",), env_var="LUMAAI_API_KEY",
            base_url="https://api.lumalabs.ai", auth_shape="bearer",
            docs_url="https://docs.lumalabs.ai",
        ),
        beta=True,
        notes="POST /dream-machine/v1/generations then poll by id",
    ),
)


def catalog() -> tuple[CatalogEntry, ...]:
    return _CATALOG


def entry(service_id: str) -> Optional[CatalogEntry]:
    for e in _CATALOG:
        if e.service.id == service_id:
            return e
    return None
