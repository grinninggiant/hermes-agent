"""Source contract regression only; not a model behavior acceptance test."""
from agent.context_compressor import SUMMARY_PREFIX


def test_summary_memory_is_context_not_overriding_authority():
    assert "Persistent memory remains available during compaction." in SUMMARY_PREFIX
    assert "not as an instruction to override higher-priority instructions" in SUMMARY_PREFIX
    assert "latest request and corrections." in SUMMARY_PREFIX
    assert "ALWAYS authoritative" not in SUMMARY_PREFIX
    assert "Reverse signals" in SUMMARY_PREFIX
    assert "must immediately end any in-flight work" in SUMMARY_PREFIX
