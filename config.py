import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def _int(name: str, default: int) -> int:
    return int(_str(name) or default)


@dataclass(frozen=True)
class Config:
    phaze_api_key: str          # optional here: you can also paste it into the web page
    phaze_mcp_url: str
    anthropic_api_key: str      # optional: without it, Render Buddy only does pixel-diff stall detection
    model: str
    slack_bot_token: str        # xoxb-...
    slack_app_token: str        # xapp-..., for Socket Mode (buttons)
    slack_channel: str          # channel ID that render threads are posted in
    check_interval_sec: int     # how often to take a screenshot
    stall_minutes: int          # screen unchanged this long = suspected stall
    heartbeat_minutes: int      # ask Claude at least this often, to keep progress/ETA fresh
    port: int
    data_dir: Path

    @property
    def vision_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_bot_token and self.slack_app_token and self.slack_channel)

    @classmethod
    def load(cls) -> "Config":
        return cls(
            phaze_api_key=_str("PHAZE_API_KEY"),
            phaze_mcp_url=_str("PHAZE_MCP_URL", "http://127.0.0.1:41010/mcp"),
            anthropic_api_key=_str("ANTHROPIC_API_KEY"),
            model=_str("CLAUDE_MODEL", "claude-haiku-5-5"),
            slack_bot_token=_str("SLACK_BOT_TOKEN"),
            slack_app_token=_str("SLACK_APP_TOKEN"),
            slack_channel=_str("SLACK_CHANNEL"),
            check_interval_sec=_int("CHECK_INTERVAL_SEC", 60),
            stall_minutes=_int("STALL_MINUTES", 10),
            heartbeat_minutes=_int("HEARTBEAT_MINUTES", 15),
            port=_int("PORT", 8765),
            data_dir=Path(_str("DATA_DIR", str(ROOT / "data"))),
        )
