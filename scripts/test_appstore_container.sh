#!/bin/bash
# Test the appstore /api/v1/containers/ endpoint

BASE="http://localhost:8001"
AUTH="admin:admin"

echo "Launching interactive vivarium container..."

curl -s -u "$AUTH" -X POST "$BASE/api/v1/containers/" \
  -H "Content-Type: application/json" \
  -d '{
    "image": "helxplatform/vivarium-jupyter@sha256:c2bda6bbddea091ed4aa96f1fa3b6b41f51ad234d432c2412dd4919b76c77f6d",
    "name": "vivarium-explore",
    "port": 8888,
    "cpus": 1.0,
    "memory": "2G",
    "env": {
      "JUPYTER_TOKEN": "test123",
      "JUPYTER_ENABLE_LAB": "yes"
    }
  }'

echo ""
