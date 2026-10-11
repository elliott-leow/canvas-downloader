"""Install user login services for Canvas MCP and a dashboard-managed Cloudflare tunnel."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from connector_config import load_config

PROJECT = Path(__file__).resolve().parent
AGENTS = Path.home() / "Library/LaunchAgents"
STATE = Path.home() / "Library/Application Support/Canvas Connector"
LOGS = Path.home() / "Library/Logs/Canvas Connector"
SERVER = "com.elliottleow.canvas-mcp"
TUNNEL = "com.elliottleow.canvas-cloudflare"
SYNC = "com.elliottleow.canvas-sync"
DOMAIN = f"gui/{os.getuid()}"


def loaded(label: str) -> bool:
    return subprocess.run(["launchctl", "print", f"{DOMAIN}/{label}"],
                          capture_output=True).returncode == 0


def install(label: str, arguments: list[str], interval: int | None = None):
    AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = AGENTS / f"{label}.plist"
    if path.exists():
        existing = plistlib.loads(path.read_bytes())
        if existing.get("Label") != label or existing.get("WorkingDirectory") != str(PROJECT):
            raise ValueError(f"Refusing to overwrite unrelated service: {path}")
    config = {
        "Label": label,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(PROJECT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "StandardOutPath": str(LOGS / f"{label}.out.log"),
        "StandardErrorPath": str(LOGS / f"{label}.err.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    if interval is not None:
        config["KeepAlive"] = False
        config["StartInterval"] = interval
    temporary = path.with_suffix(".plist.tmp")
    temporary.write_bytes(plistlib.dumps(config))
    temporary.chmod(0o600)
    subprocess.run(["plutil", "-lint", str(temporary)], check=True, capture_output=True)
    temporary.replace(path)
    if loaded(label):
        subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{label}"], check=True)
        # bootout returns before a gracefully stopping process has fully exited.
        for _ in range(150):
            if not loaded(label):
                break
            time.sleep(0.1)
        else:
            raise ValueError(f"Service is still shutting down: {label}. Retry after it exits.")
    subprocess.run(["launchctl", "enable", f"{DOMAIN}/{label}"], check=True)
    subprocess.run(["launchctl", "bootstrap", DOMAIN, str(path)], check=True)
    print(f"Installed login service: {label}")


def install_server():
    python = PROJECT / ".venv-mcp/bin/python"
    config = load_config()
    if not python.exists() or (config["auth"] == "token" and not (PROJECT / ".mcp-token").is_file()):
        raise ValueError("Install the MCP dependencies and create .mcp-token first")
    if not loaded(SERVER):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", config["port"])) == 0:
                raise ValueError("Connector port is in use. Stop the manually started MCP server with Control-C first.")
    install(SERVER, [str(python), str(PROJECT / "canvas_mcp.py"), "--transport", "http"])


def install_tunnel():
    binary = shutil.which("cloudflared")
    if not binary:
        raise ValueError("Install cloudflared with: brew install cloudflared")
    token = getpass.getpass("Paste only the Cloudflare tunnel token (hidden): ").strip()
    if len(token) < 40 or not re.fullmatch(r"[A-Za-z0-9+/=_-]+", token):
        raise ValueError("Expected the tunnel token, not the entire install command")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    path = STATE / "cloudflare-token"
    temporary = STATE / "cloudflare-token.tmp"
    # O_NOFOLLOW prevents following a malicious preexisting symlink.
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(token + "\n")
    temporary.replace(path)
    install(TUNNEL, [binary, "tunnel", "--no-autoupdate", "run", "--token-file", str(path)])
    config = load_config()
    print(f"Configure the dashboard route: {config.get('public_url', 'your hostname')} -> http://127.0.0.1:{config['port']}")
    if config.get("public_url"):
        print(f"Connector URL: {config['public_url']}/mcp")


def install_sync(interval):
    if interval < 300:
        raise ValueError("Sync interval must be at least 300 seconds")
    python = PROJECT / ".venv-mcp/bin/python"
    if not python.exists():
        raise ValueError("Install the MCP dependencies first")
    config = load_config()
    if not config.get("canvas_url"):
        raise ValueError("Configure your Canvas URL with ./setup.sh first")
    install(SYNC, [str(python), str(PROJECT / "canvas_sync.py")], interval)
    print(f"All-course sync scheduled every {interval // 60} minutes and at login.")


def status():
    for label in [SERVER, TUNNEL, SYNC]:
        result = subprocess.run(["launchctl", "print", f"{DOMAIN}/{label}"],
                                capture_output=True, text=True)
        if result.returncode:
            print(f"{label}: not loaded")
            continue
        print(label + ":")
        for line in result.stdout.splitlines():
            if re.match(r"\t(state|pid|last exit code|runs) =", line):
                print(line.strip())
    try:
        with urlopen(f"http://127.0.0.1:{load_config()['port']}/mcp", timeout=3) as response:
            print(f"Local MCP HTTP status: {response.status}")
    except HTTPError as error:
        print(f"Local MCP HTTP status: {error.code}" +
              (" (authentication required, as expected)" if error.code == 401 else ""))
    except URLError:
        print("Local MCP is not reachable")
    print(f"Logs: {LOGS}")
    sync_status = PROJECT / "downloads/.sync-status.json"
    if sync_status.is_file():
        print("Last Canvas sync:")
        print(json.dumps(json.loads(sync_status.read_text()), indent=2))


def uninstall():
    for label in [SYNC, TUNNEL, SERVER]:
        if loaded(label):
            subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{label}"], check=True)
        path = AGENTS / f"{label}.plist"
        if path.exists():
            existing = plistlib.loads(path.read_bytes())
            if existing.get("Label") == label and existing.get("WorkingDirectory") == str(PROJECT):
                path.unlink()
        print(f"Stopped and removed service: {label}")
    print("Credential files and logs were preserved.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install-server", "install-tunnel", "install-sync", "status", "uninstall"])
    parser.add_argument("--interval", type=int, default=1800, help="Canvas sync interval in seconds")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Login services require macOS. Run canvas_mcp.py and canvas_sync.py directly on other systems.")
    try:
        {"install-server": install_server, "install-tunnel": install_tunnel,
         "install-sync": lambda: install_sync(args.interval),
         "status": status, "uninstall": uninstall}[args.action]()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
