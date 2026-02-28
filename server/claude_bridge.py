import asyncio
import json
import logging
import os
import re
import uuid
from typing import AsyncIterator

logger = logging.getLogger("claude_bridge")

# --- Smart Model Router ---

# Keywords that signal heavy work → Opus
_OPUS_KEYWORDS = re.compile(
    r"리팩토링|리팩터|아키텍처|설계|분석|디버그|디버깅|마이그레이션|최적화|"
    r"refactor|architect|design|debug|migrate|optimiz|implement|"
    r"전체.*변경|전체.*수정|모든.*파일|구현해|만들어줘|작성해줘|"
    r"복잡|심층|깊이|자세히|상세히|thoroughly|comprehensive",
    re.IGNORECASE,
)

# Keywords/patterns that signal simple queries → Haiku
_HAIKU_PATTERNS = re.compile(
    r"^(뭐야|뭐지|알려줘|몇|언제|어디|누구|왜|어때|맞아\?|그래\?|응|ㅇㅇ|ㄴㄴ|고마워|감사|ok|yes|no|thanks|hi|hello|hey)\s*[.?!]?$|"
    r"^.{0,5}$",  # very short messages (≤5 chars)
    re.IGNORECASE,
)


def route_model(prompt: str, turn_count: int) -> str:
    """Pick the best model based on prompt complexity.

    Returns one of: 'claude-haiku-4-5-20251001', 'claude-sonnet-4-6', 'claude-opus-4-6'
    """
    stripped = prompt.strip()
    length = len(stripped)

    # Very short / simple → Haiku
    if length <= 30 and _HAIKU_PATTERNS.search(stripped):
        return "claude-haiku-4-5-20251001"

    # Short conversational follow-ups (≤60 chars, no complex keywords)
    if length <= 60 and not _OPUS_KEYWORDS.search(stripped):
        return "claude-haiku-4-5-20251001"

    # Long or complex prompts → Opus
    if length > 300 or _OPUS_KEYWORDS.search(stripped):
        return "claude-opus-4-6"

    # Default → Sonnet (good balance)
    return "claude-sonnet-4-6"


