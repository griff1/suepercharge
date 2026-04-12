#!/usr/bin/env bash
# Build a single Lambda deployment zip shared by all 4 functions.
# Uses `uv export` to resolve deps against pyproject.toml, installs them into
# a temp dir for the arm64/python3.12 target, then zips the tree + our source.
#
# Requires: uv, Docker (for cross-compilation of C deps like psycopg).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT/lambda-packages"
OUT_ZIP="$OUT_DIR/suepercharge.zip"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

mkdir -p "$OUT_DIR"

echo "[build_lambda] Exporting requirements..."
uv export --no-dev --no-hashes --format requirements-txt > "$BUILD_DIR/requirements.txt"

echo "[build_lambda] Installing into $BUILD_DIR/package (linux arm64, py3.12)..."
docker run --rm --platform linux/arm64 \
  -v "$BUILD_DIR:/build" \
  public.ecr.aws/sam/build-python3.12:latest \
  bash -c "pip install --target=/build/package -r /build/requirements.txt"

echo "[build_lambda] Adding source..."
cp -R "$ROOT"/{agents,clients,prompts,compliance.py,db.py,models.py,storage.py,handoff.py,load_env.py} "$BUILD_DIR/package/"

echo "[build_lambda] Zipping -> $OUT_ZIP"
(cd "$BUILD_DIR/package" && zip -qr "$OUT_ZIP" .)

echo "[build_lambda] Done: $(du -h "$OUT_ZIP" | cut -f1)"
