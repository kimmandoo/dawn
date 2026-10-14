# Dawn — 초간단 영상/음원 다운로더 (yt-dlp 기반)

**링크를 붙여넣으면 바로 다운로드.** 버튼도 필요 없습니다. Windows / macOS 지원.

## 원클릭 실행

## 다운로드 (빌드 없이 바로 사용)

[Releases](../../releases) 에서 받으세요:

| OS | 파일 |
|---|---|
| Windows x64 | `Dawn-windows-x64.zip` → 압축 해제 후 `Dawn.exe` 실행 |
| macOS Apple Silicon (M1~M4) | `Dawn-macos-arm64.zip` → `Dawn.app` |

> macOS 첫 실행: 앱 우클릭 → **열기** (미서명 앱 Gatekeeper 통과). ffmpeg·deno 내장.

| OS | 방법 |
|---|---|
| macOS | `Dawn.command` 더블클릭 (빌드 후엔 `dist/Dawn.app` 자동 실행) |
| Windows | `Dawn.bat` 더블클릭 (빌드 후엔 `dist\Dawn.exe` 자동 실행) |

첫 실행에만 가상환경+의존성 자동 설치 (인터넷 필요, 1–2분). ffmpeg는 내장 — 별도 설치 불필요.

## 기능

- **🎬 영상 + 음원**: MP4, 화질 선택 (최고/1080p/720p/480p), 제목·아티스트·날짜·설명·챕터 메타데이터 자동 삽입
- **🎵 음원만**: MP3, 음질 선택 (최고 VBR / 320 / 192 / 128 kbps), **고해상도 정사각형 앨범아트**(YouTube maxres 우선 → 중앙 크롭) + 제목/아티스트/앨범 태그 자동 삽입
- **성인 인증·로그인 필요 영상**: 브라우저 쿠키 자동 사용 (Chrome/Edge/Firefox/Safari/Brave/Opera/Vivaldi/Whale) 또는 `cookies.txt`
- **재생목록 링크**: 감지 시 "전체 받기 / 이 영상만" 선택창 — "항상 전체 받기" 옵션으로 생략 가능
- 여러 링크 한 번에 붙여넣기 (공백/줄바꿈 구분), 개별 취소, 진행률/속도/남은시간
- 차단된 네트워크에선 자동으로 다른 클라이언트(android/ios/tv)로 재시도

## 사용법

1. 링크 붙여넣기 → 0.7초 후 자동 시작 (또는 Enter)
2. 목록 항목 더블클릭 → 저장 폴더 열기

## 성인 인증 사용법

1. `⚙ 성인 인증 · 쿠키 · 재생목록` 펼치기
2. 로그인된 브라우저 선택 (예: Chrome에서 YouTube 로그인 상태)
3. **다운로드 중 해당 브라우저는 완전히 종료** (쿠키 DB 잠금 방지)
4. 또는 확장 프로그램으로 내보낸 `cookies.txt` 파일 선택

## 빌드 (배포용)

```bash
./build.sh                       # macOS -> dist/Dawn.app
# Windows (PowerShell/CMD):
#   .venv\Scripts\python -m PyInstaller dawn.spec --noconfirm   -> dist\Dawn.exe
```

## 구조

| 파일 | 역할 |
|---|---|
| `dawn/downloader.py` | yt-dlp 엔진, 쿠키, MP3 태그+앨범아트, 영상 메타데이터, 클라이언트 재시도 |
| `dawn/app.py` | PySide6 UI, 자동 다운로드, 작업 큐, 진행률 |
| `dawn.spec` | PyInstaller 빌드 정의 |
| `Dawn.command` / `Dawn.bat` | 원클릭 런처 |
