import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from .agents import Agent
from .claude_bridge import ClaudeSession

logger = logging.getLogger("group_chat")


@dataclass
class AgentResponse:
    agent: Agent
    text: str
    metadata: dict


async def run_group_chat(
    agents: list[Agent],
    user_message: str,
    cli_path: str,
    cwd: str,
    all_members: list[Agent] | None = None,
    on_agent_start: Callable[[Agent], None] | None = None,
    on_agent_done: Callable[[Agent, str], None] | None = None,
    prior_context: list[str] | None = None,
    round_number: int = 1,
    total_rounds: int = 1,
) -> list[AgentResponse]:
    """Run a group chat: each agent responds sequentially with context accumulation.

    all_members: full team roster (including non-discussion members like summarizer).
    prior_context: accumulated context from previous rounds.
    round_number / total_rounds: for round-aware system prompts.
    """
    responses: list[AgentResponse] = []
    context_parts: list[str] = list(prior_context or [])
    team_roster = all_members or agents

    if round_number > 1:
        context_parts.append(f"--- 라운드 {round_number} ---")

    for agent in agents:
        if on_agent_start:
            on_agent_start(agent)

        logger.info("Group chat R%d: %s (%s) responding...", round_number, agent.name, agent.role)

        # Build prompt with accumulated context
        prompt_parts = [f"사용자: {user_message}"]
        prompt_parts.extend(context_parts)
        prompt = "\n\n".join(prompt_parts)

        # Build round-aware system prompt
        group_system = _build_round_system_prompt(agent, team_roster, round_number, total_rounds)

        # Map AI-Team model names to full model IDs
        model = _resolve_model(agent.ai_model)

        # Run Claude CLI (one-off, no session persistence)
        session = ClaudeSession(
            session_id=f"group_{agent.id}_r{round_number}",
            cli_path=cli_path,
            cwd=cwd,
            max_turns=5,
        )

        full_text = ""
        metadata = {}

        async for chunk in session.send_message(prompt, system_prompt=group_system, model_override=model):
            if chunk["type"] == "assistant_chunk":
                full_text += chunk["content"]
            elif chunk["type"] == "assistant_done":
                metadata = chunk.get("metadata", {})
                if not full_text:
                    full_text = chunk["content"]

        if not full_text:
            full_text = "(응답 없음)"

        response = AgentResponse(agent=agent, text=full_text, metadata=metadata)
        responses.append(response)

        # Add to context for next agent (tagged with round number)
        tag = f"[{agent.name} (R{round_number})]" if total_rounds > 1 else f"[{agent.name}]"
        context_parts.append(f"{tag}: {full_text}")

        if on_agent_done:
            on_agent_done(agent, full_text)

        logger.info("Group chat R%d: %s done (%d chars)", round_number, agent.name, len(full_text))

    return responses


def _build_round_system_prompt(
    agent: Agent,
    team_roster: list[Agent],
    round_number: int,
    total_rounds: int,
) -> str:
    """Generate round-appropriate system prompt for discussion agents."""
    other_names = [a.name for a in team_roster if a.id != agent.id]
    others_str = ", ".join(other_names)

    # Single-round mode: use original simple prompt
    if total_rounds <= 1:
        return (
            f"{agent.system_prompt}\n\n"
            f"You are in a group discussion with: {others_str}. "
            f"Respond from your perspective as {agent.name} ({agent.role}). "
            f"Always respond in Korean (한국어). Keep it concise for mobile reading."
        )

    if round_number == 1:
        return (
            f"{agent.system_prompt}\n\n"
            f"너는 팀원들({others_str})과 팀 토론 중이야. "
            f"지금은 1라운드(분석 단계)야.\n"
            f"너의 역할({agent.role}) 관점에서:\n"
            f"1. 사용자 요구사항을 분석하고 너의 전문 영역에서 구체적인 스펙 항목을 제안해\n"
            f"2. 다른 팀원에게 확인이 필요한 사항이 있으면 이름을 지정해서 질문해 "
            f"(예: '@진구 이 기능의 우선순위는?')\n"
            f"3. 잠재적 리스크나 고려사항을 구체적으로 제시해\n"
            f"항상 한국어로 답변해. 모바일에서 읽기 좋게 간결하게."
        )
    else:
        return (
            f"{agent.system_prompt}\n\n"
            f"너는 팀원들({others_str})과 팀 토론 중이야. "
            f"지금은 {round_number}라운드(구체화 단계)야.\n"
            f"이전 라운드의 모든 팀원 의견을 읽고:\n"
            f"1. 너에게 온 질문이 있으면 답변해\n"
            f"2. 다른 팀원의 제안에 동의/반대/보완할 점이 있으면 구체적으로 말해 (이름 지정)\n"
            f"3. 이전 논의를 바탕으로 너의 영역 스펙을 더 구체화해\n"
            f"4. 새로운 아이디어나 빠진 부분이 있으면 추가해\n"
            f"일반적인 동의(좋습니다, 동의합니다)만 하지 말고, 구체적인 내용을 추가해. "
            f"항상 한국어로 답변해. 모바일에서 읽기 좋게 간결하게."
        )


def _resolve_model(ai_team_model: str) -> str:
    """Convert AI-Team model shorthand to full Claude model ID."""
    model_map = {
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-4-6",
        "haiku": "claude-haiku-4-5-20251001",
    }
    return model_map.get(ai_team_model, ai_team_model)
