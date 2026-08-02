# templates/

What `nightshift init` writes into a project. **Not importable code** — these are
files a repo owns after they land, and it may edit any of them freely.

They exist because the Python half of this framework was standalone before the
rest of it was: measured on 2026-08-02, an empty repo could reach one dispatchable
card and a green gate suite, but only after six files were written by hand and the
hooks were copied out of the origin project. `pip install` provided none of it.
These templates are what closes that gap.

| Path | Lands at | Notes |
|---|---|---|
| `agents/*.md` | `.claude/agents/` | the four core charters — `code-thread`, `code-reviewer`, `triage`, `stale-hunter`. The runner dispatches these by name and `card_schema` checks the file exists |
| `skills/*/SKILL.md` | `.claude/skills/` | the three operator skills. Instructions for the agent, distinct from the README, which is for the person |
| `settings.hooks.json` | merged into `.claude/settings.json` | seven hook entries. Merged, never overwritten — a project's `permissions` block is its own |
| `ai/corrections.log` | `.ai/corrections.log` | header only |
| `ai/gates/data/corrections_vocab.json` | same path | **`class` and `channel` ship empty.** See the file's own comment; this is enforcement, not omission |
| `ai/hosts.json` | `.ai/hosts.json` | the reasoning travels; the capabilities do not |
| `board/README.md` | `Board/README.md` | the lane contract |
| `gitattributes` | `.gitattributes` | `* text=auto eol=lf`, written only if absent |
| `tier-binding.md` | wherever `[tiers].binding_doc` points | written only if no document already carries the block |

## Tokens

Rendered by `nightshift.init.render` — plain `str.replace`, no template engine,
because a template that needs a dependency to read is a template nobody audits.

| Token | Filled from |
|---|---|
| `{{package}}` | `[project].source_dirs[0]` |
| `{{project}}` | `[project].name` |
| `{{integration}}` | `[branches].integration` |
| `{{maintainer}}` | `git config user.name` |
| `{{hostname}}` | `socket.gethostname()` |

An unrendered `{{...}}` left in a written file is a bug, and
`test_templates.py::test_no_token_survives_rendering` fails on it — a template
that ships a literal `{{package}}` into a charter reads as a broken install.

## What these are not

They carry the origin project's *reasoning* and none of its *rules*. There is no
corrections log, no audit matrix, no earned gate. Dated verbatim quotes keep their
attribution to "the origin project's maintainer" rather than being re-signed with
whoever installs this: the files are about honest records, and re-attributing a
quote inside one would be the first thing they tell you not to do.

`§N` citations point at design notes that deliberately do not ship. Every rule
they cite is stated in full at the point it is cited, so a reader who never sees
those documents is missing the history, not the instruction.