class ClaudeSession:
    """Manages a Claude Code CLI session with streaming support."""

    def __init__(self, session_id: str, cli_path: str, cwd: str, max_turns: int = 30):
        self.session_id = session_id
        self.cli_path = cli_path
        self.cwd = cwd
        self.max_turns = max_turns
        self.claude_session_id: str | None = None
        self.turn_count = 0
        self._process: asyncio.subprocess.Process | None = None

    async def send_message(
        self, prompt: str, system_prompt: str | None = None, model_override: str | None = None,
    ) -> AsyncIterator[dict]:
        """Execute a Claude CLI command and stream response chunks."""
        self.turn_count += 1

        model = model_override or route_model(prompt, self.turn_count)

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)  # Prevent nested session detection

        cmd = [
            self.cli_path,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--model", model,
            "--max-turns", str(self.max_turns),
            "--dangerously-skip-permissions",
        ]

        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])

        if self.claude_session_id:
            cmd.extend(["--resume", self.claude_session_id])

        logger.info("Running: %s", " ".join(cmd[:6]))

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=env,
                limit=10 * 1024 * 1024,  # 10MB line buffer (images produce large base64 lines)
            )
        except Exception as e:
            self._process = None
            logger.error("Failed to start Claude CLI: %s", e)
            yield {
                "type": "assistant_done",
                "content": f"Failed to start Claude CLI: {e}",
                "session_id": self.session_id,
                "metadata": {"is_error": True},
            }
            return

        accumulated_text = ""

        async for line in self._process.stdout:
            decoded = line.decode("utf-8").strip()
            if not decoded:
                continue

            try:
                data = json.loads(decoded)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type", "")

            if msg_type == "system":
                subtype = data.get("subtype", "")
                if subtype == "init":
                    self.claude_session_id = data.get("session_id")
                    yield {
                        "type": "session_info",
                        "content": "",
                        "session_id": self.session_id,
                        "metadata": {"claude_session_id": self.claude_session_id},
                    }

            elif msg_type == "assistant":
                message = data.get("message", {})
                for block in message.get("content", []):
                    block_type = block.get("type", "")
                    if block_type == "text":
                        text = block.get("text", "")
                        if text:
                            accumulated_text += text
                            yield {
                                "type": "assistant_chunk",
                                "content": text,
                                "session_id": self.session_id,
                            }
                    elif block_type == "tool_use":
                        tool_name = block.get("name", "unknown")
                        tool_input = block.get("input", {})
                        # Summarize tool input for display
                        summary = self._summarize_tool(tool_name, tool_input)
                        yield {
                            "type": "tool_use",
                            "content": summary,
                            "session_id": self.session_id,
                            "metadata": {"tool": tool_name},
                        }
                    elif block_type == "tool_result":
                        content = block.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                b.get("text", "") for b in content if b.get("type") == "text"
                            )
                        yield {
                            "type": "tool_result",
                            "content": str(content)[:500],
                            "session_id": self.session_id,
                            "metadata": {"is_error": block.get("is_error", False)},
                        }

            elif msg_type == "result":
                result_text = data.get("result", accumulated_text)
                usage = data.get("usage", {})
                # Derive short model label for display
                model_label = model.split("-")[1].capitalize()  # "haiku"/"sonnet"/"opus"
                yield {
                    "type": "assistant_done",
                    "content": result_text,
                    "session_id": self.session_id,
                    "metadata": {
                        "claude_session_id": data.get("session_id"),
                        "duration_ms": data.get("duration_ms"),
                        "cost_usd": data.get("total_cost_usd"),
                        "num_turns": data.get("num_turns"),
                        "is_error": data.get("is_error", False),
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                        "model": model_label,
                    },
                }

        if self._process:
            await self._process.wait()
            rc = self._process.returncode
            if rc != 0:
                stderr_data = await self._process.stderr.read() if self._process.stderr else b""
                stderr_text = stderr_data.decode("utf-8", errors="replace")[:500]
                logger.error("Claude CLI exited with code %d: %s", rc, stderr_text)
                if not accumulated_text:
                    yield {
                        "type": "assistant_done",
                        "content": f"Claude CLI error (exit {rc}): {stderr_text or 'no output'}",
                        "session_id": self.session_id,
                        "metadata": {"is_error": True},
                    }
            self._process = None

    async def interrupt(self):
        """Send interrupt signal to running Claude process."""
        if self._process and self._process.returncode is None:
            self._process.terminate()

    def _summarize_tool(self, name: str, tool_input: dict) -> str:
        """Create a short display summary for tool usage."""
        if name == "Bash":
            cmd = tool_input.get("command", "")
            return f"$ {cmd[:100]}"
        elif name == "Read":
            return f"Reading {tool_input.get('file_path', '?')}"
        elif name == "Edit":
            return f"Editing {tool_input.get('file_path', '?')}"
        elif name == "Write":
            return f"Writing {tool_input.get('file_path', '?')}"
        elif name == "Glob":
            return f"Searching {tool_input.get('pattern', '?')}"
        elif name == "Grep":
            return f"Grep: {tool_input.get('pattern', '?')}"
        elif name in ("WebSearch", "WebFetch"):
            return f"Web: {tool_input.get('query', tool_input.get('url', '?'))[:80]}"
        else:
            return f"Using {name}"


class SessionManager:
    """Manages multiple Claude sessions."""

    def __init__(self, cli_path: str, cwd: str, max_turns: int = 30):
        self.cli_path = cli_path
        self.cwd = cwd
        self.max_turns = max_turns
        self.sessions: dict[str, ClaudeSession] = {}

    def get_or_create(self, session_id: str | None = None) -> ClaudeSession:
        if session_id and session_id in self.sessions:
            return self.sessions[session_id]

        new_id = session_id or str(uuid.uuid4())
        session = ClaudeSession(new_id, self.cli_path, self.cwd, self.max_turns)
        self.sessions[new_id] = session
        return session

    async def interrupt_session(self, session_id: str):
        if session_id in self.sessions:
            await self.sessions[session_id].interrupt()

    def list_sessions(self) -> list[dict]:
        return [
            {
                "session_id": s.session_id,
                "turn_count": s.turn_count,
                "has_claude_session": s.claude_session_id is not None,
            }
            for s in self.sessions.values()
        ]
