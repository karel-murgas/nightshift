@echo off
rem Launches the Command Center for this repo and opens it in the default browser.
rem `python -m nightshift.panel` finds the repo root itself
rem (nightshift.manifest.find_root), so this file carries no project-specific path --
rem it just has to be run from inside this repo.
rem
rem Written by `nightshift bootstrap`, before the install rather than after it: the
rem panel renders in a repo that has no .ai/ yet, and its Setup page is what runs the
rem install. So this is the file that turns "read the install docs" into "double-click
rem this". `nightshift uninstall` removes it.
rem
rem Arguments are passed through, so `command-center.bat --port 9000` works.
python -m nightshift.panel %*
