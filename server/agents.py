import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("agents")


@dataclass
class Agent:
    id: str
    name: str
    role: str
    system_prompt: str
    ai_model: str


class AgentStore:
    """Access to AI-Team's agent database (read agents, write chat history)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        if not Path(db_path).exists():
            logger.warning("AI-Team DB not found: %s", db_path)

    def _connect(self, readonly: bool = True) -> sqlite3.Connection:
        if readonly:
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_all_agents(self) -> list[Agent]:
        """Get all agents sorted by sort_order."""
        if not Path(self.db_path).exists():
            return []
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT id, name, role, system_prompt, ai_model FROM agents ORDER BY sort_order"
            ).fetchall()
            conn.close()
            return [Agent(**dict(r)) for r in rows]
        except Exception:
            logger.exception("Failed to read agents from DB")
            return []

    def get_agent_by_name(self, name: str) -> Agent | None:
        """Find agent by name (case-insensitive)."""
        if not Path(self.db_path).exists():
            return None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT id, name, role, system_prompt, ai_model FROM agents WHERE LOWER(name) = LOWER(?)",
                (name,),
            ).fetchone()
            conn.close()
            return Agent(**dict(row)) if row else None
        except Exception:
            logger.exception("Failed to find agent: %s", name)
            return None

    def get_agents_by_names(self, names: list[str]) -> list[Agent]:
        """Find multiple agents by name."""
        agents = []
        for name in names:
            agent = self.get_agent_by_name(name)
            if agent:
                agents.append(agent)
        return agents

    def list_names(self) -> list[str]:
        """Get list of all agent names."""
        return [a.name for a in self.get_all_agents()]

    # --- Chat history write methods ---

    def _now(self) -> str:
        return datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")

    def create_session(self, agent_id: str, title: str, session_type: str = "individual") -> str:
        """Create a chat session. Returns session_id."""
        session_id = str(uuid.uuid4())
        now = self._now()
        try:
            conn = self._connect(readonly=False)
            conn.execute(
                "INSERT INTO chat_sessions (id, agent_id, title, session_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, agent_id, title, session_type, now, now),
            )
            conn.commit()
            conn.close()
            logger.info("Created session %s for agent %s", session_id, agent_id)
        except Exception:
            logger.exception("Failed to create session")
        return session_id

    def create_group_session(self, agents: list[Agent], title: str) -> str:
        """Create a group chat session with participants. Returns session_id."""
        session_id = str(uuid.uuid4())
        now = self._now()
        try:
            conn = self._connect(readonly=False)
            # Use first agent as primary agent_id (required by schema)
            conn.execute(
                "INSERT INTO chat_sessions (id, agent_id, title, session_type, created_at, updated_at) VALUES (?, ?, ?, 'group', ?, ?)",
                (session_id, agents[0].id, title, now, now),
            )
            for agent in agents:
                conn.execute(
                    "INSERT OR IGNORE INTO group_participants (session_id, agent_id) VALUES (?, ?)",
                    (session_id, agent.id),
                )
            conn.commit()
            conn.close()
            logger.info("Created group session %s with %d agents", session_id, len(agents))
        except Exception:
            logger.exception("Failed to create group session")
        return session_id

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        agent_id: str | None = None,
        cost_usd: float | None = None,
        token_count: int | None = None,
    ):
        """Insert a message into the chat history."""
        msg_id = str(uuid.uuid4())
        now = self._now()
        try:
            conn = self._connect(readonly=False)
            conn.execute(
                "INSERT INTO messages (id, session_id, agent_id, role, content, cost_usd, token_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (msg_id, session_id, agent_id, role, content, cost_usd, token_count, now),
            )
            # Update session timestamp
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            logger.exception("Failed to add message")

    def add_cost(
        self,
        agent_id: str,
        session_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        duration_ms: int,
    ):
        """Insert a cost record."""
        cost_id = str(uuid.uuid4())
        try:
            conn = self._connect(readonly=False)
            conn.execute(
                "INSERT INTO costs (id, agent_id, session_id, model, input_tokens, output_tokens, cost_usd, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cost_id, agent_id, session_id, model, input_tokens, output_tokens, cost_usd, duration_ms),
            )
            conn.commit()
            conn.close()
        except Exception:
            logger.exception("Failed to add cost")
