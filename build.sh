#!/usr/bin/env bash
# Dawn 빌드: macOS -> dist/Dawn.app, Windows(이 스크립트 대신 아래 명령) -> dist/Dawn.exe
#   Windows: python scripts/fetch_bypass.py && python -m PyInstaller dawn.spec --noconfirm
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q -r requirements.txt
./.venv/bin/python scripts/fetch_bypass.py
./.venv/bin/python -m PyInstaller dawn.spec --noconfirm --clean
echo "==> dist/ 확인"
ls dist/
