#!/bin/bash
set -e

cd "$(dirname "$0")"

# ─── Read version files ────────────────────────────────────
VERSION=$(cat VERSION | tr -d ' \n')
IMAGE_NAME="ghcr.io/rayyume/raynews"
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

if [ "$CURRENT_BRANCH" = "beta" ]; then
  # ═══ BETA BUILD ═══════════════════════════════════════════
  BETA_REV=$(cat BETA_REVISION | tr -d ' \n')
  BETA_TAG="v${VERSION}-beta.${BETA_REV}"
  FULL_TAG="${IMAGE_NAME}:${BETA_TAG}"

  echo "=== Building RayNews ${BETA_TAG} (beta) ==="

  docker buildx build \
    --platform linux/amd64 \
    -t "${FULL_TAG}" \
    -t "${IMAGE_NAME}:dev" \
    --push \
    .

  echo "=== Pushed ${BETA_TAG} === 🧪"

  # Increment beta revision
  NEXT_BETA=$((BETA_REV + 1))
  echo "$NEXT_BETA" > BETA_REVISION

  # Commit beta revision bump (no VERSION change)
  git add BETA_REVISION
  git commit -m "chore(beta): bump to ${BETA_TAG} → beta.${NEXT_BETA}"
  git push origin beta

  echo "=== BETA_REVISION bumped to ${NEXT_BETA} ==="

elif [ "$CURRENT_BRANCH" = "main" ]; then
  # ═══ PRODUCTION BUILD ═════════════════════════════════════
  echo "=== Building RayNews v${VERSION} (production) ==="

  docker buildx build \
    --platform linux/amd64 \
    -t "${IMAGE_NAME}:latest" \
    -t "${IMAGE_NAME}:v${VERSION}" \
    --push \
    .

  echo "=== Pushed v${VERSION} + latest === 🚀"

  # Reset beta revision counter
  echo "1" > BETA_REVISION

  # Increment patch version (X.Y.Z → X.Y.(Z+1))
  MAJOR="${VERSION%%.*}"
  REST="${VERSION#*.}"
  MINOR="${REST%.*}"
  PATCH="${REST#*.}"
  NEXT="${MAJOR}.${MINOR}.$((PATCH + 1))"
  echo "$NEXT" > VERSION

  git add VERSION BETA_REVISION
  git commit -m "chore: release v${VERSION}, bump to v${NEXT}, reset beta"
  git push origin main

  echo "=== VERSION bumped to ${NEXT}, BETA_REVISION reset to 1 ==="

  # Sync beta branch with main
  echo "=== Syncing beta branch with main ==="
  git checkout beta
  git merge main --ff-only
  git push origin beta
  git checkout main
  echo "=== Beta synced with main ==="

else
  echo "ERROR: Unsupported branch '${CURRENT_BRANCH}'"
  echo "Use 'beta' for test builds or 'main' for production releases."
  exit 1
fi

echo "=== Done ==="
