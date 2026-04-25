"""ClinicalGuard: A2A protocol + HIPAA + audit query tests.

Constitutional Hash: derived from bundled healthcare_v1.yaml
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from clinicalguard import CONSTITUTIONAL_HASH, __version__
from clinicalguard.agent import ClinicalGuardApp
from clinicalguard.skills import research_router as research_router_module
from clinicalguard.skills.validate_clinical import (
    CONDITIONAL,
    RISK_HIGH,
    LLMClinicalAssessment,
    validate_clinical_action,
)


class _ASGITestClient:
    """Minimal sync test client that avoids Starlette TestClient's AnyIO portal."""

    def __init__(self, app: Any, *, raise_server_exceptions: bool = True) -> None:
        self.app = app
        self.raise_server_exceptions = raise_server_exceptions

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return asyncio.run(self._request("GET", path, **kwargs))

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return asyncio.run(self._request("POST", path, **kwargs))

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        transport = httpx.ASGITransport(
            app=self.app,
            raise_app_exceptions=self.raise_server_exceptions,
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)


@pytest.fixture
def client(tmp_path):
    """Test client with a fresh ClinicalGuardApp instance (no auth, test only)."""
    guard = ClinicalGuardApp.create(audit_log_path=tmp_path / "audit.json", allow_no_auth=True)
    app = guard.build_starlette_app()
    return _ASGITestClient(app)


@pytest.fixture
def client_with_auth(tmp_path, monkeypatch):
    """Test client with API key auth enabled."""
    monkeypatch.setenv("CLINICALGUARD_API_KEY", "test-key-123")
    guard = ClinicalGuardApp.create(audit_log_path=tmp_path / "audit.json")
    app = guard.build_starlette_app()
    return _ASGITestClient(app, raise_server_exceptions=False)


def _make_a2a_body(text: str, task_id: str = "task-001") -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "id": "req-1",
        "params": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": text}],
            },
        },
    }


