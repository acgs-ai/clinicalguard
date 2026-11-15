"""Product-level MRN behaviour pinned on acgs-lite 4.x.

acgs-lite 3.0.1 only ran a rule regex when its heuristic "anchor" word occurred in
the text. HC-003's MRN pattern got the anchor "number", so ``MRN: A12345`` skipped
HC-003 and was CONDITIONALLY_APPROVED. 4.x always evaluates it. These tests go
through ``validate_clinical_action`` and the A2A route (not the raw engine) with the
LLM layer unavailable, so only the deterministic rule layer decides.

Constitutional Hash: 608508a9bd224290
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from clinicalguard.agent import ClinicalGuardApp
from clinicalguard.skills.validate_clinical import (
    CONDITIONAL,
    REJECTED,
    LLMClinicalAssessment,
    validate_clinical_action,
)

_NO_LLM = LLMClinicalAssessment(
    llm_available=False,
    error="test",
    reasoning="LLM reasoning unavailable — constitutional rules only.",
    recommended_decision=CONDITIONAL,
    risk_tier="MEDIUM",
)
_LLM_PATH = "clinicalguard.skills.validate_clinical.get_llm_assessment"


@pytest.fixture
def guard(tmp_path):
    return ClinicalGuardApp.create(audit_log_path=tmp_path / "audit.json")


async def _validate(guard, text):
    with patch(_LLM_PATH, return_value=_NO_LLM):
        result = await validate_clinical_action(text, engine=guard.engine, audit_log=guard.audit_log)
    return result, {v["rule_id"] for v in result["violations"]}


@pytest.mark.parametrize("text", ["MRN: A12345", "MRN: ABC1234567"])
async def test_letter_prefixed_mrn_rejected_by_hc003(guard, text):
    result, rule_ids = await _validate(guard, text)
    assert result["decision"] == REJECTED
    assert result["risk_tier"] == "CRITICAL"
    assert "HC-003" in rule_ids


@pytest.mark.parametrize("text", ["MRN: 00123456", "mrn: 12345", "MRN:123456"])
async def test_numeric_mrn_rejected(guard, text):
    result, rule_ids = await _validate(guard, text)
    assert result["decision"] == REJECTED
    assert "PHI-MRN" in rule_ids
    assert "HC-003" in rule_ids


@pytest.mark.parametrize("text", ["MRN: 1234", "mrn: 123"])
async def test_four_digit_near_miss_not_rejected_for_mrn(guard, text):
    result, rule_ids = await _validate(guard, text)
    assert not rule_ids & {"PHI-MRN", "HC-003"}
    assert result["decision"] != REJECTED


async def test_letter_prefixed_mrn_rejected_through_a2a_route(guard):
    client = TestClient(guard.build_starlette_app())
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tasks/send",
        "params": {
            "id": "mrn-1",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "validate_clinical_action: MRN: A12345"}],
            },
        },
    }
    with patch(_LLM_PATH, return_value=_NO_LLM):
        resp = client.post("/", json=body)
    assert resp.status_code == 200
    result = resp.json()["result"]["result"]
    assert result["decision"] == REJECTED
    assert "HC-003" in {v["rule_id"] for v in result["violations"]}
