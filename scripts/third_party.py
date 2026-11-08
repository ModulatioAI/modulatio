#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Write THIRD-PARTY.md: every package the release .deb bundles, with its licence (a release step).

    python3 scripts/third_party.py [site-packages]

Reads packaging/requirements-web.lock for the bundled packages and their pinned versions, and each
package's installed metadata (by default .venv/lib/python3.12/site-packages) for its licence and
any NOTICE file, whose text Apache-2.0 section 4(d) says must travel with the work. Only reads
files; nothing is imported from the packages.
"""
from __future__ import annotations

import re
import sys
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "packaging" / "requirements-web.lock"
OUT = ROOT / "THIRD-PARTY.md"
PINNED = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def metadata(site: Path) -> dict[str, Path]:
    """Each installed distribution's dist-info folder, by its normalised name."""
    found = {}
    for info in site.glob("*.dist-info"):
        meta = info / "METADATA"
        if meta.exists():
            found[norm(Parser().parsestr(meta.read_text(errors="replace"), headersonly=True)["Name"] or "")] = info
    return found


def licence(info: Path) -> str:
    head = Parser().parsestr((info / "METADATA").read_text(errors="replace"), headersonly=True)
    if head.get("License-Expression"):
        return head["License-Expression"]
    classes = [c.split("::")[-1].strip() for c in head.get_all("Classifier") or [] if c.startswith("License ::")]
    stated = (head.get("License") or "").strip().splitlines()[0:1]
    said = "; ".join(dict.fromkeys(classes + stated)) or "not stated in its metadata"
    # Metadata that is blank, unknown or says proprietary is checked against the licence file itself.
    if re.search(r"(?i)unknown|proprietary|not stated", said):
        for f in sorted(p for p in info.rglob("*") if p.is_file() and re.match(r"(?i)^(licen[cs]e|copying)", p.name)):
            first = next((ln.strip() for ln in f.read_text(errors="replace").splitlines() if ln.strip()), "")
            if first:
                return f"{said} (its licence file: {first})"
    return said


def notices(info: Path) -> list[Path]:
    return sorted(p for p in info.rglob("*") if p.is_file() and re.match(r"(?i)^notice", p.name))


def main() -> int:
    site = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".venv" / "lib" / "python3.12" / "site-packages"
    installed = metadata(site)
    rows, texts, unread = [], [], []
    for line in LOCK.read_text().splitlines():
        m = PINNED.match(line)
        if not m:
            continue
        name, version = m.groups()
        info = installed.get(norm(name))
        if info is None:
            unread.append(name)
            rows.append(f"| {name} | {version} | not read: not installed where this ran |")
            continue
        rows.append(f"| {name} | {version} | {licence(info)} |")
        for n in notices(info):
            texts.append(f"### {name} {version}\n\n```\n{n.read_text(errors='replace').strip()}\n```\n")
    OUT.write_text(
        "# Third-party software in Modulatio\n\n"
        "Modulatio's own code is under the Apache License 2.0 (LICENSE; credits in NOTICE). The release\n"
        "package for Debian and Ubuntu bundles the Python packages below, each unmodified and each keeping\n"
        "its own licence files in its installed `.dist-info` folder, and a Python interpreter from\n"
        "python-build-standalone (astral-sh), CPython under the PSF License 2.0 with the libraries it\n"
        "carries under their own licences. Written by scripts/third_party.py from\n"
        "packaging/requirements-web.lock.\n\n"
        "| Package | Version | Licence |\n|---|---|---|\n" + "\n".join(rows) + "\n\n"
        "## NOTICE texts of bundled packages\n\n"
        "Reproduced as Apache-2.0 section 4(d) asks of a work that redistributes them.\n\n" + "\n".join(texts))
    if unread:
        print(f"licence not read for {len(unread)}: {', '.join(unread)}", file=sys.stderr)
    print(f"wrote {OUT}: {len(rows)} packages, {len(texts)} NOTICE texts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
