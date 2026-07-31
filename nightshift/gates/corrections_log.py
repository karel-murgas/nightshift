"""Gate: `.ai/corrections.log` stays parseable and clusterable.

Earned by an observed failure, not by being easy to write (`00_architecture.md`
§15). Session H was the first session to *read* this log rather than append to
it, and the read very nearly failed: the old `RULE_CLASS` field was free text
and had grown **40 distinct values across 50 entries**, so the one operation the
log exists to support -- "cluster by class of thing corrected" -- had no field
to cluster on. The single most valuable axis, *who found the defect* (§15's
attention metric), was not a field at all; it was buried in the prose of NOTE
and had to be recovered by reading all fifty.

So the failure this gate prevents is specific and has already happened once: a
log that is written faithfully for six sessions and turns out to be unclusterable
the first time someone needs it. That is expensive exactly in proportion to how
long it goes unnoticed, which is the argument for checking it now rather than at
the next read.

The vocabularies live in `<project>/.ai/gates/data/corrections_vocab.json`, not
in this file, for the same reason the i18n allowlists do: a value list that
ships inside the gate gets edited to make a run pass. Editing a data file to
add a class is a visible decision; that is the intent, and it is the appeal
path (§4 of `10_self_improvement.md`) for this gate specifically. That data
file is project-owned (it is this project's own vocabulary), which is why the
path is still `.ai/gates/data/...` even though the gate itself now ships in
core (07_portability.md §8 step 3).

What this gate deliberately does NOT check: whether the class assigned to an
entry is the *right* one. That is judgment, it is the whole content of the
entry, and a gate that guessed at it would be re-checking what a person decided
(§12).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from nightshift.gates.base import Violation

NAME = "corrections_log"
FAST = True
DESCRIPTION = "the correction log parses and its class/channel values are in vocabulary"

_LOG = Path(".ai") / "corrections.log"
_VOCAB = Path(".ai") / "gates" / "data" / "corrections_vocab.json"


def check(repo_root: Path) -> list[Violation]:
    log = repo_root / _LOG
    vocab_path = repo_root / _VOCAB
    rel = _LOG.as_posix()

    if not log.is_file():
        return [Violation(rel, 0, "the correction log does not exist — every §15 metric depends on it")]
    if not vocab_path.is_file():
        return [Violation(_VOCAB.as_posix(), 0, "the vocabulary file the log is checked against is missing")]

    # Reuse the reader rather than growing a second parser. It ships in the
    # framework package (07_portability.md §8 step 2), so this is a plain import.
    from nightshift import corrections

    vocab = {k: v for k, v in json.loads(vocab_path.read_text(encoding="utf-8")).items()
             if not k.startswith("_")}
    entries, malformed = corrections.parse(repo_root)
    problems = corrections.validate(entries, vocab)

    violations = [Violation(rel, line_no, reason) for line_no, reason in malformed]
    violations += [Violation(rel, line_no, reason) for line_no, reason in problems]

    # The archive (12_corrections_lifecycle.md) must stay just as parseable, and
    # every line in it must actually carry a disposition -- that is the one thing
    # that makes an entry eligible to have been compacted there in the first
    # place. A resolved marker missing on an archived entry means something was
    # moved that should not have been.
    arch_entries, arch_malformed = corrections.parse_archive(repo_root)
    archive_path = repo_root / corrections.ARCHIVE
    if archive_path.is_file():
        arch_rel = corrections.ARCHIVE.as_posix()
        arch_problems = corrections.validate(arch_entries, vocab)
        violations += [Violation(arch_rel, line_no, reason) for line_no, reason in arch_malformed]
        violations += [Violation(arch_rel, line_no, reason) for line_no, reason in arch_problems]
        for e in arch_entries:
            if not e.disposition:
                violations.append(Violation(
                    arch_rel, e.line_no,
                    "archived entry has no disposition -- it should not have been compacted",
                ))

    # A vocabulary value nobody uses is the mirror of a value nobody declared:
    # it is how a closed list quietly becomes an open one, by accumulating
    # plausible-sounding categories that never describe a real entry. Count usage
    # across the archive too, not just the active log: `--compact` legitimately
    # moves every entry of a resolved class out of the active file, and a value is
    # only truly dead if nothing in the whole corpus -- active or archived -- uses it.
    for axis in ("class", "channel"):
        field = "klass" if axis == "class" else axis
        used = {getattr(e, field) for e in entries + arch_entries}
        for value in sorted(set(vocab[axis]) - used):
            violations.append(Violation(
                _VOCAB.as_posix(), 0,
                f"{axis} {value!r} is declared in the vocabulary but no entry uses it — "
                f"remove it or the list stops meaning anything",
            ))
    return sorted(violations, key=lambda v: (v.file, v.line, v.rule))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
