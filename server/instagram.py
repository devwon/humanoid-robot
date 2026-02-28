import asyncio
import hashlib
import hmac
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .agents import AgentStore
    from .claude_bridge import SessionManager
    from .config import Config
    from .cost_logger import CostLogger

logger = logging.getLogger("instagram")

GRAPH_API_FB = "https://graph.facebook.com/v21.0/me/messages"
GRAPH_API_IG = "https://graph.instagram.com/v21.0/me/messages"
DM_CHAR_LIMIT = 1000
DEDUP_WINDOW_SEC = 60  # ignore duplicate webhooks within this window

# Patterns for group chat / agent mention triggers
_GROUP_TRIGGER = re.compile(r"^(팀|team)\s*:?\s+", re.IGNORECASE)
_AGENT_MENTION = re.compile(r"^@(\S+)\s+", re.IGNORECASE)
_ROLE_TRIGGER = re.compile(r"^number\s*(\d)\s*:?\s+", re.IGNORECASE)

# "number N" → Korean agent name (sort_order 기준)
# 1=도라에몽, 2=진구, 3=이슬이, 4=비실이, 5=퉁퉁이
_NUMBER_TO_AGENT = {
    "1": "도라에몽",
    "2": "진구",
    "3": "이슬이",
    "4": "비실이",
    "5": "퉁퉁이",
}


