"""Gate machinery. `base` is the type every gate returns; `run` discovers and
drives them.

The gates *themselves* mostly do not live here yet — step 3 of `07_portability.md`
§8 moves the 23 that travel. What is already true, and is the reason `run` was
moved first, is that a project's own gates never move: §15's rule is that gate
selection follows observed failures, so a rule earned by one repo's incident stays
in that repo's `.ai/gates/`. `run` therefore has to drive two directories from the
day it ships, not from the day core has gates of its own.
"""
