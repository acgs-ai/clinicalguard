"""ClinicalGuard: Constitutional AI Governance for Healthcare Agents.

An A2A agent that validates proposed clinical actions against a 20-rule
Healthcare AI Constitution using LLM reasoning + MACI enforcement.

Constitutional Hash: derived from bundled healthcare_v1.yaml
"""

from __future__ import annotations

from pathlib import Path

from acgs_lite.constitution import Constitution

__version__ = "1.0.1"


def _load_constitution_hash() -> str:
    constitution_path = Path(__file__).with_name("constitution") / "healthcare_v1.yaml"
    return Constitution.from_yaml(str(constitution_path)).hash


CONSTITUTIONAL_HASH = _load_constitution_hash()

__all__ = ["CONSTITUTIONAL_HASH", "__version__"]
