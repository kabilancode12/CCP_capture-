"""Code snapshots: the working tree as hidden Git commits, without touching the developer's work.

Modules:
    git: a safe wrapper around the ``git`` command (no shell, no prompts, time limits).
    taker: builds a snapshot in a temporary index, excluding files that contain secrets.
    push: where snapshots are pushed, and the queue for pushes that failed.
    service: the actions hooks call (start, snapshot, finish); never raises.

See docs/snapshots.md.
"""
