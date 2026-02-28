import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.request import urlopen

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

import httpx as _httpx  # noqa: used in _handle_instagram_for_robot
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from . import terminal
from .agents import AgentStore
from .claude_bridge import SessionManager
from .config import Config
from .cost_logger import CostLogger
from .ai_brain import RobotBrain
from .gcal import fetch_calendar_context_async, fetch_events_async
from .gemini_tts import generate_tts_base64, transcribe_audio
from .instagram import InstagramBot
from .robot_bridge import RobotController

import re as _re

def _extract_tts_text(text: str) -> str:
    """Extract only conversational parts for TTS — strip code, URLs, paths, markdown."""
    # Remove code blocks
    s = _re.sub(r'```[\s\S]*?```', '', text)
    # Remove inline code
    s = _re.sub(r'`[^`]+`', '', s)
    # Remove URLs
    s = _re.sub(r'https?://\S+', '', s)
    # Remove file paths
    s = _re.sub(r'[~/]\S+\.\S+', '', s)
    # Remove markdown
    s = _re.sub(r'[*_#\[\]()>]', '', s)
    # Collapse whitespace
    s = _re.sub(r'\n{2,}', '\n', s)
    s = _re.sub(r' {2,}', ' ', s)
    return s.strip()


config = Config()
session_manager = SessionManager(
    cli_path=config.claude_cli_path,
    cwd=config.working_directory,
    max_turns=config.max_turns,
)

cost_logger = CostLogger(config.cost_log_path)
agent_store = AgentStore(config.ai_team_db_path)

# --- Gemini Brain (Human as a Robot) ---
monitor_clients: set[WebSocket] = set()
pwa_clients: set[WebSocket] = set()
# Server-side conversation history (shared across all devices)
conversation_history: list[dict] = []
MAX_HISTORY = 200

_HISTORY_TYPES = {"speech", "stt_result", "tool_call", "tool_result", "thinking", "error"}


async def broadcast_monitor_event(event: dict):
    dead = set()
    for ws in monitor_clients:
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    monitor_clients.difference_update(dead)


async def broadcast_pwa_event(event: dict):
    # Save to server history (skip tts_audio — too large)
    if event.get("type") in _HISTORY_TYPES:
        conversation_history.append(event)
        if len(conversation_history) > MAX_HISTORY:
            del conversation_history[:len(conversation_history) - MAX_HISTORY]
    dead = set()
    for ws in pwa_clients:
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    pwa_clients.difference_update(dead)


robot_brain = RobotBrain(
    cli_path=config.claude_cli_path,
    cwd=config.hackathon_project_root,
    gemini_api_key=config.gemini_api_key,
    gemini_model=config.gemini_model,
    max_turns=config.max_turns,
    ig_page_token=config.instagram_page_token,
    ig_fb_page_token=config.instagram_fb_page_token,
)

instagram_bot = InstagramBot(
    config=config,
    session_manager=session_manager,
    cost_logger=cost_logger,
    exchange_rate_fn=lambda: _fetch_usd_krw(),
    agent_store=agent_store,
) if config.instagram_verify_token else None

robot_controller = RobotController(config)

STATIC_DIR = Path(__file__).parent.parent / "static"

# --- Exchange rate cache ---
_exchange_cache = {"rate": 1450.0, "updated_at": 0}  # fallback rate


def _fetch_usd_krw() -> float:
    """Fetch USD/KRW rate, cached for 1 hour."""
    now = time.time()
    if now - _exchange_cache["updated_at"] < 3600:
        return _exchange_cache["rate"]
    try:
        resp = urlopen("https://open.er-api.com/v6/latest/USD", timeout=5)
        data = json.loads(resp.read())
        rate = data["rates"]["KRW"]
        _exchange_cache["rate"] = rate
        _exchange_cache["updated_at"] = now
        return rate
    except Exception:
        return _exchange_cache["rate"]


ROBOT_STATE_FILE = Path(__file__).parent.parent / "data" / "robot_state.json"


def _save_robot_state():
    """Persist glasses_user_id and calendar to disk."""
    ROBOT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "glasses_user_id": robot_brain.glasses_user_id,
        "glasses_username": robot_brain.glasses_username,
        "calendar_context": robot_brain.calendar_context,
    }
    ROBOT_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


