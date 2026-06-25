#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build the offline docs bundle from the site's MDX pages (a release step).

Converts ``modulatio-site/src/content/docs/*.mdx`` → plain ``.md`` and writes:
  - ``src/modulatio/_docs/*.md`` + ``manifest.json`` — the install baseline
    (so the DOCS tab works offline out of the box), and
  - ``docs-bundle.tar.gz`` + ``manifest.json`` into the site's
    ``public/docs/offline/`` — what ``modulatio.docs.update_docs`` downloads.

The site MDX is plain markdown plus a thin Starlight layer; the converter only
undoes what's actually used (frontmatter, ``import`` lines, ``<Aside>``).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

#: Reading order: top-of-funnel first, then concepts → architecture → ops →
#: reference, then the long-form guides. Per-version release notes + the
#: (constantly-changing) roadmap stay online; not bundled.
_SECTIONS = ["getting-started", "concepts", "architecture", "operations", "reference"]
_TOP_FIRST = ["overview"]
_TOP_LAST = ["methodology", "troubleshooting"]

_IMPORT_RE = re.compile(r"^\s*import\s.+$", re.MULTILINE)
_ASIDE_RE = re.compile(r"<Aside\b([^>]*)>(.*?)</Aside>", re.DOTALL)
_TITLE_ATTR_RE = re.compile(r'title="([^"]*)"')


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(title, body)``, stripping a leading ``--- … ---`` block."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm, body = text[3:end], text[end + 4:].lstrip("\n")
            m = re.search(r"^title:\s*(.+)$", fm, re.MULTILINE)
            title = m.group(1).strip().strip("\"'") if m else ""
            return title, body
    return "", text


def _aside_to_blockquote(m: re.Match) -> str:
    title = _TITLE_ATTR_RE.search(m.group(1))
    lines: list[str] = []
    if title:
        lines += [f"**{title.group(1)}**", ""]
    lines += m.group(2).strip().splitlines()
    return "\n".join(f"> {ln}" if ln else ">" for ln in lines)


def convert_mdx(text: str) -> tuple[str, str]:
    """MDX page text → ``(title, markdown)``. Strips frontmatter + ``import``
    lines, renders ``<Aside>`` as a blockquote, and ensures the title is the H1."""
    title, body = _split_frontmatter(text)
    body = _IMPORT_RE.sub("", body)
    body = _ASIDE_RE.sub(_aside_to_blockquote, body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if title and not body.startswith("# "):
        body = f"# {title}\n\n{body}"
    return title, body


def _ordered_pages(site_docs: Path) -> list[Path]:
    """The .mdx pages to bundle, in reading order."""
    out = [site_docs / f"{s}.mdx" for s in _TOP_FIRST]
    for section in _SECTIONS:
        out += sorted((site_docs / section).glob("*.mdx"))
    out += [site_docs / f"{s}.mdx" for s in _TOP_LAST]
    return [p for p in out if p.is_file()]


def _package_version() -> str:
    init = (_REPO / "src" / "modulatio" / "__init__.py").read_text("utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    return m.group(1) if m else "0"


def build(site_docs: Path, baseline: Path, site_public: Path | None) -> dict:
    """Convert the site docs → numbered .md in `baseline`, write its manifest,
    and (if `site_public` given) the hostable bundle. Returns the manifest."""
    baseline.mkdir(parents=True, exist_ok=True)
    for stale in baseline.glob("*.md"):
        stale.unlink()

    pages: list[dict] = []
    for i, src in enumerate(_ordered_pages(site_docs), start=1):
        title, md = convert_mdx(src.read_text("utf-8"))
        slug = f"{i:02d}-{src.stem}"
        (baseline / f"{slug}.md").write_text(md + "\n", "utf-8")
        pages.append({"slug": slug, "title": title or src.stem})

    version = _package_version()
    manifest = {"version": version, "bundle": "docs-bundle.tar.gz", "pages": pages}
    (baseline / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")

    if site_public is not None:
        offline = site_public / "docs" / "offline"
        offline.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for p in sorted(baseline.glob("*.md")):
                tar.add(p, arcname=p.name)
            tar.add(baseline / "manifest.json", arcname="manifest.json")
        raw = buf.getvalue()
        (offline / "docs-bundle.tar.gz").write_bytes(raw)
        remote = {**manifest, "bundle_sha256": hashlib.sha256(raw).hexdigest()}
        (offline / "manifest.json").write_text(json.dumps(remote, indent=2), "utf-8")
        manifest = remote
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site", type=Path, default=_REPO.parent / "modulatio-site")
    args = ap.parse_args()
    site_docs = args.site / "src" / "content" / "docs"
    baseline = _REPO / "src" / "modulatio" / "_docs"
    public = args.site / "public" if (args.site / "public").is_dir() else None
    mf = build(site_docs, baseline, public)
    print(f"built {len(mf['pages'])} docs (v{mf['version']}) -> {baseline}"
          + (f" + bundle -> {public}/docs/offline" if public else ""))


if __name__ == "__main__":
    main()
