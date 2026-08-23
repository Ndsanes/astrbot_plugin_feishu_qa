#!/usr/bin/env sh
# 打包 astrbot_plugin_feishu_qa 为可上传 AstrBot 的 zip。
# - 把工作区根部的 astrbot_lark_kit/ 作为子包 vendored 进插件目录;
# - 下载官方 lark-cli release(linux-amd64/arm64)进 vendor/,实现"适配器携带
#   lark-cli",运行时不依赖宿主机安装(见 astrbot_lark_kit.cli.find_bundled_cli)。
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKSPACE_ROOT="$(cd "$PROJECT_DIR/.." && pwd)"
PLUGIN_DIR_NAME="$(basename "$PROJECT_DIR")"
KIT_SRC="$WORKSPACE_ROOT/astrbot_lark_kit"
DIST_DIR="$PROJECT_DIR/dist"
OUTPUT_NAME="${1:-${PLUGIN_DIR_NAME}.zip}"
OUTPUT_PATH="$DIST_DIR/$OUTPUT_NAME"

CLI_VERSION="${LARK_CLI_VERSION:-1.0.85}"
RELEASE_BASE="https://github.com/larksuite/cli/releases/download/v${CLI_VERSION}"

if ! command -v zip >/dev/null 2>&1; then
  echo "error: 'zip' command not found"
  exit 1
fi
if [ ! -d "$KIT_SRC" ]; then
  echo "error: kit source not found: $KIT_SRC"
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

# kit 作为子包 vendored 进插件目录。
rsync -a \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  --exclude 'tests/' \
  "$KIT_SRC/" "$TMP_DIR/$PLUGIN_DIR_NAME/astrbot_lark_kit/"

# 携带官方 lark-cli(linux-amd64/arm64),带 sha256 校验。
CHECKSUMS="$TMP_DIR/checksums.txt"
curl -fsSL --http1.1 --retry 3 "$RELEASE_BASE/checksums.txt" -o "$CHECKSUMS"
for plat in linux-amd64 linux-arm64; do
  archive="lark-cli-${CLI_VERSION}-${plat}.tar.gz"
  expected=$(grep " ${archive}\$" "$CHECKSUMS" | awk '{print $1}')
  if [ -z "$expected" ]; then
    echo "error: no checksum for $archive"
    exit 1
  fi
  curl -fsSL --http1.1 --retry 3 "$RELEASE_BASE/$archive" -o "$TMP_DIR/$archive"
  echo "$expected  $TMP_DIR/$archive" | shasum -a 256 -c - >/dev/null
  mkdir -p "$TMP_DIR/$PLUGIN_DIR_NAME/vendor/lark-cli/$plat"
  tar -xzf "$TMP_DIR/$archive" -C "$TMP_DIR/$PLUGIN_DIR_NAME/vendor/lark-cli/$plat" lark-cli
  chmod +x "$TMP_DIR/$PLUGIN_DIR_NAME/vendor/lark-cli/$plat/lark-cli"
  echo "bundled lark-cli $plat ($CLI_VERSION)"
done

(
  cd "$TMP_DIR"
  rm -f "$OUTPUT_PATH"
  zip -qr "$OUTPUT_PATH" "$PLUGIN_DIR_NAME"
)

echo "package created: $OUTPUT_PATH"
echo "upload this zip in AstrBot plugin install page."
