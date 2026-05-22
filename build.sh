#!/bin/bash
set -e

cd "$(dirname "$0")"

VERSION=$(cat VERSION | tr -d ' \n')
IMAGE_NAME="ghcr.io/your-org/raynews"

echo "=== Building RayNews v${VERSION} ==="

# Build and push with both tags
docker buildx build \
  --platform linux/amd64 \
  -t "${IMAGE_NAME}:latest" \
  -t "${IMAGE_NAME}:v${VERSION}" \
  --push \
  .

echo "=== Pushed v${VERSION} + latest ==="

# Increment patch version (X.Y.Z → X.Y.(Z+1))
MAJOR="${VERSION%%.*}"
REST="${VERSION#*.}"
MINOR="${REST%.*}"
PATCH="${REST#*.}"
NEXT="${MAJOR}.${MINOR}.$((PATCH + 1))"
echo "$NEXT" > VERSION
echo "=== VERSION bumped to ${NEXT} ==="

# Commit version bump
git add VERSION
git commit -m "chore: bump to v${NEXT}"
git push

echo "=== Done ==="
