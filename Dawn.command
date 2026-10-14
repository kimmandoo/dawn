#!/bin/bash
# Dawn — 더블클릭 실행 (macOS). 첫 실행 시 환경 자동 설치.
cd "$(dirname "$0")" || exit 1
PY=./.venv/bin/python

if [ ! -x "$PY" ]; then
  echo "첫 실행: 환경 준비 중… (1-2분)"
  python3 -m venv .venv || { echo "Python3 필요: https://python.org"; read -r; exit 1; }
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r requirements.txt || { echo "설치 실패 — 인터넷 연결 확인"; read -r; exit 1; }
fi

# deno: yt-dlp의 JS 챌린지(YouTube 봇 차단 우회)에 사용 — 없으면 자동 설치, 실패해도 앱은 동작
export PATH="$HOME/.deno/bin:/opt/homebrew/bin:$PATH"
if ! command -v deno >/dev/null 2>&1; then
  echo "deno 설치 중 (YouTube 안정성 향상)…"
  if command -v brew >/dev/null 2>&1; then
    brew install deno -q 2>/dev/null || curl -fsSL https://deno.land/install.sh | sh -s -- -y -q >/dev/null 2>&1 || true
  else
    curl -fsSL https://deno.land/install.sh | sh -s -- -y -q >/dev/null 2>&1 || true
  fi
fi

# 이미 빌드된 앱이 있으면 그걸 실행
if [ -d "dist/Dawn.app" ]; then
  open "dist/Dawn.app"
else
  "$PY" main.py
fi
