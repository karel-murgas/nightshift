"""What the diff reviewer is shown (`nightshift.reviewdiff`).

The rule: hunks a gate or another stage owns are cut *and listed*, never dropped
silently — and prose (docs, comments, memory files) always stays, because 11 of 14
historical `needs_fix` verdicts were wrong prose.
"""
from __future__ import annotations

from pathlib import Path

from nightshift import reviewdiff

_ADAPTER = '''
import re
from pathlib import Path
_ZONE = re.compile(r'^\\s*"(en|cs)":\\s*\\{\\s*$')
_KEY = re.compile(r'^\\s*"([^"]+)":\\s*"')
def i18n_path(root):
    return Path(root) / "strings.py"
def load_strings(root):
    return {}
def zone_key_lines(root):
    zones, lang = {}, None
    for i, line in enumerate(Path(root, "strings.py").read_text().splitlines(), 1):
        m = _ZONE.match(line)
        if m:
            lang = m.group(1); zones[lang] = {}
        elif lang and (k := _KEY.match(line)):
            zones[lang][k.group(1)] = i
    return zones
'''


def _project(root: Path) -> Path:
    (root / ".ai").mkdir(parents=True)
    (root / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "demo"\nsource_dirs = ["src"]\n\n'
        '[i18n]\nadapter = ".ai/adapter.py"\nbase = "en"\ntargets = ["cs"]\n',
        encoding="utf-8")
    (root / ".ai" / "adapter.py").write_text(_ADAPTER, encoding="utf-8")
    lines = ["_S = {", '    "en": {']
    lines += [f'        "k{i}": "english {i}",' for i in range(1, 6)]
    lines += ["    },", '    "cs": {']
    lines += [f'        "k{i}": "cesky {i}",' for i in range(1, 6)]
    lines += ["    },", "}"]
    (root / "strings.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _hunk(path: str, new_start: int, body: str) -> str:
    return (f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -{new_start},3 +{new_start},3 @@\n{body}")


def test_target_language_hunks_are_cut_and_listed_base_language_stays(tmp_path):
    root = _project(tmp_path)
    # strings.py: line 3-7 are en keys, 10-14 are cs keys.
    en_hunk = '@@ -4,1 +4,1 @@\n-        "k2": "old",\n+        "k2": "english 2",\n'
    cs_hunk = '@@ -11,1 +11,1 @@\n-        "k2": "stare",\n+        "k2": "cesky 2",\n'
    diff = ("diff --git a/strings.py b/strings.py\nindex 1..2 100644\n"
            "--- a/strings.py\n+++ b/strings.py\n" + en_hunk + cs_hunk)
    shown = reviewdiff.filter_diff(diff, root)
    assert '"english 2"' in shown.text
    assert '"cesky 2"' not in shown.text
    assert any("strings.py" in o and "cs translation" in o and "+1 −1" in o
               for o in shown.omitted)


def test_a_file_whose_every_hunk_is_cut_leaves_no_empty_header(tmp_path):
    root = _project(tmp_path)
    diff = _hunk("strings.py", 11, ' "k1": "cesky 1",\n-        "k2": "a",\n+        "k2": "b",\n')
    shown = reviewdiff.filter_diff(diff, root)
    assert "strings.py" not in shown.text


def test_binaries_and_generated_views_are_listed_prose_stays(tmp_path):
    root = _project(tmp_path)
    diff = (
        "diff --git a/art/icon.png b/art/icon.png\nindex 1..2 100644\n"
        "Binary files a/art/icon.png and b/art/icon.png differ\n"
        + _hunk("Routing.md", 1, " a\n-b\n+c\n")
        + _hunk(".claude/memory/state.md", 1, " a\n-the old claim\n+the new claim\n")
    )
    shown = reviewdiff.filter_diff(diff, root)
    assert "the new claim" in shown.text
    assert "icon.png" not in shown.text and "Routing.md" not in shown.text
    note = shown.omitted_note("/tree/full.patch")
    assert "icon.png" in note and "Routing.md" in note and "/tree/full.patch" in note


def test_nothing_to_filter_means_the_diff_is_untouched_and_no_note(tmp_path):
    diff = _hunk("src/a.py", 1, " x\n-y\n+z\n")
    shown = reviewdiff.filter_diff(diff, tmp_path)   # no manifest at all
    assert shown.text == diff
    assert shown.omitted_note("/p") == ""
