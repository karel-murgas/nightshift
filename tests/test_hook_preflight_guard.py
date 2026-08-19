"""Tests for the `preflight_guard` PreToolUse hook.

Closes the 2026-08-03 incident: the guard resolved the repository with
`manifest.find_root()`, which answers from the *process* working directory. A
session rooted in the consuming project ran a push against the framework repo, the
guard looked up the receipt in the session's repo, found one, and let a commit whose
preflight had failed go out. Driving one repo from a session rooted in another is
routine in this pair of repos, so the hole was on the normal path.

Everything here therefore builds **two real git repositories** and asks which one's
receipt the guard read. A fixture that stubbed `is_validated` could not tell a fix
from the bug — the bug was passing the wrong root to a function that worked fine.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nightshift import preflight
from nightshift.hooks import preflight_guard

import _fixtures


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], check=True,
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return (out.stdout or "").strip()


@pytest.fixture(autouse=True)
def _no_receipt_override(monkeypatch):
    """`PREFLIGHT_RECEIPT` points every root at one file, which would make the two
    repositories here indistinguishable — the exact confusion under test."""
    monkeypatch.delenv("PREFLIGHT_RECEIPT", raising=False)


def _repo(tmp_path: Path, name: str, *, manifest: bool = True) -> Path:
    """A git repository with one commit, and by default a nightshift manifest."""
    repo = tmp_path / name
    (repo / ".ai").mkdir(parents=True)
    if manifest:
        (repo / ".ai" / "manifest.toml").write_text(
            f'[project]\nname = "{name}"\n', encoding="utf-8", newline="\n")
    (repo / "file.txt").write_text("one\n", encoding="utf-8", newline="\n")
    _fixtures.git_init(repo, branch="main",
                       extra_config=(("commit.gpgsign", "false"),))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    return repo


def _commit(repo: Path, text: str) -> str:
    (repo / "file.txt").write_text(text + "\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", text)
    return _git(repo, "rev-parse", "HEAD")


def _validate(repo: Path) -> str:
    """Give `repo` a receipt for its current HEAD, the way a passing preflight does."""
    sha = _git(repo, "rev-parse", "HEAD")
    preflight.write_receipt(repo, sha, None)
    return sha


def _verdict(decision: dict) -> str:
    return decision["hookSpecificOutput"]["permissionDecision"]


def _reason(decision: dict) -> str:
    return decision["hookSpecificOutput"].get("permissionDecisionReason", "")


# --- the baseline the fix must not break ----------------------------------------


def test_a_push_from_the_session_repo_with_a_receipt_is_allowed(tmp_path):
    """The common case, and the one the guard has always got right. It has to keep
    working after the repository stopped being found from the process cwd."""
    repo = _repo(tmp_path, "proj")
    _validate(repo)
    assert _verdict(preflight_guard.decide("git push", repo)) == "allow"


def test_a_push_from_the_session_repo_without_a_receipt_is_denied(tmp_path):
    repo = _repo(tmp_path, "proj")
    decision = preflight_guard.decide("git push origin main", repo)
    assert _verdict(decision) == "deny"
    assert "preflight has not validated" in _reason(decision)


def test_a_git_command_that_publishes_nothing_is_untouched(tmp_path):
    """`git status`, and `git log --grep=push` in particular: the word "push" inside
    an argument must not be mistaken for the subcommand."""
    repo = _repo(tmp_path, "proj")
    for command in ("git status", "git log --grep=push", "git commit -m 'ready to push'"):
        assert _verdict(preflight_guard.decide(command, repo)) == "allow", command


# --- the incident ----------------------------------------------------------------


def test_a_cross_repo_push_checks_the_targets_receipt_not_the_sessions(tmp_path):
    """The 2026-08-03 failure, verbatim: the session's repo is validated, the repo
    being pushed is not, and the old guard allowed it because it only ever asked the
    session's repo."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _validate(session)

    decision = preflight_guard.decide(f"git -C {target} push", session)
    assert _verdict(decision) == "deny"
    assert "target" in _reason(decision), "the denial must name the repo it checked"


def test_a_cross_repo_push_is_allowed_when_the_target_itself_is_validated(tmp_path):
    """The mirror image, and the proof that the guard really moved rather than
    getting stricter: here it is the *session* that has no receipt, and the push is
    allowed because the repo being pushed does."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _validate(target)

    assert _verdict(preflight_guard.decide(f"git -C {target} push", session)) == "allow"


def test_a_receipt_for_an_older_commit_does_not_unblock_the_new_tip(tmp_path):
    """Staleness is per-commit, in the target repo too. A receipt is written for a
    SHA, so committing on top of a validated commit re-arms the guard."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _validate(target)
    new = _commit(target, "two")

    decision = preflight_guard.decide(f"git -C {target} push", session)
    assert _verdict(decision) == "deny"
    assert new[:8] in _reason(decision)


