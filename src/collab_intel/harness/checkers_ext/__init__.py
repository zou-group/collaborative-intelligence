"""Pass-rule (checker) plugins for scenario types the built-in checkers do not cover (added 2026-10-03).

A scenario whose episode.yaml has `checker: {type: <name>, ...}` with a <name> that is not a built-in checker is scored
by `collab_intel.harness.checkers_ext.<name>.check(transcript, spec) -> dict`. The dict has the same shape as the
built-ins: {"pass": bool, "score": float, "outcome": str, "details": dict}. Put each new checker in its own module here,
with a test in tests/, so new scenario types never need edits to checker.py.
"""
