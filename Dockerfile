FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install the package from the build context (the repository root). .dockerignore
# is an allow-list, so the context holds only the package sources. The
# [anthropic] extra lets ANTHROPIC_API_KEY enable LLM reasoning; without a key
# the service answers from the constitutional rules only.
COPY . /src
RUN pip install "/src[anthropic]" && rm -rf /src

# Non-root runtime user; audit log lives on a volume, never in /tmp.
RUN groupadd -r clinicalguard \
    && useradd -r -g clinicalguard -d /home/clinicalguard -m -s /sbin/nologin clinicalguard \
    && mkdir -p /data \
    && chown clinicalguard:clinicalguard /data

ENV CLINICALGUARD_AUDIT_LOG=/data/clinicalguard_audit.json
VOLUME /data

EXPOSE 8080
USER clinicalguard

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]

CMD ["uvicorn", "clinicalguard.main:app", "--host", "0.0.0.0", "--port", "8080"]