# --- one tree, several commits ----------------------------------------------------
#
# The receipt attests to *content*. Keyed on the SHA alone, the ordinary
# validate-then-merge-then-push sequence was unpushable by construction: the merge
# mints a new commit over files that were validated seconds earlier, and the guard
# demanded the whole run again to reach the only verdict it could reach.


def test_pushing_a_no_ff_merge_of_a_validated_branch_is_allowed(tmp_path):
    """The sequence this fixes, end to end. Measured 2026-08-19 on
    `ai/panel-activity-decide`: branch tip and merge commit both at tree
    `254dad63`, and the push was denied after a passing 13-minute preflight."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _git(target, "checkout", "-q", "-b", "feature")
    tip = _commit(target, "two")
    preflight.write_receipt(target, tip, None)

    _git(target, "checkout", "-q", "main")
    _git(target, "merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    merge = _git(target, "rev-parse", "HEAD")
    assert merge != tip, "a --no-ff merge must be its own commit, or this proves nothing"
    assert _git(target, "rev-parse", "HEAD^{tree}") == _git(target, "rev-parse", f"{tip}^{{tree}}")

    assert _verdict(preflight_guard.decide(f"git -C {target} push", session)) == "allow"


def test_an_amended_message_does_not_re_arm_the_guard(tmp_path):
    """Same files, new SHA. Re-running the suite over byte-identical content to
    approve a reworded commit message is work that cannot change its own answer."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _validate(target)
    _git(target, "commit", "-q", "--amend", "-m", "one, said better")

    assert _verdict(preflight_guard.decide(f"git -C {target} push", session)) == "allow"


def test_a_merge_that_actually_changes_content_is_still_guarded(tmp_path):
    """The strictness is for exactly this: when the integration branch has moved,
    the merge produces a tree neither parent carries, so nothing attests to it and
    it is validated in full. A tree match must never be reachable by combining two
    separately-validated trees."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _git(target, "checkout", "-q", "-b", "feature")
    (target / "from-feature.txt").write_text("f\n", encoding="utf-8", newline="\n")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "feature side")
    tip = _git(target, "rev-parse", "HEAD")
    preflight.write_receipt(target, tip, None)

    _git(target, "checkout", "-q", "main")
    (target / "from-main.txt").write_text("m\n", encoding="utf-8", newline="\n")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "main moved")
    _validate(target)
    _git(target, "merge", "-q", "--no-ff", "feature", "-m", "real merge")

    decision = preflight_guard.decide(f"git -C {target} push", session)
    assert _verdict(decision) == "deny", "the merged tree was never validated"


def test_a_receipt_written_before_trees_were_recorded_validates_nothing_extra(tmp_path):
    """An old receipt has no `tree` field. Two `None`s comparing equal would turn
    every entry into a wildcard — the widening direction, on the file that decides
    what may leave the branch."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _validate(target)
    path = target / preflight.RECEIPT
    entries = json.loads(path.read_text(encoding="utf-8"))
    for entry in entries:
        entry.pop("tree", None)
    path.write_text(json.dumps(entries), encoding="utf-8", newline="\n")

    new = _commit(target, "two")
    decision = preflight_guard.decide(f"git -C {target} push", session)
    assert _verdict(decision) == "deny"
    assert new[:8] in _reason(decision)


def test_a_cd_earlier_in_the_command_moves_the_repo_that_is_checked(tmp_path):
    """`cd ../other && git push` is how the incident was actually typed. Following it
    needs the `;`/`&&` operators to be tokens, which plain `shlex.split` does not do."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _validate(session)

    for command in (f"cd {target} && git push",
                    f"cd {target}; git push",
                    "cd ../target && git push"):
        assert _verdict(preflight_guard.decide(command, session)) == "deny", command

    _validate(target)
    assert _verdict(preflight_guard.decide(f"cd {target} && git push", session)) == "allow"


def test_a_subshell_cd_is_undone_when_the_subshell_closes(tmp_path):
    """`(cd a && git push) && git push` pushes two different repositories. Carrying
    `a` forward past the `)` would check the first repo's receipt for the second
    push — the same wrong-repo answer in miniature."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _validate(target)

    # First push is against `target` (validated); the second is against the session
    # repo, which is not — so the command as a whole must be denied.
    decision = preflight_guard.decide(f"(cd {target} && git push) && git push", session)
    assert _verdict(decision) == "deny"
    assert "session" in _reason(decision)


