# Humanoid Robot

> **Human-as-a-Robot**: AI가 두뇌, 사람이 신체가 되는 시스템

AI 두뇌(Claude Code CLI)가 스마트 글래스를 쓴 사람을 원격 제어하는 풀스택 시스템.
음성/텍스트/이미지 입력을 받아 실시간으로 판단하고, Instagram DM을 통해 글래스 HUD에 지시를 표시합니다.

```
Ray-Ban Meta Glasses ↔ Bluetooth ↔ Phone PWA ↔ WSS ↔ FastAPI Server ↔ Claude Code CLI
                                                          ↕               ↕
                                                    Gemini Vision    LeRobot SO-101
                                                    Gemini TTS       Google Calendar
                                                    Instagram DM
```

## Features

### Core
- **Claude Code Bridge** — CLI subprocess를 WebSocket으로 래핑, 스트리밍 JSON 응답
- **Dual Mode UI** — Claude 대화 모드 + 터미널(tmux) 직접 제어 모드
- **Voice I/O** — Web Speech API (STT) + Gemini TTS, 코드블록 자동 생략

### Human-as-a-Robot
- **AI Brain** — Claude Code가 두뇌 역할, 선제적 판단과 지시
- **Gemini Vision** — 카메라/스크린샷 이미지 분석 (글래스 시점)
- **Instagram DM Display** — Meta Ray-Ban 글래스 HUD에 텍스트 표시
- **Google Calendar** — 일정 컨텍스트 자동 주입, 선제적 알림

### Robot Control
- **LeRobot SO-101** — 6-DOF follower arm 실시간 제어
- **Dual Camera** — 전면/상단 카메라 스트리밍
- **Named Poses** — 사전 정의된 자세 (home, wave, point 등)

### Monitoring
- **Cost Dashboard** — 메시지별 토큰 수, USD/KRW 비용 추적
- **Robot Monitor** — 관절 상태, 카메라 피드 실시간 모니터링
- **PWA** — 홈화면 설치, 오프라인 지원

## Architecture

```
server/
├── main.py              # FastAPI + WebSocket routes + static files
├── claude_bridge.py     # Claude CLI session manager (streaming JSON)
├── ai_brain.py          # Robot orchestrator (Claude + Gemini + Instagram)
├── gemini_tts.py        # Gemini API: TTS + STT
├── instagram.py         # Instagram Graph API webhook handler
├── gcal.py              # Google Calendar integration
├── robot_bridge.py      # LeRobot SO-101 controller
├── terminal.py          # tmux session wrapper
├── agents.py            # Multi-agent persona store
├── group_chat.py        # Multi-agent orchestration
├── cost_logger.py       # NDJSON token/cost logger
├── tool_executor.py     # Tool execution utilities
└── config.py            # Dataclass-based configuration

static/
├── index.html           # Main PWA (Claude + Terminal tabs)
├── app.js               # WebSocket client
├── speech.js            # Web Speech API
├── dashboard.html/js    # Cost tracking dashboard
├── monitor.html         # Robot state monitor
├── human-robot.html     # Human-as-a-robot control UI
└── style.css            # Mobile dark mode
```

## Setup

### Prerequisites
- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)
- FFmpeg (optional, for audio processing)

### Install

```bash
git clone https://github.com/devwon/humanoid-robot.git
cd humanoid-robot
pip install -r requirements.txt
```

### Environment Variables

```bash
# Required
export GEMINI_API_KEY="your-gemini-api-key"

# Optional — Instagram DM display
export INSTAGRAM_VERIFY_TOKEN="webhook-verify-token"
export INSTAGRAM_PAGE_TOKEN="ig-page-token"
export INSTAGRAM_APP_SECRET="ig-app-secret"
export INSTAGRAM_FB_PAGE_TOKEN="fb-page-token"
export GLASSES_USERNAME="instagram-username"

# Optional — Google Calendar
export GCAL_CREDENTIALS_PATH="/path/to/gcal_credentials.json"

# Optional — Claude CLI path (default: ~/.local/bin/claude)
export CLAUDE_CLI_PATH="/path/to/claude"
```

### Google Calendar (optional)

```bash
# 1. Download OAuth credentials from Google Cloud Console
# 2. Set the path
export GCAL_CREDENTIALS_PATH="/path/to/credentials.json"

# 3. Run one-time auth
python3 scripts/gcal_auth.py
```

### Run

```bash
./scripts/start.sh
```

자체서명 SSL 인증서를 자동 생성하고, QR 코드를 표시합니다.
폰 브라우저로 QR을 스캔해 접속하세요.

## How It Works

### Human-as-a-Robot Mode

1. 사용자가 Ray-Ban Meta 글래스를 착용
2. 글래스 카메라로 촬영한 이미지가 Gemini Vision으로 분석
3. Claude Code (AI 두뇌)가 상황을 판단하고 행동 결정
4. Instagram DM으로 글래스 HUD에 지시 텍스트 표시
5. Gemini TTS로 음성 안내 동시 제공
6. 사용자(로봇 신체)가 AI의 지시에 따라 행동

### Remote CLI Bridge Mode

1. 폰 PWA에서 음성/텍스트 입력
2. WebSocket으로 Mac 서버에 전달
3. Claude Code CLI가 코드 작성, 파일 수정, 명령 실행
4. 스트리밍 응답을 실시간 표시 + TTS 출력

## Documentation

프로젝트의 상세 컨셉과 아키텍처는 [Human_as_a_Robot.pdf](./Human_as_a_Robot.pdf)를 참조하세요.

## License

MIT
