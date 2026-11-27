"""Shared test configuration.

Constitutional Hash: 608508a9bd224290
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _explicit_unauthenticated_mode(monkeypatch):
    """Run the suite in the explicit local-development auth mode.

    The service refuses task requests when no API key is configured. Most tests
    exercise behaviour behind the auth check, so they opt in to the documented
    development mode here. Tests of the auth check itself remove this variable
    (see ``TestAuthFailsClosed``) or set ``CLINICALGUARD_API_KEY``, which is
    always enforced regardless of this setting.
    """
    monkeypatch.delenv("CLINICALGUARD_API_KEY", raising=False)
    monkeypatch.setenv("CLINICALGUARD_ALLOW_UNAUTHENTICATED", "1")
