# Multi-stage Dockerfile for self-hosting Omakase as a web service.
# Build:  docker build -t omakase:latest .
# Run:    docker run -d --name omakase -p 8765:8765 \
#           -e OMAKASE_API_KEY=... \
#           -e MAL_CLIENT_ID=...    # only if using MyAnimeList as a source
#           omakase:latest

FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f AS builder
WORKDIR /build
COPY requirements-build.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-build.txt
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --no-isolation --outdir /wheels

FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f AS runtime

RUN apt-get update && \
    apt-get install --no-install-recommends -y ca-certificates ffmpeg && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --create-home --uid 1000 omakase
WORKDIR /home/omakase

ARG OMAKASE_AGENT_TOOLS=true
COPY requirements-runtime.txt requirements-agents.txt /tmp/
RUN case "$OMAKASE_AGENT_TOOLS" in true) requirements=agents ;; false) requirements=runtime ;; *) exit 2 ;; esac && \
    pip install --no-cache-dir --require-hashes -r /tmp/requirements-$requirements.txt && \
    rm -f /tmp/requirements-runtime.txt /tmp/requirements-agents.txt
COPY --from=builder /wheels/*.whl /tmp/
RUN pip install --no-cache-dir --no-deps /tmp/*.whl && rm -f /tmp/*.whl

ARG OMAKASE_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/JhinxDev/omakase-plus"
LABEL org.opencontainers.image.description="Omakase private media discovery and orchestration"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.revision=$OMAKASE_REVISION

USER omakase
ENV OMAKASE_PORT=8765
ENV OMAKASE_REVISION=$OMAKASE_REVISION
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/').read()" || exit 1

CMD ["omakase", "web", "--host", "0.0.0.0"]
