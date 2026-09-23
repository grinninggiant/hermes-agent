"""Depth routing reaches the real profile loader, credential resolver and child constructor."""
import json
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tests.tools.test_delegate import _make_mock_parent
from tools import delegate_tool as dt


def _spawn(tmp_path, monkeypatch, delegation, depth=0, credentials_cfg=None):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"delegation": delegation}), encoding="utf-8")
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    parent = _make_mock_parent(depth=depth)
    parent.reasoning_config = {"enabled": True, "effort": "low"}
    parent._fallback_chain = [{"provider": "fallback", "model": "parent"}]
    parent.request_overrides = {"parent": True}
    built = []
    children = []

    def make_child(**kwargs):
        built.append(kwargs)
        child = MagicMock()
        children.append(child)
        return child

    def fake_runtime(**kw):
        return {"model": kw["target_model"], "provider": kw["requested"],
                "base_url": "https://chatgpt.com/backend-api/codex", "api_key": "test-token",
                "api_mode": "codex_responses"}

    def fake_run(*a, **kw):
        return {"task_index": 0, "status": "completed", "summary": "ok", "api_calls": 1,
                "duration_seconds": 0.1, "model": "test", "exit_reason": "completed"}

    token = set_hermes_home_override(tmp_path)
    try:
        with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=fake_runtime), \
             patch("run_agent.AIAgent", side_effect=make_child), \
             patch.object(dt, "_run_single_child", side_effect=fake_run):
            result = json.loads(dt.delegate_task(goal="route", parent_agent=parent, credentials_cfg=credentials_cfg))
    finally:
        reset_hermes_home_override(token)
    return result, built, children


