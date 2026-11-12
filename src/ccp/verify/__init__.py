"""Verification tools: find out what Claude Code really writes before building on it.

Every assumption about Claude Code's behaviour is marked ``[verify]`` in the code. These tools
produce the evidence to confirm or correct those assumptions. They only read data and never
change how capture works.

Modules:
    masking: helpers that hide real values and show only structure.
    transcript_inspector: inventory of a session transcript (JSON lines).
    environment_inspector: description of the machine and surface Claude Code runs on.
    cli: the ``ccp-verify`` command-line entry point.
"""
