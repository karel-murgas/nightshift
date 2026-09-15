"""What the diff reviewer is shown (`nightshift.reviewdiff`).

The rule: only files with nothing to read (binaries, generated board views) are cut,
and they are *listed*, never dropped silently. Everything a reader can judge stays —
prose, because 11 of 14 historical `needs_fix` verdicts were wrong prose, and
translation values, because no gate checks what a translation means.
"""
from __future__ import annotations

from nightshift import reviewdiff


def _hunk(path: str, body: str) -> str:
    return (f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,3 +1,3 @@\n{body}")


def test_binaries_and_generated_views_are_cut_and_listed(tmp_path):
    diff = (
        "diff --git a/art/icon.png b/art/icon.png\nindex 1..2 100644\n"
        "Binary files a/art/icon.png and b/art/icon.png differ\n"
        + _hunk("Routing.md", " a\n-b\n+c\n")
        + _hunk("src/a.py", " x\n-y\n+z\n")
    )
    shown = reviewdiff.filter_diff(diff)
    assert "src/a.py" in shown.text
    assert "icon.png" not in shown.text and "Routing.md" not in shown.text
    note = shown.omitted_note("/tree/full.patch")
    assert "icon.png" in note and "Routing.md" in note and "/tree/full.patch" in note


def test_translation_values_and_prose_always_stay():
    diff = (_hunk("project/core/i18n.py", '     "cs": {\n-        "k": "stare",\n+        "k": "nove",\n')
            + _hunk(".claude/memory/state.md", " a\n-the old claim\n+the new claim\n"))
    shown = reviewdiff.filter_diff(diff)
    assert shown.text == diff
    assert shown.omitted_note("/p") == ""
