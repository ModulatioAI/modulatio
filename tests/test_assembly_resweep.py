"""0.9.0 pre-ship re-sweep regressions for assembly.py.

Finding 1 (LOW, #101/0.9.0): the deliverable digest for a single-file-output
family (media composites) used to fall to ``_generic_digest``, which stats the
INPUT unit files from ``units_used``. For a media join the deliverable IS the
single composited binary (``output_file``), so the verifier's "eyes" were
pointed at N input files instead of the one produced artifact. The fix: when a
real composite ``output_file`` is present, the generic digest describes THAT one
file (1 part, its byte size, ``whole_size`` = same). Product-agnostic — it keys
on "a composite was produced", not on "media".
"""

from __future__ import annotations

from pathlib import Path

from modulatio import assembly


def _write(root: Path, name: str, data: bytes) -> Path:
    p = root / name
    p.write_bytes(data)
    return p


def test_media_digest_describes_composite_not_input_units(tmp_path: Path):
    # Two small input units (what units_used names) ...
    _write(tmp_path, "clip_a.mp4", b"a" * 100)
    _write(tmp_path, "clip_b.mp4", b"b" * 100)
    # ... and the single composite the media join actually produced.
    composite = _write(tmp_path, "composite.mp4", b"c" * 4096)

    digest = assembly.build_deliverable_digest(
        {"units": ["clip_a.mp4", "clip_b.mp4"]},
        ["clip_a.mp4", "clip_b.mp4"],
        tmp_path,
        strategy="media",
        output_file=composite,
    )

    # The deliverable is the ONE composite, not the N inputs.
    assert digest.part_count == 1
    assert digest.parts == [{"label": "composite.mp4", "size": 4096}]
    assert digest.whole_size == 4096
    assert digest.whole_size_unit == "bytes"
    assert digest.part_size_unit == "bytes"


def test_generic_digest_without_output_file_still_stats_units(tmp_path: Path):
    # No composite output → unchanged behavior: parts = the input units.
    _write(tmp_path, "u1.bin", b"x" * 10)
    _write(tmp_path, "u2.bin", b"y" * 20)

    digest = assembly.build_deliverable_digest(
        {"units": ["u1.bin", "u2.bin"]},
        ["u1.bin", "u2.bin"],
        tmp_path,
        strategy="media",
    )

    assert digest.part_count == 2
    assert [p["size"] for p in digest.parts] == [10, 20]
    assert digest.whole_size is None


def test_missing_output_file_falls_back_to_unit_digest(tmp_path: Path):
    # A composite path that does not exist on disk (fail-open): describe the units.
    _write(tmp_path, "only.bin", b"z" * 5)
    ghost = tmp_path / "ghost_composite.mp4"  # never written

    digest = assembly.build_deliverable_digest(
        {"units": ["only.bin"]},
        ["only.bin"],
        tmp_path,
        strategy="media",
        output_file=ghost,
    )

    assert digest.part_count == 1
    assert digest.parts == [{"label": "only.bin", "size": 5}]
    assert digest.whole_size is None