@pytest.mark.parametrize("depth,model,effort,role", [
    (0, "gpt-6-astra", "xhigh", "orchestrator"),
    (1, "gpt-6-sol", "high", "orchestrator"),
    (2, "gpt-6-luna", "max", "leaf"),
])
def test_depth_routes_are_exact_and_depth_derived(tmp_path, monkeypatch, depth, model, effort, role):
    cfg = {"max_spawn_depth": 3, "provider": "legacy", "model": "legacy-model",
           "reasoning_effort": "low", "api_key": "legacy-secret", "base_url": "https://legacy.invalid/v1",
           "request_overrides": {"legacy": True},
           "fallback_providers": [{"provider": "legacy", "model": "fallback"}],
           "depth_routes": {str(n): {"model": m, "provider": "openai-codex", "reasoning_effort": e}
                            for n, m, e in [(1, "gpt-6-astra", "xhigh"), (2, "gpt-6-sol", "high"),
                                            (3, "gpt-6-luna", "max")]}}
    result, built, children = _spawn(tmp_path, monkeypatch, cfg, depth=depth)
    assert result["results"][0]["status"] == "completed"
    child = built[0]
    assert (child["model"], child["provider"], child["reasoning_config"]) == (
        model, "openai-codex", {"enabled": True, "effort": effort})
    assert children[0]._delegate_role == role
    assert children[0]._delegate_depth == depth + 1
    assert child["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert child["api_key"] == "test-token"
    assert child["fallback_model"] is None
    assert child["request_overrides"] == {}


def test_depth_route_without_effort_inherits_parent_not_global(tmp_path, monkeypatch):
    cfg = {"max_spawn_depth": 3, "reasoning_effort": "high",
           "depth_routes": {"1": {"model": "gpt-6-luna", "provider": "openai-codex"}}}
    result, built, _ = _spawn(tmp_path, monkeypatch, cfg)
    assert result["results"][0]["status"] == "completed"
    assert built[0]["reasoning_config"] == {"enabled": True, "effort": "low"}


def test_gap_retains_global_route_and_no_closest_depth_inheritance(tmp_path, monkeypatch):
    cfg = {"max_spawn_depth": 3, "model": "legacy-model", "reasoning_effort": "high",
           "depth_routes": {"1": {"model": "gpt-6-astra", "provider": "openai-codex",
                                  "reasoning_effort": "xhigh"}}}
    result, built, _ = _spawn(tmp_path, monkeypatch, cfg, depth=1)
    assert result["results"][0]["status"] == "completed"
    assert built[0]["model"] == "legacy-model"
    assert built[0]["reasoning_config"] == {"enabled": True, "effort": "high"}


def test_internal_route_even_empty_bypasses_depth_routes(tmp_path, monkeypatch):
    cfg = {"max_spawn_depth": 3, "reasoning_effort": "high",
           "depth_routes": {"1": {"provider": "openai-codex", "model": "gpt-6-astra",
                                  "reasoning_effort": "xhigh"}}}
    for internal, expected in [({}, "high"),  # legacy internal empty route keeps general reasoning default
                               ({"provider": "openai-codex", "model": "review-model",
                                 "reasoning_effort": "medium"}, "medium")]:
        result, built, _ = _spawn(tmp_path, monkeypatch, cfg, credentials_cfg=internal)
        assert result["results"][0]["status"] == "completed"
        assert built[0]["reasoning_config"]["effort"] == expected
        assert built[0]["model"] != "gpt-6-astra"


@pytest.mark.parametrize("depth,model,effort,role", [
    (0, "gpt-6-astra", "xhigh", "orchestrator"),
    (1, "gpt-6-sol", "high", "orchestrator"),
    (2, "gpt-6-luna", "max", "leaf"),
])
def test_native_child_route_reaches_responses_wire(tmp_path, monkeypatch, depth, model, effort, role):
    """Real native AIAgent children retain the depth route and parent ownership."""
    from types import SimpleNamespace

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"delegation": {
        "max_spawn_depth": 3, "depth_routes": {
            1: {"provider": "openai-codex", "model": "gpt-6-astra", "reasoning_effort": "xhigh"},
            2: {"provider": "openai-codex", "model": "gpt-6-sol", "reasoning_effort": "high"},
            3: {"provider": "openai-codex", "model": "gpt-6-luna", "reasoning_effort": "max"}},
    }}), encoding="utf-8")
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda *a, **kw: {})
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", lambda **kw: SimpleNamespace())
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda *a, **kw: [])
    parent = _make_mock_parent(depth=depth)
    parent._session_db = None
    seen = {}

    def capture(task_index, goal, child, **kw):
        try:
            seen["role"] = child._delegate_role
            seen["depth"] = child._delegate_depth
            seen["provider"] = child.provider
            seen["parent"] = child._delegate_parent_ref()
            seen["wire"] = child._get_transport().build_kwargs(
                model=child.model, messages=[{"role": "user", "content": "Hi"}], tools=[],
                provider=child.provider, base_url=child.base_url, is_codex_backend=True,
                reasoning_config=child.reasoning_config,
            )
        finally:
            child.close()
        return {"task_index": 0, "status": "completed", "summary": "ok", "api_calls": 1,
                "duration_seconds": 0.1, "model": model, "exit_reason": "completed"}

    token = set_hermes_home_override(tmp_path)
    try:
        with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value={
            "model": model, "provider": "openai-codex", "api_key": "test-token",
            "base_url": "https://chatgpt.com/backend-api/codex", "api_mode": "codex_responses",
        }), patch.object(dt, "_run_single_child", side_effect=capture):
            result = json.loads(dt.delegate_task(goal="native child", parent_agent=parent))
    finally:
        reset_hermes_home_override(token)
    assert result["results"][0]["status"] == "completed"
    assert seen["role"] == role and seen["depth"] == depth + 1
    assert seen["provider"] == "openai-codex"
    assert seen["parent"] is parent
    assert seen["wire"]["model"] == model
    assert seen["wire"]["reasoning"]["effort"] == effort


@pytest.mark.parametrize("routes", [
    {"0": {"model": "m"}}, {"01": {"model": "m"}}, {"two": {"model": "m"}},
    {"1": {"modle": "m"}}, {"1": "gpt-6-luna"},
    {"1": {"model": ""}}, {"1": {"model": "m", "reasoning_effort": "highest"}},
    {"1": {"model": "m", "reasoning_effort": True}},
    {"2": {"model": "m", "api_key": 3}},
    {"1": {"model": "m", "api_mode": "unknown"}},
    {"1": {"model": "m", "request_overrides": []}},
    {"1": {"model": "m", "fallback_providers": [{"model": "missing-provider"}]}},
    {True: {"model": "m"}}, [],
])
def test_invalid_route_fails_before_spawn(tmp_path, monkeypatch, routes):
    result, built, _ = _spawn(tmp_path, monkeypatch, {"max_spawn_depth": 3, "depth_routes": routes})
    assert "error" in result
    assert "depth_routes" in result["error"]
    assert built == []