def _load_robot_state():
    """Restore glasses_user_id and calendar from disk on startup."""
    if not ROBOT_STATE_FILE.exists():
        return
    try:
        state = json.loads(ROBOT_STATE_FILE.read_text())
        if state.get("glasses_user_id"):
            robot_brain.glasses_user_id = state["glasses_user_id"]
            logger.info("Auto-restored glasses_user_id: %s", robot_brain.glasses_user_id)
        if state.get("glasses_username"):
            robot_brain.glasses_username = state["glasses_username"]
        if state.get("calendar_context"):
            robot_brain.calendar_context = state["calendar_context"]
            logger.info("Auto-restored calendar (%d chars)", len(robot_brain.calendar_context))
    except Exception as e:
        logger.warning("Failed to load robot state: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _fetch_usd_krw()  # warm cache on startup
    _load_robot_state()
    # Auto-fetch Google Calendar on startup (overrides saved state if successful)
    try:
        calendar = await fetch_calendar_context_async(days_ahead=1)
        if calendar:
            robot_brain.calendar_context = calendar
            _save_robot_state()
            logger.info("Auto-loaded Google Calendar (%d chars)", len(calendar))
    except Exception as e:
        logger.warning("Google Calendar auto-load failed: %s", e)
    reminder_task = asyncio.create_task(_calendar_reminder_loop())
    try:
        yield
    finally:
        reminder_task.cancel()


app = FastAPI(title="Remote CLI Bridge", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- Hackathon static file serving (hackathon.devwon.ai) ---
HACKATHON_DIR = Path.home() / "hackathon-demo"
HACKATHON_DIR.mkdir(exist_ok=True)

from starlette.middleware.base import BaseHTTPMiddleware

class HackathonHostMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "")
        if "hackathon.devwon.ai" in host or "demo.devwon.ai" in host:
            # Serve static files from ~/hackathon-demo/
            path = request.url.path.lstrip("/")
            if not path:
                path = "index.html"
            file_path = HACKATHON_DIR / path
            if file_path.is_file():
                return FileResponse(str(file_path))
            # Try with .html extension
            if (HACKATHON_DIR / (path + ".html")).is_file():
                return FileResponse(str(HACKATHON_DIR / (path + ".html")))
            # Directory → index.html
            if file_path.is_dir() and (file_path / "index.html").is_file():
                return FileResponse(str(file_path / "index.html"))
            return Response(status_code=404, content="Not found")
        return await call_next(request)

app.add_middleware(HackathonHostMiddleware)


@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
async def health():
    return {"status": "ok", "sessions": len(session_manager.sessions)}


@app.get("/api/sessions")
async def list_sessions():
    return session_manager.list_sessions()


@app.get("/dashboard")
async def dashboard():
    return FileResponse(str(STATIC_DIR / "dashboard.html"))


@app.get("/api/costs")
async def get_costs():
    return cost_logger.read_all()


# --- Robot ---

@app.get("/api/robot/status")
async def robot_status():
    return robot_controller.get_status()


@app.get("/api/robot/camera/{name}")
async def robot_camera_snapshot(name: str):
    """Single JPEG snapshot for robot cameras (front / top)."""
    if name not in ("front", "top"):
        return Response(status_code=404)
    frame = robot_controller.get_camera_frame(name)
    if not frame:
        return Response(status_code=204)
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


# --- Instagram Webhook ---

@app.get("/webhooks/instagram")
async def instagram_webhook_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if not instagram_bot:
        return Response(status_code=404)
    challenge = instagram_bot.verify_webhook(hub_mode or "", hub_token or "", hub_challenge or "")
    if challenge:
        return PlainTextResponse(challenge)
    return Response(status_code=403)


@app.post("/webhooks/instagram")
async def instagram_webhook_receive(request: Request):
    if not instagram_bot:
        return Response(status_code=404)
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not instagram_bot.verify_signature(body, signature):
        return Response(status_code=403)
    payload = await request.json()

    # Auto-resolve glasses user_id from username on incoming DMs
    if robot_brain.glasses_username and not robot_brain.glasses_user_id:
        _try_resolve_glasses_user(payload)

    # If robot brain mode is active, intercept DMs for glasses pipeline
    if robot_brain.glasses_user_id:
        asyncio.create_task(_handle_instagram_for_robot(payload))
    else:
        await instagram_bot.handle_webhook(payload)
    return Response(status_code=200)


def _try_resolve_glasses_user(payload: dict):
    """Auto-set glasses_user_id by matching username from instagram_bot cache."""
    if not instagram_bot:
        return
    target = robot_brain.glasses_username.lower()
    # Check cached usernames in instagram_bot
    for sid, uname in instagram_bot._username_cache.items():
        if uname and uname.lower() == target:
            robot_brain.glasses_user_id = sid
            logger.info("Auto-resolved glasses user: @%s → %s", target, sid)
            return
    # Also check sender_id from the current payload
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            sid = event.get("sender", {}).get("id")
            if sid and sid not in instagram_bot._username_cache:
                # Will be resolved on next webhook cycle via instagram_bot
                pass


# --- Robot Brain: activate glasses user via API ---

@app.post("/api/human-robot/activate")
async def activate_glasses_user(request: Request):
    """Set the glasses wearer. Accepts user_id (numeric) or username."""
    data = await request.json()
    user_id = data.get("user_id", "")
    username = data.get("username", "")
    if user_id:
        robot_brain.glasses_user_id = user_id
        _save_robot_state()
        return {"status": "ok", "glasses_user_id": user_id}
    if username:
        robot_brain.glasses_username = username
        _save_robot_state()
        # Will auto-resolve sender_id on first DM from this user
        return {"status": "ok", "glasses_username": username, "note": "sender_id will auto-resolve on first DM"}
    return Response(status_code=400)


@app.post("/api/human-robot/deactivate")
async def deactivate_glasses_user():
    """Clear glasses user — DMs go back to normal instagram_bot."""
    robot_brain.glasses_user_id = None
    _save_robot_state()
    return {"status": "ok"}


@app.get("/api/human-robot/status")
async def robot_brain_status():
    return {
        "glasses_user_id": robot_brain.glasses_user_id,
        "session_turns": robot_brain.session.turn_count,
    }


@app.post("/api/human-robot/test-dm")
async def test_dm(request: Request):
    """Test sending Instagram DM to glasses."""
    data = await request.json()
    text = data.get("text", "Hello from AI Brain!")
    if not robot_brain.glasses_user_id:
        return {"error": "glasses_user_id not set"}
    await robot_brain.send_to_glasses(text)
    return {"status": "sent", "to": robot_brain.glasses_user_id, "text": text}


@app.post("/api/human-robot/calendar")
async def set_calendar(request: Request):
    """Update the AI brain's calendar context."""
    data = await request.json()
    robot_brain.calendar_context = data.get("calendar", "")
    _save_robot_state()
    return {"status": "ok", "calendar_length": len(robot_brain.calendar_context)}


@app.get("/api/human-robot/calendar")
async def get_calendar():
    return {"calendar": robot_brain.calendar_context}


async def _handle_instagram_for_robot(payload: dict):
    """Intercept Instagram DM and forward to robot brain."""
    logger.info("Robot handler received webhook: object=%s", payload.get("object"))
    if payload.get("object") != "instagram":
        return

    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            message = event.get("message", {})
            if message.get("is_echo") or not message:
                logger.info("Skipping echo/empty message")
                continue

            sender_id = event.get("sender", {}).get("id")
            logger.info("DM from sender_id=%s, glasses_user_id=%s, text=%s",
                        sender_id, robot_brain.glasses_user_id, message.get("text", "")[:50])

            # Auto-resolve: if glasses_user_id not set yet, resolve from this sender
            if not robot_brain.glasses_user_id and sender_id and instagram_bot:
                username = await instagram_bot._resolve_username(sender_id)
                if username and username.lower() == robot_brain.glasses_username.lower():
                    robot_brain.glasses_user_id = sender_id
                    logger.info("Auto-resolved glasses user: @%s → %s", username, sender_id)

            if sender_id != robot_brain.glasses_user_id:
                logger.info("Ignoring DM from non-glasses user: %s", sender_id)
                continue

            text = message.get("text", "")
            attachments = message.get("attachments", [])
            image_urls = [
                a["payload"]["url"]
                for a in attachments
                if a.get("type") == "image" and a.get("payload", {}).get("url")
            ]

            # Download image if present
            image_data = None
            if image_urls:
                try:
                    async with _httpx.AsyncClient(timeout=15) as client:
                        resp = await client.get(image_urls[0])
                        if resp.status_code == 200:
                            image_data = resp.content
                except Exception as e:
                    logger.error("Failed to download glasses image: %s", e)

            # Notify PWA that input came from DM
            await broadcast_pwa_event({"type": "stt_result", "content": f"[DM] {text}" if text else "[DM] 📸 이미지"})

            # Process through robot brain and broadcast
            speech_buffer = ""
            async for ev in robot_brain.process_message(text=text, image_data=image_data):
                await broadcast_monitor_event(ev)
                await broadcast_pwa_event(ev)

                if ev.get("type") == "speech":
                    speech_buffer += ev["content"]

            # When done: TTS + DM to glasses
            if speech_buffer:
                # TTS audio → PWA → glasses speaker via Bluetooth
                audio = await generate_tts_base64(
                    text=speech_buffer[:500], api_key=config.gemini_api_key
                )
                if audio:
                    await broadcast_pwa_event({"type": "tts_audio", "content": audio})

                # Instagram DM → glasses HUD display
                await robot_brain.send_to_glasses(speech_buffer)


@app.get("/human-robot")
async def human_robot_page():
    return FileResponse(str(STATIC_DIR / "human-robot.html"))


@app.get("/human-robot/monitor")
async def human_robot_monitor_page():
    return FileResponse(str(STATIC_DIR / "monitor.html"))


@app.websocket("/ws/monitor")
async def monitor_websocket(websocket: WebSocket):
    await websocket.accept()
    monitor_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        monitor_clients.discard(websocket)


_robot_lock = asyncio.Lock()
_last_webcam_observation = ""  # avoid repeating same observation


_notified_events: set[str] = set()  # event_id + date → avoid duplicate reminders


async def _send_proactive(text: str):
    """Broadcast a proactive message to all clients with TTS and glasses DM."""
    await broadcast_pwa_event({"type": "speech", "content": text})
    audio = await generate_tts_base64(text=text[:500], api_key=config.gemini_api_key)
    if audio:
        await broadcast_pwa_event({"type": "tts_audio", "content": audio})
    await robot_brain.send_to_glasses(text)


async def _calendar_reminder_loop():
    """Background task: send proactive calendar reminders."""
    from datetime import datetime, timezone, timedelta

    kst = timezone(timedelta(hours=9))

    # 서버 시작 직후 오늘 일정 브리핑 (10초 후 — 클라이언트 연결 대기)
    await asyncio.sleep(10)
    if robot_brain.calendar_context and robot_brain.calendar_context != "오늘 일정 없음":
        await _send_proactive(f"오늘 일정 알려줄게.\n{robot_brain.calendar_context}")
    else:
        await _send_proactive("오늘 일정은 없어.")

    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(kst)
            events = await fetch_events_async(days_ahead=1)

            for ev in events:
                if not ev["start_dt"] or ev["all_day"]:
                    continue

                minutes_until = (ev["start_dt"] - now).total_seconds() / 60
                notify_key = f"{ev['id']}-30min"

                # 30분 전 알림 (25~35분 사이)
                if 25 <= minutes_until <= 35 and notify_key not in _notified_events:
                    _notified_events.add(notify_key)
                    loc = f" @ {ev['location']}" if ev["location"] else ""
                    msg = f"30분 후에 '{ev['summary']}' 시작해{loc}. 준비해."
                    logger.info("Proactive reminder: %s", msg)
                    await _send_proactive(msg)

                # 5분 전 알림
                notify_key_5 = f"{ev['id']}-5min"
                if 3 <= minutes_until <= 7 and notify_key_5 not in _notified_events:
                    _notified_events.add(notify_key_5)
                    msg = f"5분 후 '{ev['summary']}' 시작이야. 지금 출발해야 해."
                    logger.info("Proactive reminder: %s", msg)
                    await _send_proactive(msg)

        except Exception as e:
            logger.error("Calendar reminder loop error: %s", e)


_WEBCAM_OBSERVE_PROMPT = (
    "You are an AI observing a person through a webcam. "
    "Check their posture and surroundings. Respond in Korean.\n"
    "Rules:\n"
    "- If posture is bad (slouching, hunching, too close to screen): describe the issue in 1 short sentence.\n"
    "- If something interesting/notable is visible: describe it in 1 sentence.\n"
    "- If everything looks normal and nothing notable: respond with exactly: [NORMAL]\n"
    "- Be concise. Max 1-2 sentences."
)


async def _handle_webcam_frame(ws: WebSocket, image_b64: str):
    """Analyze webcam frame via Gemini Flash; if noteworthy, forward to brain."""
    global _last_webcam_observation
    if _robot_lock.locked():
        return  # brain busy, skip silently

    try:
        import base64 as b64mod
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={config.gemini_api_key}"
        body = {
            "contents": [{"parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
                {"text": _WEBCAM_OBSERVE_PROMPT},
            ]}],
            "generationConfig": {"maxOutputTokens": 256, "temperature": 0.3},
        }

        async with _httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, headers={"Content-Type": "application/json"})
            if resp.status_code != 200:
                logger.error("Webcam vision error %d: %s", resp.status_code, resp.text[:200])
                return

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return

            parts = candidates[0].get("content", {}).get("parts", [])
            observation = " ".join(
                p["text"] for p in parts if "text" in p and not p.get("thought")
            ).strip()

            if not observation or "[NORMAL]" in observation.upper():
                logger.info("Webcam: normal")
                return

            # Avoid repeating same observation
            if observation == _last_webcam_observation:
                return
            _last_webcam_observation = observation

            logger.info("Webcam observation: %s", observation[:200])
            # Forward to brain as a webcam context message
            await _handle_robot_message(
                ws, f"[실시간 웹캠 관찰] {observation}"
            )

    except Exception as e:
        logger.error("Webcam handler error: %s", e)


