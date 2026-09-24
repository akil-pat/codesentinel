"""FastAPI app exposing the GitHub webhook service."""

from fastapi import FastAPI

app = FastAPI(title="codesentinel", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
