import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8000
    ssl_certfile: str = str(Path(__file__).parent.parent / "certs" / "cert.pem")
    ssl_keyfile: str = str(Path(__file__).parent.parent / "certs" / "key.pem")
    claude_cli_path: str = os.environ.get("CLAUDE_CLI_PATH", str(Path.home() / ".local" / "bin" / "claude"))
    working_directory: str = str(Path.home())
    max_turns: int = 30
    cost_log_path: str = str(Path(__file__).parent.parent / "logs" / "cost.ndjson")

    # AI-Team agent database
    ai_team_db_path: str = str(Path.home() / "Library/Application Support/ai-team/ai-team.db")

    # Robot (SO-101)
    robot_port: str = "/dev/tty.usbmodem5A7C1169841"
    robot_id: str = "devwon_follower_arm"
    robot_camera_front: int = 0
    robot_camera_top: int = 1

    # Gemini
    gemini_api_key: str = field(default_factory=lambda: _load_gemini_key())
    gemini_model: str = "gemini-2.5-pro"
    hackathon_project_root: str = str(Path.home() / "hackathon-demo")

    # Instagram Graph API
    instagram_verify_token: str = os.environ.get("INSTAGRAM_VERIFY_TOKEN", "")
    instagram_page_token: str = os.environ.get("INSTAGRAM_PAGE_TOKEN", "")
    instagram_app_secret: str = os.environ.get("INSTAGRAM_APP_SECRET", "")
    instagram_fb_page_token: str = os.environ.get("INSTAGRAM_FB_PAGE_TOKEN", "")
    # Comma-separated list of Instagram usernames allowed to trigger webhooks (empty = allow all)
    instagram_allowed_usernames: str = os.environ.get("INSTAGRAM_ALLOWED_USERNAMES", "")


def _load_gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        env_path = Path.home() / "ai-team" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("GEMINI_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return key
