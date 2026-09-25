"""Tests for plugin message injection across CLI and gateway hosts."""

from queue import SimpleQueue
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import hermes_yaml as yaml
import pytest

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _context(name: str = "notify-plugin") -> tuple[PluginContext, PluginManager]:
    manager = PluginManager()
    manifest = PluginManifest(name=name, key=name, source="user")
    return PluginContext(manifest, manager), manager


def _write_plugin_config(tmp_path, monkeypatch, entry: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"entries": {"notify-plugin": entry}}})
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))


async def _async_guard():
    return True


@pytest.mark.parametrize("binding,guard", [(None, lambda: True), ("session", True), ("session", _async_guard)])
def test_guarded_injection_requires_exact_binding_and_sync_guard(tmp_path, monkeypatch, binding, guard):
    _write_plugin_config(tmp_path, monkeypatch, {"allow_gateway_injection": True})
    context, manager = _context()
    injector = MagicMock(return_value=True)
    manager.set_gateway_message_injector(object(), injector)
    assert context.inject_message("wake", session_key="agent:main:telegram:dm:42",
                                  expected_session_id=binding, dispatch_guard=guard) is False
    injector.assert_not_called()


def test_bound_gateway_injection_never_uses_unbound_tui_host(tmp_path, monkeypatch):
    _write_plugin_config(tmp_path, monkeypatch, {"allow_gateway_injection": True})
    context, manager = _context()
    tui = MagicMock(return_value=True)
    gateway = MagicMock(return_value=True)
    manager.set_tui_message_injector(object(), tui)
    manager.set_gateway_message_injector(object(), gateway)
    guard = lambda: True
    assert context.inject_message("wake", session_key="session-key",
                                  expected_session_id="original-session", dispatch_guard=guard) is True
    tui.assert_not_called()
    assert gateway.call_args.kwargs["expected_session_id"] == "original-session"
    assert gateway.call_args.kwargs["dispatch_guard"]() is True


def test_guard_returning_coroutine_is_rejected_without_leaking_it(tmp_path, monkeypatch, recwarn):
    _write_plugin_config(tmp_path, monkeypatch, {"allow_gateway_injection": True})
    context, manager = _context()
    injector = MagicMock(return_value=True)
    manager.set_gateway_message_injector(object(), injector)
    assert context.inject_message("wake", session_key="agent:main:telegram:dm:42",
        expected_session_id="session", dispatch_guard=lambda: _async_guard()) is True
    guard = injector.call_args.kwargs["dispatch_guard"]
    assert guard() is False
    assert not [w for w in recwarn.list if "never awaited" in str(w.message)]


@pytest.mark.parametrize("ambient,owned", [(True, False), (False, True)])
def test_gateway_permission_belongs_to_manager_home(tmp_path, monkeypatch, ambient, owned):
    _write_plugin_config(tmp_path, monkeypatch, {"allow_gateway_injection": ambient})
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    (owner_home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"entries": {"notify-plugin": {"allow_gateway_injection": owned}}}
    }))
    manager = PluginManager(scope_key=str(owner_home))
    context = PluginContext(PluginManifest(name="notify-plugin", key="notify-plugin", source="user"), manager)
    injector = MagicMock(return_value=True)
    manager.set_gateway_message_injector(object(), injector)
    assert context.inject_message("wake", session_key="agent:main:telegram:dm:42") is owned
    assert injector.call_count == int(owned)


def test_cli_idle_injection_keeps_existing_queue_behaviour():
    context, manager = _context()
    cli = SimpleNamespace(
        _agent_running=False,
        _pending_input=SimpleQueue(),
        _interrupt_queue=SimpleQueue(),
    )
    manager._cli_ref = cli

    assert context.inject_message("new input") is True
    assert cli._pending_input.get_nowait() == "new input"
    assert cli._interrupt_queue.empty()


def test_cli_running_injection_keeps_existing_interrupt_behaviour():
    context, manager = _context()
    cli = SimpleNamespace(
        _agent_running=True,
        _pending_input=SimpleQueue(),
        _interrupt_queue=SimpleQueue(),
    )
    manager._cli_ref = cli

    assert context.inject_message("status", "system") is True
    assert cli._interrupt_queue.get_nowait() == "[system] status"
    assert cli._pending_input.empty()


def test_gateway_injection_requires_session_key(tmp_path, monkeypatch):
    _write_plugin_config(
        tmp_path,
        monkeypatch,
        {"allow_gateway_injection": True},
    )
    context, manager = _context()
    injector = MagicMock(return_value=True)
    manager.set_gateway_message_injector(object(), injector)

    assert context.inject_message("wake up") is False
    injector.assert_not_called()


def test_gateway_injection_requires_explicit_permission(tmp_path, monkeypatch):
    _write_plugin_config(tmp_path, monkeypatch, {})
    context, manager = _context()
    injector = MagicMock(return_value=True)
    manager.set_gateway_message_injector(object(), injector)

    assert (
        context.inject_message(
            "wake up",
            session_key="agent:main:telegram:dm:42",
        )
        is False
    )
    injector.assert_not_called()


def test_gateway_injection_does_not_treat_string_as_permission(tmp_path, monkeypatch):
    _write_plugin_config(
        tmp_path,
        monkeypatch,
        {"allow_gateway_injection": "false"},
    )
    context, manager = _context()
    injector = MagicMock(return_value=True)
    manager.set_gateway_message_injector(object(), injector)

    assert (
        context.inject_message(
            "wake up",
            session_key="agent:main:telegram:dm:42",
        )
        is False
    )
    injector.assert_not_called()


def test_gateway_injection_fails_closed_when_config_cannot_be_read():
    context, manager = _context()
    injector = MagicMock(return_value=True)
    manager.set_gateway_message_injector(object(), injector)

    with patch(
        "hermes_cli.plugins.load_config_readonly",
        side_effect=OSError("config unavailable"),
    ):
        assert (
            context.inject_message(
                "wake up",
                session_key="agent:main:telegram:dm:42",
            )
            is False
        )

    injector.assert_not_called()


def test_gateway_injection_requires_live_host(tmp_path, monkeypatch):
    _write_plugin_config(
        tmp_path,
        monkeypatch,
        {"allow_gateway_injection": True},
    )
    context, manager = _context()

    assert manager.has_gateway_message_injector is False
    assert (
        context.inject_message(
            "wake up",
            session_key="agent:main:telegram:dm:42",
        )
        is False
    )


def test_gateway_injection_passes_host_owned_plugin_identity(tmp_path, monkeypatch):
    _write_plugin_config(
        tmp_path,
        monkeypatch,
        {"allow_gateway_injection": True},
    )
    context, manager = _context()
    injector = MagicMock(return_value=True)
    manager.set_gateway_message_injector(object(), injector)

    result = context.inject_message(
        "wake up",
        role="system",
        session_key="agent:main:telegram:dm:42",
    )

    assert result is True
    injector.assert_called_once_with(
        session_key="agent:main:telegram:dm:42",
        content="[system] wake up",
        plugin_id="notify-plugin",
    )


def test_gateway_injection_fails_closed_on_host_exception(tmp_path, monkeypatch):
    _write_plugin_config(
        tmp_path,
        monkeypatch,
        {"allow_gateway_injection": True},
    )
    context, manager = _context()
    injector = MagicMock(side_effect=RuntimeError("gateway unavailable"))
    manager.set_gateway_message_injector(object(), injector)

    assert (
        context.inject_message(
            "wake up",
            session_key="agent:main:telegram:dm:42",
        )
        is False
    )
