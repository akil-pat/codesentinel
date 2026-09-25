"""FastAPI app exposing the GitHub webhook service."""

from fastapi import FastAPI

from codesentinel.api.webhooks import router as webhooks_router

app = FastAPI(title="codesentinel", version="0.1.0")
app.include_router(webhooks_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
