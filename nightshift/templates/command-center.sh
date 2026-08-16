#!/bin/sh
# Launches the Command Center for this repo and opens it in the default browser.
# `python -m nightshift.panel` finds the repo root itself
# (nightshift.manifest.find_root), so this file carries no project-specific path --
# it just has to be run from inside this repo.
#
# Written by `nightshift bootstrap`, before the install rather than after it: the
# panel renders in a repo that has no .ai/ yet, and its Setup page is what runs the
# install. So this is the file that turns "read the install docs" into "run this".
# `nightshift uninstall` removes it.
#
# The .bat beside this one is the same launcher for Windows. Both are committed,
# because an install is a property of the repo rather than of the machine that ran
# it, and a clone should work wherever it lands.
exec python -m nightshift.panel "$@"
