#!/bin/bash
# Builds all homelab agent Docker images and pushes to the local registry.
# Run this after adding dependencies or updating pyproject.toml/uv.lock.
set -e
REGISTRY="${REGISTRY:-localhost:5000}"
TOOLS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

agents=(agent-sre-patrol agent-storage-report agent-media-health agent-network-sentinel)

for agent in "${agents[@]}"; do
    tag="${REGISTRY}/homelab-${agent}:latest"
    echo "[build] ${tag} ..."
    docker build -t "$tag" "$TOOLS_DIR/$agent/"
    echo "[push]  ${tag} ..."
    docker push "$tag"
done

echo "[done] All agent images pushed to ${REGISTRY}."
