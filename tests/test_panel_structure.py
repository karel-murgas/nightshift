"""The Command Center's shape: pages by whose move it is, one row, one place per card.

Three properties the restructure (`command-center-restructure`) exists for, pinned so
they cannot drift back one section at a time:

- every list on every page renders through `panel._item` — no hand-built rows or tables;
- a board card appears in exactly one place across the page set;
- every verb the handler routes is still reachable from a page, the launch dialog or
  the page script — derived from the handler's own source, not from a list here.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from nightshift import panel
from test_panel import _card, _git, _repo, _get, server  # noqa: F401 — `server` is a fixture

CARD_IDS = ("t-run", "t-hand", "t-chore", "d-one", "p-one", "r-one", "b-one", "f-one")


def _busy_board(root: Path) -> Path:
    """One card in every lane a page lists, plus routed notes and an idea."""
    for card_id, unattended in (("t-run", "true"), ("t-hand", "false"),
                                ("t-chore", "false")):
        path = _card(root, "tasks", card_id, unattended=unattended)
        text = path.read_text(encoding="utf-8") + (
            "\n## Approach\n\nDo the one thing.\n\n## Open Questions\n\nnone.\n")
        if card_id == "t-chore":
            text = text.replace("verify: play", "verify: play\nkind: chore")
        path.write_text(text, encoding="utf-8", newline="")
    _card(root, "needs-decision", "d-one")
    _card(root, "testing", "p-one")
    _card(root, "review", "r-one")
    _card(root, "blocked", "b-one")
    _card(root, "failed", "f-one")
    inbox = root / "Board" / "inbox"
    (inbox / "n-chore.md").write_text("---\nroute: chore\n---\nA small thing.\n",
                                      encoding="utf-8", newline="")
    (inbox / "n-triage.md").write_text("---\nroute: triage\n---\nA big thing.\n",
                                       encoding="utf-8", newline="")
    (inbox / "n-plain.md").write_text("Not classified yet.\n", encoding="utf-8", newline="")
    (root / "Board" / "ideas" / "i-one.md").write_text("UNREADBODY\n", encoding="utf-8",
                                                      newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "busy board")
    return root


@pytest.fixture(scope="module")
def pages(tmp_path_factory) -> dict[str, str]:
    root = _busy_board(_repo(tmp_path_factory.mktemp("busy")))
    out = {page: panel.render_page(page, root) for page in panel.PAGES + ("history",)}
    out["decide"] = panel.render_decide(root, "d-one")
    return out


def _main(html: str) -> str:
    """The page content, without the rail, the header and the launch dialog."""
    return html.split("</dialog>", 1)[-1]


def test_every_row_is_an_item(pages):
    for name, html in pages.items():
        if name == "decide":
            continue
        rows = re.findall(r'<div class="row(?: [^"]*)?"[^>]*>', html)
        assert rows or name in ("running", "history"), f"{name} renders no rows at all — the fixture is not busy enough"
        for row in rows:
            assert "data-item=" in row, f"{name}: a row not drawn by panel._item: {row}"
        assert "<table" not in _main(html), f"{name} still hand-builds a table"


def test_only_the_item_helper_calls_the_row_primitive():
    source = inspect.getsource(panel)
    calls = len(re.findall(r"\b_row\(", source))
    assert calls == 2, "only `def _row(` and the one call inside `_item` may exist"
    assert "_row(" in inspect.getsource(panel._item)


def test_a_card_appears_in_exactly_one_place(pages):
    seen: dict[str, list[str]] = {}
    for page in panel.PAGES:
        for card_id in re.findall(r'data-card="([^"]+)"', pages[page]):
            seen.setdefault(card_id, []).append(page)
    for card_id in CARD_IDS:
        assert seen.get(card_id), f"{card_id} is on no page"
    doubled = {k: v for k, v in seen.items() if len(v) > 1}
    assert not doubled, f"cards listed in more than one place: {doubled}"


def test_each_lane_lands_on_the_page_whose_move_it_is(pages):
    where = {page: set(re.findall(r'data-card="([^"]+)"', pages[page]))
             for page in panel.PAGES}
    assert {"t-run", "t-hand", "t-chore"} <= where["queue"]
    assert {"d-one", "p-one", "r-one", "b-one", "f-one"} <= where["you"]
    assert "n-chore.md" in pages["capture"] and "i-one.md" in pages["capture"]


#: Verbs whose control renders only in a state the busy board does not reach, each
#: with the renderer that emits it — checked against that renderer's source instead.
_CONDITIONAL = {
    "api/stop": "_runstate_html",            # a live run
    "api/talk": "_roster_item",              # a run with a transcript
    "api/setup": "_system_setup",            # an uninstalled repo
    "api/update/apply": "_system_files",     # a template change
    "api/update/take": "_system_files",      # a conflict
    "api/update/keep": "_system_files",
    "api/update/merge": "_system_files",
    "api/audio/pick": "_audio_blocks",       # harvested audio
    "api/image/pick": "_image_blocks",       # harvested images
    "api/system/corrections": "_system_corrections",  # a harvest backlog
    "api/local": "_local_html",              # a machine declaring a local model
    "api/review": "_review_section",         # a review card with commits to review
    "api/review-all": "_review_section",
}
#: Reached by the browser without a button: page data, the refresh, and the one-card
#: reorder the drag never used (it posts `reorder-many`).
_NOT_A_BUTTON = {"api/body", "api/refresh", "api/reorder"}


def _routes() -> set[str]:
    source = inspect.getsource(panel.Handler._route) + inspect.getsource(panel.Handler.do_GET)
    found = set(re.findall(r'path == "(api/[a-z/-]+)"', source))
    for group in re.findall(r"path in \(([^)]*)\)", source):
        found |= set(re.findall(r'"(api/[a-z/-]+)"', group))
    found |= {f"api/system/{name}" for name in
              re.findall(r'name == "([a-z]+)"', inspect.getsource(panel._system_verb))}
    return found


def test_every_verb_the_handler_routes_is_still_reachable(pages):
    everything = "".join(pages.values()) + panel.TEMPLATE.read_text(encoding="utf-8")
    routes = _routes()
    assert len(routes) > 40, "the route parser found too little to mean anything"
    missing = []
    for route in sorted(routes - _NOT_A_BUTTON):
        if f"/{route}" in everything or f"'{route}'" in everything:
            continue
        renderer = _CONDITIONAL.get(route)
        if renderer and f"/{route}" in inspect.getsource(getattr(panel, renderer)):
            continue
        missing.append(route)
    assert not missing, f"verbs with no control left on any page: {missing}"


def test_the_header_is_one_line_and_the_dispatch_options_are_in_the_dialog(pages):
    html = pages["queue"]
    header = html.split('id="statusrail"', 1)[1].split("<dialog", 1)[0]
    for control in ('id="tierpick"', 'id="allowpaid"', "<select"):
        assert control not in header
    dialog = html.split("<dialog", 1)[1].split("</dialog>", 1)[0]
    for control in ('id="tierpick"', 'id="tieroverride"', 'id="allowpaid"', "Switch account"):
        assert control in dialog
    assert "paid overage" in header, "the money rule's state stays visible at a glance"


def test_starting_an_agent_goes_through_the_launch_dialog(pages):
    queue = pages["queue"]
    assert "launch.apply(null," in queue
    assert "post('/api/dispatch'" not in queue and "post('/api/work'" not in queue


def test_ideas_are_listed_by_name_and_never_read(pages):
    assert "i-one.md" in pages["capture"]
    assert "UNREADBODY" not in "".join(pages.values())


@pytest.mark.parametrize("old,new", sorted(panel.OLD_PAGES.items()))
def test_old_addresses_land_on_the_new_page(server, old, new):  # noqa: F811
    base, _root = server
    status, text = _get(base, old)
    assert status == 200
    assert f'href="/{new}" aria-current="page"' in text


def test_the_local_row_is_hidden_for_interactive_launches(pages):
    """An interactive session always runs `claude` on cloud, so offering the local
    model there would be a control that does nothing. The dialog names which launches
    are interactive; each must be a real route that opens a session."""
    routes = _routes()
    for path in panel.INTERACTIVE_PATHS:
        assert path.lstrip("/") in routes, f"{path} is not a route"
    session_source = inspect.getsource(panel)
    assert session_source.count("session_argv(") >= len(panel.INTERACTIVE_PATHS)
    dialog = pages["queue"].split("<dialog", 1)[1].split("</dialog>", 1)[0]
    assert "data-interactive=" in dialog
    script = panel.TEMPLATE.read_text(encoding="utf-8")
    assert 'getElementById("opt-local")' in script and "dataset.interactive" in script
    assert ".opt[hidden]" in script, "without it the flex rule keeps the hidden row on screen"
