"""Source contract regression only; not a model behavior acceptance test."""
import ast
from pathlib import Path


def test_summary_memory_is_context_not_overriding_authority():
    path = Path(__file__).resolve().parents[2] / "agent/context_compressor.py"
    tree = ast.parse(path.read_text())
    value = next(node.value for node in tree.body
                 if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "SUMMARY_PREFIX"
                         for target in node.targets))
    assert isinstance(value, ast.JoinedStr)
    prefix = "".join(part.value for part in value.values
                     if isinstance(part, ast.Constant) and isinstance(part.value, str))
    assert "Persistent memory remains available during compaction." in prefix
    assert "not as an instruction to override higher-priority instructions" in prefix
    assert "latest request and corrections." in prefix
    assert "ALWAYS authoritative" not in prefix
    assert "Reverse signals" in prefix
    assert "must immediately end any in-flight work" in prefix
