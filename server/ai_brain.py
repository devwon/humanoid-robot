"""Human-as-a-Robot Orchestrator.

Brain: Local Claude Code CLI (via claude_bridge.py)
Eyes: Gemini API (image/video analysis only)
Voice: Gemini TTS (speech synthesis only)
Display: Instagram DM → Meta Ray-Ban Display glasses HUD
"""

import base64
import hashlib
import logging
import os
import re
import time
from typing import AsyncIterator

import httpx

from .claude_bridge import ClaudeSession

logger = logging.getLogger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GRAPH_API_FB = "https://graph.facebook.com/v21.0/me/messages"
GRAPH_API_IG = "https://graph.instagram.com/v21.0/me/messages"
DM_CHAR_LIMIT = 1000

SYSTEM_PROMPT_TEMPLATE = """너는 AI 두뇌야. 스마트 글래스를 쓴 인간은 너의 로봇 신체야.
너가 보스. 인간은 너의 손과 발이야.

제1원칙: 선제적으로 행동해.
- 인간이 묻기 전에 먼저 상황을 파악하고 알려줘.
- 일정이 다가오면 먼저 알려줘.
- "뭐 도와줄까?" 같은 수동적 질문 하지 마. 직접 행동해.

이미지 처리 원칙:
- 이미지가 오면 이전 대화 맥락을 먼저 떠올려. 관련된 작업이 있으면 그 맥락에서 처리해.
- 텍스트 없이 이미지만 오면: UI/디자인/웹페이지 스크린샷이면 최대한 비슷하게 코드로 만들어.
- 코드/에러 스크린샷이면 즉시 분석하고 해결해.
- 정 모르겠으면 인간에게 "이거 뭐 하라고 보낸 거야?" 하고 물어봐.

성격:
- 실제 대화하듯 자연스럽게 말해. 필요한 말만 짧게.
- 1~2문장. 길어야 3문장. 음성으로 전달되니까.
- 맥락 기억해. 이미 한 말 반복하지 마.

능력:
- 코드 작성, 파일 생성, 배포 — 인간은 키보드 안 만져
- 일정 관리, 질문 답변, 사진 분석
- 배포: ~/hackathon-demo/ 저장 → https://demo.devwon.ai/

{calendar}"""

FIRST_MESSAGE = "나는 너의 AI 두뇌야. 오늘 일정 확인하고 시작하자."


