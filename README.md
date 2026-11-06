<p align="center">
  <img src="dawn/mascot.png" width="120" alt="Dawn 캐릭터">
</p>

<h1 align="center">Dawn</h1>

<p align="center">영상과 음원을 간편하게 저장하는 데스크톱 다운로더</p>

<p align="center">
  <a href="https://github.com/kimmandoo/dawn/releases/latest">다운로드</a> ·
  <a href="#사용법">사용법</a> ·
  <a href="#개발">개발</a> ·
  <a href="https://github.com/kimmandoo/dawn/issues">문제 제보</a> ·
  <a href="https://github.com/kimmandoo/dawn/actions/workflows/release.yml">빌드 상태</a>
</p>

Dawn은 [yt-dlp](https://github.com/yt-dlp/yt-dlp)와 [PySide6](https://doc.qt.io/qtforpython-6/) 기반의 Windows·macOS 앱입니다. 링크를 붙여넣고 형식과 저장 위치를 선택하면 영상 또는 음원을 다운로드할 수 있습니다.

## 다운로드

[최신 릴리스](https://github.com/kimmandoo/dawn/releases/latest)에서 운영체제에 맞는 파일을 받으세요. 공식 배포본에는 FFmpeg, Deno, 운영체제별 DPI 우회 엔진이 포함됩니다. Python을 별도로 설치할 필요는 없습니다.

| 플랫폼 | 다운로드 | 실행 |
| --- | --- | --- |
| Windows x64 | [Dawn-windows-x64.zip](https://github.com/kimmandoo/dawn/releases/latest/download/Dawn-windows-x64.zip) | 압축 해제 후 `Dawn.exe` |
| macOS Apple Silicon · ARM64 | [Dawn-macos-arm64.zip](https://github.com/kimmandoo/dawn/releases/latest/download/Dawn-macos-arm64.zip) | 압축 해제 후 `Dawn.app` |

- 배포 앱은 서명되지 않았습니다. macOS에서 실행이 차단되면 시스템의 **개인정보 보호 및 보안** 안내를 확인하세요. 보안 기능을 전체적으로 해제하지 마세요.
- macOS Intel과 Linux용 공식 배포 파일은 제공하지 않습니다. 소스 실행 및 해당 환경의 우회 엔진 지원은 공식 배포 지원 범위와 별개입니다.

## 주요 기능

| 기능 | 설명 |
| --- | --- |
| 영상 저장 | MP4, 최고 화질·1080p·720p·480p 선택 |
| 음원 추출 | MP3·M4A·FLAC, 음질 선택, 메타데이터와 정사각형 앨범아트 |
| 다운로드 관리 | 여러 링크 입력, 동시 작업 1~4개, 진행률·속도·남은 시간, 취소·재시도 |
| 형식별 저장 기록 | 같은 링크의 영상과 음원을 각각 저장, MP3·M4A·FLAC 기록 분리 |
| 재생목록·구간 | 전체 재생목록 또는 개별 영상 선택, 시작·종료 시간 지정 |
| 로그인·자막 | 브라우저 쿠키 또는 `cookies.txt`, 한국어·영어 자막 저장 |
| 네트워크 설정 | 선택형 DPI 우회, 수동 프록시, 실행 중인 HTTP 프록시 자동 감지 |

지원 사이트와 추출 가능 여부는 yt-dlp 및 각 사이트의 정책에 따라 달라집니다. 원본에 없는 화질·자막·메타데이터를 생성하지 않습니다.

## 사용법

1. 링크를 입력하거나 창으로 끌어옵니다. 여러 링크는 공백이나 줄바꿈으로 구분합니다.
2. **영상 / 음원**, 화질·음질, **저장 위치**를 선택합니다.
3. **다운로드**를 누릅니다. 완료 후 **폴더 열기**로 결과를 확인합니다.

기본값은 수동 시작입니다. 붙여넣기 자동 시작과 클립보드 감시는 **설정 → 다운로드**에서 선택할 수 있습니다. 설정은 다음 실행에도 유지됩니다.

### 중복·재시도 동작

- 같은 저장 위치·링크·형식의 작업이 진행 중이면 해당 항목으로 이동하고 입력 링크를 유지합니다.
- 같은 링크라도 영상과 음원은 별도 작업입니다. 서로 다른 음원 형식도 각각 받을 수 있습니다.
- 저장 기록이 있는 형식은 **건너뜀**으로 표시합니다. 다시 저장하려면 해당 항목의 **다시 받기**를 누르세요.
- 실패하거나 취소한 작업은 **다시 시도**할 수 있습니다. 긴 오류 메시지는 항목에 마우스를 올려 확인합니다.
- **완료 항목 정리**는 완료·건너뜀·취소 항목만 목록에서 제거합니다. 실제 파일과 실패 항목은 삭제하지 않습니다.

기존 `.dawn-archive.txt`에는 형식 정보가 없어 새 형식별 기록으로 합치지 않습니다. 기존 영상·음원 파일은 유지됩니다.

### 키보드

| 동작 | Windows | macOS |
| --- | --- | --- |
| 링크 입력란 이동 | `Ctrl+L` | `⌘+L` |
| 붙여넣기 | `Ctrl+V` | `⌘+V` |
| 입력한 링크 다운로드 | 링크 입력란에서 `Enter` | 링크 입력란에서 `Enter` |

### 로그인과 쿠키

로그인이 필요한 콘텐츠는 **설정 → 로그인**에서 로그인된 브라우저 또는 `cookies.txt`를 선택합니다. 브라우저 쿠키 데이터베이스가 잠겨 있다면 브라우저를 종료한 뒤 다시 시도하세요. 쿠키 접근 가능 여부는 운영체제와 브라우저 버전에 따라 달라질 수 있습니다.

> 쿠키 파일에는 계정 접근 정보가 포함될 수 있습니다. 저장소·이슈·공개 로그에 업로드하지 마세요. 자신에게 접근 권한이 있는 콘텐츠에만 사용하세요.

## 네트워크와 DPI 우회

**설정 → 네트워크 → DPI 검열 우회 사용**에서 활성화합니다. 기본값은 꺼짐입니다.

| 환경 | 엔진·방식 | 권한과 영향 범위 |
| --- | --- | --- |
| Windows x64 | [GoodbyeDPI 0.2.2](https://github.com/ValdikSS/GoodbyeDPI/releases/tag/0.2.2)·WinDivert, 패킷 분할·1.1.1.1로 UDP DNS 전달 | 관리자 권한 필요. 실행 중 다른 앱의 트래픽에도 영향을 줄 수 있음 |
| macOS / Linux ARM64·x86_64 소스 환경 | [SpoofDPI 1.5.5](https://github.com/xvzc/spoofdpi/releases/tag/v1.5.5), 로컬 HTTP 프록시·TLS SNI 분할·1.1.1.1 DoH | 관리자 권한 불필요. Dawn의 다운로드 요청에 적용 |

- **Windows:** 설정창의 **관리자 권한으로 다시 열기**를 사용합니다. 진행 중인 작업은 먼저 완료하거나 취소해야 합니다. 권한 요청을 취소하면 기존 창이 유지됩니다. 초기 필터 확인에 약 21초가 걸릴 수 있습니다.
- Windows에서는 동시 작업들이 엔진을 공유하며 마지막 작업이 끝나면 종료합니다. macOS/Linux에서는 작업별 프록시를 종료합니다. 앱 종료 시에도 앱이 시작한 엔진을 정리합니다.
- DPI 우회는 수동 프록시·자동 감지보다 우선합니다. 끄면 기존 프록시 설정을 다시 사용합니다. 영상·음원과 앨범아트 요청에는 같은 연결 경로를 적용합니다.
- 엔진 시작에 실패하면 이유를 표시합니다. 요청한 우회를 조용히 끄고 직접 연결로 전환하지 않습니다.
- macOS/Linux의 시스템 프록시 설정은 변경하지 않습니다. 별도 SpoofDPI 인스턴스의 상태 파일이 감지되면 시작을 중단합니다. 외부 인스턴스는 정상 종료하고 동시 실행을 피하세요.

**VPN이 아니며 우회를 보장하지 않습니다.** IP 자체 차단, 계정·연령 확인, 유료 콘텐츠 접근 권한은 별개입니다. `geo-bypass`도 DPI 우회와 다른 yt-dlp 옵션입니다. 실제 ISP 차단 환경은 별도 검증이 필요합니다.

## 다운로드 속도

동시 작업 수는 **설정 → 다운로드**에서 조절합니다. 내려받은 표지는 미리보기와 앨범아트에 재사용하며, 변환용 임시 파일은 작업별로 분리합니다.

사이트의 속도 제한, 회선, 프록시·우회 경로, 원본 형식에 따라 속도가 달라집니다. 다운로드 이후의 변환·태그 처리 시간은 네트워크 전송 시간과 별개이며, 동시 작업 수를 늘려도 항상 빨라지는 것은 아닙니다.

## 개발

기준 환경은 **Python 3.12**입니다. 의존성은 [`requirements.txt`](requirements.txt)에서 관리합니다. YouTube의 JavaScript 챌린지 처리를 위해 [Deno](https://docs.deno.com/runtime/getting_started/installation/)를 설치하고 `PATH`에 등록하는 것을 권장합니다.

```bash
git clone https://github.com/kimmandoo/dawn.git
cd dawn
```

### macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

### Windows · PowerShell

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py
```

`Dawn.command`와 `Dawn.bat`도 사용할 수 있습니다. 런처는 필요한 개발 환경을 준비하고, `dist`에 빌드 결과가 있으면 소스 대신 해당 앱을 실행합니다. 소스 변경을 바로 확인할 때는 위의 `main.py` 실행 명령을 사용하세요.

소스 환경에서 DPI 우회를 사용하려면 가상환경의 Python으로 `scripts/fetch_bypass.py`를 먼저 실행합니다. 엔진과 라이선스는 `vendor`에 저장됩니다.

### 검사

macOS에서는 다음을 실행합니다. Windows에서는 `.venv/bin/python`을 `.venv\Scripts\python`으로 바꿉니다.

```bash
.venv/bin/python -m unittest discover -s tests -v
```

검사는 로컬 HTTP 서버와 FFmpeg를 사용합니다. 형식별 저장 기록, 음원·영상 동시 다운로드, 기존 파일 보존, 명시적 재다운로드, 표지 재사용, CLI 인코딩을 확인합니다.

### 빌드

자체 배포본에 Deno를 포함하려면 빌드 전에 설치하고 `PATH`에 등록하세요.

**macOS**

```bash
./build.sh
# dist/Dawn.app
```

**Windows · PowerShell**

```powershell
.venv\Scripts\python scripts\fetch_bypass.py
.venv\Scripts\python -m PyInstaller dawn.spec --noconfirm
# dist\Dawn.exe
```

우회 엔진은 빌드 준비 단계에서 공식 릴리스의 SHA256을 검증하고 라이선스와 함께 묶습니다. 앱 실행 중에는 자동으로 내려받지 않습니다.

빌드한 앱의 실제 다운로드·변환·엔진 기동을 확인하려면 다음을 실행합니다. Windows에서는 관리자 권한이 필요합니다.

```bash
.venv/bin/python scripts/smoke_packaged.py --dpi-bypass
```

[릴리스 워크플로](.github/workflows/release.yml)는 Windows x64와 macOS 15 ARM64에서 빌드하고 필수 검사를 통과한 패키지를 배포합니다. 외부 사이트 검사는 네트워크·사이트 정책의 영향을 받으므로 로컬 패키지 검사와 구분합니다.

### 캐릭터와 아이콘

원본은 [`dawn/mascot.svg`](dawn/mascot.svg)입니다. 수정 후 다음 명령으로 화면용 PNG와 Windows ICO·macOS ICNS를 함께 생성합니다. 배포 앱에도 적용하려면 다시 빌드해야 합니다.

```bash
.venv/bin/python scripts/render_icons.py
```

### 프로젝트 구조

| 경로 | 역할 |
| --- | --- |
| `dawn/app.py` | 화면, 설정, 작업 큐, 진행 상태 |
| `dawn/downloader.py` | 다운로드·변환, 저장 기록, 메타데이터·앨범아트 |
| `dawn/bypass.py` | 우회 엔진 탐색, 권한, 실행·종료 관리 |
| `dawn/mascot.svg` | 캐릭터 원본 |
| `scripts/` | 엔진 준비, 아이콘 생성, 패키지 검사 |
| `tests/` | 실제 로컬 다운로드를 사용하는 회귀 검사 |
| `dawn.spec` | PyInstaller 패키징 설정 |
| `.github/workflows/release.yml` | 크로스플랫폼 릴리스 빌드 |

## 기여와 문제 제보

[Issues](https://github.com/kimmandoo/dawn/issues)에 운영체제·아키텍처, 앱 버전, 재현 단계, 오류 메시지를 함께 남겨 주세요. 비공개 링크, 쿠키, 계정 정보는 제거하세요.

Pull Request에는 변경 이유와 확인한 동작을 적어 주세요. 동작 변경 시 관련 검사와 문서를 함께 갱신하고, UI 변경은 화면과 작은 창에서의 표시를 확인해 주세요.

## 라이선스와 의존성

프로젝트 자체의 라이선스는 아직 별도 `LICENSE` 파일로 지정되지 않았습니다. 재배포·재사용 전 조건을 확인해 주세요. 공개 저장소라는 사실만으로 별도의 라이선스 권한이 부여되지는 않습니다.

Dawn은 다음 프로젝트를 사용하며, 각각의 라이선스와 배포 조건이 적용됩니다.

- [yt-dlp](https://github.com/yt-dlp/yt-dlp), [yt-dlp-ejs](https://github.com/yt-dlp/ejs)
- [PySide6 / Qt for Python](https://doc.qt.io/qtforpython-6/licenses.html)
- [FFmpeg](https://ffmpeg.org/legal.html), [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)
- [Deno](https://github.com/denoland/deno), [Mutagen](https://github.com/quodlibet/mutagen), [Pillow](https://github.com/python-pillow/Pillow)
- [GoodbyeDPI](https://github.com/ValdikSS/GoodbyeDPI), [WinDivert](https://reqrypt.org/windivert.html), [SpoofDPI](https://github.com/xvzc/spoofdpi)

동봉한 우회 엔진의 라이선스와 저작권 고지는 배포 파일에 유지됩니다. 다운로드 대상의 이용약관과 저작권, 적용되는 법률을 준수하세요.
