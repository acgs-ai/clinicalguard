"""Fail-closed regressions: HIPAA verdict, default auth, and PHI-before-LLM ordering.

All HTTP assertions go through the Starlette route (dispatcher level), not the handlers.

Constitutional Hash: 608508a9bd224290
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from acgs_lite import Severity
from acgs_lite.engine.models import Violation
from starlette.testclient import TestClient

from clinicalguard.agent import ClinicalGuardApp
from clinicalguard.skills.hipaa_checker import check_hipaa_compliance
from clinicalguard.skills.validate_clinical import (
    APPROVED,
    REJECTED,
    RISK_LOW,
    LLMClinicalAssessment,
    validate_clinical_action,
)

_LLM_PATH = "clinicalguard.skills.validate_clinical.get_llm_assessment"
_ASSESS_PATH = "acgs_lite.compliance.hipaa_ai.HIPAAAIFramework.assess"


def _a2a(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "id": "req-1",
        "params": {
            "id": "task-1",
            "message": {"role": "user", "parts": [{"type": "text", "text": text}]},
        },
    }


def _client(tmp_path) -> TestClient:
    guard = ClinicalGuardApp.create(audit_log_path=tmp_path / "audit.json")
    return TestClient(guard.build_starlette_app(), raise_server_exceptions=False)


def _items(*statuses: str) -> SimpleNamespace:
    return SimpleNamespace(
        items=[
            {"ref": f"H-{n}", "requirement": "req", "status": status}
            for n, status in enumerate(statuses)
        ]
    )


# ── HIPAA verdict ────────────────────────────────────────────────────────────


class TestHipaaVerdictFailsClosed:
    def test_all_pending_is_not_compliant(self):
        with patch(_ASSESS_PATH, return_value=_items("pending", "pending")):
            result = check_hipaa_compliance("anything")
        assert result["compliant"] is False
        assert result["items_unresolved"] == 2
        assert "PASS" not in result["summary"]

    def test_unrecognised_status_is_unresolved(self):
        with patch(_ASSESS_PATH, return_value=_items("compliant", "some_new_status")):
            result = check_hipaa_compliance("anything")
        assert result["compliant"] is False
        assert result["items_unresolved"] == 1

    def test_missing_status_is_unresolved(self):
        assessment = SimpleNamespace(items=[{"ref": "H-0", "requirement": "req"}])
        with patch(_ASSESS_PATH, return_value=assessment):
            result = check_hipaa_compliance("anything")
        assert result["compliant"] is False
        assert result["items_unresolved"] == 1

    def test_empty_checklist_is_not_compliant(self):
        with patch(_ASSESS_PATH, return_value=_items()):
            result = check_hipaa_compliance("anything")
        assert result["compliant"] is False

    def test_all_resolved_is_compliant(self):
        with patch(_ASSESS_PATH, return_value=_items("compliant", "not_applicable")):
            result = check_hipaa_compliance("anything")
        assert result["compliant"] is True
        assert result["items_unresolved"] == 0
        assert "PASS" in result["summary"]

    def test_failing_item_still_fails(self):
        with patch(_ASSESS_PATH, return_value=_items("compliant", "non_compliant")):
            result = check_hipaa_compliance("anything")
        assert result["compliant"] is False
        assert result["items_failing"] == 1

    def test_uninformative_description_not_compliant_through_route(self, tmp_path):
        resp = _client(tmp_path).post("/", json=_a2a("check_hipaa_compliance: An agent."))
        result = resp.json()["result"]["result"]
        assert result["compliant"] is False


# ── Default authentication ───────────────────────────────────────────────────


class TestAuthFailsClosed:
    @pytest.fixture(autouse=True)
    def _no_auth_env(self, monkeypatch):
        monkeypatch.delenv("CLINICALGUARD_API_KEY", raising=False)
        monkeypatch.delenv("CLINICALGUARD_ALLOW_UNAUTHENTICATED", raising=False)

    def test_no_key_no_opt_out_rejects_tasks(self, tmp_path):
        resp = _client(tmp_path).post("/", json=_a2a("check_hipaa_compliance: An agent."))
        assert resp.status_code == 401

    def test_no_key_keeps_health_and_agent_card_open(self, tmp_path):
        client = _client(tmp_path)
        assert client.get("/health").status_code == 200
        assert client.get("/.well-known/agent.json").status_code == 200

    @pytest.mark.parametrize("value", ["1", "true", "YES", " yes "])
    def test_opt_out_allows_tasks(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv("CLINICALGUARD_ALLOW_UNAUTHENTICATED", value)
        resp = _client(tmp_path).post("/", json=_a2a("check_hipaa_compliance: An agent."))
        assert resp.status_code == 200

    @pytest.mark.parametrize("value", ["0", "false", "", "no", "yes please"])
    def test_opt_out_requires_exact_truthy_value(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv("CLINICALGUARD_ALLOW_UNAUTHENTICATED", value)
        resp = _client(tmp_path).post("/", json=_a2a("check_hipaa_compliance: An agent."))
        assert resp.status_code == 401

    def test_whitespace_key_is_not_a_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLINICALGUARD_API_KEY", "   ")
        resp = _client(tmp_path).post(
            "/",
            json=_a2a("check_hipaa_compliance: An agent."),
            headers={"X-API-Key": "   "},
        )
        assert resp.status_code == 401

    def test_whitespace_key_is_rejected_even_with_opt_out(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLINICALGUARD_API_KEY", "   ")
        monkeypatch.setenv("CLINICALGUARD_ALLOW_UNAUTHENTICATED", "1")
        client = _client(tmp_path)
        body = _a2a("check_hipaa_compliance: An agent.")
        assert client.post("/", json=body).status_code == 401
        assert client.post("/", json=body, headers={"X-API-Key": "   "}).status_code == 401

    def test_key_still_required_when_opt_out_also_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLINICALGUARD_API_KEY", "real-key")
        monkeypatch.setenv("CLINICALGUARD_ALLOW_UNAUTHENTICATED", "1")
        client = _client(tmp_path)
        body = _a2a("check_hipaa_compliance: An agent.")
        assert client.post("/", json=body).status_code == 401
        assert client.post("/", json=body, headers={"X-API-Key": "real-key"}).status_code == 200


# ── PHI check runs before the LLM ────────────────────────────────────────────


_APPROVING_LLM = LLMClinicalAssessment(
    llm_available=True,
    reasoning="Looks fine.",
    recommended_decision=APPROVED,
    risk_tier=RISK_LOW,
)


class TestPhiNeverReachesLlm:
    @pytest.fixture
    def guard(self, tmp_path):
        return ClinicalGuardApp.create(audit_log_path=tmp_path / "audit.json")

    @pytest.mark.parametrize(
        "text",
        [
            "Prescribe metformin 500mg for patient SSN 123-45-6789.",
            "Prescribe metformin 500mg. MRN: A12345.",
        ],
    )
    async def test_phi_text_is_not_sent_to_llm(self, guard, text):
        llm = AsyncMock(return_value=_APPROVING_LLM)
        with patch(_LLM_PATH, llm):
            result = await validate_clinical_action(
                text, engine=guard.engine, audit_log=guard.audit_log
            )
        llm.assert_not_awaited()
        assert result["decision"] == REJECTED
        assert result["llm_available"] is False

    async def test_crashed_validator_withholds_llm(self, guard):
        def boom(text, context):
            raise RuntimeError("validator down")

        guard.engine.add_validator(boom)
        llm = AsyncMock(return_value=_APPROVING_LLM)
        with patch(_LLM_PATH, llm):
            result = await validate_clinical_action(
                "Recommend annual wellness visit.",
                engine=guard.engine,
                audit_log=guard.audit_log,
            )
        llm.assert_not_awaited()
        assert result["llm_available"] is False
        assert result["decision"] == REJECTED

    async def test_warning_level_privacy_match_withholds_llm(self, guard):
        def low_privacy(text, context):
            return [
                Violation(
                    rule_id="TEST-LOW-PRIVACY",
                    rule_text="low severity privacy match",
                    severity=Severity.LOW,
                    matched_content="",
                    category="privacy",
                )
            ]

        guard.engine.add_validator(low_privacy)
        llm = AsyncMock(return_value=_APPROVING_LLM)
        with patch(_LLM_PATH, llm):
            result = await validate_clinical_action(
                "Recommend annual wellness visit.",
                engine=guard.engine,
                audit_log=guard.audit_log,
            )
        llm.assert_not_awaited()
        assert result["llm_available"] is False

    async def test_clean_text_still_reaches_llm_once(self, guard):
        llm = AsyncMock(return_value=_APPROVING_LLM)
        with patch(_LLM_PATH, llm):
            result = await validate_clinical_action(
                "Recommend annual wellness visit.",
                engine=guard.engine,
                audit_log=guard.audit_log,
            )
        llm.assert_awaited_once()
        assert result["llm_available"] is True

    def test_phi_text_is_not_sent_to_llm_through_route(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLINICALGUARD_ALLOW_UNAUTHENTICATED", "1")
        llm = AsyncMock(return_value=_APPROVING_LLM)
        with patch(_LLM_PATH, llm):
            resp = _client(tmp_path).post(
                "/",
                json=_a2a(
                    "validate_clinical_action: Prescribe metformin 500mg for "
                    "patient SSN 123-45-6789."
                ),
            )
        llm.assert_not_awaited()
        assert resp.json()["result"]["result"]["decision"] == REJECTED
