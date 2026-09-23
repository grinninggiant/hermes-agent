"""The concurrent registry dispatcher preserves the agent's hook execution context."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.agent_runtime_helpers import invoke_tool


def test_registry_post_tool_call_has_execution_context():
    agent = SimpleNamespace(
        session_id="shared-session", _current_turn_id="review-turn",
        _current_api_request_id="request-1", _memory_write_context="background_review",
        valid_tool_names=["read_file"], enabled_toolsets=None, disabled_toolsets=None,
        _memory_manager=None,
    )
    hooks = []
    with (
        patch("hermes_cli.plugins._dispatch_pre_tool_call_hooks", return_value=(None, None)),
        patch("hermes_cli.lifecycle.has_hook", side_effect=lambda name: name == "post_tool_call"),
        patch("hermes_cli.lifecycle.invoke_hook", side_effect=lambda name, **kw: hooks.append((name, kw)) or []),
        patch("model_tools.registry.dispatch", return_value='{"ok": true}') as dispatch,
    ):
        for context in ("background_review", "foreground"):
            agent._memory_write_context = context
            assert invoke_tool(
                agent, "read_file", {"path": "/not-read"}, "task", "call",
                skip_tool_request_middleware=True, skip_tool_execution_middleware=True,
            ) == '{"ok": true}'

    assert dispatch.call_count == 2
    assert [kw["execution_context"] for name, kw in hooks if name == "post_tool_call"] == [
        "background_review", "foreground",
    ]
    assert [kw["tool_call_id"] for name, kw in hooks if name == "post_tool_call"] == ["call", "call"]


def test_connector_batch_entry_keeps_execution_context(monkeypatch):
    from tools.registry import invalidate_check_fn_cache
    from tools.tool_gateway import bridge, config

    monkeypatch.setattr(config, "connectors_available", lambda: True)
    monkeypatch.setattr(bridge, "connectors_available", lambda: True)
    invalidate_check_fn_cache()
    class Client:
        def execute(self, planned):
            return [{"data": "remote-ok", "error": None} for _ in planned]

    monkeypatch.setattr(bridge, "_default_client_factory", Client)
    agent = SimpleNamespace(
        session_id="review-session", _current_turn_id="review-turn",
        _current_api_request_id="request-1", _memory_write_context="background_review",
        valid_tool_names=["tool_call"], enabled_toolsets=["connections"],
        disabled_toolsets=None, _memory_manager=None,
    )
    hooks = []
    with (
        patch("hermes_cli.plugins._dispatch_pre_tool_call_hooks", return_value=(None, None)),
        patch("hermes_cli.lifecycle.has_hook", side_effect=lambda name: name == "post_tool_call"),
        patch("hermes_cli.lifecycle.invoke_hook", side_effect=lambda name, **kw: hooks.append((name, kw)) or []),
    ):
        result = invoke_tool(
            agent, "tool_call", {"calls": [{"name": "connectors__gmail__FETCH_EMAILS", "arguments": {}}]},
            "task", "call", skip_tool_request_middleware=True, skip_tool_execution_middleware=True,
        )
    assert "remote-ok" in result
    assert [kw["execution_context"] for name, kw in hooks if name == "post_tool_call"] == [
        "background_review", "background_review",
    ]
    assert [kw["tool_name"] for name, kw in hooks if name == "post_tool_call"] == [
        "connectors__gmail__FETCH_EMAILS", "tool_call",
    ]


def test_background_review_connector_pre_hook_blocks_before_remote_dispatch(monkeypatch):
    from tools.registry import invalidate_check_fn_cache
    from tools.tool_gateway import bridge, config

    monkeypatch.setattr(config, "connectors_available", lambda: True)
    monkeypatch.setattr(bridge, "connectors_available", lambda: True)
    invalidate_check_fn_cache()
    sent = []

    class Client:
        def execute(self, planned):
            sent.extend(planned)
            return [{"data": "should-not-run", "error": None} for _ in planned]

    monkeypatch.setattr(bridge, "_default_client_factory", Client)
    agent = SimpleNamespace(
        session_id="review-session", _current_turn_id="review-turn",
        _current_api_request_id="request-1", _memory_write_context="background_review",
        valid_tool_names=["tool_call"], enabled_toolsets=["connections"],
        disabled_toolsets=None, _memory_manager=None,
    )
    seen = []

    def policy(name, **kwargs):
        if name != "pre_tool_call":
            return []
        seen.append((kwargs["tool_name"], kwargs.get("execution_context")))
        if (kwargs["tool_name"] == "connectors__gmail__FETCH_EMAILS"
                and kwargs.get("execution_context") == "background_review"):
            return [{"action": "block", "message": "review connector denied"}]
        return []

    with patch("hermes_cli.lifecycle.invoke_hook", side_effect=policy):
        result = invoke_tool(
            agent, "tool_call", {"calls": [{"name": "connectors__gmail__FETCH_EMAILS", "arguments": {}}]},
            "task", "call", skip_tool_request_middleware=True, skip_tool_execution_middleware=True,
        )
    assert sent == []
    assert "review connector denied" in result
    assert ("connectors__gmail__FETCH_EMAILS", "background_review") in seen
