"""The hooks that make a rule impossible to skip rather than merely
documented (00_architecture.md §16, 10_self_improvement.md §4). Eleven of
them declare a `SUBJECT`, per `hooks.discover.discover()`; the rest of this
package is support code they share (`shellwords`, `project_script`,
`session_memo`, `discover` itself).

Wired as `python -m nightshift.hooks.<name>` in a consuming project's
`.claude/settings.json` (07_portability.md §8 step 3). Each module is also
runnable by hand for testing — see its own docstring for the exact command.
"""