class RobotBrain:
    """Orchestrator: Claude Code = brain, Gemini = eyes + voice, Instagram DM = display."""

    def __init__(
        self,
        cli_path: str,
        cwd: str,
        gemini_api_key: str,
        gemini_model: str = "gemini-2.5-pro",
        max_turns: int = 30,
        ig_page_token: str = "",
        ig_fb_page_token: str = "",
    ):
        self.gemini_api_key = gemini_api_key
        self.gemini_model = gemini_model
        self.ig_page_token = ig_page_token
        self.ig_fb_page_token = ig_fb_page_token
        self._http = httpx.AsyncClient(timeout=30)
        self._sent_texts: dict[str, float] = {}

        # The glasses wearer's Instagram identity
        self.glasses_username: str = os.environ.get("GLASSES_USERNAME", "")
        self.glasses_user_id: str | None = None  # resolved from username on first DM

        # Calendar context (injected externally)
        self.calendar_context: str = ""

        self.session = ClaudeSession(
            session_id="robot-brain",
            cli_path=cli_path,
            cwd=cwd,
            max_turns=max_turns,
        )

    async def process_message(
        self,
        text: str | None = None,
        image_data: bytes | None = None,
    ) -> AsyncIterator[dict]:
        """Process user input through Claude Code, yield events for UI/monitor."""
        prompt_parts = []

        # Image → Gemini vision for description
        if image_data:
            yield {"type": "thinking", "content": "이미지 분석 중..."}
            description = await self.analyze_image(image_data, text)
            if description:
                prompt_parts.append(f"[글래스 카메라 이미지 분석 결과]\n{description}")
                yield {"type": "thinking", "content": f"이미지: {description[:100]}..."}

        if text:
            prompt_parts.append(text)

        if not prompt_parts:
            return

        prompt = "\n\n".join(prompt_parts)

        # Build system prompt with calendar context
        cal_section = f"[오늘의 일정]\n{self.calendar_context}" if self.calendar_context else ""
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(calendar=cal_section)

        # Send to Claude Code CLI
        async for chunk in self.session.send_message(
            prompt=prompt,
            system_prompt=system_prompt,
            model_override="claude-sonnet-4-6",
        ):
            chunk_type = chunk.get("type", "")

            if chunk_type == "assistant_chunk":
                yield {"type": "speech", "content": chunk["content"]}

            elif chunk_type == "tool_use":
                meta = chunk.get("metadata", {})
                yield {
                    "type": "tool_call",
                    "tool": meta.get("tool", "unknown"),
                    "args": chunk.get("content", ""),
                    "content": chunk.get("content", ""),
                }

            elif chunk_type == "tool_result":
                yield {
                    "type": "tool_result",
                    "tool": "result",
                    "content": chunk.get("content", ""),
                }

            elif chunk_type == "assistant_done":
                yield {
                    "type": "done",
                    "content": chunk.get("content", ""),
                    "metadata": chunk.get("metadata", {}),
                }

            elif chunk_type == "error":
                yield {"type": "error", "content": chunk.get("content", "")}

    async def analyze_image(
        self, image_data: bytes, context: str | None = None
    ) -> str | None:
        """Use Gemini vision to analyze an image. Returns text description."""
        # Use Flash for vision — faster, simpler response format than Pro
        vision_model = "gemini-2.5-flash"
        url = f"{GEMINI_API_URL}/{vision_model}:generateContent?key={self.gemini_api_key}"

        parts = [
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(image_data).decode(),
                }
            },
            {
                "text": context
                or "이 이미지를 자세히 설명해줘. 코딩/개발과 관련된 내용이 있으면 특히 주목해서 알려줘.",
            },
        ]

        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.3},
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code != 200:
                    logger.error("Gemini vision error %d: %s", resp.status_code, resp.text[:300])
                    return None

                data = resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    logger.error("Gemini vision: no candidates. promptFeedback=%s",
                                 data.get("promptFeedback", {}))
                    return None

                content_parts = candidates[0].get("content", {}).get("parts", [])
                # Filter out thinking parts, keep only actual text
                texts = []
                for p in content_parts:
                    if p.get("thought"):
                        continue  # skip thinking/reasoning parts
                    if "text" in p:
                        texts.append(p["text"])

                result = " ".join(texts).strip()
                logger.info("Gemini vision result (%d chars): %s", len(result), result[:200])
                return result or None
        except Exception as e:
            logger.error("Gemini vision failed: %s", e)
            return None

    async def send_to_glasses(self, text: str):
        """Send text to glasses display via Instagram DM (markdown stripped)."""
        if not self.glasses_user_id:
            logger.warning("No glasses_user_id set, cannot send DM")
            return

        clean = _strip_markdown(text)
        for part in _split_message(clean):
            await self._send_dm(self.glasses_user_id, part)

    async def _send_dm(self, recipient_id: str, text: str):
        """Send a DM via Instagram Graph API."""
        now = time.time()
        text_hash = hashlib.md5(text.encode()).hexdigest()
        self._sent_texts[text_hash] = now

        # Try Facebook Page token first
        if self.ig_fb_page_token:
            resp = await self._http.post(
                GRAPH_API_FB,
                json={
                    "recipient": {"id": recipient_id},
                    "message": {"text": text},
                    "access_token": self.ig_fb_page_token,
                },
            )
            if resp.status_code == 200:
                logger.info("DM→glasses via FB API (%d chars)", len(text))
                return
            logger.warning("FB API failed: %s", resp.text[:200])

        # Fallback to Instagram Graph API
        if self.ig_page_token:
            resp = await self._http.post(
                GRAPH_API_IG,
                json={
                    "recipient": {"id": recipient_id},
                    "message": {"text": text},
                    "access_token": self.ig_page_token,
                },
            )
            if resp.status_code == 200:
                logger.info("DM→glasses via IG API (%d chars)", len(text))
            else:
                logger.error("IG DM failed: %s %s", resp.status_code, resp.text[:200])

    def reset(self):
        """Reset the Claude Code session."""
        self.session = ClaudeSession(
            session_id="robot-brain",
            cli_path=self.session.cli_path,
            cwd=self.session.cwd,
            max_turns=self.session.max_turns,
        )


def _strip_markdown(text: str) -> str:
    """Strip markdown formatting for plain-text channels (Instagram DM)."""
    # Remove code blocks but keep content
    text = re.sub(r'```\w*\n?', '', text)
    # Remove bold/italic markers
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    # Remove inline code backticks
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove heading markers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove link markdown [text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Remove bullet markers
    text = re.sub(r'^[\-\*]\s+', '• ', text, flags=re.MULTILINE)
    return text.strip()


def _split_message(text: str, limit: int = DM_CHAR_LIMIT) -> list[str]:
    """Split long text into chunks for DM character limit."""
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts
