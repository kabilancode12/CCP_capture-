"""CCP Explorer: a server-rendered website for browsing captured chats (standard library only).

Entry points: :func:`ccp.explorer.app.create_app` (to mount at ``/explorer``) and the command
``python -m ccp.explorer`` (``create-user`` and ``serve``). See docs/explorer.md.
"""

from ccp.explorer.plugin import register

__all__ = ["register"]
