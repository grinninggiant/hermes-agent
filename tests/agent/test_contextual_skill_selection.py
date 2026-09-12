"""Contextual skill selection preserves indexed domain guidance and boundaries."""
from agent.prompt_builder import _render_skills_index


def test_contextual_selection_preserves_index_and_required_boundaries():
    skills = {'software': [('alpha', 'Alpha trigger'), ('alpha', 'duplicate'), ('beta', 'Beta trigger')]}
    for tools in (None, {'terminal'}, {'web_search'}):
        output = _render_skills_index(skills, {}, None, tools)
        assert 'stated trigger matches' in output
        assert 'even partially relevant' not in output
        assert 'security, approval, credential, Stop, or human-owned completion boundaries' in output
        assert '<available_skills>\n  software:\n    - alpha: Alpha trigger\n    - beta: Beta trigger\n</available_skills>' in output
        assert 'load only references needed' in output
        assert 'skill_manage' in output
        compact = _render_skills_index(skills, {}, frozenset({'software'}), tools)
        assert 'software [names only]: alpha, beta' in compact
    assert _render_skills_index({}, {}, None, None) == ''
