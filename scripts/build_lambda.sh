#!/usr/bin/env bash
# Build build/lambda.zip: runtime deps (Linux wheels for the Lambda arch) + src package.
set -euo pipefail

ARCH="${LAMBDA_ARCH:-arm64}"           # arm64 | x86_64 — must match Terraform
PY_VERSION="${LAMBDA_PYTHON:-3.13}"

case "$ARCH" in
  arm64)  PLATFORM="aarch64-manylinux2014" ;;
  x86_64) PLATFORM="x86_64-manylinux2014" ;;
  *) echo "Unsupported arch: $ARCH" >&2; exit 1 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
PKG="$BUILD/package"

rm -rf "$BUILD"
mkdir -p "$PKG"

# boto3/botocore (and their deps) ship with the Lambda Python runtime; don't bundle them.
RUNTIME_PROVIDED='^(boto3|botocore|s3transfer|jmespath|python-dateutil|six|urllib3)=='
uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt \
  | grep -Ev "$RUNTIME_PROVIDED" > "$BUILD/requirements.txt"

uv pip install \
  --quiet \
  --target "$PKG" \
  --python-platform "$PLATFORM" \
  --python-version "$PY_VERSION" \
  --only-binary=:all: \
  --no-deps \
  -r "$BUILD/requirements.txt"

cp -R "$ROOT/src/calculator" "$PKG/"
find "$PKG" -type d -name "__pycache__" -prune -exec rm -rf {} +

# Fixed timestamps so identical inputs produce an identical zip (stable source_code_hash).
find "$PKG" -exec touch -t 202001010000 {} +
(cd "$PKG" && find . -type f | LC_ALL=C sort | zip -q -X -@ "$BUILD/lambda.zip")

echo "Built $BUILD/lambda.zip ($(du -h "$BUILD/lambda.zip" | cut -f1)) for $ARCH / python$PY_VERSION"
