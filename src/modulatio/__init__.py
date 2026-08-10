# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
__version__ = "1.0.1"


def installed_version() -> "str | None":
    """The version installed ON DISK right now — read at call time, unlike
    ``__version__`` (frozen at import). A long-lived process compares the two
    to detect that a reinstall landed underneath it (stale-engine skew: the
    disk serves new statics while the process runs old routes). ``None`` when
    the installed metadata is unreadable — unknown, never reported as stale."""
    try:
        import json
        from importlib import metadata
        dist = metadata.distribution("modulatio")
        # An EDITABLE install's dist-info never tracks the code (the process
        # imports the source tree; the metadata version is whatever the last
        # `pip install -e` wrote) — a broken signal in both directions, so
        # report unknown rather than a false skew.
        direct = dist.read_text("direct_url.json")
        if direct and json.loads(direct).get("dir_info", {}).get("editable"):
            return None
        return dist.version
    except Exception:  # noqa: BLE001 — missing/broken dist-info = unknown
        return None
