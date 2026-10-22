# syntax=docker/dockerfile:1
# API + dashboard + worker image. The LLM planner talks to any OpenAI-compatible server
# (ARP_LLM_BASE_URL); without it only the v0-rules workflow is available.
FROM mirror.gcr.io/library/python:3.12-slim
RUN useradd -u 10001 -m app && mkdir -p /data && chown app /data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY results ./results
# Optional: a corporate CA for TLS-intercepting proxies (docker build --secret id=pip_ca,src=ca.pem).
RUN --mount=type=secret,id=pip_ca,required=false \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    pip install --no-cache-dir .
USER 10001
ENV ARP_ITSM_PATH=/data/itsm.db ARP_TRACE_DIR=/data/traces
EXPOSE 8095
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8095/health/live')"
CMD ["arp", "serve", "--host", "0.0.0.0", "--port", "8095"]
