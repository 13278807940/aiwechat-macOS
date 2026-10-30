#!/bin/bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "此脚本需要 macOS。Windows 请运行 scripts/Build-MacFromWindows.ps1。" >&2
  exit 2
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
export MACOSX_DEPLOYMENT_TARGET=12.0
export MAC_BUILD_ARCH="${MAC_BUILD_ARCH:-$(uname -m)}"
"$PYTHON_BIN" -c 'import sys, platform; assert sys.version_info[:2] == (3,11), "需要 Python 3.11"; assert sys.platform == "darwin"; print("构建架构:", platform.machine())'
if [[ ! -d .venv ]]; then
  "$PYTHON_BIN" -m venv .venv
fi
PYTHON="$PROJECT_ROOT/.venv/bin/python"
"$PYTHON" -c 'import os,sys,platform; assert sys.version_info[:2] == (3,11), "现有 .venv 不是 Python 3.11，请使用新的构建目录"; assert platform.machine() == os.environ["MAC_BUILD_ARCH"], "现有 Python/.venv 架构与目标不一致"'
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r requirements-macos.txt
"$PYTHON" -m compileall -q main.py mac_assistant
"$PYTHON" -m unittest discover -s tests -p 'test_*.py'
if command -v node >/dev/null 2>&1; then
  node tests/test_bridge.cjs
fi
"$PYTHON" -m PyInstaller --noconfirm --clean packaging/ZhongBenMac.spec

APP_PATH="$PROJECT_ROOT/dist/中犇微信助手.app"
[[ -d "$APP_PATH" ]] || { echo "未生成 .app" >&2; exit 3; }
codesign --verify --deep --strict "$APP_PATH"
STAMP="$(date +%Y%m%d-%H%M%S)"
BASE="中犇微信助手-2.0.3-macOS12以上-${MAC_BUILD_ARCH}-${STAMP}"
mkdir -p release
"$PYTHON" scripts/verify_macos_bundle.py --app "$APP_PATH" --arch "$MAC_BUILD_ARCH" \
  --min-os 12.0 --version 2.0.3 --output "release/${BASE}.兼容性校验.json"
"$PYTHON" - "$APP_PATH" "$PROJECT_ROOT/release/${BASE}.启动自检.json" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

app, report = Path(sys.argv[1]), Path(sys.argv[2])
subprocess.run([str(app / "Contents/MacOS/ZhongBenAssistant"), "--smoke-test", str(report)],
               check=True, timeout=75)
result = json.loads(report.read_text(encoding="utf-8"))
assert result.get("ok") and result.get("webengine") and result.get("frozen"), result
print("Frozen app startup and offline QtWebEngine renderer: PASS")
PY
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/zhongben-dmg.XXXXXX")"
trap 'if [[ -n "${STAGING:-}" && -d "$STAGING" ]]; then /bin/rm -rf -- "$STAGING"; fi' EXIT
ditto "$APP_PATH" "$STAGING/中犇微信助手.app"
ln -s /Applications "$STAGING/Applications"
hdiutil create -volname "中犇微信助手" -srcfolder "$STAGING" -format UDZO -ov "release/${BASE}.dmg"
ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "release/${BASE}.app.zip"
shasum -a 256 "release/${BASE}.dmg" "release/${BASE}.app.zip" > "release/${BASE}.sha256.txt"
printf 'macOS 12.0+ / %s\n构建时间: %s\n签名: %s\n旧系统12/13: CoreGraphics单窗口截图；14及以上: ScreenCaptureKit。\n兼容性检查和启动自检通过不代表已在macOS12/13完成微信实机验收。\n' "$MAC_BUILD_ARCH" "$STAMP" "${MAC_CODESIGN_IDENTITY:-ad-hoc，本地测试签名，未公证}" > "release/${BASE}.说明.txt"
echo "构建完成: $PROJECT_ROOT/release/${BASE}.dmg"
echo "构建成功不代表已完成微信实机识别、权限和发送测试。"
