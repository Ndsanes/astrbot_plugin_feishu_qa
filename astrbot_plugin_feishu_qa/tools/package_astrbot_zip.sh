#!/usr/bin/env sh
# 打包 astrbot_plugin_feishu_qa 为可上传 AstrBot 的 zip。
# - 零第三方依赖:飞书访问全部经 lark_cli 平台适配器(astrbot_plugin_lark_cli_platform)
#   的 gateway 转发,本插件不携带 lark-cli、kit 或任何凭据/登录态。
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PLUGIN_DIR_NAME="$(basename "$PROJECT_DIR")"
DIST_DIR="$PROJECT_DIR/dist"
OUTPUT_NAME="${1:-${PLUGIN_DIR_NAME}.zip}"
OUTPUT_PATH="$DIST_DIR/$OUTPUT_NAME"


if ! command -v zip >/dev/null 2>&1; then
  echo "error: 'zip' command not found"
  exit 1
fi
TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

mkdir -p "$DIST_DIR"
mkdir -p "$TMP_DIR/$PLUGIN_DIR_NAME"

# 复制插件文件到临时目录，排除运行时和开发产物。
rsync -a \
  --exclude '.git/' \
  --exclude '.github/' \
  --exclude '.venv/' \
  --exclude '.ruff_cache/' \
  --exclude '.pytest_cache/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  --exclude '.env' \
  --exclude 'dist/' \
  --exclude 'tests/' \
  --exclude 'tools/__pycache__/' \
  --exclude 'vendor/' \
  "$PROJECT_DIR/" "$TMP_DIR/$PLUGIN_DIR_NAME/"


(
  cd "$TMP_DIR"
  rm -f "$OUTPUT_PATH"
  zip -qr "$OUTPUT_PATH" "$PLUGIN_DIR_NAME"
)

echo "package created: $OUTPUT_PATH"
echo "upload this zip in AstrBot plugin install page."
