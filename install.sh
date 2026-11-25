#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/didi4007/comic-to-video.git"
SKILL_NAME="comic-to-video"
TARGET_DIR="${HOME}/.gemini/config/skills/${SKILL_NAME}"

echo "==> Installing ${SKILL_NAME} to ${TARGET_DIR} ..."
mkdir -p "$(dirname "${TARGET_DIR}")"

if [ -d "${TARGET_DIR}/.git" ]; then
  echo "==> Existing installation detected, pulling latest changes..."
  git -C "${TARGET_DIR}" pull --ff-only
else
  if [ -d "${TARGET_DIR}" ]; then
    BACKUP_DIR="${TARGET_DIR}.bak.$(date +%Y%m%d%H%M%S)"
    echo "==> Backing up existing folder to ${BACKUP_DIR}"
    mv "${TARGET_DIR}" "${BACKUP_DIR}"
  fi
  git clone --depth 1 "${REPO_URL}" "${TARGET_DIR}"
fi

chmod +x "${TARGET_DIR}/scripts/build_comic_video.py" "${TARGET_DIR}/install.sh"

echo ""
echo "==> Checking runtime dependencies..."
MISSING=0
for bin in ffmpeg ffprobe uv; do
  if command -v "${bin}" >/dev/null 2>&1; then
    echo "  [OK] ${bin}: $(command -v "${bin}")"
  else
    echo "  [MISSING] ${bin}"
    MISSING=1
  fi
done

if [ "${MISSING}" -eq 1 ]; then
  echo ""
  echo "Hint (macOS Homebrew): brew install ffmpeg uv"
fi

echo ""
echo "✅ ${SKILL_NAME} installed successfully at: ${TARGET_DIR}"