def test_work_tree_and_git_dir_name_the_repository_too(tmp_path):
    """Two more ways to push a repo you are not standing in. `--git-dir` points at
    `…/.git`, whose parent is the work tree."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _validate(session)

    for command in (f"git --work-tree={target} --git-dir={target}/.git push",
                    f"git --git-dir {target}/.git push"):
        assert _verdict(preflight_guard.decide(command, session)) == "deny", command


def test_a_merge_checks_the_tip_of_the_ref_in_the_target_repo(tmp_path):
    """The ref has to be resolved in the repo being merged into, not the session's —
    `feature` may not even exist there, and if it does it is a different commit."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _validate(session)
    _git(target, "checkout", "-q", "-b", "feature")
    tip = _commit(target, "feature work")
    _git(target, "checkout", "-q", "main")

    decision = preflight_guard.decide(f"git -C {target} merge feature", session)
    assert _verdict(decision) == "deny"
    assert tip[:8] in _reason(decision)

    preflight.write_receipt(target, tip, None)
    assert _verdict(preflight_guard.decide(f"git -C {target} merge feature", session)) == "allow"


def test_gh_pr_create_is_still_guarded_in_the_directory_it_runs_in(tmp_path):
    """`gh` has no `-C`; the only thing that moves it is the working directory. It was
    guarded before this change and has to stay guarded after it."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _validate(session)

    assert _verdict(preflight_guard.decide("gh pr create --fill", session)) == "allow"
    assert _verdict(preflight_guard.decide(
        f"cd {target} && gh pr create --fill", session)) == "deny"
    assert _verdict(preflight_guard.decide("gh pr list", session)) == "allow"


def test_an_environment_prefix_does_not_hide_the_git_command(tmp_path):
    """`GIT_TRACE=1 git push` is still a push. A parser that read the assignment as
    the program name would wave it through."""
    repo = _repo(tmp_path, "proj")
    assert _verdict(preflight_guard.decide("GIT_TRACE=1 git push", repo)) == "deny"


def test_a_quoted_path_with_spaces_still_names_a_repository(tmp_path):
    """The lexer gives up backslash escaping so unquoted Windows paths survive (see
    `_tokenize`); quoting has to carry spaces after that, so it is asserted here
    rather than assumed."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "a target")
    _validate(session)

    assert _verdict(preflight_guard.decide(f'git -C "{target}" push', session)) == "deny"


def test_bash_dash_c_is_followed_one_level_down(tmp_path):
    """`bash -c 'cd other && git push'` hides the whole command in one token. Left
    unread it is a push the guard never sees."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _validate(session)

    decision = preflight_guard.decide(f"bash -c 'cd {target} && git push'", session)
    assert _verdict(decision) == "deny"


# --- repos that are not ours ------------------------------------------------------


def test_a_repo_without_a_manifest_is_not_ours_to_block(tmp_path):
    """A guard that denied every push to every unrelated repository the maintainer
    touches from an agent session would be switched off within a day — which puts the
    cross-repo hole back *and* removes the protection. No `.ai/manifest.toml`, no
    opinion."""
    session = _repo(tmp_path, "session")
    stranger = _repo(tmp_path, "stranger", manifest=False)

    assert _verdict(preflight_guard.decide(f"git -C {stranger} push", session)) == "allow"
    assert _verdict(preflight_guard.decide(f"cd {stranger} && git push", session)) == "allow"


def test_a_directory_that_is_not_a_git_work_tree_is_allowed(tmp_path):
    """There is no receipt to check and no push that can succeed. Denying here would
    be noise with nothing behind it."""
    session = _repo(tmp_path, "session")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _verdict(preflight_guard.decide(f"git -C {plain} push", session)) == "allow"


# --- failing closed ---------------------------------------------------------------


def test_a_command_whose_quoting_does_not_balance_is_denied(tmp_path):
    """The old hook allowed anything it could not lex. That is the hole restored
    silently: an unreadable command is exactly when the guard knows least."""
    repo = _repo(tmp_path, "proj")
    _validate(repo)
    decision = preflight_guard.decide('git push "origin', repo)
    assert _verdict(decision) == "deny"
    assert "could not tell which repository" in _reason(decision)


def test_an_unlexable_command_with_no_sign_of_a_push_is_still_allowed(tmp_path):
    """Failing closed is scoped to commands that look like a publish. Denying every
    malformed Bash call would be a different, much larger rule."""
    repo = _repo(tmp_path, "proj")
    assert _verdict(preflight_guard.decide('echo "unterminated', repo)) == "allow"


def test_a_cd_the_guard_cannot_follow_denies_the_push_after_it(tmp_path):
    """`cd $TARGET && git push`: the variable is not expanded in the token stream, so
    the directory is unknown. Unknown is the state the incident happened in."""
    repo = _repo(tmp_path, "proj")
    _validate(repo)
    for command in ("cd $TARGET && git push", "cd && git push", "cd - && git push",
                    "pushd ../other && git push"):
        decision = preflight_guard.decide(command, repo)
        assert _verdict(decision) == "deny", command
        assert "could not tell which repository" in _reason(decision), command


def test_git_run_through_another_program_is_denied(tmp_path):
    """`xargs git push` says plainly what it does and not from where. A bare `git`
    token followed by a guarded subcommand is legible intent with an illegible
    target, which is the definition of the case that must fail closed."""
    repo = _repo(tmp_path, "proj")
    _validate(repo)
    assert _verdict(preflight_guard.decide("echo x | xargs git push", repo)) == "deny"


def test_the_word_git_in_an_argument_is_not_a_hidden_push(tmp_path):
    """The counterweight to the test above: `grep` over a file that mentions pushing
    must not be denied, or the guard becomes noise and gets removed."""
    repo = _repo(tmp_path, "proj")
    _validate(repo)
    for command in ('grep -r "git push" .', "grep git README.md", "echo 'git push'"):
        assert _verdict(preflight_guard.decide(command, repo)) == "allow", command


def test_the_cannot_tell_denial_says_how_to_rewrite_the_command(tmp_path):
    """A refusal that does not name a way forward reads as a bug, and the next
    person's fix is to disable the hook."""
    repo = _repo(tmp_path, "proj")
    reason = _reason(preflight_guard.decide("cd $TARGET && git push", repo))
    assert "git -C <path> push" in reason
    assert "2026-08-03" in reason, "the denial should carry the incident that earned it"


