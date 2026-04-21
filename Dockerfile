FROM python:3.11-slim

LABEL maintainer="RENCI MISM Team"
LABEL description="MISM Execution Platform API"

WORKDIR /app

# Copy mism-registry (DAL) first — it's a local dependency
COPY vendor/metadata-schema /app/vendor/metadata-schema

# Copy execution platform source
COPY pyproject.toml ./
COPY src/ ./src/

# Install both packages
RUN pip install --no-cache-dir \
    /app/vendor/metadata-schema \
    . \
    && pip cache purge \
    && rm -rf /app/vendor

EXPOSE 8000

CMD ["uvicorn", "main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
