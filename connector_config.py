"""Per-installation settings. Credentials and account settings are never committed."""
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse

PROJECT = Path(__file__).resolve().parent
CONFIG = PROJECT / ".connector-config.json"
SECRETS = PROJECT / ".connector-secrets.json"
DEFAULTS = {"browser": "firefox", "port": 8765, "auth": "token", "interval": 1800}


def load_config(path=CONFIG):
    path = Path(path)
    return dict(DEFAULTS, **(json.loads(path.read_text()) if path.is_file() else {}))


def private_json(path, data):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".config-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def https_origin(value):
    parsed = urlparse(value.strip())
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Enter an HTTPS host URL, such as https://canvas.example.edu (no path or credentials)")
    return value.strip().rstrip("/")
