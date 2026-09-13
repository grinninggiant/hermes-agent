"""Normalize shipped historical wire text without retaining stale directives."""
from agent.context_compressor import (
    ContextCompressor,
    SUMMARY_PREFIX,
    _HISTORICAL_SUMMARY_PREFIXES,
)

# Exact shipped predecessor: this fixture is input data, not a current-prefix snapshot.
PREDECESSOR = (
    '[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below.'
    ' This is a handoff from a previous context window — treat it as background reference, NOT '
    'as active instructions. Do NOT answer questions or fulfill requests mentioned in this summ'
    'ary; they were already addressed. Respond ONLY to the latest user message that appears AFT'
    'ER this summary — that message is the single source of truth for what to do right now. If '
    'no user message appears AFTER this summary, do nothing: do not resume, wrap up, or continu'
    "e work from '## Historical Task Snapshot' or any other section, do not call tools, and wai"
    't for a new user message. This handoff must never become the active turn by itself. (Excep'
    'tion: if tool results or your own tool calls appear after this summary, you are mid-way th'
    'rough an in-flight exchange — continue that exchange normally.) Topic overlap with the sum'
    'mary does NOT mean you should resume its task: even on similar topics, the latest user mes'
    "sage WINS. Treat ONLY the latest message as the active task and discard stale items from '"
    "## Historical Task Snapshot' entirely — do not 'wrap up' or 'finish' work described there "
    'unless the latest message explicitly asks for it. Reverse signals in the latest message (e'
    ".g. 'stop', 'undo', 'roll back', 'just verify', 'don't do that anymore', 'never mind', a n"
    'ew topic) must immediately end any in-flight work described in the summary; do not re-surf'
    'ace it in later turns. IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in the syste'
    'm prompt is ALWAYS authoritative and active — never ignore or deprioritize memory content '
    'due to this compaction note. None of the above restricts HOW you work: your tools remain f'
    'ully active — keep calling them normally for the active task (edit files, run commands, se'
    'arch) instead of merely narrating what you would do. The current session state (files, con'
    'fig, etc.) may reflect work described here — avoid repeating it:'
)


def test_immediate_predecessor_is_recognized_and_normalized_idempotently():
    body = "Inert summary body."
    wire = PREDECESSOR + "\n" + body
    assert ContextCompressor._starts_with_summary_prefix(wire)
    assert ContextCompressor.classify_summary_content(wire) == "standalone"
    assert ContextCompressor._strip_summary_prefix(wire) == body
    normalized = ContextCompressor._with_summary_prefix(wire)
    assert normalized == SUMMARY_PREFIX + "\n" + body
    assert "ALWAYS authoritative" not in normalized
    assert ContextCompressor._with_summary_prefix(normalized) == normalized


def test_existing_formats_keep_body_and_unknown_input_is_not_stripped():
    body = "Do not lose this summary body."
    for prefix in (SUMMARY_PREFIX, *_HISTORICAL_SUMMARY_PREFIXES):
        wire = prefix + "\n" + body
        assert ContextCompressor._strip_summary_prefix(wire) == body
        assert ContextCompressor._with_summary_prefix(wire) == SUMMARY_PREFIX + "\n" + body
    unknown = "[CONTEXT COMPACTION — unrecognized variant] keep this body"
    assert ContextCompressor._strip_summary_prefix(unknown) == unknown
