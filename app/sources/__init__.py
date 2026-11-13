"""Clients for the open sources behind artist pages. Each takes a lookups.Http, returns plain dicts, and
raises lookups.StepStopped when its service refuses. The parse_* functions read one response and make no
requests, so tests can feed them recorded answers."""
