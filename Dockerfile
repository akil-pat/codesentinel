FROM python:3.11-slim

# git is a runtime dependency, not just a dev tool: the webhook service
# shells out to it to check out each reviewed PR's head commit (see
# src/codesentinel/api/github_client.py:checkout_pull_request).
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only what the build needs, not the whole tree (eval/, tests/, etc.
# aren't needed by the deployed webhook service and are left out of the
# image entirely — see .dockerignore).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 1000 codesentinel
USER codesentinel

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["uvicorn", "codesentinel.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
