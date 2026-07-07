# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""ActivityEvent → JSON-safe dict, with the secret red-line applied.

Every string that crosses the web boundary passes through
``logstore.scrub_secrets`` (the same transform the LOGS surface uses),
so an embedded ``api_key=…`` / bearer token in an event detail can
never reach a browser. Unknown detail objects are stringified — the
frame contract is plain JSON, never a pickle of engine internals.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from modulatio.logstore import scrub_secrets
from modulatio.types import ActivityEvent


def json_safe(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return scrub_secrets(repr(value))


def event_to_json(event: ActivityEvent) -> dict:
    return {k: json_safe(v) for k, v in asdict(event).items()}
