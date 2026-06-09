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
    --build-arg COMMIT_SHA="$(git rev-parse --short HEAD)" \
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
    --build-arg FULL_VERSION_OVERRIDE="v${VERSION}" \
    --build-arg COMMIT_SHA="$(git rev-parse --short HEAD)" \
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

  # Create GitHub Release (concise notes)
  echo "=== Creating GitHub Release v${VERSION} ==="
  LAST_TAG=$(git describe --tags --abbrev=0 HEAD^ 2>/dev/null || echo "")
  if [ -n "$LAST_TAG" ]; then
    LOG=$(git log "$LAST_TAG"..HEAD --oneline --no-merges --format="  - %s" | grep -viE 'bump|chore|beta' | head -10)
    [ -z "$LOG" ] && LOG="  - 常规更新"
    RELEASE_NOTES="### **Debug**\\n$LOG"
  else
    RELEASE_NOTES="### **Debug**\\n- 初始正式发布 v${VERSION}"
  fi

  # Extract GitHub token from remote URL (https://user:TOKEN@github.com/...)
  REMOTE_URL=$(git remote get-url origin)
  GITHUB_TOKEN=$(echo "$REMOTE_URL" | sed 's|.*://[^:]*:\([^@]*\)@.*|\1|')

  # Build release notes JSON safely with Python
  python3 -c "
import json
payload = json.dumps({'tag_name': 'v${VERSION}', 'name': 'v${VERSION}', 'body': '''${RELEASE_NOTES}'''})
print(payload)
" | curl -sL -X POST \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: token $GITHUB_TOKEN" \
    https://api.github.com/repos/rayyume/RayNews-Reader/releases \
    -d @- > /dev/null
  echo "=== Release v${VERSION} created ==="

else
  echo "ERROR: Unsupported branch '${CURRENT_BRANCH}'"
  echo "Use 'beta' for test builds or 'main' for production releases."
  exit 1
fi

echo "=== Done ==="