class InstagramBot:
    """Handles Instagram DM webhook events and Claude Code integration."""

    def __init__(
        self,
        config: "Config",
        session_manager: "SessionManager",
        cost_logger: "CostLogger",
        exchange_rate_fn=None,
        agent_store: "AgentStore | None" = None,
    ):
        self.config = config
        self.session_manager = session_manager
        self.cost_logger = cost_logger
        self._exchange_rate_fn = exchange_rate_fn
        self._agent_store = agent_store
        self._http = httpx.AsyncClient(timeout=30)
        # Map Instagram sender_id → bridge session_id for conversation continuity
        self._sender_sessions: dict[str, str] = {}
        # Dedup: (message_timestamp, text_hash) → wall-clock time seen
        self._seen_messages: dict[tuple, float] = {}
        # Track texts we sent to filter echoes without is_echo flag
        self._sent_texts: dict[str, float] = {}
        # Cache sender_id → username for filtering
        self._username_cache: dict[str, str | None] = {}
        # Allowed usernames (lowercase)
        self._allowed_usernames: set[str] = set()
        if config.instagram_allowed_usernames:
            self._allowed_usernames = {
                u.strip().lower()
                for u in config.instagram_allowed_usernames.split(",")
                if u.strip()
            }
            logger.info("Allowed IG usernames: %s", self._allowed_usernames)

    def verify_signature(self, payload: bytes, signature: str) -> bool:
        """Verify X-Hub-Signature-256 from Meta."""
        if not self.config.instagram_app_secret:
            return True  # Skip if not configured
        expected = hmac.new(
            self.config.instagram_app_secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return signature == f"sha256={expected}"

    def verify_webhook(self, mode: str, token: str, challenge: str) -> str | None:
        """Handle webhook verification GET request. Returns challenge on success."""
        if mode == "subscribe" and token == self.config.instagram_verify_token:
            logger.info("Webhook verified")
            return challenge
        logger.warning("Webhook verification failed: mode=%s", mode)
        return None

    def _is_duplicate(self, timestamp: int, text: str) -> bool:
        """Check if we already processed this message (dedup across entries)."""
        now = time.time()
        # Clean old entries
        expired = [k for k, v in self._seen_messages.items() if now - v > DEDUP_WINDOW_SEC]
        for k in expired:
            del self._seen_messages[k]

        key = (timestamp, hashlib.md5(text.encode()).hexdigest())
        if key in self._seen_messages:
            return True
        self._seen_messages[key] = now
        return False

    def _parse_trigger(self, text: str) -> tuple[str, str, list[str]]:
        """Parse message for group/agent triggers.

        Returns: (mode, clean_text, agent_names)
          mode: "group" | "agent" | "normal"
        """
        # Check group chat trigger: "팀: message" or "team: message"
        m = _GROUP_TRIGGER.match(text)
        if m:
            clean = text[m.end():]
            return "group", clean, []

        # Check individual agent mention: "@에이전트이름 message"
        m = _AGENT_MENTION.match(text)
        if m and self._agent_store:
            name = m.group(1)
            agent = self._agent_store.get_agent_by_name(name)
            if agent:
                clean = text[m.end():]
                return "agent", clean, [agent.name]

        # Check "number N" trigger: "number 1 check this code"
        m = _ROLE_TRIGGER.match(text)
        if m and self._agent_store:
            role_key = m.group(1)
            agent_name = _NUMBER_TO_AGENT.get(role_key)
            if agent_name:
                clean = text[m.end():]
                return "agent", clean, [agent_name]

        # Check Korean agent name prefix: "도라에몽 이거 봐줘"
        if self._agent_store:
            for agent in self._agent_store.get_all_agents():
                if text.startswith(agent.name) and len(text) > len(agent.name):
                    rest = text[len(agent.name):]
                    if rest[0] in (" ", ":", " "):  # space, colon, or full-width space
                        clean = rest.lstrip(": \u3000")
                        return "agent", clean, [agent.name]

        return "normal", text, []

    async def _resolve_username(self, sender_id: str) -> str | None:
        """Look up Instagram username from sender_id via Graph API. Cached."""
        if sender_id in self._username_cache:
            return self._username_cache[sender_id]
        token = self.config.instagram_page_token
        if not token:
            return None
        try:
            resp = await self._http.get(
                f"https://graph.instagram.com/v21.0/{sender_id}",
                params={"fields": "username", "access_token": token},
            )
            if resp.status_code == 200:
                username = resp.json().get("username")
                self._username_cache[sender_id] = username
                logger.info("Resolved sender %s → @%s", sender_id, username)
                return username
            logger.warning("Failed to resolve sender %s: %s", sender_id, resp.status_code)
        except Exception:
            logger.exception("Error resolving username for %s", sender_id)
        self._username_cache[sender_id] = None
        return None

    async def _is_sender_allowed(self, sender_id: str) -> bool:
        """Check if sender is in the allowed list. If no list, allow all."""
        if not self._allowed_usernames:
            return True
        username = await self._resolve_username(sender_id)
        if not username:
            return False
        return username.lower() in self._allowed_usernames

    async def handle_webhook(self, payload: dict):
        """Process incoming webhook payload. Called as background task."""
        logger.info("Webhook payload: %s", payload)

        if payload.get("object") != "instagram":
            return

        for entry in payload.get("entry", []):
            for event in entry.get("messaging", []):
                message = event.get("message", {})

                # Skip echo messages (our own outgoing messages)
                if message.get("is_echo"):
                    continue

                # Skip non-message events (read receipts, etc.)
                if not message:
                    continue

                sender_id = event.get("sender", {}).get("id")
                text = message.get("text", "")
                msg_ts = event.get("timestamp", 0)
                attachments = message.get("attachments", [])

                # Extract image URLs from attachments
                image_urls = [
                    a["payload"]["url"]
                    for a in attachments
                    if a.get("type") == "image" and a.get("payload", {}).get("url")
                ]

                if not sender_id or (not text and not image_urls):
                    continue

                # Deduplicate: Meta sends the same DM across multiple entry IDs
                dedup_key = text or message.get("mid", "")
                if self._is_duplicate(msg_ts, dedup_key):
                    logger.info("Skipping duplicate: sender=%s text=%s", sender_id, dedup_key[:50])
                    continue

                # Skip echoes of our own replies (some entries lack is_echo)
                if text:
                    text_hash = hashlib.md5(text.encode()).hexdigest()
                    if text_hash in self._sent_texts:
                        logger.info("Skipping own echo: sender=%s text=%s", sender_id, text[:50])
                        continue

                # Check sender allowlist
                if not await self._is_sender_allowed(sender_id):
                    logger.info("Ignoring DM from non-allowed sender %s", sender_id)
                    continue

                logger.info("DM from %s: text=%s images=%d", sender_id, text[:100], len(image_urls))

                # Parse trigger mode
                mode, clean_text, agent_names = self._parse_trigger(text)

                if mode == "group":
                    asyncio.create_task(self._process_group_chat(sender_id, clean_text))
                elif mode == "agent":
                    asyncio.create_task(self._process_agent_message(sender_id, clean_text, agent_names[0]))
                else:
                    asyncio.create_task(self._process_message(sender_id, text, image_urls))

    async def _download_image(self, url: str) -> str | None:
        """Download image from URL and save to temp file. Returns file path."""
        try:
            resp = await self._http.get(url)
            if resp.status_code != 200:
                logger.error("Failed to download image: %s", resp.status_code)
                return None

            # Detect extension from content-type
            ct = resp.headers.get("content-type", "")
            ext = ".jpg"
            if "png" in ct:
                ext = ".png"
            elif "gif" in ct:
                ext = ".gif"
            elif "webp" in ct:
                ext = ".webp"

            tmp = tempfile.NamedTemporaryFile(
                suffix=ext, prefix="ig_img_", dir="/tmp", delete=False
            )
            tmp.write(resp.content)
            tmp.close()
            logger.info("Image saved: %s (%d bytes)", tmp.name, len(resp.content))
            return tmp.name
        except Exception:
            logger.exception("Error downloading image")
            return None

    async def _process_message(self, sender_id: str, text: str, image_urls: list[str] | None = None):
        """Run Claude and send the response back as Instagram DM (normal mode)."""
        image_paths = []
        try:
            logger.info("Processing from %s: text=%s images=%d", sender_id, text[:100], len(image_urls or []))

            # Get or create session keyed by sender_id
            session_id = self._sender_sessions.get(sender_id)
            session = self.session_manager.get_or_create(session_id)
            self._sender_sessions[sender_id] = session.session_id

            # Download images and build prompt
            for url in (image_urls or []):
                path = await self._download_image(url)
                if path:
                    image_paths.append(path)

            # Build prompt with Korean instruction
            parts = ["[System: Always respond in Korean (한국어)]"]
            if image_paths:
                paths_str = ", ".join(image_paths)
                parts.append(f"\n[첨부된 이미지를 Read 도구로 읽어서 분석해주세요: {paths_str}]")
            if text:
                parts.append(f"\n{text}")
            elif image_paths:
                parts.append("\n이 이미지를 분석해주세요.")

            prompt = "\n".join(parts)

            # Collect full response
            full_text = ""
            metadata = {}

            async for chunk in session.send_message(prompt):
                if chunk["type"] == "assistant_chunk":
                    full_text += chunk["content"]
                elif chunk["type"] == "assistant_done":
                    metadata = chunk.get("metadata", {})
                    if not full_text:
                        full_text = chunk["content"]

            if not full_text:
                full_text = "(no response)"

            self._log_cost(metadata, session.session_id, "instagram")

            # Send reply (split if too long)
            for part in _split_message(full_text):
                await self._send_dm(sender_id, part)

        except Exception:
            logger.exception("Error processing DM from %s", sender_id)
            await self._send_dm(sender_id, "Error processing your request.")
        finally:
            for p in image_paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass

    async def _process_agent_message(self, sender_id: str, text: str, agent_name: str):
        """Run Claude with a specific AI-Team agent's system prompt."""
        try:
            agent = self._agent_store.get_agent_by_name(agent_name)
            if not agent:
                await self._send_dm(sender_id, f"에이전트 '{agent_name}'을(를) 찾을 수 없습니다.")
                return

            logger.info("Agent chat: %s → %s: %s", sender_id, agent.name, text[:100])

            from .group_chat import _resolve_model

            session_id = self._sender_sessions.get(sender_id)
            session = self.session_manager.get_or_create(session_id)
            self._sender_sessions[sender_id] = session.session_id

            system_prompt = f"{agent.system_prompt}\n\nAlways respond in Korean (한국어)."
            model = _resolve_model(agent.ai_model)

            full_text = ""
            metadata = {}

            async for chunk in session.send_message(text, system_prompt=system_prompt, model_override=model):
                if chunk["type"] == "assistant_chunk":
                    full_text += chunk["content"]
                elif chunk["type"] == "assistant_done":
                    metadata = chunk.get("metadata", {})
                    if not full_text:
                        full_text = chunk["content"]

            if not full_text:
                full_text = "(응답 없음)"

            reply = f"[{agent.name}]\n{full_text}"
            self._log_cost(metadata, session.session_id, "instagram_agent", agent_name=agent.name)

            # Save to AI-Team DB
            self._save_to_ai_team(agent, text, full_text, metadata)

            for part in _split_message(reply):
                await self._send_dm(sender_id, part)

        except Exception:
            logger.exception("Error in agent chat from %s", sender_id)
            await self._send_dm(sender_id, "Error processing your request.")

    async def _process_group_chat(self, sender_id: str, text: str):
        """Run multi-round group discussion, then 비실이 compiles dev spec."""
        try:
            if not self._agent_store:
                await self._send_dm(sender_id, "에이전트가 설정되지 않았습니다.")
                return

            agents = self._agent_store.get_all_agents()
            if not agents:
                await self._send_dm(sender_id, "등록된 에이전트가 없습니다.")
                return

            # Separate 비실이 as spec compiler, rest as discussion agents
            summarizer = None
            discussion_agents = []
            for agent in agents:
                if agent.name == "비실이":
                    summarizer = agent
                else:
                    discussion_agents.append(agent)

            agent_names = ", ".join(a.name for a in agents)
            num_rounds = 2
            logger.info(
                "Group chat from %s: %d agents (%s), %d rounds: %s",
                sender_id, len(agents), agent_names, num_rounds, text[:100],
            )

            from .group_chat import run_group_chat, _resolve_model, AgentResponse
            from .claude_bridge import ClaudeSession

            # Status DM
            await self._send_dm(
                sender_id,
                f"🤔 팀 토론 시작... ({agent_names})\n📋 {num_rounds}라운드 논의 후 스펙 정리 예정",
            )

            # Step 1: Multi-round discussion
            all_responses = []
            context_parts = []

            for round_num in range(1, num_rounds + 1):
                if round_num > 1:
                    await self._send_dm(
                        sender_id,
                        f"💬 라운드 {round_num}/{num_rounds}: 스펙 구체화 중...",
                    )

                round_responses = await run_group_chat(
                    agents=discussion_agents,
                    user_message=text,
                    cli_path=self.config.claude_cli_path,
                    cwd=self.config.working_directory,
                    all_members=agents,
                    prior_context=context_parts,
                    round_number=round_num,
                    total_rounds=num_rounds,
                )

                all_responses.extend(round_responses)
                for r in round_responses:
                    context_parts.append(f"[{r.agent.name} (R{round_num})]: {r.text}")

            # Step 2: 비실이 compiles development spec
            if summarizer and all_responses:
                logger.info("Group chat: 비실이 compiling spec from %d responses", len(all_responses))
                await self._send_dm(sender_id, "📝 비실이가 개발 스펙 정리 중...")

                discussion = "\n\n".join(
                    f"[{r.agent.name} ({r.agent.role})]: {r.text}" for r in all_responses
                )
                spec_prompt = (
                    f"사용자 요구사항: {text}\n\n"
                    f"팀 토론 내용 ({num_rounds}라운드):\n{discussion}\n\n"
                    f"위 팀 토론 결과를 바탕으로 개발 스펙 문서를 작성해줘.\n"
                    f"다음 구조로 정리해:\n"
                    f"## 1. 기능 요구사항\n"
                    f"## 2. 기술 스펙\n"
                    f"## 3. 디자인 가이드라인\n"
                    f"## 4. QA 체크리스트\n"
                    f"## 5. 우선순위 및 일정 제안\n"
                    f"팀원들이 합의한 내용은 확정으로, 이견이 있는 부분은 [미확정]으로 표시해."
                )

                spec_system = (
                    f"{summarizer.system_prompt}\n\n"
                    f"너는 팀 토론의 결과를 개발 스펙 문서로 정리하는 역할이야. "
                    f"팀원들의 논의를 빠짐없이 반영하되, 실행 가능한 구체적 스펙으로 구조화해. "
                    f"일반적인 요약이 아니라, 개발자가 바로 작업에 들어갈 수 있는 수준의 스펙 문서를 만들어. "
                    f"항상 한국어로 답변해."
                )

                session = ClaudeSession(
                    session_id=f"group_spec_{summarizer.id}",
                    cli_path=self.config.claude_cli_path,
                    cwd=self.config.working_directory,
                    max_turns=5,
                )

                spec_text = ""
                spec_meta = {}
                model = _resolve_model(summarizer.ai_model)

                async for chunk in session.send_message(
                    spec_prompt, system_prompt=spec_system, model_override=model
                ):
                    if chunk["type"] == "assistant_chunk":
                        spec_text += chunk["content"]
                    elif chunk["type"] == "assistant_done":
                        spec_meta = chunk.get("metadata", {})
                        if not spec_text:
                            spec_text = chunk["content"]

                if not spec_text:
                    spec_text = "(스펙 정리 실패)"

                spec_response = AgentResponse(agent=summarizer, text=spec_text, metadata=spec_meta)
                all_responses.append(spec_response)

            # Save all responses to AI-Team DB
            self._save_group_to_ai_team(agents, text, all_responses)

            # Log costs for all agents
            for resp in all_responses:
                self._log_cost(resp.metadata, f"group_{resp.agent.id}", "instagram_group", agent_name=resp.agent.name)

            # Send 비실이's compiled spec via DM
            if summarizer and all_responses:
                spec_resp = all_responses[-1]  # 비실이's spec is last
                reply = f"[{spec_resp.agent.name} 📋 개발 스펙]\n{spec_resp.text}"
                for part in _split_message(reply):
                    await self._send_dm(sender_id, part)
            else:
                # Fallback: send all responses if no summarizer
                for resp in all_responses:
                    reply = f"[{resp.agent.name} - {resp.agent.role}]\n{resp.text}"
                    for part in _split_message(reply):
                        await self._send_dm(sender_id, part)

        except Exception:
            logger.exception("Error in group chat from %s", sender_id)
            await self._send_dm(sender_id, "Error processing group chat.")

    def _save_to_ai_team(self, agent: "Agent", user_text: str, response: str, metadata: dict):
        """Save individual agent conversation to AI-Team DB."""
        if not self._agent_store:
            return
        try:
            from .agents import Agent  # noqa: F811

            session_id = self._agent_store.create_session(
                agent.id, f"[IG] {user_text[:30]}", "individual"
            )
            self._agent_store.add_message(session_id, "user", user_text)
            cost = metadata.get("cost_usd") or 0
            tokens = (metadata.get("input_tokens", 0) or 0) + (metadata.get("output_tokens", 0) or 0)
            self._agent_store.add_message(
                session_id, "assistant", response, agent_id=agent.id,
                cost_usd=cost if cost else None, token_count=tokens if tokens else None,
            )
            if cost:
                self._agent_store.add_cost(
                    agent.id, session_id, metadata.get("model", "unknown"),
                    metadata.get("input_tokens", 0) or 0, metadata.get("output_tokens", 0) or 0,
                    cost, metadata.get("duration_ms", 0) or 0,
                )
        except Exception:
            logger.exception("Failed to save to AI-Team DB")

    def _save_group_to_ai_team(self, agents: list, user_text: str, responses: list):
        """Save group chat conversation to AI-Team DB."""
        if not self._agent_store:
            return
        try:
            session_id = self._agent_store.create_group_session(
                agents, f"[IG 팀] {user_text[:30]}"
            )
            self._agent_store.add_message(session_id, "user", user_text)
            for resp in responses:
                cost = resp.metadata.get("cost_usd") or 0
                tokens = (resp.metadata.get("input_tokens", 0) or 0) + (resp.metadata.get("output_tokens", 0) or 0)
                self._agent_store.add_message(
                    session_id, "assistant", f"[{resp.agent.name}]: {resp.text}",
                    agent_id=resp.agent.id,
                    cost_usd=cost if cost else None, token_count=tokens if tokens else None,
                )
                if cost:
                    self._agent_store.add_cost(
                        resp.agent.id, session_id, resp.metadata.get("model", "unknown"),
                        resp.metadata.get("input_tokens", 0) or 0, resp.metadata.get("output_tokens", 0) or 0,
                        cost, resp.metadata.get("duration_ms", 0) or 0,
                    )
        except Exception:
            logger.exception("Failed to save group chat to AI-Team DB")

    def _log_cost(self, metadata: dict, session_id: str, source: str, agent_name: str | None = None):
        """Log cost for a Claude response."""
        if not metadata:
            return
        rate = self._exchange_rate_fn() if self._exchange_rate_fn else 1450.0
        cost_usd = metadata.get("cost_usd", 0) or 0
        entry = {
            "model": metadata.get("model", "unknown"),
            "cost_usd": cost_usd,
            "cost_krw": round(cost_usd * rate),
            "exchange_rate": rate,
            "input_tokens": metadata.get("input_tokens", 0),
            "output_tokens": metadata.get("output_tokens", 0),
            "cache_read_tokens": metadata.get("cache_read_tokens", 0),
            "cache_creation_tokens": metadata.get("cache_creation_tokens", 0),
            "duration_ms": metadata.get("duration_ms", 0),
            "session_id": session_id,
            "num_turns": metadata.get("num_turns", 0),
            "is_error": metadata.get("is_error", False),
            "source": source,
        }
        if agent_name:
            entry["agent"] = agent_name
        self.cost_logger.log(entry)

    async def _send_dm(self, recipient_id: str, text: str):
        """Send a DM reply via Instagram Graph API."""
        # Track sent text to filter echoes without is_echo flag
        now = time.time()
        text_hash = hashlib.md5(text.encode()).hexdigest()
        self._sent_texts[text_hash] = now
        # Clean old entries
        expired = [k for k, v in self._sent_texts.items() if now - v > DEDUP_WINDOW_SEC]
        for k in expired:
            del self._sent_texts[k]

        # Try Facebook Page token first (more reliable for messaging)
        fb_token = self.config.instagram_fb_page_token
        ig_token = self.config.instagram_page_token

        if fb_token:
            resp = await self._http.post(
                GRAPH_API_FB,
                json={
                    "recipient": {"id": recipient_id},
                    "message": {"text": text},
                    "access_token": fb_token,
                },
            )
            if resp.status_code == 200:
                logger.info("DM sent via FB API to %s (%d chars)", recipient_id, len(text))
                return
            logger.warning("FB API failed: %s, trying IG API", resp.text[:200])

        # Fallback to Instagram Graph API
        resp = await self._http.post(
            GRAPH_API_IG,
            json={
                "recipient": {"id": recipient_id},
                "message": {"text": text},
                "access_token": ig_token,
            },
        )
        if resp.status_code != 200:
            logger.error("Failed to send DM: %s %s", resp.status_code, resp.text)
        else:
            logger.info("DM sent to %s (%d chars)", recipient_id, len(text))


def _split_message(text: str, limit: int = DM_CHAR_LIMIT) -> list[str]:
    """Split long text into chunks respecting the DM character limit."""
    if len(text) <= limit:
        return [text]

    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break

        # Try to split at last newline within limit
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            # Fall back to last space
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit

        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")

    return parts
