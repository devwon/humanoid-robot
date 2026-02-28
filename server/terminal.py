import asyncio
import shutil


TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"


async def _run(cmd: list[str]) -> tuple[str, int]:
    """Run a command and return (stdout, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", errors="replace").strip(), proc.returncode


async def list_sessions() -> list[dict]:
    """List all tmux sessions with their windows."""
    out, rc = await _run([
        TMUX, "list-sessions", "-F",
        "#{session_name}\t#{session_windows}\t#{session_attached}\t#{session_created}",
    ])
    if rc != 0:
        return []

    sessions = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            sessions.append({
                "name": parts[0],
                "windows": int(parts[1]),
                "attached": int(parts[2]) > 0,
                "created": int(parts[3]),
            })
    return sessions


async def list_windows(session: str) -> list[dict]:
    """List windows in a tmux session."""
    out, rc = await _run([
        TMUX, "list-windows", "-t", session, "-F",
        "#{window_index}\t#{window_name}\t#{window_active}",
    ])
    if rc != 0:
        return []

    windows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            windows.append({
                "index": int(parts[0]),
                "name": parts[1],
                "active": int(parts[2]) > 0,
            })
    return windows


async def capture_pane(target: str, lines: int = 80) -> str:
    """Capture visible content of a tmux pane.

    Args:
        target: tmux target (session:window.pane), e.g. "main:0.0"
        lines: number of history lines to capture (negative = from scrollback)
    """
    out, rc = await _run([
        TMUX, "capture-pane", "-t", target, "-p", "-e",
        "-S", str(-lines),
    ])
    return out if rc == 0 else f"[Error capturing pane: {out}]"


async def send_keys(target: str, keys: str, enter: bool = True) -> str:
    """Send keys to a tmux pane.

    Args:
        target: tmux target (session:window.pane)
        keys: the text/command to send
        enter: whether to press Enter after
    """
    cmd = [TMUX, "send-keys", "-t", target, keys]
    if enter:
        cmd.append("Enter")
    out, rc = await _run(cmd)
    if rc != 0:
        return f"[Error: {out}]"
    # Wait a moment for command to execute, then capture output
    await asyncio.sleep(0.5)
    return await capture_pane(target)


async def create_session(name: str, command: str | None = None) -> str:
    """Create a new detached tmux session."""
    cmd = [TMUX, "new-session", "-d", "-s", name]
    if command:
        cmd.extend(["-c", command])
    out, rc = await _run(cmd)
    return "" if rc == 0 else f"[Error: {out}]"


async def kill_session(name: str) -> str:
    """Kill a tmux session."""
    out, rc = await _run([TMUX, "kill-session", "-t", name])
    return "" if rc == 0 else f"[Error: {out}]"