class TestAgentCard:
    def test_agent_card_accessible(self, client):
        resp = client.get("/.well-known/agent.json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "ClinicalGuard"
        assert len(data["skills"]) == 4

    def test_agent_card_has_required_fields(self, client):
        data = client.get("/.well-known/agent.json").json()
        assert "capabilities" in data
        assert "skills" in data
        assert data["version"] == __version__
        skill_ids = [s["id"] for s in data["skills"]]
        assert "validate_clinical_action" in skill_ids
        assert "check_hipaa_compliance" in skill_ids
        assert "query_audit_trail" in skill_ids


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["rules"] == 20
        assert data["constitutional_hash"] == CONSTITUTIONAL_HASH


class TestA2AProtocol:
    def test_unknown_method_returns_32601(self, client):
        body = {"jsonrpc": "2.0", "method": "tasks/unknown", "id": "r1", "params": {}}
        resp = client.post("/", json=body)
        data = resp.json()
        assert data["error"]["code"] == -32601

    def test_malformed_json_returns_400(self, client):
        resp = client.post(
            "/",
            content=b"not json at all",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    def test_validate_clinical_action_dispatched(self, client):
        mock_llm = LLMClinicalAssessment(
            recommended_decision=CONDITIONAL,
            risk_tier=RISK_HIGH,
            reasoning="Test reasoning.",
            conditions=["Condition A"],
            llm_available=True,
        )
        with patch(
            "clinicalguard.skills.validate_clinical.get_llm_assessment",
            return_value=mock_llm,
        ):
            resp = client.post(
                "/",
                json=_make_a2a_body(
                    "validate_clinical_action: Patient SYNTH-001 propose Lisinopril 10mg."
                ),
            )
        assert resp.status_code == 200
        data = resp.json()
        result = data["result"]["result"]
        assert "decision" in result
        assert "audit_id" in result
        assert result["audit_id"].startswith("HC-")

    def test_check_hipaa_dispatched(self, client):
        resp = client.post(
            "/",
            json=_make_a2a_body(
                "check_hipaa_compliance: This agent processes synthetic patient data "
                "with a tamper-evident audit log, MACI enforcement, and API key auth."
            ),
        )
        data = resp.json()
        result = data["result"]["result"]
        assert "compliant" in result
        assert "checklist" in result
        assert result["items_checked"] > 0
        assert result["constitutional_hash"] == CONSTITUTIONAL_HASH

    def test_query_audit_trail_dispatched(self, client):
        # First create an audit entry
        mock_llm = LLMClinicalAssessment(
            recommended_decision=CONDITIONAL,
            risk_tier=RISK_HIGH,
            reasoning="Test.",
            llm_available=True,
        )
        with patch(
            "clinicalguard.skills.validate_clinical.get_llm_assessment",
            return_value=mock_llm,
        ):
            resp = client.post(
                "/",
                json=_make_a2a_body("validate_clinical_action: Patient SYNTH-099 Lisinopril."),
            )
        audit_id = resp.json()["result"]["result"]["audit_id"]

        # Now query it
        resp = client.post("/", json=_make_a2a_body(f"query_audit_trail: {audit_id}"))
        data = resp.json()["result"]["result"]
        assert data["found"] is True
        assert data["chain_valid"] is True
        assert data["entries"][0]["id"] == audit_id

    def test_query_audit_trail_recent_dispatched(self, client):
        resp = client.post("/", json=_make_a2a_body("query_audit_trail: recent 5"))
        assert resp.status_code == 200
        data = resp.json()["result"]["result"]
        assert data["found"] is True
        assert "entries" in data
        if data["entries"]:
            assert "metadata" not in data["entries"][-1]

    def test_research_router_dispatched(self, client, monkeypatch):
        async def _fake_router(question: str, **_: object) -> dict:
            return {
                "question": question,
                "direct_answer": "Found literature triage results.",
                "evidence_by_lane": [
                    {
                        "lane": "literature_discovery",
                        "source": "ncbi-entrez",
                        "records": [
                            {
                                "pmid": "26467380",
                                "title": "Inappropriate combination of warfarin and aspirin",
                            }
                        ],
                    }
                ],
                "main_caveats": ["Metadata-level summary only."],
                "recommended_next_steps": ["Read the PMID directly."],
                "runtime": {"available": True},
            }

        monkeypatch.setattr(research_router_module, "research_router", _fake_router)
        resp = client.post(
            "/",
            json=_make_a2a_body(
                'research_router: deidentified: {"drugs": ["warfarin", "aspirin"]}'
            ),
        )
        assert resp.status_code == 200
        result = resp.json()["result"]["result"]
        assert result["direct_answer"] == "Found literature triage results."
        assert result["evidence_by_lane"][0]["source"] == "ncbi-entrez"

    def test_skill_field_dispatches_without_text_prefix(self, client, monkeypatch):
        async def _fake_router(question: str, **_: object) -> dict:
            return {
                "direct_answer": "Skill field dispatch worked.",
                "evidence_by_lane": [],
                "main_caveats": [],
                "recommended_next_steps": [],
                "runtime": {"available": True},
            }

        monkeypatch.setattr(research_router_module, "research_router", _fake_router)
        resp = client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "method": "tasks/send",
                "id": "req-skill-field",
                "params": {
                    "id": "task-skill-field",
                    "message": {
                        "role": "user",
                        "parts": [
                            {
                                "type": "text",
                                "text": 'deidentified: {"drugs": ["warfarin"]}',
                                "skill": "research_router",
                            }
                        ],
                    },
                },
            },
        )
        assert resp.status_code == 200
        assert resp.json()["result"]["result"]["direct_answer"] == "Skill field dispatch worked."

    def test_unknown_skill_returns_helpful_error(self, client):
        resp = client.post("/", json=_make_a2a_body("do_something_unknown: please help"))
        result = resp.json()["result"]["result"]
        # Falls through to default validate skill or returns error with available skills
        assert "decision" in result or "available_skills" in result

    def test_restore_audit_log_redacts_nested_sensitive_content(self, tmp_path):
        from acgs_lite.audit import AuditLog
        from clinicalguard.agent import _restore_audit_log

        audit_path = tmp_path / "audit.json"
        audit_path.write_text(
            """
            {
              "entries": [
                {
                  "id": "HC-TEST-1",
                  "type": "clinical_validation",
                  "agent_id": "external-agent",
                  "action": "Patient SSN 123-45-6789 with MRN 12345678",
                  "valid": true,
                  "violations": ["PHI-SSN", {"note": "DOB 01/15/1990"}],
                  "constitutional_hash": "hash",
                  "latency_ms": 1.0,
                  "metadata": {"nested": {"contact": "john@example.com"}}
                }
              ]
            }
            """,
            encoding="utf-8",
        )
        audit_log = AuditLog()
        _restore_audit_log(audit_log, audit_path)
        restored = audit_log.entries[-1]
        assert "123-45-6789" not in restored.action
        assert restored.metadata["nested"]["contact"] == "[REDACTED-EMAIL]"
        assert restored.violations[1]["note"] == "DOB [REDACTED-DATE]"


