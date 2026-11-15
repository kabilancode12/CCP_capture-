"""The places where other parts of CCP plug into the server.

The server offers two extension points, kept in a :class:`PluginRegistry`:

* **after-index callbacks**: functions called with a session id after the indexer finished a job
  for that session (the catalog and the decisions use this to follow the index);
* **mounts**: extra web applications served under a path such as ``/explorer``.

Optional packages register themselves **by name**. For each name in :data:`PLUGINS` the server
looks at its switch (``CCP_CATALOG_ENABLED``, ``CCP_DECISIONS_ENABLED``, ``CCP_EXPLORER_ENABLED``,
all off unless set to true) and, if it is on, imports ``ccp.<name>`` and calls its function::

    def register(registry: PluginRegistry, context: PluginContext) -> None: ...

which adds its callbacks and mounts. A package that is not there, or has no ``register``, is
skipped quietly (a switch that is on for a package that does not exist yet is not an error). A
package that is there but fails while loading is skipped with a warning that names the package and
the kind of error, never the details: one broken plug-in must not stop the ingestion API.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from starlette.types import ASGIApp

from ccp.settings import Settings
from ccp.storage import ObjectStore

logger = logging.getLogger("ccp.server")

# Plug-in name -> the environment switch that turns it on.
PLUGINS = {
    "catalog": "CCP_CATALOG_ENABLED",
    "decisions": "CCP_DECISIONS_ENABLED",
    "explorer": "CCP_EXPLORER_ENABLED",
}

# These paths belong to the ingestion API; a plug-in cannot mount over them.
_RESERVED_PATHS = ("/health", "/v1")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass
class PluginRegistry:
    """What plug-ins have registered.

    Attributes:
        after_index: Callbacks ``f(session_id)``, called one after the other in a separate thread
            (so a slow one never holds up indexing). A callback that raises is logged and the next
            one still runs. The same session is called again after each later indexing job, so a
            callback must be safe to repeat and decide for itself whether there is work to do.
        mounts: ``(path, application)`` pairs, served next to the API.
        loaded: The names of the plug-ins that registered.
    """

    after_index: list[Callable[[str], None]] = field(default_factory=list)
    mounts: list[tuple[str, ASGIApp]] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)

    def add_after_index(self, callback: Callable[[str], None]) -> None:
        """Call ``callback(session_id)`` after a session has been indexed."""
        self.after_index.append(callback)

    def add_mount(self, path: str, app: ASGIApp) -> None:
        """Serve ``app`` under ``path`` (for example ``/explorer``).

        Raises:
            ValueError: If the path is not a plain ``/name`` path, is the API's own, or is
                already taken.
        """
        clean = path.rstrip("/")
        if not clean.startswith("/") or clean == "":
            raise ValueError("a mount path must look like /name")
        if any(clean == r or clean.startswith(r + "/") for r in _RESERVED_PATHS):
            raise ValueError(f"{clean} belongs to the ingestion API")
        if any(clean == taken for taken, _ in self.mounts):
            raise ValueError(f"{clean} is already mounted")
        self.mounts.append((clean, app))


@dataclass(frozen=True)
class PluginContext:
    """What a plug-in may need to build its parts.

    Attributes:
        settings: The validated server settings (database, storage, API).
        store: The object store (the raw transcripts bucket).
        env: The environment (real variables and the ``.env`` file), for the plug-in's own
            switches and secrets.
    """

    settings: Settings
    store: ObjectStore
    env: Mapping[str, str]


def is_enabled(env: Mapping[str, str], switch: str) -> bool:
    """True if the environment variable ``switch`` is set to true/yes/on/1."""
    return env.get(switch, "").strip().lower() in _TRUE_VALUES


def load_plugins(
    registry: PluginRegistry,
    context: PluginContext,
    plugins: Mapping[str, str] | None = None,
) -> list[str]:
    """Let every enabled and installed plug-in register itself.

    Args:
        registry: Where plug-ins add their callbacks and mounts.
        context: Passed to each plug-in's ``register``.
        plugins: Plug-in name -> environment switch; defaults to :data:`PLUGINS`.

    Returns:
        The names of the plug-ins that registered.
    """
    for name, switch in (PLUGINS if plugins is None else plugins).items():
        if not is_enabled(context.env, switch):
            continue
        module_name = f"ccp.{name}"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as error:
            if error.name in (module_name, "ccp"):
                logger.info("plug-in %s is enabled but not installed: skipped", name)
            else:  # the package exists but one of its own imports is missing
                logger.warning("plug-in %s could not be loaded (%s)", name, type(error).__name__)
            continue
        except Exception as error:  # noqa: BLE001  (a broken plug-in must not stop the server)
            logger.warning("plug-in %s could not be loaded (%s)", name, type(error).__name__)
            continue
        register = getattr(module, "register", None)
        if not callable(register):
            logger.info("plug-in %s has no register(): skipped", name)
            continue
        before = (len(registry.after_index), len(registry.mounts))
        try:
            register(registry, context)
        except Exception as error:  # noqa: BLE001
            # Undo a half-finished registration so a broken plug-in leaves nothing behind.
            del registry.after_index[before[0] :]
            del registry.mounts[before[1] :]
            logger.warning("plug-in %s failed to register (%s)", name, type(error).__name__)
            continue
        registry.loaded.append(name)
        logger.info("plug-in %s registered", name)
    return list(registry.loaded)
