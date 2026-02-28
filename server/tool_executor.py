"""Tool executor for Gemini Brain - executes shell commands, file ops, deploy, git."""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Commands that are blocked for safety
BLOCKED_COMMANDS = ["rm -rf /", "rm -rf /*", "mkfs", "dd if=", ":(){", "fork bomb"]


class ToolExecutor:
    """Execute tools called by the Gemini brain."""

    def __init__(
        self,
        project_root: str,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self.project_root = Path(project_root)
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.on_event = on_event or (lambda e: asyncio.sleep(0))

    async def execute(self, tool_name: str, args: dict[str, Any]) -> str:
        """Dispatch tool execution. Returns result string for Gemini."""
        handler = getattr(self, tool_name, None)
        if handler is None:
            return f"Error: Unknown tool '{tool_name}'"

        await self.on_event(
            {"type": "tool_call", "tool": tool_name, "args": _summarize_args(args)}
        )

        try:
            result = await handler(**args)
        except Exception as e:
            result = f"Error: {e}"
            logger.error("Tool %s failed: %s", tool_name, e)

        await self.on_event(
            {
                "type": "tool_result",
                "tool": tool_name,
                "content": result[:2000] if len(result) > 2000 else result,
            }
        )
        return result

    async def execute_command(
        self, command: str, working_directory: str | None = None
    ) -> str:
        """Run shell command via asyncio subprocess."""
        for blocked in BLOCKED_COMMANDS:
            if blocked in command:
                return f"Error: Blocked dangerous command: {command}"

        cwd = working_directory or str(self.project_root)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env={**os.environ, "PATH": os.environ.get("PATH", "")},
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

            output = ""
            if stdout:
                output += stdout.decode(errors="replace")
            if stderr:
                output += "\n[stderr] " + stderr.decode(errors="replace")

            if len(output) > 4000:
                output = output[:4000] + "\n... (truncated)"

            return output.strip() or f"Command completed with exit code {proc.returncode}"
        except asyncio.TimeoutError:
            return "Error: Command timed out after 60 seconds"
        except Exception as e:
            return f"Error executing command: {e}"

    async def create_file(self, path: str, content: str) -> str:
        """Create a new file with given content."""
        file_path = self._resolve_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

        await self.on_event(
            {"type": "file_created", "path": path, "content": content}
        )
        return f"File created: {path} ({len(content)} chars)"

    async def edit_file(self, path: str, content: str) -> str:
        """Overwrite file with new content."""
        file_path = self._resolve_path(path)
        if not file_path.exists():
            return f"Error: File not found: {path}"

        file_path.write_text(content, encoding="utf-8")

        await self.on_event(
            {"type": "file_created", "path": path, "content": content}
        )
        return f"File updated: {path} ({len(content)} chars)"

    async def read_file(self, path: str) -> str:
        """Read file content."""
        file_path = self._resolve_path(path)
        if not file_path.exists():
            return f"Error: File not found: {path}"

        content = file_path.read_text(encoding="utf-8")
        if len(content) > 4000:
            content = content[:4000] + "\n... (truncated)"
        return content

    async def list_directory(self, path: str) -> str:
        """List files in a directory."""
        dir_path = self._resolve_path(path)
        if not dir_path.exists():
            return f"Error: Directory not found: {path}"

        items = sorted(dir_path.iterdir())
        lines = []
        for item in items:
            prefix = "d " if item.is_dir() else "f "
            lines.append(f"{prefix}{item.name}")
        return "\n".join(lines) or "(empty directory)"

    async def deploy(self, project_dir: str) -> str:
        """Deploy to Vercel."""
        cwd = self._resolve_path(project_dir)
        if not cwd.exists():
            return f"Error: Directory not found: {project_dir}"

        return await self.execute_command(
            "npx --yes vercel --yes --prod", working_directory=str(cwd)
        )

    async def git_commit(
        self, message: str, project_dir: str | None = None
    ) -> str:
        """Stage all changes and commit."""
        cwd = str(self._resolve_path(project_dir or "."))

        # Init if needed
        git_dir = Path(cwd) / ".git"
        if not git_dir.exists():
            await self.execute_command("git init", working_directory=cwd)

        add_result = await self.execute_command("git add -A", working_directory=cwd)
        commit_result = await self.execute_command(
            f'git commit -m "{message}"', working_directory=cwd
        )
        return f"{add_result}\n{commit_result}"

    def _resolve_path(self, path: str) -> Path:
        """Resolve path relative to project root."""
        p = Path(path)
        if p.is_absolute():
            return p
        return self.project_root / p


def _summarize_args(args: dict) -> dict:
    """Summarize args for logging (truncate long values)."""
    summary = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            summary[k] = v[:200] + "..."
        else:
            summary[k] = v
    return summary
