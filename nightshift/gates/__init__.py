"""Gate machinery. `base` is the type every gate returns; `run` discovers and
drives them.

27 core gates live here, each declaring a `SUBJECT` (`00_architecture.md` §16). A
project's own gates never move here: §15's rule is that gate selection follows
observed failures, so a rule earned by one repo's incident stays in that repo's
`.ai/gates/`. `run` drives both directories — this package's and the consuming
project's — every time it runs.
"""
