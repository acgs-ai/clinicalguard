"""ClinicalGuard entrypoint.

Usage:
    uvicorn clinicalguard.main:app --host 0.0.0.0 --port 8080
    python -m clinicalguard.main

Environment variables:
    CLINICALGUARD_API_KEY      Required. Without it task requests get 401.
    CLINICALGUARD_ALLOW_UNAUTHENTICATED
                               Set to 1 to accept task requests with no API key.
                               Local development only.
    HOST                       Bind address for `python -m clinicalguard.main`.
                               Default: 127.0.0.1
    CLINICALGUARD_AUDIT_LOG    Path to persist audit log JSON.
                               Default: $XDG_STATE_HOME/clinicalguard/clinicalguard_audit.json
                               (~/.local/state/... if unset). Startup fails if unwritable.
    CLINICALGUARD_URL          Public URL for agent card. Default: http://localhost:8080
    PI_BINARY                  Path to pi binary. Default: pi (must be in PATH).

Constitutional Hash: 608508a9bd224290
"""

from __future__ import annotations

import logging
import os

from .agent import create_app, resolve_audit_log_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_audit_log_path = resolve_audit_log_path()

app = create_app(audit_log_path=_audit_log_path)

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(
        "clinicalguard.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=port,
        log_level="info",
    )
