from pathlib import Path

from nightshift import corrections


def _write_log(path: Path, *lines: str) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_class_listing_includes_archived_entries(tmp_path, monkeypatch, capsys):
    """`--class`/`--channel` used to call only parse(), never parse_archive(), so
    an entry already compacted into corrections.archive.log was silently missing
    from the listing even though report() already combined both logs."""
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    (ai_dir / "manifest.toml").write_text("", encoding="utf-8")
    _write_log(
        ai_dir / "corrections.log",
        "2026-09-01|active-entry|derived-not-verified|maintainer|no-gap|"
        "an active entry that should always show up in the listing regardless",
    )
    _write_log(
        ai_dir / "corrections.archive.log",
        "2026-08-01|archived-entry|derived-not-verified|maintainer|no-gap|"
        "an archived entry that must still show up when filtering by class "
        "[[disposition: commit: abc123]]",
    )

    monkeypatch.chdir(tmp_path)
    corrections.main(["--class", "derived-not-verified"])

    out = capsys.readouterr().out
    assert "active-entry" in out
    assert "archived-entry" in out
    assert "2 entr(y/ies)." in out


def test_channel_listing_includes_archived_entries(tmp_path, monkeypatch, capsys):
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    (ai_dir / "manifest.toml").write_text("", encoding="utf-8")
    _write_log(
        ai_dir / "corrections.log",
        "2026-09-01|active-entry|derived-not-verified|maintainer|no-gap|"
        "an active entry that should always show up in the listing regardless",
    )
    _write_log(
        ai_dir / "corrections.archive.log",
        "2026-08-01|archived-entry|derived-not-verified|gate|no-gap|"
        "an archived entry found by a different channel than the active one "
        "[[disposition: commit: abc123]]",
    )

    monkeypatch.chdir(tmp_path)
    corrections.main(["--channel", "gate"])

    out = capsys.readouterr().out
    assert "archived-entry" in out
    assert "active-entry" not in out
