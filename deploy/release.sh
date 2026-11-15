#!/usr/bin/env bash
set -euo pipefail

: "${IMAGE_PREFIX:?set IMAGE_PREFIX, e.g. registry.example.com/hmm}"
: "${IMAGE_TAG:?set IMAGE_TAG, e.g. v1.0.0}"
platforms="${PLATFORMS:-linux/amd64,linux/arm64}"

cd "$(dirname "$0")/.."

for image in app:deploy/python.Dockerfile web:deploy/web.Dockerfile backup:deploy/backup/Dockerfile; do
    name="${image%%:*}"
    docker buildx build --platform "$platforms" --file "${image#*:}" \
        --tag "${IMAGE_PREFIX}-${name}:${IMAGE_TAG}" --push .
done
