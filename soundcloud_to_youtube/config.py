import os
import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".sc2yt"
CONFIG_FILE = CONFIG_DIR / "config.json"
CLIENT_SECRETS = CONFIG_DIR / "client_secrets.json"
TOKEN_FILE = CONFIG_DIR / "token.json"
STATE_FILE = CONFIG_DIR / "state.json"
WORK_DIR = CONFIG_DIR / "downloads"


def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        default_config = {
            "youtube": {
                "privacy_status": "private",
                "category_id": "10",
                "tags": ["soundcloud", "music"],
                # Optional: erzwingt Upload auf einen bestimmten Kanal (Brand Account).
                # channelId aus YouTube Studio -> Einstellungen -> Kanal -> Erweitert.
                "channel_id": ""
            },
            "scdl": {"onlymp3": True},
            "video": {"width": 1280, "height": 720},
            "upload": {"max_retries": 3, "retry_delay_seconds": 5}
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=2)
        print(f"Config erstellt: {CONFIG_FILE}")


def load_config():
    ensure_config_dir()
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_client_secrets_path():
    ensure_config_dir()
    if not CLIENT_SECRETS.exists():
        print(f"!!! Bitte lege deine client_secrets.json hier ab: {CLIENT_SECRETS}")
    return str(CLIENT_SECRETS)


def get_token_path():
    ensure_config_dir()
    return str(TOKEN_FILE)


def get_state_path():
    ensure_config_dir()
    return str(STATE_FILE)


def get_work_dir():
    ensure_config_dir()
    return str(WORK_DIR)
