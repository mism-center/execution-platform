FROM python:3.11-slim

LABEL maintainer="RENCI MISM Team"
LABEL description="MISM Execution Platform API"

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && pip install --no-cache-dir . \
    && pip cache purge \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

EXPOSE 8000

CMD ["uvicorn", "main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
