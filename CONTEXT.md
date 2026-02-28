# Remote CLI Bridge - 세션 컨텍스트

## 프로젝트 개요
원격에서 Claude Code와 음성/텍스트로 소통하기 위한 브릿지 시스템.
음성/텍스트 입력 → 폰 브라우저(PWA) → WebSocket → Mac FastAPI 서버 → Claude Code CLI subprocess → 응답 스트리밍 → TTS 출력.
Cloudflare Tunnel을 통해 외부 네트워크에서도 접근 가능 (remote.devwon.ai).

## 아키텍처
```
[Ray-Ban Meta 글래스] ↔ Bluetooth ↔ [폰 브라우저 PWA] ↔ WSS ↔ [Mac FastAPI 서버] ↔ subprocess ↔ [Claude Code CLI]
```

## 프로젝트 구조
```
~/remote-cli-bridge/
├── server/
│   ├── __init__.py
│   ├── main.py              # FastAPI + WebSocket + 정적파일 서빙 + 환율 API
│   ├── claude_bridge.py     # Claude CLI subprocess 래퍼, 세션 관리, 스트리밍
│   ├── terminal.py          # tmux 세션 제어 (list/capture/send/create/kill)
│   └── config.py            # 설정 (호스트, 포트, SSL, CLI 경로)
├── static/
│   ├── index.html           # PWA 메인 (Claude모드 + Terminal모드 탭)
│   ├── app.js               # WSClient, App 클래스 (듀얼모드)
│   ├── speech.js            # Web Speech API (STT + TTS)
│   ├── style.css            # 다크모드 모바일 UI
│   ├── manifest.json        # PWA 매니페스트
│   ├── sw.js                # 서비스 워커
│   └── icon.svg
├── certs/
│   ├── cert.pem             # 자체서명 SSL 인증서
│   └── key.pem
├── scripts/
│   └── start.sh             # 실행 스크립트 (인증서 생성 + QR코드 + uvicorn)
├── logs/
└── requirements.txt         # fastapi, uvicorn, websockets, pydantic, qrcode
```

## 구현 완료 기능
1. **Claude 모드**: 텍스트/음성 입력 → Claude Code CLI 스트리밍 응답 → TTS 출력
2. **Terminal 모드**: tmux 세션 직접 제어 (명령 전송, 출력 캡처, 세션 생성/삭제)
3. **토큰/비용 표시**: 메시지별 토큰 수, USD 비용, KRW 환산 (open.er-api.com 1시간 캐싱)
4. **PWA**: 홈화면 추가, 오프라인 캐싱
5. **음성 I/O**: 한국어/영어 STT, 문장 단위 TTS, 코드블록 감지 생략
6. **launchd 서비스**: 자동 시작/재시작 (`~/Library/LaunchAgents/com.devwon.remote-cli-bridge.plist`)

## 현재 상태
- 서버 실행 중: `https://0.0.0.0:8000` (SSL)
- tmux 세션: `ai-team`, `work`, `test-session`
- `~/.tmux.conf`에 `set-environment -gr CLAUDECODE` 추가 (중첩 실행 방지 해제)
- 폰 Chrome에서 접속 확인 완료

## 주요 기술 결정
- Claude CLI: `-p` + `--output-format stream-json` + `--dangerously-skip-permissions`
- 세션 이어가기: `--resume` 플래그
- Web Speech API HTTPS 요구사항 → 자체서명 인증서
- tmux를 iTerm2와 글래스 브릿지 간 공유 레이어로 사용

## 미완료/참고
- `sudo pmset -c sleep 0 && sudo pmset -c disablesleep 1` 클램쉘 모드 수면 방지 (Terminal.app에서 직접 실행 필요)
- Safari는 자체서명 인증서 문제 있음 → Chrome 사용 권장

## 새 세션에서 이어가기
```bash
tmux attach -t work
cd ~/remote-cli-bridge
claude --resume
# 또는 새 세션으로:
claude -p "CONTEXT.md 파일을 읽고 이전 작업을 이어가줘"
```
