# clinicalguard

[![PyPI](https://img.shields.io/pypi/v/clinicalguard)](https://pypi.org/project/clinicalguard/)
[![Python](https://img.shields.io/pypi/pyversions/clinicalguard)](https://pypi.org/project/clinicalguard/)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

**Constitutional AI governance for clinical decision support — an A2A agent that validates proposed clinical actions against a 20-rule Healthcare AI Constitution.**

ClinicalGuard is a Starlette-based A2A (Agent-to-Agent) JSON-RPC service. It exposes four skills: clinical action validation (LLM reasoning + constitutional enforcement), HIPAA compliance checking, tamper-evident audit log queries, and governed life-science research routing. Every decision is cryptographically logged in a hash-chained audit trail.

## Installation

```bash
pip install clinicalguard
```

> ClinicalGuard is a **service**, not a library. Install then run with `uvicorn` (see below). LLM-backed clinical reasoning also requires an LLM provider:
>
> ```bash
> pip install "clinicalguard[anthropic]"   # or [openai]
> ```

External clinical LLM use is **disabled by default**. Even when those extras are installed, `validate_clinical_action` stays rule-first unless `CLINICALGUARD_ENABLE_EXTERNAL_CLINICAL_LLM=true` is explicitly set.

Requires Python 3.11+.

## Running the Service

```bash
uvicorn clinicalguard.main:app --host 0.0.0.0 --port 8080
```

Or with the module entry point:

```bash
python -m clinicalguard.main
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CLINICALGUARD_API_KEY` | _(unset)_ | When set, all requests require `X-API-Key: <key>` header |
| `ENVIRONMENT` | _(unset)_ | Set to `production` to require `CLINICALGUARD_API_KEY` at startup |
| `CLINICALGUARD_AUDIT_LOG` | `/tmp/clinicalguard_audit.json` | Path for persisting the audit log |
| `CLINICALGUARD_URL` | `http://localhost:8080` | Public URL reported in the agent card |
| `CLINICALGUARD_ENABLE_EXTERNAL_CLINICAL_LLM` | `false` | Enables Anthropic or `pi`-based clinical reasoning instead of rule-only fallback |
| `CLINICALGUARD_LIFE_SCIENCE_RUNTIME_ROOT` | _(auto-discover)_ | Optional root path for the installed Life Science plugin runtime |
| `CLINICALGUARD_REQUIRE_LIFE_SCIENCE_RUNTIME` | `false` | When `true`, startup fails if runtime scripts cannot be resolved |
| `CLINICALGUARD_LOCAL_MODEL_ENABLED` | `true` | Enables local-model-first planning and summarization for `research_router` |
| `CLINICALGUARD_LOCAL_MODEL_API_BASE` | _(unset)_ | OpenAI-compatible **loopback-only** local inference endpoint for `research_router` |
| `CLINICALGUARD_LOCAL_MODEL_NAME` | `clinicalguard-qwen35-4b` | Local model id served by the inference backend |
| `CLINICALGUARD_LOCAL_MODEL_TIMEOUT_SEC` | `30` | Timeout for local-model requests |
| `CLINICALGUARD_REQUIRE_LOCAL_MODEL` | `false` | When `true`, startup fails if the local model is not configured |
| `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` | _(unset)_ | LLM credentials for clinical reasoning skill |

## Calling the Agent

ClinicalGuard speaks the A2A JSON-RPC protocol. All requests are `POST /` with `Content-Type: application/json`. The only supported method is `tasks/send`.

### Validate a clinical action

```bash
curl -s http://localhost:8080/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "tasks/send",
    "params": {
      "id": "task-001",
      "message": {
        "role": "user",
        "parts": [{"type": "text",
                   "text": "validate_clinical_action: Patient on Warfarin. Propose Aspirin 325mg daily."}]
      }
    }
  }'
```

Response fields: `decision` (APPROVED / CONDITIONALLY_APPROVED / REJECTED), `risk_tier` (LOW / MEDIUM / HIGH / CRITICAL), `reasoning`, `drug_interactions`, `conditions`, `audit_id`.

The warfarin-plus-aspirin example is intentionally conservative: it is a demo scenario chosen because published literature discusses the safety tradeoffs and appropriateness concerns of combining anticoagulation with aspirin. Representative PubMed records include PMID `26467380` ("Inappropriate combination of warfarin and aspirin") and PMID `39197978` ("Anticoagulation Alone vs Anticoagulation Plus Aspirin or DAPT Following Left Atrial Appendage Occlusion"). ClinicalGuard remains a governance/demo service, not a substitute for clinician review, product labeling, or local guideline checks.

### Check HIPAA compliance

```bash
curl -s http://localhost:8080/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "2",
    "method": "tasks/send",
    "params": {
      "id": "task-002",
      "message": {
        "role": "user",
        "parts": [{"type": "text",
                   "text": "check_hipaa_compliance: Agent processes synthetic patient records, maintains MACI audit log, uses TLS in transit."}]
      }
    }
  }'
```

Response fields: `compliant` (bool), `items_checked`, `items_passing`, `items_failing`, `checklist` (list with status + MACI-mapped mitigations), `constitutional_hash`.

### Query audit trail

```bash
curl -s http://localhost:8080/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "3",
    "method": "tasks/send",
    "params": {
      "id": "task-003",
      "message": {
        "role": "user",
        "parts": [{"type": "text", "text": "query_audit_trail: last 5"}]
      }
    }
  }'
```

Skill name can also be provided in the `skill` field of the first message part instead of as a text prefix.

### Route a broad life-science research question

```bash
curl -s http://localhost:8080/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "4",
    "method": "tasks/send",
    "params": {
      "id": "task-004",
      "message": {
        "role": "user",
        "parts": [{"type": "text",
                   "text": "research_router: deidentified: {\"drugs\": [\"warfarin\", \"aspirin\"]}"}]
      }
    }
  }'
```

This skill uses deterministic retrieval plus a local-model-first planning and summary path. In the current implementation it:

- validates runtime availability at startup
- prefers a local OpenAI-compatible model for lane planning and abstract summaries
- requires the request to be explicitly marked `deidentified:` or `de-identified:` and supplied as a structured JSON entity payload before any routing or retrieval
- routes broad literature questions to PubMed runtime lookups
- fetches PubMed metadata plus abstract text snippets for first-pass summaries
- uses multi-lane routing for mixed gene/disease/drug questions
- routes proteomics-oriented queries to ProteomeXchange dataset lookups
- audit-logs both the router query and each downstream runtime call

OpenAI is intentionally **not** part of the active runtime path right now. If introduced later, it should be advisory/escalation-only under BAA and zero-retention constraints, not the final governance authority.

For privacy, `research_router` is designed for **de-identified** research prompts only. Requests are blocked unless they are explicitly marked `deidentified:` or `de-identified:` and provide only structured entity lists such as `genes`, `drugs`, `diseases`, or `accessions`. PHI-like identifier matches still trigger a fail-closed block before any local-model planning or external literature retrieval.

## Key Features

- **`validate_clinical_action`** — two-layer architecture: LLM clinical reasoning (evidence tier, drug interactions, step therapy) + `GovernanceEngine` constitutional enforcement (MACI, keyword/pattern rules, audit)
- **`check_hipaa_compliance`** — runs `acgs_lite.compliance.hipaa_ai.HIPAAAIFramework` against an agent description; maps each checklist item to its MACI role mitigation
- **`query_audit_trail`** — tamper-evident audit trail query; returns entries from the hash-chained `AuditLog`
- **`research_router`** — broad life-science research triage via installed runtime scripts (currently PubMed metadata + abstract snippets and ProteomeXchange dataset discovery)
- **Healthcare AI Constitution** — bundled 20-rule `constitution/healthcare_v1.yaml`; covers medication safety, PII/PHI, MACI enforcement, EHR access controls
- **PHI detection** — `phi_detector` custom validator catches 10 of 18 HIPAA Safe Harbor identifiers (SSN, MRN, DOB, phone, email, insurance ID, IP, account #, device/UDI, license #)
- **Clinical decision audit** — `clinical_decision_auditor` custom validator logs every clinical decision as a governance event
- **Security** — `X-API-Key` auth (when `CLINICALGUARD_API_KEY` is set), 64 KB request body limit, 10 K char text limit, input Unicode normalisation
- **A2A agent card** — `GET /.well-known/agent.json` returns the agent card for discovery

## Skill Reference

| Skill ID | Prefix / `skill` field | Description |
|----------|----------------------|-------------|
| `validate_clinical_action` | `validate_clinical_action: <text>` | Clinical action validation with LLM + constitutional rules |
| `check_hipaa_compliance` | `check_hipaa_compliance: <text>` | HIPAA compliance checklist against an agent description |
| `query_audit_trail` | `query_audit_trail: <query>` | Query the tamper-evident audit log |
| `research_router` | `research_router: <question>` | Route a broad life-science question to runtime evidence discovery |

## Package Structure

| Module | Description |
|--------|-------------|
| `clinicalguard.agent` | `create_app()` — builds the Starlette app with all routes and validators |
| `clinicalguard.main` | `app` — ASGI app; entry point for `uvicorn` |
| `clinicalguard.skills.validate_clinical` | `validate_clinical_action(text, engine, audit_log)` |
| `clinicalguard.skills.hipaa_checker` | `check_hipaa_compliance(agent_description)` |
| `clinicalguard.skills.research_router` | `research_router(question, audit_log=..., on_persist=...)` using installed Life Science runtime scripts |
| `training/research_router_sft.py` | Starter Unsloth/TRL fine-tuning script for `unsloth/Qwen3.5-4B-Base` |
| `clinicalguard.skills.healthcare_validators` | `phi_detector`, `clinical_decision_auditor`, `adverse_event_logger` custom validators |
| `constitution/healthcare_v1.yaml` | Bundled 20-rule Healthcare AI Constitution |

## Runtime dependencies

- `acgs-lite>=2.5`
- `starlette>=0.37`
- `uvicorn[standard]>=0.29`
- `pyyaml>=6.0`
- `httpx>=0.27`
- `pydantic>=2.0`

## License

AGPL-3.0-or-later.

## Links

- [Homepage](https://acgs.ai)
- [PyPI](https://pypi.org/project/clinicalguard/)
- [Issues](https://github.com/dislovelhl/clinicalguard/issues)