async def _handle_robot_stt(ws: WebSocket, audio_b64: str, mime_type: str):
    """Transcribe audio via Gemini STT, then forward to robot brain."""
    if _robot_lock.locked():
        logger.info("STT skipped — brain is busy")
        await broadcast_pwa_event({"type": "thinking", "content": "처리 중... 잠시 후 다시 말해주세요"})
        return
    try:
        logger.info("STT request received: %d bytes, mime=%s", len(audio_b64), mime_type)
        await broadcast_pwa_event({"type": "thinking", "content": "음성 인식 중..."})
        text = await transcribe_audio(audio_b64, config.gemini_api_key, mime_type)
        if not text:
            logger.info("STT returned empty result")
            await broadcast_pwa_event({"type": "error", "content": "음성 인식 실패 — 다시 말해주세요"})
            return
        logger.info("STT result: %s", text)
        # Show transcription to all clients
        await broadcast_pwa_event({"type": "stt_result", "content": text})
        # Forward to robot brain
        await _handle_robot_message(ws, text)
    except Exception as e:
        logger.error("STT handler error: %s", e)
        await broadcast_pwa_event({"type": "error", "content": f"STT error: {e}"})


async def _handle_robot_message(ws: WebSocket, text: str, image_data: bytes | None = None):
    async with _robot_lock:
        try:
            speech_buffer = ""
            async for event in robot_brain.process_message(text=text, image_data=image_data):
                await broadcast_pwa_event(event)
                await broadcast_monitor_event(event)

                if event.get("type") == "speech":
                    speech_buffer += event["content"]

                if event.get("type") == "done" and speech_buffer:
                    tts_text = _extract_tts_text(speech_buffer)
                    if tts_text:
                        audio = await generate_tts_base64(
                            text=tts_text[:500], api_key=config.gemini_api_key
                        )
                        if audio:
                            logger.info("TTS broadcasting to %d clients", len(pwa_clients))
                            await broadcast_pwa_event({"type": "tts_audio", "content": audio})

                    await robot_brain.send_to_glasses(speech_buffer)
        except Exception as e:
            logger.error("Robot brain error: %s", e)

