# ClinicalGuard Deployment Guide

## Quick Start (Local)

```bash
# From the repository root
pip install ".[anthropic]"          # requires acgs-lite 4.x, installed automatically
uvicorn clinicalguard.main:app --host 0.0.0.0 --port 8080
```

Health check: `curl http://localhost:8080/health`

Task requests need `CLINICALGUARD_API_KEY` set and sent as `X-API-Key`. For local
experiments without a key, start with `CLINICALGUARD_ALLOW_UNAUTHENTICATED=1`.

The audit log defaults to `~/.local/state/clinicalguard/clinicalguard_audit.json`
(`$XDG_STATE_HOME` is honoured). Set `CLINICALGUARD_AUDIT_LOG` to change it. The
service refuses to start if that path cannot be written.

## Docker

```bash
# From the repository root (the Dockerfile installs the package from the build context)
docker build -t clinicalguard .
docker run -p 8080:8080 \
  -v clinicalguard_data:/data \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e CLINICALGUARD_API_KEY=your-secret-key \
  clinicalguard
```

Omit `ANTHROPIC_API_KEY` to run rule-only (constitutional rules without LLM reasoning).

## Fly.io

This repository ships no `fly.toml`. Create one with `fly launch` and mount a volume at `/data`
(the image writes its audit log to `/data/clinicalguard_audit.json`). Set the secrets with
`fly secrets set ANTHROPIC_API_KEY=... CLINICALGUARD_API_KEY=...`.

## Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `ANTHROPIC_API_KEY` | For LLM layer | — | Claude API key for clinical reasoning |
| `CLINICALGUARD_API_KEY` | Yes | — | API key for request authentication. Without it task requests get 401 |
| `CLINICALGUARD_ALLOW_UNAUTHENTICATED` | No | — | `1` accepts task requests with no API key. Local development only; ignored when a key is set |
| `HOST` | No | `127.0.0.1` | Bind address, read only by `python -m clinicalguard.main` |
| `CLINICALGUARD_AUDIT_LOG` | No | `/data/clinicalguard_audit.json` in Docker; `~/.local/state/clinicalguard/clinicalguard_audit.json` locally | Audit log file path (startup fails if not writable) |
| `CLINICALGUARD_MODEL` | No | `claude-haiku-4-5` | LLM model for clinical reasoning |
| `CLINICALGUARD_URL` | No | — | Public URL for agent card registration |
| `PORT` | No | `8080` | Listen port, read only by `python -m clinicalguard.main` (`main.py`). The Docker image starts `uvicorn` with a fixed `--port 8080`, so `PORT` has no effect there |

## Security

- **Non-root container**: runs as `clinicalguard` user
- **Persistent audit**: `/data` volume for tamper-evident logs
- **Input limits**: 64KB request body, 10K char action text
- **Unicode normalization**: NFKC + zero-width/bidi stripping
- **Error sanitization**: no exception details leaked to clients
- **API key auth**: constant-time comparison against `CLINICALGUARD_API_KEY`. Fails closed: with no key configured, task requests are refused
- **Detected PHI is not sent to the LLM**: the deterministic rules run before the LLM; text matching a PHI/privacy rule is rejected without being sent to the model. Detection is pattern-based, so PHI the patterns miss (names, unusual formats) is not covered. Rejected text is still recorded in the audit log

## Monitoring

Health endpoint returns:
```json
{
  "status": "ok",
  "rules": 20,
  "audit_entries": 142,
  "chain_valid": true,
  "constitutional_hash": "8494a847758c08dc"
}
```

Monitor `chain_valid` — if false, the audit trail has been tampered with.

## CI/CD

`.github/workflows/ci.yml` runs on pushes and pull requests to `main`:

1. `test` job (Python 3.11 and 3.12): `pip install -e ".[dev]"`, `ruff check .`, then
   `python -m pytest tests/ --import-mode=importlib -m "not slow and not benchmark and not e2e"`.
2. `package` job: `python -m build --wheel --sdist` and `twine check dist/*`.

`.github/workflows/publish.yml` builds, checks and publishes to PyPI when a GitHub release is published.

There is no deploy workflow in this repository.
