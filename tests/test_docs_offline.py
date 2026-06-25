# SPDX-License-Identifier: Apache-2.0
"""Offline-docs: the DOCS reader prefers a downloaded cache over the install
baseline, so `update_docs` can keep the docs current without internet."""
from __future__ import annotations

import json

from modulatio import config, docs


def _seed_cache(page_slug: str = "99-cached", title: str = "Cached Page") -> None:
    """Write a minimal valid docs cache under the (isolated) CONFIG_DIR."""
    root = config.CONFIG_DIR / "docs-cache"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{page_slug}.md").write_text(f"# {title}\n\nfrom the cache\n", "utf-8")
    (root / "manifest.json").write_text(
        json.dumps({"version": "9.9.9", "pages": [{"slug": page_slug, "title": title}]}),
        "utf-8",
    )


def test_reads_baseline_when_no_cache():
    # No cache seeded → the install baseline (the bundled _docs/) is read.
    slugs = [s for s, _ in docs.list_docs()]
    assert slugs, "baseline docs should exist in the install"
    assert "99-cached" not in slugs


def test_cache_takes_precedence_over_baseline():
    _seed_cache()
    slugs = [s for s, _ in docs.list_docs()]
    assert slugs == ["99-cached"], "a valid cache replaces the baseline"
    assert "from the cache" in docs.read_doc("99-cached")


def test_partial_cache_without_manifest_falls_back_to_baseline(tmp_path):
    # A cache dir with .md files but NO manifest (e.g. a half-finished write) is
    # ignored — never read a half-applied update.
    root = config.CONFIG_DIR / "docs-cache"
    root.mkdir(parents=True, exist_ok=True)
    (root / "99-cached.md").write_text("# Cached\n", "utf-8")
    slugs = [s for s, _ in docs.list_docs()]
    assert "99-cached" not in slugs, "no manifest => baseline, not the partial cache"


# ── update_docs: fetch + verify + atomic swap ─────────────────────────────

import hashlib  # noqa: E402
import io  # noqa: E402
import tarfile  # noqa: E402


def _build_bundle(version: str, pages: dict[str, str]) -> tuple[dict, bytes]:
    """Return (remote_manifest, tar_gz_bytes) for a docs bundle. The tar holds
    the .md pages + a manifest.json; the remote manifest carries bundle_sha256."""
    inner_manifest = {
        "version": version,
        "pages": [{"slug": s, "title": s} for s in pages],
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for slug, body in pages.items():
            data = body.encode()
            ti = tarfile.TarInfo(f"{slug}.md")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        mj = json.dumps(inner_manifest).encode()
        ti = tarfile.TarInfo("manifest.json")
        ti.size = len(mj)
        tar.addfile(ti, io.BytesIO(mj))
    raw = buf.getvalue()
    remote_manifest = {
        "version": version,
        "bundle": "docs-bundle.tar.gz",
        "bundle_sha256": hashlib.sha256(raw).hexdigest(),
        "pages": inner_manifest["pages"],
    }
    return remote_manifest, raw


def test_update_downloads_and_swaps_when_newer(monkeypatch):
    manifest, raw = _build_bundle("9.9.9", {"01-intro": "# Intro\n\nfresh online docs"})
    monkeypatch.setattr(docs, "_fetch_json", lambda url: manifest)
    monkeypatch.setattr(docs, "_fetch_bytes", lambda url: raw)

    msg = docs.update_docs()
    assert "9.9.9" in msg
    assert [s for s, _ in docs.list_docs()] == ["01-intro"]
    assert "fresh online docs" in docs.read_doc("01-intro")
    # the swapped cache carries its manifest (so it's the active root)
    assert (config.CONFIG_DIR / "docs-cache" / "manifest.json").is_file()


def test_update_noop_when_not_newer(monkeypatch):
    _seed_cache("01-x", "X")  # local cache at version 9.9.9
    manifest = {"version": "9.9.9", "bundle": "b.tgz", "bundle_sha256": "x", "pages": []}
    monkeypatch.setattr(docs, "_fetch_json", lambda url: manifest)
    called = {"bytes": False}
    monkeypatch.setattr(docs, "_fetch_bytes",
                        lambda url: called.__setitem__("bytes", True) or b"")
    msg = docs.update_docs()
    assert "up to date" in msg
    assert called["bytes"] is False, "must not download when not newer"
    assert [s for s, _ in docs.list_docs()] == ["01-x"]


def test_update_bad_checksum_keeps_current(monkeypatch):
    _seed_cache("01-keep", "Keep")
    manifest, raw = _build_bundle("9.9.9", {"01-new": "# New\n"})
    manifest["version"] = "10.0.0"            # newer, so it tries to download
    manifest["bundle_sha256"] = "deadbeef"    # ...but the checksum is wrong
    monkeypatch.setattr(docs, "_fetch_json", lambda url: manifest)
    monkeypatch.setattr(docs, "_fetch_bytes", lambda url: raw)
    msg = docs.update_docs()
    assert "checksum" in msg.lower()
    assert [s for s, _ in docs.list_docs()] == ["01-keep"], "kept the old docs"


def test_update_offline_keeps_current(monkeypatch):
    _seed_cache("01-keep", "Keep")
    def boom(url):
        raise OSError("no network")
    monkeypatch.setattr(docs, "_fetch_json", boom)
    msg = docs.update_docs()
    assert "Offline" in msg
    assert [s for s, _ in docs.list_docs()] == ["01-keep"]


def test_update_rejects_traversal_member(monkeypatch):
    _seed_cache("01-keep", "Keep")
    # craft a malicious tar with a member escaping the cache dir
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"pwned"
        ti = tarfile.TarInfo("../escape.md")
        ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))
        mj = json.dumps({"version": "10.0.0"}).encode()
        ti = tarfile.TarInfo("manifest.json")
        ti.size = len(mj)
        tar.addfile(ti, io.BytesIO(mj))
    raw = buf.getvalue()
    manifest = {"version": "10.0.0", "bundle": "b.tgz",
                "bundle_sha256": hashlib.sha256(raw).hexdigest(), "pages": []}
    monkeypatch.setattr(docs, "_fetch_json", lambda url: manifest)
    monkeypatch.setattr(docs, "_fetch_bytes", lambda url: raw)
    msg = docs.update_docs()
    assert "failed" in msg.lower()
    assert [s for s, _ in docs.list_docs()] == ["01-keep"], "kept old docs; no escape"
    assert not (config.CONFIG_DIR.parent / "escape.md").exists()
