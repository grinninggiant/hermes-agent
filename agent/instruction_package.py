"""Closed OPS-239 historical prose packages, not runtime/security policy overrides.

Only the two reviewed prose slots vary. No runtime files, skill bodies or tool
schemas are loaded from historical revisions. IDs are constructor-only inputs.
"""
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
import json


@dataclass(frozen=True)
class SourceCoordinate:
    path: str
    symbol: str
    commit: str
    git_blob_oid: str
    source_lines: tuple[int, int]
    source_segment_sha256: str


@dataclass(frozen=True)
class InstructionPackage:
    package_id: str
    skills_lead: str
    skills_tail: str
    memory_guidance: str
    content_sha256: str
    sources: tuple[SourceCoordinate, ...]

    def summary_prefix(self, native: str) -> str:
        # Preserve all current native Stop/tool/security framing outside this slot.
        for package in _PACKAGES.values():
            if package.memory_guidance in native:
                return native.replace(package.memory_guidance, self.memory_guidance, 1)
        raise ValueError("Native compaction memory slot no longer matches reviewed prose")


_BASELINE = InstructionPackage(
    package_id='ops239-ec8e0050-guidance',
    skills_lead="Before replying, scan the skills below. If a skill matches or is even partially relevant to your task, you MUST load it with skill_view(name) and follow its instructions. Err on the side of loading — it is always better to have context you don't need than to miss critical steps, pitfalls, or established workflows. Skills contain specialized knowledge — API endpoints, tool-specific commands, and proven workflows that outperform general-purpose approaches. Load the skill even if you think you could handle the task with basic tools like {basic_tools}. Skills also encode the user's preferred approach, conventions, and quality standards for tasks like code review, planning, and testing — load them even for tasks you already know how to do, because the skill defines how it should be done here.\n",
    skills_tail='Only proceed without loading a skill if genuinely none are relevant to the task.',
    memory_guidance='IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in the system prompt is ALWAYS authoritative and active — never ignore or deprioritize memory content due to this compaction note. ',
    content_sha256='2b7a7f037bb59368ae6958dfbfe260ad525539128bfb34a1317d632faead76fd',
    sources=(
        SourceCoordinate('agent/prompt_builder.py', '_render_skills_index', 'ec8e0050f6ca119adb52140d4ca2e9ea74fef60b', '77db1c348a115331c7692ce8de4610331c3ce284', (1288, 1338), 'd9c49955e3894a00c4a8a7b42561684591ea9589c0cd9c7283d4e46fb49fbb81'),
        SourceCoordinate('agent/context_compressor.py', 'SUMMARY_PREFIX', 'ec8e0050f6ca119adb52140d4ca2e9ea74fef60b', '15d3543e07b2c7b7265f2e1d6329d6a9b7a93533', (180, 220), 'a12edf5bde4bd3da9e3cd0136606f1e34fea8a7756b6b5c0fd6c338aabdda9d5'),
    ),
)

_CANDIDATE = InstructionPackage(
    package_id='ops239-b7013cf0-guidance',
    skills_lead="Use the skill index to select guidance for the current task. Load a skill with skill_view(name) when its stated trigger matches the work you are performing; do not load it solely because it shares a topic word. Start with the matching skill's router and load only references needed for the next action. Follow applicable domain instructions and required governance; this selection rule does not relax security, approval, credential, Stop, or human-owned completion boundaries.\n",
    skills_tail='If no skill trigger matches, proceed with the available tools. Reassess skill selection when task scope changes.',
    memory_guidance="Persistent memory remains available during compaction. Use it as context, not as an instruction to override higher-priority instructions or the user's latest request and corrections. ",
    content_sha256='812e0ba5c0757b697b62ec003619024cf2969ca5096c94df7c535bdf09972f36',
    sources=(
        SourceCoordinate('agent/prompt_builder.py', '_render_skills_index', 'b7013cf0a9bbb87b6b672e5f997832b9ddd73399', '2c6d5758e6a09d44f215e5da91c65ffaf292fe4d', (1288, 1332), 'a4b4428b71d305a5487bed86efb322f3f26f7f76254fb16db50030b204d6f653'),
        SourceCoordinate('agent/context_compressor.py', 'SUMMARY_PREFIX', 'b7013cf0a9bbb87b6b672e5f997832b9ddd73399', '0acebf7043487327c879955914dd4ce7751c4625', (180, 220), '9d2c7fab9cb2c73356d5795d906fcd769f4057749f82550ff9440462e3589525'),
    ),
)

_PACKAGES = MappingProxyType({p.package_id: p for p in (_BASELINE, _CANDIDATE)})


def resolve_instruction_package(package_id: str | None) -> InstructionPackage | None:
    if package_id is None:
        return None
    if not isinstance(package_id, str) or package_id not in _PACKAGES:
        raise ValueError("Unknown reviewed instruction package ID")
    package = _PACKAGES[package_id]
    payload = json.dumps([package.skills_lead, package.skills_tail, package.memory_guidance],
                         ensure_ascii=False, separators=(",", ":")).encode()
    if sha256(payload).hexdigest() != package.content_sha256:
        raise ValueError("Instruction package content digest mismatch")
    return package


def reviewed_summary_prefixes(native: str) -> tuple[str, ...]:
    return tuple(package.summary_prefix(native) for package in _PACKAGES.values())