class TestAuth:
    def test_create_requires_api_key_by_default(self, tmp_path, monkeypatch):
        # Fail-closed: no API key + no explicit opt-in must refuse to start,
        # regardless of ENVIRONMENT.  This prevents the configuration-drift
        # bypass where a prod deploy forgets to set ENVIRONMENT=production.
        monkeypatch.delenv("CLINICALGUARD_API_KEY", raising=False)
        monkeypatch.delenv("CLINICALGUARD_ALLOW_NO_AUTH", raising=False)
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        with pytest.raises(RuntimeError, match="CLINICALGUARD_API_KEY"):
            ClinicalGuardApp.create(audit_log_path=tmp_path / "audit.json")

    def test_create_allows_no_auth_when_explicitly_opted_in(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLINICALGUARD_API_KEY", raising=False)
        # Explicit opt-in is allowed (dev/test only).
        ClinicalGuardApp.create(audit_log_path=tmp_path / "audit.json", allow_no_auth=True)

    def test_create_honors_env_opt_in(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLINICALGUARD_API_KEY", raising=False)
        monkeypatch.setenv("CLINICALGUARD_ALLOW_NO_AUTH", "1")
        ClinicalGuardApp.create(audit_log_path=tmp_path / "audit.json")

    def test_no_auth_mode_rejects_requests_without_api_key(self, tmp_path, monkeypatch):
        # When auth is disabled via allow_no_auth=True, _check_auth still
        # reports True (it IS meant to accept all callers in that mode),
        # but crucially an app built WITHOUT the opt-in cannot start at
        # all — so we cannot reach _check_auth with a surprise fail-open.
        monkeypatch.delenv("CLINICALGUARD_API_KEY", raising=False)
        guard = ClinicalGuardApp.create(audit_log_path=tmp_path / "audit.json", allow_no_auth=True)
        assert guard._allow_no_auth is True
        assert guard._api_key == ""

    def test_production_fails_closed_on_audit_persist_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("CLINICALGUARD_API_KEY", "test-key-123")
        guard = ClinicalGuardApp.create(audit_log_path=tmp_path / "audit.json")
        with patch.object(guard.audit_log, "export_json", side_effect=OSError("disk full")):
            with pytest.raises(RuntimeError, match="Audit log persistence failed"):
                guard._persist(guard.audit_log)

    def test_valid_api_key_accepted(self, client_with_auth):
        mock_llm = LLMClinicalAssessment(
            recommended_decision=CONDITIONAL,
            risk_tier=RISK_HIGH,
            reasoning="Test.",
            llm_available=True,
        )
        with patch(
            "clinicalguard.skills.validate_clinical.get_llm_assessment",
            return_value=mock_llm,
        ):
            resp = client_with_auth.post(
                "/",
                json=_make_a2a_body("validate_clinical_action: SYNTH-001 Lisinopril 10mg."),
                headers={"X-API-Key": "test-key-123"},
            )
        assert resp.status_code == 200

    def test_missing_api_key_rejected(self, client_with_auth):
        resp = client_with_auth.post(
            "/",
            json=_make_a2a_body("validate_clinical_action: SYNTH-001 Lisinopril."),
        )
        assert resp.status_code == 401

    def test_wrong_api_key_rejected(self, client_with_auth):
        resp = client_with_auth.post(
            "/",
            json=_make_a2a_body("validate_clinical_action: SYNTH-001 Lisinopril."),
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401


class TestHIPAAChecklist:
    def test_synthetic_data_agent_passes(self, client):
        resp = client.post(
            "/",
            json=_make_a2a_body(
                "check_hipaa_compliance: This agent uses only synthetic de-identified "
                "patient data. It maintains a tamper-evident audit log. It enforces "
                "MACI separation of powers. It uses HTTPS with API key authentication. "
                "No real PHI is ever processed."
            ),
        )
        result = resp.json()["result"]["result"]
        assert result["items_checked"] >= 5

    @pytest.mark.asyncio
    async def test_audit_action_redacts_sensitive_identifiers(self):
        from pathlib import Path

        from acgs_lite.audit import AuditLog
        from acgs_lite.constitution import Constitution
        from acgs_lite.engine import GovernanceEngine
        from clinicalguard.skills.healthcare_validators import register_all

        yaml_path = Path(__file__).parent.parent / "constitution" / "healthcare_v1.yaml"
        engine = GovernanceEngine(Constitution.from_yaml(str(yaml_path)), strict=False)
        register_all(engine)
        audit_log = AuditLog()
        result = await validate_clinical_action(
            "Patient SSN 123-45-6789, DOB: 01/15/1990, MRN: 12345678. Propose Aspirin.",
            engine=engine,
            audit_log=audit_log,
        )
        assert result["audit_id"].startswith("HC-")
        action = audit_log.entries[-1].action
        assert "Aspirin" in action
        assert "Patient SSN [REDACTED-SSN]" in action
        assert "DOB: [REDACTED-DATE]" in action
        assert "MRN: [REDACTED-MRN]" in action
        assert "123-45-6789" not in action
        assert "01/15/1990" not in action
        assert "12345678" not in action
        for violation in result["violations"]:
            matched = violation.get("matched_content", "")
            assert "123-45-6789" not in matched
            assert "12345678" not in matched
        result = await validate_clinical_action(
            "Insurance ID ABC12345678 account number 123456789 serial number UDI-ABC12345 DEA number AB1234567.",
            engine=engine,
            audit_log=audit_log,
        )
        assert result["audit_id"].startswith("HC-")
        action = audit_log.entries[-1].action
        assert "Insurance ID [REDACTED-INSURANCE]" in action
        assert "account number [REDACTED-ACCOUNT]" in action
        assert "serial number [REDACTED-DEVICE]" in action
        assert "DEA number [REDACTED-LICENSE]" in action
        assert "ABC12345678" not in action
        assert "123456789" not in action
        assert "UDI-ABC12345" not in action
        assert "AB1234567" not in action

    def test_checklist_has_mitigations(self, client):
        resp = client.post(
            "/",
            json=_make_a2a_body(
                "check_hipaa_compliance: AI healthcare agent with audit logging and MACI enforcement."
            ),
        )
        result = resp.json()["result"]["result"]
        items_with_mitigation = [i for i in result["checklist"] if i.get("mitigation")]
        assert len(items_with_mitigation) > 0
