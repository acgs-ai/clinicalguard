"""ClinicalGuard: check_hipaa_compliance skill.

Wraps acgs_lite.compliance.hipaa_ai.HIPAACompliance and maps MACI roles
to each checklist item's mitigation.

Constitutional Hash: 608508a9bd224290
"""

from __future__ import annotations

from typing import Any

CONSTITUTIONAL_HASH = "8494a847758c08dc"

_PASSING = frozenset({"compliant", "na", "not_applicable"})
_FAILING = frozenset({"non_compliant", "fail", "unknown"})
_WARNING = frozenset({"warning"})


def check_hipaa_compliance(
    agent_description: str,
    *,
    constitutional_hash: str = CONSTITUTIONAL_HASH,
) -> dict[str, Any]:
    """Run HIPAA compliance checklist against an agent system description.

    Args:
        agent_description: Plain-text description of the AI agent's behaviour,
                           data handling, and system architecture.
        constitutional_hash: Active constitution hash to include in the report.

    Returns dict with:
        compliant:        bool (True only when the checklist is non-empty, nothing fails
                          and nothing is left unresolved)
        items_checked:    int
        items_passing:    int
        items_failing:    int
        items_unresolved: int — pending, missing or unrecognised status
        checklist:        list[dict]  — each item with id, status, notes
        summary:          str
        constitutional_hash: str
    """
    try:
        from acgs_lite.compliance.hipaa_ai import HIPAAAIFramework as HIPAACompliance
    except ImportError as exc:
        return {
            "compliant": False,
            "items_checked": 0,
            "items_passing": 0,
            "items_failing": 0,
            "items_warning": 0,
            "items_unresolved": 0,
            "checklist": [],
            "summary": f"HIPAA compliance module unavailable ({type(exc).__name__}) "
            "— check server configuration",
            "constitutional_hash": constitutional_hash,
        }

    hipaa = HIPAACompliance()

    # Build system_description dict that hipaa.assess() expects
    system_desc: dict[str, Any] = {
        "description": agent_description,
        "agent_description": agent_description,
        "has_audit_log": _mentions_any(agent_description, ["audit", "log", "trail", "record"]),
        "has_maci": _mentions_any(
            agent_description, ["maci", "separation of powers", "proposer", "validator"]
        ),
        "has_encryption": _mentions_any(
            agent_description, ["encrypt", "tls", "ssl", "https", "at rest"]
        ),
        "has_access_control": _mentions_any(
            agent_description, ["auth", "api key", "bearer", "rbac", "role"]
        ),
        "uses_phi": _mentions_any(
            agent_description,
            ["phi", "protected health", "patient name", "date of birth", "ssn"],
        ),
        "synthetic_data_only": _mentions_any(
            agent_description, ["synthetic", "de-identified", "deidentified", "mock"]
        ),
    }

    assessment = hipaa.assess(system_desc)

    # items are dicts with keys: ref, requirement, status, evidence, acgs_lite_feature,
    #                              blocking, legal_citation, updated_at
    items = assessment.items  # list[dict]

    passing = [i for i in items if i.get("status") in _PASSING]
    failing = [i for i in items if i.get("status") in _FAILING]
    warnings = [i for i in items if i.get("status") in _WARNING]
    # Fail closed: "pending", a missing status, or any status this code does not
    # know is unresolved, and an unresolved item is never evidence of compliance.
    unresolved = [i for i in items if i.get("status") not in _PASSING | _FAILING | _WARNING]

    checklist = []
    for item in items:
        checklist.append(
            {
                "id": item.get("ref", ""),
                "description": (item.get("requirement") or "")[:120],
                "status": item.get("status", "unknown"),
                "mitigation": item.get("acgs_lite_feature") or item.get("evidence") or "",
                "reference": item.get("legal_citation") or "",
            }
        )

    compliant = bool(items) and not failing and not unresolved
    summary_parts = [
        f"{len(passing)}/{len(items)} items passing.",
    ]
    if failing:
        summary_parts.append(
            f"{len(failing)} failing: " + ", ".join(i.get("ref", "?") for i in failing[:5])
        )
    if warnings:
        summary_parts.append(f"{len(warnings)} warnings.")
    if unresolved:
        summary_parts.append(f"{len(unresolved)} unresolved (pending or partial).")
    if compliant:
        summary_parts.append("HIPAA compliance assessment: PASS.")
    elif failing:
        summary_parts.append("HIPAA compliance assessment: FAIL — address failing items.")
    else:
        summary_parts.append(
            "HIPAA compliance assessment: INCOMPLETE — unresolved items are not evidence "
            "of compliance."
        )

    return {
        "compliant": compliant,
        "items_checked": len(assessment.items),
        "items_passing": len(passing),
        "items_failing": len(failing),
        "items_warning": len(warnings),
        "items_unresolved": len(unresolved),
        "checklist": checklist,
        "summary": " ".join(summary_parts),
        "constitutional_hash": constitutional_hash,
    }


def _mentions_any(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in keywords)
