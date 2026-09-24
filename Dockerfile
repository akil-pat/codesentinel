# Placeholder — filled in once the FastAPI service is implemented.
# Packaging is intentionally sequenced last; see README status note.
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
CMD ["uvicorn", "codesentinel.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
