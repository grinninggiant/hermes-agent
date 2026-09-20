"""Offline native binding contracts; not paired provider/AC5 acceptance."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

BASE = "ops239-ec8e0050-guidance"
CAND = "ops239-b7013cf0-guidance"


def test_closed_immutable_package_and_native_slots(tmp_path, monkeypatch):
    from agent.instruction_package import resolve_instruction_package
    from agent.context_compressor import ContextCompressor, SUMMARY_PREFIX
    from agent.system_prompt import _skills_prompt
    from run_agent import AIAgent

    for bad in ("unknown", "/tmp/prompt", "", {}, BASE + " "):
        with pytest.raises(ValueError):
            resolve_instruction_package(bad)
    with pytest.raises(ValueError):
        AIAgent(instruction_package_id="unknown")
    baseline, candidate = map(resolve_instruction_package, (BASE, CAND))
    with pytest.raises(FrozenInstanceError):
        candidate.memory_guidance = "arbitrary"
    assert baseline.content_sha256 != candidate.content_sha256
    assert len(candidate.sources) == 2
    assert all(s.commit == "b7013cf0a9bbb87b6b672e5f997832b9ddd73399" for s in candidate.sources)

    skill = tmp_path / "skills" / "local" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: local\ndescription: Use when testing local binding.\n---\nReal body remains native.\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    def assemble(package_id):
        agent = SimpleNamespace(valid_tool_names={"skill_view", "terminal"}, platform="cli",
                                _instruction_package=resolve_instruction_package(package_id))
        return _skills_prompt(agent)
    native = assemble(None)
    assert native == _skills_prompt(SimpleNamespace(valid_tool_names={"skill_view", "terminal"}, platform="cli"))
    before, after = assemble(BASE), assemble(CAND)
    assert before != after
    assert before.replace(baseline.skills_lead.format(basic_tools="terminal"), candidate.skills_lead).replace(baseline.skills_tail, candidate.skills_tail) == after
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(assemble, [BASE, CAND] * 8))
    assert values == [before, after] * 8
    from tools.skills_tool import skill_view
    import json
    loaded = json.loads(skill_view("local"))
    assert loaded["success"]
    assert "Real body remains native." in loaded["content"]
    assert _skills_prompt(SimpleNamespace(valid_tool_names=set(), platform="cli",
                                        _instruction_package=candidate)) == ""

    compressors = [ContextCompressor("offline", instruction_package_id=p) for p in (BASE, CAND, None)]
    b, c, n = compressors
    assert n._with_summary_prefix("body") == SUMMARY_PREFIX + "\nbody"
    expected = candidate.summary_prefix(SUMMARY_PREFIX)
    baseline_prefix = baseline.summary_prefix(SUMMARY_PREFIX)
    assert c._with_summary_prefix("body") == expected + "\nbody"
    assert b._with_summary_prefix(c._with_summary_prefix("body")) == baseline_prefix + "\nbody"
    assert c._with_summary_prefix(b._with_summary_prefix("body")) == expected + "\nbody"
    assert c._with_summary_prefix(c._with_summary_prefix("body")) == expected + "\nbody"
    assert c.classify_summary_content(expected + "\nbody") == "standalone"
    assert c._render_micro_marker_content("body").startswith(expected)
    assert b._render_micro_marker_content("body").startswith(baseline_prefix)
    assert assemble(None) == native
    assert ContextCompressor._with_summary_prefix("body") == SUMMARY_PREFIX + "\nbody"


def test_real_agent_constructor_assembly_and_cached_turn_prefix(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    import run_agent
    from agent.instruction_package import resolve_instruction_package
    from agent.system_prompt import build_system_prompt
    from agent.agent_init import init_agent
    from agent.context_compressor import SUMMARY_PREFIX

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("model:\n  context_length: 128000\nmemory:\n  memory_enabled: false\n  user_profile_enabled: false\n")
    skill = tmp_path / "skills" / "local" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: local\ndescription: Use when checking bindings.\n---\nNative skill body.\n")
    # External client boundary only; constructor, schemas, assembly and compressor are real.
    client = MagicMock()
    client.chat.completions.create.side_effect = AssertionError("No provider calls authorized")
    from agent import process_bootstrap
    monkeypatch.setattr(process_bootstrap, "OpenAI", lambda **kwargs: client)
    agents = [run_agent.AIAgent(
        model="gpt-4.1", provider="openai", base_url="https://offline.invalid/v1", api_key="test-only",
        enabled_toolsets=["skills"], skip_memory=True, skip_context_files=True,
        quiet_mode=True, save_trajectories=False, session_id="20260920_120000_test",
        instruction_package_id=p,
    ) for p in (BASE, CAND, None)]
    b, c, n = agents
    prompts = [build_system_prompt(a) for a in agents]
    baseline, candidate = map(resolve_instruction_package, (BASE, CAND))
    assert n._instruction_package is None
    del n._instruction_package
    assert build_system_prompt(n) == prompts[2]
    n._instruction_package = None
    assert prompts[0].replace(baseline.skills_lead.format(basic_tools="terminal"), candidate.skills_lead).replace(baseline.skills_tail, candidate.skills_tail) == prompts[1]
    assert b.tools == c.tools == n.tools
    assert b.context_compressor._instruction_package is baseline
    assert c.context_compressor._instruction_package is candidate
    assert c.context_compressor._with_summary_prefix("body").startswith(candidate.summary_prefix(SUMMARY_PREFIX))
    # The session cache is native-owned; package selection has no mutation API.
    for a, prompt in zip(agents, prompts):
        a._cached_system_prompt = prompt
        with pytest.raises(ValueError, match="already bound"):
            init_agent(a, instruction_package_id=CAND)
        assert a._cached_system_prompt == prompt
        assert build_system_prompt(a) == prompt
    client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize("routed", [False, True], ids=["cache-parity", "routed"])
@pytest.mark.parametrize("package_id", [BASE, CAND, None])
def test_native_fork_preserves_bound_package_through_prompt_rebuild(tmp_path, monkeypatch, routed, package_id):
    from unittest.mock import MagicMock
    from agent import process_bootstrap
    from agent.background_review import build_cache_parity_fork
    from agent.context_compressor import SUMMARY_PREFIX
    from agent.system_prompt import build_system_prompt
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "model:\n  context_length: 128000\nmemory:\n  memory_enabled: false\n  user_profile_enabled: false\n"
    )
    skill = tmp_path / "skills" / "local" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: local\ndescription: Use when checking fork bindings.\n---\nNative body.\n")
    client = MagicMock()
    client.chat.completions.create.side_effect = AssertionError("No provider calls authorized")
    monkeypatch.setattr(process_bootstrap, "OpenAI", lambda **kwargs: client)
    parent = AIAgent(
        model="gpt-4.1", provider="openai", base_url="https://offline.invalid/v1", api_key="test-only",
        enabled_toolsets=["skills"], skip_memory=True, skip_context_files=True,
        quiet_mode=True, save_trajectories=False, instruction_package_id=package_id,
    )
    parent._cached_system_prompt = build_system_prompt(parent)
    parent_prompt = parent._cached_system_prompt
    task_cfg = ({"provider": "custom", "model": "gpt-4.1-mini",
                 "base_url": "https://offline.invalid/v1", "api_key": "test-only"}
                if routed else {})
    fork, _, was_routed = build_cache_parity_fork(parent, task_cfg, max_iterations=2)
    try:
        assert was_routed is routed
        assert fork._instruction_package is parent._instruction_package
        assert fork.context_compressor._instruction_package is parent.context_compressor._instruction_package
        assert fork.context_compressor._with_summary_prefix("body") == parent.context_compressor._with_summary_prefix("body")
        if not routed:
            assert fork._cached_system_prompt == parent_prompt
            assert fork.tools == parent.tools
        # Exercise native reconstruction after the compression cache boundary, not just inherited bytes.
        fork._cached_system_prompt = None
        rebuilt = build_system_prompt(fork)
        if parent._instruction_package is not None:
            package = parent._instruction_package
            assert package.skills_lead.format(basic_tools="terminal") in rebuilt
            assert package.skills_tail in rebuilt
            assert fork.context_compressor._with_summary_prefix("body") == package.summary_prefix(SUMMARY_PREFIX) + "\nbody"
        assert parent._cached_system_prompt == parent_prompt
        client.chat.completions.create.assert_not_called()
    finally:
        fork.release_clients()
        parent.release_clients()
