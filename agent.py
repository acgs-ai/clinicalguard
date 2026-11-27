"""ClinicalGuard A2A Agent.

Custom Starlette dispatcher — NOT a wrapper around create_a2a_app().
create_a2a_app() has no extension point; this is a clean reimplementation
that reuses the ACGS components (GovernanceEngine, AuditLog, MACIEnforcer).

Skill dispatch: reads a "skill" field from the first message part, or falls
back to a keyword prefix in the text itself.

Supported skills:
  validate_clinical_action   — LLM + constitutional clinical validation
  check_hipaa_compliance     — HIPAA checklist against an agent description
  query_audit_trail          — Tamper-evident audit log query

Security: task requests need X-API-Key matching CLINICALGUARD_API_KEY. With no key
configured they are refused, unless CLINICALGUARD_ALLOW_UNAUTHENTICATED is set.

Constitutional Hash: 608508a9bd224290
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from acgs_lite.audit import AuditLog
from acgs_lite.constitution import Constitution
from acgs_lite.engine import GovernanceEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .skills.audit_query import query_audit_trail
from .skills.hipaa_checker import check_hipaa_compliance
from .skills.validate_clinical import validate_clinical_action

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes"})

CONSTITUTIONAL_HASH = "8494a847758c08dc"

# Security limits
MAX_REQUEST_BODY_BYTES = 64 * 1024  # 64 KB max request body
MAX_ACTION_TEXT_CHARS = 10_000  # 10K chars max clinical action text

# Audit log location
AUDIT_LOG_ENV = "CLINICALGUARD_AUDIT_LOG"
DEFAULT_AUDIT_LOG_NAME = "clinicalguard_audit.json"


def resolve_audit_log_path(environ: dict[str, str] | None = None) -> Path:
    """Return the audit log path after proving that this exact path can be written.

    Uses ``CLINICALGUARD_AUDIT_LOG`` when set (made absolute against the current
    directory; symlinks are not resolved). Otherwise the default is
    ``$XDG_STATE_HOME/clinicalguard/clinicalguard_audit.json`` (falling back to
    ``~/.local/state``).

    Startup checks, in order:

    * the parent directory is created if missing (mode 0o700; an existing
      directory is never chmod-ed);
    * a directory at the path is rejected;
    * an existing path (symlinks followed) must be a regular file that opens for
      update; devices such as ``/dev/null`` and FIFOs are rejected, and the file is
      not truncated or modified;
    * otherwise the log file is created with mode 0o600 and an empty entry list.

    Any failure raises ``RuntimeError`` so a misconfigured deployment stops at
    startup instead of answering requests it cannot write an audit entry for.
    """
    env = os.environ if environ is None else environ
    configured = (env.get(AUDIT_LOG_ENV) or "").strip()  # blank means unset
    try:
        if configured:
            path = Path(os.path.abspath(Path(configured).expanduser()))
        else:
            state_home = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
            default = Path(state_home) / "clinicalguard" / DEFAULT_AUDIT_LOG_NAME
            path = Path(os.path.abspath(default))
    except RuntimeError as exc:  # no home directory to expand "~" or the default
        raise RuntimeError(
            f"Cannot determine a home directory for the audit log path. "
            f"Set {AUDIT_LOG_ENV} to an absolute, writable file path."
        ) from exc
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_dir():
            raise IsADirectoryError(f"{path} is a directory")
        if path.exists() or path.is_symlink():
            # Check the file type BEFORE opening: opening a FIFO or device can block
            # or succeed on something that discards writes (e.g. /dev/null).
            if not stat.S_ISREG(os.stat(path).st_mode):  # follows symlinks
                raise OSError(errno.EINVAL, "not a regular file")
            with open(path, "r+b"):
                pass
        else:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write('{"entries": []}')
    except OSError as exc:
        raise RuntimeError(
            f"Audit log path {str(path)!r} is not writable ({type(exc).__name__}: "
            f"{exc.strerror or exc}). Set {AUDIT_LOG_ENV} to a writable file path."
        ) from exc
    return path


# ──────────────────────────────────────────────────────────────────────────────
# Agent card (Prompt Opinion Marketplace registration)
# ──────────────────────────────────────────────────────────────────────────────

_AGENT_CARD: dict[str, Any] = {
    "name": "ClinicalGuard",
    "description": (
        "AI safety infrastructure for clinical decision support. "
        "Validates proposed clinical actions (medication orders, prior auth) against a "
        "20-rule Healthcare AI Constitution using LLM reasoning + deterministic "
        "constitutional enforcement (MACI separation of powers). "
        "Every decision is cryptographically logged in a tamper-evident audit trail."
    ),
    "version": __version__,
    "url": os.environ.get("CLINICALGUARD_URL", "http://localhost:8080"),
    "capabilities": ["tasks"],
    "defaultInputModes": ["text/plain", "application/json"],
    "defaultOutputModes": ["application/json"],
    "skills": [
        {
            "id": "validate_clinical_action",
            "name": "Validate Clinical Action",
            "description": (
                "Validate a proposed clinical action (medication order, prior auth, "
                "care plan change) against the Healthcare AI Constitution. "
                "Returns: decision (APPROVED/CONDITIONALLY_APPROVED/REJECTED), "
                "risk_tier, reasoning, drug_interactions, conditions, audit_id."
            ),
            "inputModes": ["text/plain"],
            "outputModes": ["application/json"],
            "examples": [
                (
                    "validate_clinical_action: Patient SYNTH-042 on Warfarin. "
                    "Propose Aspirin 325mg daily."
                ),
                (
                    "validate_clinical_action: Prescribe Adalimumab 40mg Q2W for RA. "
                    "No prior treatment documented."
                ),
            ],
        },
        {
            "id": "check_hipaa_compliance",
            "name": "Check HIPAA Compliance",
            "description": (
                "Run a HIPAA compliance checklist against an AI agent system "
                "description. Returns: compliant bool, items_checked, checklist with "
                "status and MACI-mapped mitigations, constitutional_hash."
            ),
            "inputModes": ["text/plain"],
            "outputModes": ["application/json"],
            "examples": [
                (
                    "check_hipaa_compliance: This agent processes synthetic patient data, "
                    "maintains an audit log..."
                ),
            ],
        },
        {
            "id": "query_audit_trail",
            "name": "Query Audit Trail",
            "description": (
                "Query the tamper-evident audit trail. "
                "Returns entries by audit_id or the most recent N entries. "
                "Includes chain_valid flag for integrity verification."
            ),
            "inputModes": ["text/plain"],
            "outputModes": ["application/json"],
            "examples": [
                "query_audit_trail: HC-20260401-A7F2B3",
                "query_audit_trail: recent 10",
            ],
        },
    ],
    "provider": {
        "organization": "ACGS Project",
        "url": "https://github.com/acgs-project/acgs-clean",
    },
    "securitySchemes": {
        "apiKey": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
        }
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# ClinicalGuardApp
# ──────────────────────────────────────────────────────────────────────────────


class ClinicalGuardApp:
    """Starlette-based A2A server for ClinicalGuard.

    Usage::

        app = ClinicalGuardApp.create()
        # Run with: uvicorn clinicalguard.agent:app_instance
    """

    def __init__(
        self,
        constitution: Constitution,
        audit_log: AuditLog,
        audit_log_path: Path | None = None,
    ) -> None:
        self.engine = GovernanceEngine(constitution, strict=False)
        # Wire custom healthcare validators (PHI-PHONE, PHI-DOB, etc.)
        from .skills.healthcare_validators import register_all

        register_all(self.engine)
        self.audit_log = audit_log
        self.audit_log_path = audit_log_path
        # A whitespace-only key is not a key. It is also not "no key": the operator
        # meant to set one, so the unauthenticated opt-out does not apply to it.
        raw_api_key = os.environ.get("CLINICALGUARD_API_KEY", "")
        self._api_key = raw_api_key.strip()
        self._api_key_blank = bool(raw_api_key) and not self._api_key
        if self._api_key_blank:
            logger.error(
                "CLINICALGUARD_API_KEY is set but contains only whitespace: "
                "all task requests are rejected until a real key is configured."
            )
        self._allow_unauthenticated = (
            os.environ.get("CLINICALGUARD_ALLOW_UNAUTHENTICATED", "").strip().lower()
            in _TRUTHY
        )
        if not self._api_key:
            if self._allow_unauthenticated:
                logger.warning(
                    "CLINICALGUARD_ALLOW_UNAUTHENTICATED is set and no CLINICALGUARD_API_KEY "
                    "is configured: task requests are accepted without authentication. "
                    "Local development only."
                )
            else:
                logger.warning(
                    "No CLINICALGUARD_API_KEY configured: task requests will be rejected "
                    "with 401. Set CLINICALGUARD_API_KEY, or "
                    "CLINICALGUARD_ALLOW_UNAUTHENTICATED=1 for local development."
                )

    @classmethod
    def create(
        cls,
        constitution_path: str | Path | None = None,
        audit_log_path: str | Path | None = None,
    ) -> ClinicalGuardApp:
        """Factory: load constitution from YAML, restore audit log from file."""
        if constitution_path is None:
            constitution_path = Path(__file__).parent / "constitution" / "healthcare_v1.yaml"

        constitution = Constitution.from_yaml(str(constitution_path))
        logger.info(
            "Loaded Healthcare AI Constitution: %d rules, hash=%s",
            len(constitution.rules),
            constitution.hash,
        )

        audit_log = AuditLog()

        # Restore persisted audit entries on startup
        if audit_log_path is not None:
            audit_log_path = Path(audit_log_path)
            if audit_log_path.exists():
                try:
                    if audit_log_path.stat().st_size > 0:
                        _restore_audit_log(audit_log, audit_log_path)
                        logger.info(
                            "Restored %d audit entries from %s", len(audit_log), audit_log_path
                        )
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        f"Failed to restore non-empty audit log {str(audit_log_path)!r} "
                        f"({type(exc).__name__}: {exc}). Refusing to start to prevent "
                        f"overwriting forensic audit history."
                    ) from exc

        return cls(constitution=constitution, audit_log=audit_log, audit_log_path=audit_log_path)

    def _persist(self, audit_log: AuditLog) -> None:
        """Persist audit log to file (called after each write)."""
        if self.audit_log_path:
            try:
                audit_log.export_json(self.audit_log_path)
            except OSError as exc:
                logger.warning("Audit log persistence failed: %s", type(exc).__name__)

    def _check_auth(self, request: Request) -> bool:
        """Return True if auth passes.

        Fails closed: with no API key configured every task request is refused unless
        the operator explicitly opted out with CLINICALGUARD_ALLOW_UNAUTHENTICATED.
        A configured key is always enforced, whatever the opt-out says.
        """
        import hmac

        if not self._api_key:
            return self._allow_unauthenticated and not self._api_key_blank
        return hmac.compare_digest(
            request.headers.get("X-API-Key", "").encode(),
            self._api_key.encode(),
        )

    # ── Route handlers ────────────────────────────────────────────────────────

    async def handle_agent_card(self, request: Request) -> JSONResponse:
        return JSONResponse(_AGENT_CARD)

    async def handle_health(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "rules": len(self.engine.constitution.rules),
                "audit_entries": len(self.audit_log),
                "chain_valid": self.audit_log.verify_chain(),
                "constitutional_hash": self.engine.constitution.hash,
            }
        )

    async def handle_a2a(self, request: Request) -> JSONResponse:
        """Main A2A task handler — dispatches to skills by name."""
        if not self._check_auth(request):
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32001, "message": "Unauthorized — provide X-API-Key header"},
                },
                status_code=401,
            )

        # Enforce request body size limit (handle non-numeric Content-Length)
        content_length = request.headers.get("content-length")
        try:
            if content_length and int(content_length) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Request body too large"},
                    },
                    status_code=413,
                )
        except (ValueError, TypeError):
            pass  # Non-numeric Content-Length — let body read handle it

        try:
            raw_body = await request.body()
            if len(raw_body) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Request body too large"},
                    },
                    status_code=413,
                )
            body_parsed = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error — invalid JSON"},
                },
                status_code=400,
            )

        # Reject top-level non-object JSON (arrays, strings, etc.)
        if not isinstance(body_parsed, dict):
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Request must be a JSON object"},
                },
                status_code=400,
            )
        body: dict[str, Any] = body_parsed

        req_id = body.get("id")
        method = body.get("method", "")

        if method != "tasks/send":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method!r}. Supported: tasks/send",
                    },
                }
            )

        params = body.get("params", {})
        if not isinstance(params, dict):
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": "Invalid params — expected object"},
                },
                status_code=400,
            )
        task_id = params.get("id") or f"task-{uuid.uuid4().hex[:8]}"
        message = params.get("message", {})
        parts = message.get("parts", []) if isinstance(message, dict) else []
        if not isinstance(parts, list):
            parts = []
        first_part = parts[0] if parts else {}
        text = first_part.get("text", "") if isinstance(first_part, dict) else ""
        if not isinstance(text, str):
            text = str(text) if text is not None else ""

        # Enforce action text size limit
        if len(text) > MAX_ACTION_TEXT_CHARS:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32602,
                        "message": f"Action text too long ({len(text)} chars, "
                        f"max {MAX_ACTION_TEXT_CHARS})",
                    },
                },
                status_code=400,
            )

        # Normalize Unicode (NFKC) to defeat homoglyph/zero-width bypasses,
        # then strip null bytes, control characters, and default-ignorable code points
        text = unicodedata.normalize("NFKC", text)
        text = "".join(
            c
            for c in text
            if c in ("\n", "\t")
            or (ord(c) >= 32 and unicodedata.category(c) not in ("Cf", "Cc", "Cn"))
        )

        skill_name, skill_input = _parse_skill(text)

        try:
            result_data = await self._dispatch(skill_name, skill_input)
        except Exception:
            logger.exception("Skill dispatch error for skill=%r", skill_name)
            # Return JSON-RPC error — never fake a successful completion
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32603,
                        "message": "Internal processing error",
                        "data": {"skill": skill_name, "task_id": task_id},
                    },
                },
                status_code=500,
            )

        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "id": task_id,
                    "status": "completed",
                    "result": result_data,
                },
            }
        )

    async def _dispatch(self, skill_name: str, skill_input: str) -> dict[str, Any]:
        """Route to the appropriate skill handler."""
        if skill_name == "validate_clinical_action":
            return await validate_clinical_action(
                skill_input,
                engine=self.engine,
                audit_log=self.audit_log,
                on_persist=self._persist,
            )

        if skill_name == "check_hipaa_compliance":
            return check_hipaa_compliance(
                skill_input,
                constitutional_hash=self.engine.constitution.hash,
            )

        if skill_name == "query_audit_trail":
            # Input can be an audit_id ("HC-20260401-XXXXXX"), "recent [N]", or "last [N]"
            audit_id: str | None = None
            limit = 20
            stripped = skill_input.strip()
            if stripped:
                tokens = stripped.split()
                lower_tokens = [t.lower() for t in tokens]
                first = tokens[0]
                if first.upper().startswith("HC-"):
                    audit_id = first
                else:
                    matched_kw = next(
                        (kw for kw in ("recent", "last") if kw in lower_tokens),
                        None,
                    )
                    if matched_kw is not None:
                        idx = lower_tokens.index(matched_kw)
                        if idx + 1 < len(lower_tokens) and lower_tokens[idx + 1].isdigit():
                            limit = max(1, min(int(lower_tokens[idx + 1]), 500))
                    elif all(c.isalnum() or c in "-_" for c in first):
                        audit_id = first
            return query_audit_trail(self.audit_log, audit_id=audit_id, limit=limit)

        # Unknown skill — return helpful error
        available = ["validate_clinical_action", "check_hipaa_compliance", "query_audit_trail"]
        return {
            "error": f"Unknown skill: {skill_name!r}",
            "available_skills": available,
            "hint": f"Prefix your message with one of: {', '.join(available)}: <your input>",
        }

    def build_starlette_app(self) -> Starlette:
        """Build and return the Starlette ASGI app."""
        return Starlette(
            routes=[
                Route("/.well-known/agent.json", self.handle_agent_card, methods=["GET"]),
                Route("/health", self.handle_health, methods=["GET"]),
                Route("/", self.handle_a2a, methods=["POST"]),
            ]
        )


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _parse_skill(text: str) -> tuple[str, str]:
    """Parse 'skill_name: input text' or fall back to keyword detection.

    Returns (skill_name, skill_input).
    """
    text = text.strip()
    known_skills = [
        "validate_clinical_action",
        "check_hipaa_compliance",
        "query_audit_trail",
    ]

    # Try explicit prefix "skill_name: ..."
    for skill in known_skills:
        if text.lower().startswith(skill + ":"):
            return skill, text[len(skill) + 1 :].strip()
        # Also match without underscore spaces for natural use
        alt = skill.replace("_", " ")
        if text.lower().startswith(alt + ":"):
            return skill, text[len(alt) + 1 :].strip()

    # Detect "prefix: input" where prefix looks like a skill name but isn't known.
    # This prevents falling through to validate_clinical_action on explicit but
    # unrecognised skill names (e.g. "do_something_unknown: ...").
    if ":" in text:
        prefix = text.split(":")[0].strip().lower().replace(" ", "_")
        if prefix and all(c.isalnum() or c == "_" for c in prefix):
            return "unknown", text

    # Keyword-based fallback
    text_lower = text.lower()
    if any(
        kw in text_lower
        for kw in ["validate", "prescribe", "medication", "dose", "drug", "patient synth"]
    ):
        return "validate_clinical_action", text
    if any(kw in text_lower for kw in ["hipaa", "compliance", "phi", "privacy"]):
        return "check_hipaa_compliance", text
    if any(kw in text_lower for kw in ["audit", "query audit", "trail", "hc-2"]):
        return "query_audit_trail", text

    # Default: try to validate as a clinical action
    return "validate_clinical_action", text


def _restore_audit_log(audit_log: AuditLog, path: Path) -> None:
    """Reload persisted audit entries into the live AuditLog."""
    from acgs_lite.audit import AuditEntry

    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise TypeError("Audit log JSON root must be an object")
    raw_entries = data.get("entries", [])
    if not isinstance(raw_entries, list):
        raise TypeError("Audit log 'entries' field must be a list")
    for entry_dict in raw_entries:
        if not isinstance(entry_dict, dict):
            raise TypeError("Each audit log entry must be an object")
        entry = AuditEntry(
            id=entry_dict["id"],
            type=entry_dict.get("type", "clinical_validation"),
            agent_id=entry_dict.get("agent_id", ""),
            action=entry_dict.get("action", ""),
            valid=entry_dict.get("valid", True),
            violations=entry_dict.get("violations", []),
            constitutional_hash=entry_dict.get("constitutional_hash", ""),
            latency_ms=entry_dict.get("latency_ms", 0.0),
            metadata=entry_dict.get("metadata", {}),
            timestamp=entry_dict.get("timestamp", ""),
        )
        audit_log.record(entry)


# ──────────────────────────────────────────────────────────────────────────────
# Module-level app instance (for uvicorn: clinicalguard.agent:app)
# ──────────────────────────────────────────────────────────────────────────────


def create_app(
    constitution_path: str | Path | None = None,
    audit_log_path: str | Path | None = None,
) -> Starlette:
    """Create the ClinicalGuard Starlette app."""
    guard = ClinicalGuardApp.create(
        constitution_path=constitution_path,
        audit_log_path=audit_log_path,
    )
    return guard.build_starlette_app()
