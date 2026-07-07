# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""The Modulatio WebOS — the TUI's layout in a browser.

One new surface over the same body: every route in this package is a
thin binding onto an engine seam the TUI already consumes (design:
docs/design/2026-07-07-webui-design.md). FastAPI + uvicorn arrive via
the optional ``[web]`` extra; the base install carries none of it, and
nothing outside this package may import from it.
"""
