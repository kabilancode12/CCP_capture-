"""Tests for the plug-in registry and the loading of plug-ins by name."""

import logging
import sys
import types
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from ccp.server.plugins import (
    PLUGINS,
    PluginContext,
    PluginRegistry,
    is_enabled,
    load_plugins,
)
from ccp.storage import ObjectStore

from .conftest import FakeStore, make_settings


def context(env: dict[str, str]) -> PluginContext:
    return PluginContext(make_settings(), cast(ObjectStore, FakeStore()), env)


def sub_app() -> Starlette:
    async def hello(request: Any) -> PlainTextResponse:
        return PlainTextResponse("hello")

    return Starlette(routes=[Route("/", hello)])


@pytest.fixture
def fake_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., types.ModuleType]]:
    """Make ``ccp.<name>`` importable as a tiny fake package."""

    def make(name: str, register: Any = None) -> types.ModuleType:
        module = types.ModuleType(f"ccp.{name}")
        if register is not None:
            module.register = register  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, f"ccp.{name}", module)
        return module

    yield make


def test_the_three_known_plugins_and_their_switches() -> None:
    assert PLUGINS == {
        "catalog": "CCP_CATALOG_ENABLED",
        "decisions": "CCP_DECISIONS_ENABLED",
        "explorer": "CCP_EXPLORER_ENABLED",
    }


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_true_values_enable(value: str) -> None:
    assert is_enabled({"X": value}, "X")


@pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe"])
def test_other_values_do_not_enable(value: str) -> None:
    assert not is_enabled({"X": value}, "X")
    assert not is_enabled({}, "X")


def test_an_enabled_plugin_registers_its_callbacks_and_mounts(
    fake_module: Callable[..., types.ModuleType],
) -> None:
    seen: list[str] = []

    def register(registry: PluginRegistry, ctx: PluginContext) -> None:
        registry.add_after_index(seen.append)
        registry.add_mount("/explorer", sub_app())

    fake_module("explorer", register)
    registry = PluginRegistry()
    loaded = load_plugins(registry, context({"CCP_EXPLORER_ENABLED": "true"}))
    assert loaded == ["explorer"] == registry.loaded
    assert [path for path, _ in registry.mounts] == ["/explorer"]
    registry.after_index[0]("s1")
    assert seen == ["s1"]


def test_a_plugin_gets_the_context(fake_module: Callable[..., types.ModuleType]) -> None:
    got: list[PluginContext] = []
    fake_module("catalog", lambda registry, ctx: got.append(ctx))
    ctx = context({"CCP_CATALOG_ENABLED": "1", "CCP_EXPLORER_FLAG": "s"})
    load_plugins(PluginRegistry(), ctx)
    assert got == [ctx] and got[0].env["CCP_EXPLORER_FLAG"] == "s"


def test_a_plugin_that_is_switched_off_is_not_even_imported(
    fake_module: Callable[..., types.ModuleType],
) -> None:
    def register(registry: PluginRegistry, ctx: PluginContext) -> None:
        raise AssertionError("must not be called")

    fake_module("decisions", register)
    assert load_plugins(PluginRegistry(), context({})) == []
    assert load_plugins(PluginRegistry(), context({"CCP_DECISIONS_ENABLED": "false"})) == []


def test_a_missing_package_is_skipped_quietly(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    registry = PluginRegistry()
    plugins = {"no_such_plugin_xyz": "SWITCH"}
    assert load_plugins(registry, context({"SWITCH": "true"}), plugins) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_package_without_register_is_skipped(
    fake_module: Callable[..., types.ModuleType],
) -> None:
    fake_module("catalog")
    assert load_plugins(PluginRegistry(), context({"CCP_CATALOG_ENABLED": "true"})) == []


def test_a_broken_plugin_is_skipped_with_a_warning_and_leaves_nothing_behind(
    fake_module: Callable[..., types.ModuleType], caplog: pytest.LogCaptureFixture
) -> None:
    def broken(registry: PluginRegistry, ctx: PluginContext) -> None:
        registry.add_after_index(lambda s: None)
        registry.add_mount("/half", sub_app())
        raise RuntimeError("secret detail")

    seen: list[str] = []
    fake_module("catalog", broken)
    fake_module("explorer", lambda registry, ctx: registry.add_after_index(seen.append))
    registry = PluginRegistry()
    env = {"CCP_CATALOG_ENABLED": "true", "CCP_EXPLORER_ENABLED": "true"}
    assert load_plugins(registry, context(env)) == ["explorer"]
    assert registry.mounts == [] and len(registry.after_index) == 1
    assert "catalog failed to register (RuntimeError)" in caplog.text
    assert "secret detail" not in caplog.text  # only the kind of error is logged


def test_a_plugin_whose_own_import_is_missing_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import importlib

    def fail(name: str) -> types.ModuleType:
        raise ModuleNotFoundError("no module named 'jinja2'", name="jinja2")

    monkeypatch.setattr(importlib, "import_module", fail)
    assert load_plugins(PluginRegistry(), context({"CCP_CATALOG_ENABLED": "true"})) == []
    assert "catalog could not be loaded (ModuleNotFoundError)" in caplog.text


@pytest.mark.parametrize("path", ["", "/", "explorer", "/health", "/v1", "/v1/x"])
def test_bad_mount_paths_are_refused(path: str) -> None:
    with pytest.raises(ValueError):
        PluginRegistry().add_mount(path, sub_app())


def test_a_mount_path_cannot_be_used_twice_and_a_trailing_slash_is_ignored() -> None:
    registry = PluginRegistry()
    registry.add_mount("/explorer/", sub_app())
    assert registry.mounts[0][0] == "/explorer"
    with pytest.raises(ValueError, match="already"):
        registry.add_mount("/explorer", sub_app())