# --- the hook as it is actually invoked -------------------------------------------


def test_the_hook_answers_the_real_stdin_protocol(tmp_path):
    """Everything above calls `decide` directly. This is the only test that proves
    the module still works when Claude Code runs it — payload in, decision JSON out,
    exit 0 either way."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    _validate(session)

    def ask(command: str) -> dict:
        payload = json.dumps({"cwd": str(session), "tool_name": "Bash",
                              "tool_input": {"command": command}})
        out = subprocess.run([sys.executable, "-m", "nightshift.hooks.preflight_guard"],
                             input=payload, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    assert _verdict(ask("git push")) == "allow"
    assert _verdict(ask(f"git -C {target} push")) == "deny"


def test_the_session_cwd_comes_from_the_payload_not_the_process(tmp_path, monkeypatch):
    """The hook is a separate process and nothing promises it starts in the session's
    directory. Reading `cwd` from the payload is what makes a bare `git push` resolve
    to the repo the session is in rather than wherever the hook happened to launch."""
    session = _repo(tmp_path, "session")
    monkeypatch.chdir(tmp_path)
    assert preflight_guard._session_cwd({"cwd": str(session)}) == Path(str(session))
    assert preflight_guard._session_cwd({}) == Path.cwd()


# --- here-documents are data, not command text ---------------------------------


def test_a_here_document_body_is_not_read_as_a_command(tmp_path):
    """The guard denied the commit of its own rewrite, and then denied the script
    written to investigate that.

    Both bodies described the incident this guard was built for — in prose, with an
    apostrophe in "the session's repo". A heredoc body is data the shell hands to a
    program's stdin and is never parsed as command text, but the guard read it, decided
    the command looked guarded because the prose said so, then could not lex it because
    of the apostrophe, and failed closed on a commit that pushed nothing.
    """
    session = _repo(tmp_path, "session")
    body = "The old guard checked the session's repo, so a push went out unverified."
    command = f"git commit -F - <<'MSG'\n{body}\nMSG\n"

    assert _verdict(preflight_guard.decide(command, session)) == "allow"


def test_a_push_after_a_here_document_is_still_seen(tmp_path):
    """Stripping the body must not strip the command that follows it — otherwise the
    fix for a false positive is a hole."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    command = ("git commit -F - <<'MSG'\nordinary message\nMSG\n"
               f"git -C {target.as_posix()} push\n")

    assert _verdict(preflight_guard.decide(command, session)) == "deny"


def test_a_here_string_is_not_mistaken_for_a_here_document(tmp_path):
    """`<<<` is a here-string: one word, on the same line, and its content is not a
    body to skip. Swallowing the rest of the command as if it were would hide a push."""
    session = _repo(tmp_path, "session")
    target = _repo(tmp_path, "target")
    command = f"cat <<<word\ngit -C {target.as_posix()} push\n"

    assert _verdict(preflight_guard.decide(command, session)) == "deny"