async def _broadcast_robot_state(websocket: WebSocket, state_queue: asyncio.Queue):
    """Forward robot state updates to a WebSocket client."""
    try:
        while True:
            state_json = await state_queue.get()
            await websocket.send_json({"type": "robot_state", "content": state_json})
    except (asyncio.CancelledError, Exception):
        pass


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    pwa_clients.add(websocket)
    client_id = str(uuid.uuid4())
    robot_state_task = None
    state_queue = None

    # Send conversation history to new client
    if conversation_history:
        await websocket.send_json({"type": "history_sync", "history": conversation_history})

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "content": "Invalid JSON"})
                continue

            msg_type = msg.get("type", "")
            content = msg.get("content", "").strip()
            session_id = msg.get("session_id")

            if msg_type == "interrupt":
                if session_id:
                    await session_manager.interrupt_session(session_id)
                await websocket.send_json({"type": "status", "content": "Interrupted"})
                continue

            # --- Terminal commands ---
            if msg_type == "terminal_list":
                sessions = await terminal.list_sessions()
                await websocket.send_json({"type": "terminal_sessions", "content": json.dumps(sessions)})
                continue

            if msg_type == "terminal_windows":
                windows = await terminal.list_windows(content)
                await websocket.send_json({"type": "terminal_windows", "content": json.dumps(windows), "metadata": {"session": content}})
                continue

            if msg_type == "terminal_capture":
                target = content or msg.get("target", "")
                output = await terminal.capture_pane(target)
                await websocket.send_json({"type": "terminal_output", "content": output, "metadata": {"target": target}})
                continue

            if msg_type == "terminal_send":
                target = msg.get("target", "")
                output = await terminal.send_keys(target, content)
                await websocket.send_json({"type": "terminal_output", "content": output, "metadata": {"target": target}})
                continue

            if msg_type == "terminal_key":
                target = msg.get("target", "")
                output = await terminal.send_keys(target, content, enter=False)
                await websocket.send_json({"type": "terminal_output", "content": output, "metadata": {"target": target}})
                continue

            if msg_type == "terminal_create":
                name = content or f"glasses-{uuid.uuid4().hex[:6]}"
                err = await terminal.create_session(name)
                if err:
                    await websocket.send_json({"type": "error", "content": err})
                else:
                    sessions = await terminal.list_sessions()
                    await websocket.send_json({"type": "terminal_sessions", "content": json.dumps(sessions)})
                continue

            if msg_type == "terminal_kill":
                err = await terminal.kill_session(content)
                if err:
                    await websocket.send_json({"type": "error", "content": err})
                else:
                    sessions = await terminal.list_sessions()
                    await websocket.send_json({"type": "terminal_sessions", "content": json.dumps(sessions)})
                continue

            # --- Robot commands ---
            if msg_type == "robot_connect":
                err = robot_controller.connect()
                if err and err != "Already connected":
                    await websocket.send_json({"type": "robot_error", "content": err})
                else:
                    await websocket.send_json({"type": "robot_connected", "content": ""})
                    # Start state broadcasting for this client
                    if not robot_state_task:
                        state_queue = robot_controller.subscribe_state()
                        robot_state_task = asyncio.create_task(
                            _broadcast_robot_state(websocket, state_queue)
                        )
                continue

            if msg_type == "robot_disconnect":
                if robot_state_task:
                    robot_state_task.cancel()
                    robot_state_task = None
                    if state_queue:
                        robot_controller.unsubscribe_state(state_queue)
                        state_queue = None
                err = robot_controller.disconnect()
                if err:
                    await websocket.send_json({"type": "robot_error", "content": err})
                else:
                    await websocket.send_json({"type": "robot_disconnected", "content": ""})
                continue

            if msg_type == "robot_action":
                try:
                    action = json.loads(content)
                    mode = msg.get("subtype", "absolute")
                    robot_controller.send_action(action, mode=mode)
                except (json.JSONDecodeError, Exception) as e:
                    await websocket.send_json({"type": "robot_error", "content": str(e)})
                continue

            if msg_type == "robot_velocity":
                try:
                    velocity = json.loads(content)
                    if velocity:
                        robot_controller.set_velocity(velocity)
                    else:
                        robot_controller.clear_velocity()
                except (json.JSONDecodeError, Exception) as e:
                    await websocket.send_json({"type": "robot_error", "content": str(e)})
                continue

            if msg_type == "robot_home":
                from .robot_bridge import HOME_POSITION
                robot_controller.move_to(HOME_POSITION, speed=80.0)
                continue

            if msg_type == "robot_stop":
                robot_controller.stop()
                await websocket.send_json({"type": "robot_state", "content": json.dumps(robot_controller.get_state())})
                continue

            if msg_type == "robot_status":
                await websocket.send_json({"type": "robot_state", "content": json.dumps(robot_controller.get_status())})
                continue

            # --- Robot Brain (Claude Code + Gemini vision/TTS) ---
            if msg_type == "gemini_text":
                asyncio.create_task(_handle_robot_message(websocket, content))
                continue

            if msg_type == "gemini_image":
                import base64 as b64mod
                image_bytes = b64mod.b64decode(msg.get("image_data", ""))
                caption = msg.get("caption", "")
                asyncio.create_task(
                    _handle_robot_message(websocket, caption or "이 이미지를 분석해줘", image_bytes)
                )
                continue

            if msg_type == "webcam_frame":
                image_b64 = msg.get("image_data", "")
                if image_b64:
                    asyncio.create_task(_handle_webcam_frame(websocket, image_b64))
                continue

            if msg_type == "gemini_stt":
                # Audio from glasses Bluetooth mic → Gemini STT → text → robot brain
                audio_b64 = msg.get("audio_data", "")
                audio_mime = msg.get("mime_type", "audio/webm")
                if audio_b64:
                    asyncio.create_task(_handle_robot_stt(websocket, audio_b64, audio_mime))
                continue

            if msg_type == "gemini_reset":
                robot_brain.reset()
                conversation_history.clear()
                await websocket.send_json({"type": "gemini_status", "content": "Session reset"})
                continue

            if msg_type == "robot_start":
                # AI initiates — only on fresh session (no previous turns)
                if robot_brain.session.turn_count == 0:
                    from .ai_brain import FIRST_MESSAGE
                    await websocket.send_json({"type": "speech", "content": FIRST_MESSAGE})
                    audio = await generate_tts_base64(
                        text=FIRST_MESSAGE, api_key=config.gemini_api_key
                    )
                    if audio:
                        await websocket.send_json({"type": "tts_audio", "content": audio})
                    await robot_brain.send_to_glasses(FIRST_MESSAGE)
                continue

            if msg_type in ("user_text", "user_voice"):
                if not content:
                    await websocket.send_json(
                        {"type": "error", "content": "Empty message"}
                    )
                    continue

                session = session_manager.get_or_create(session_id)

                await websocket.send_json(
                    {
                        "type": "status",
                        "content": "Processing...",
                        "session_id": session.session_id,
                    }
                )

                try:
                    async for chunk in session.send_message(content):
                        if chunk.get("type") == "assistant_done":
                            meta = chunk.get("metadata", {})
                            cost_usd = meta.get("cost_usd")
                            if cost_usd:
                                rate = _fetch_usd_krw()
                                meta["cost_krw"] = round(float(cost_usd) * rate)
                                meta["exchange_rate"] = round(rate, 2)
                            cost_logger.log({
                                "model": meta.get("model", "unknown"),
                                "cost_usd": float(cost_usd) if cost_usd else 0,
                                "cost_krw": meta.get("cost_krw", 0),
                                "exchange_rate": meta.get("exchange_rate", _exchange_cache["rate"]),
                                "input_tokens": meta.get("input_tokens", 0),
                                "output_tokens": meta.get("output_tokens", 0),
                                "cache_read_tokens": meta.get("cache_read_tokens", 0),
                                "cache_creation_tokens": meta.get("cache_creation_tokens", 0),
                                "duration_ms": meta.get("duration_ms"),
                                "session_id": session.session_id,
                                "num_turns": meta.get("num_turns"),
                                "is_error": meta.get("is_error", False),
                            })
                        await websocket.send_json(chunk)
                except Exception as e:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "content": f"Claude error: {str(e)}",
                            "session_id": session.session_id,
                        }
                    )

    except WebSocketDisconnect:
        pass
    finally:
        pwa_clients.discard(websocket)
        if robot_state_task:
            robot_state_task.cancel()
        if state_queue:
            robot_controller.unsubscribe_state(state_queue)


def main():
    import uvicorn

    ssl_kwargs = {}
    if Path(config.ssl_certfile).exists() and Path(config.ssl_keyfile).exists():
        ssl_kwargs["ssl_certfile"] = config.ssl_certfile
        ssl_kwargs["ssl_keyfile"] = config.ssl_keyfile

    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        reload=False,
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
