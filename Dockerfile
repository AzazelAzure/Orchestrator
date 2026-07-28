# Orchestrator R4 local control-plane stack
FROM python:3.12-slim AS base

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 orch \
    && useradd --uid 10001 --gid orch --create-home --home-dir /home/orch orch \
    && mkdir -p /data /tmp/orch \
    && chown -R orch:orch /app /data /tmp/orch

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY agentic ./agentic
COPY deploy ./deploy
COPY scripts ./scripts

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e '.[api,worker,mcp]' \
    && chown -R orch:orch /app

# Control-plane roles intentionally lack orch-script + script execution authority.
ENV ORCH_SCRIPT_ROLE=control

USER orch

FROM base AS coordinator
ENV FLOW_DB_PATH=/data/state.db
ENV COORDINATOR_HOST=0.0.0.0
# Port is internal-only; Compose must not publish 9001.
EXPOSE 9001
CMD ["python", "-m", "flow_engine.coordinator.http_service"]

FROM base AS api
EXPOSE 8000
# Bind all interfaces inside the container; Compose publishes to loopback only.
CMD ["gunicorn", "flow_engine.control_plane.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "--worker-tmp-dir", "/tmp", "--no-control-socket"]

FROM base AS worker
CMD ["celery", "-A", "flow_engine.workers.celery_app", "worker", "--loglevel=info", "-Q", "provider-mock", "--concurrency", "1"]

# R4C: networked script-worker controller — spool dispatch only; no subprocess.
FROM base AS script-worker
USER root
RUN mkdir -p /var/orch/spool /etc/orch \
    && chown -R orch:orch /var/orch /etc/orch
USER orch
ENV ORCH_SCRIPT_ROLE=script-worker
ENV ORCH_SCRIPT_SPOOL_DIR=/var/orch/spool
ENV ORCH_SCRIPT_RUNNER_ATTESTATION_FILE=/etc/orch/script-runner.attestation.json
CMD ["celery", "-A", "flow_engine.workers.celery_app", "worker", "--loglevel=info", "-Q", "script-sandbox", "--concurrency", "1"]

# R4C: networkless script-runner — orch-script only; no DB/Redis/coordinator creds.
FROM base AS script-runner
USER root
RUN mkdir -p /etc/orch /tmp/orch/workspace /var/orch/spool \
    && cp /app/src/flow_engine/script_sandbox/orch_script_cli.py /usr/local/bin/orch-script \
    && chmod 0555 /usr/local/bin/orch-script \
    && python -c "import os; os.environ['ORCH_TESTING']='1'; from flow_engine.script_sandbox.pins import orch_script_source_digest, verify_executable_bytes; verify_executable_bytes(expected_digest=orch_script_source_digest())" \
    && chown -R orch:orch /etc/orch /tmp/orch /var/orch
USER orch
ENV ORCH_SCRIPT_ROLE=script-runner
ENV ORCH_SCRIPT_EXECUTABLE=/usr/local/bin/orch-script
ENV ORCH_SCRIPT_WORKSPACE=/tmp/orch/workspace
ENV ORCH_SCRIPT_SPOOL_DIR=/var/orch/spool
ENV ORCH_SCRIPT_RUNNER_ATTESTATION_FILE=/etc/orch/script-runner.attestation.json
# No COORDINATOR_URL / CELERY / REDIS / tokens in this image's default env.
CMD ["python", "-m", "flow_engine.script_sandbox.runner_service"]

# R4C: Asia/Manila Celery Beat + scheduler queue consumer (embedded beat).
FROM base AS scheduler
ENV TZ=Asia/Manila
CMD ["celery", "-A", "flow_engine.workers.celery_app", "worker", "--loglevel=info", "-Q", "scheduler", "--concurrency", "1", "-B", "--schedule", "/tmp/celerybeat-schedule"]

# R4B: MCP lane containers call DRF only — no SQLite volume, no coordinator URL.
FROM base AS mcp-lane
ENV ORCH_MCP_HEALTH_PORT=9100
EXPOSE 9100
CMD ["python", "-m", "flow_engine.mcp_lanes.service_main", "--ready-loop"]
