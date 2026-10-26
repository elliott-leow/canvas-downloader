"""Guided, free local setup for the Canvas MCP connector."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from cryptography.fernet import Fernet

from canvas_downloader import SUPPORTED_BROWSERS
from archive_io import trim_connector_logs
from connector_config import CONFIG, PROJECT, SECRETS, https_origin, load_config, private_json


def prompt(label, default=""):
    value = input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
    return value or default


def configure(args):
    config = load_config()
    canvas_url = args.canvas_url or prompt("Your Canvas website URL", config.get("canvas_url", ""))
    canvas_url = https_origin(canvas_url)
    if config.get("canvas_url") and config["canvas_url"] != canvas_url and (PROJECT / "downloads/.sync-state.json").exists():
        raise ValueError("Use a separate clone for a different Canvas account/host so archives remain separate.")
    browser = args.browser or prompt("Browser signed into Canvas", config["browser"])
    if browser not in SUPPORTED_BROWSERS:
        raise ValueError("Supported browsers: " + ", ".join(SUPPORTED_BROWSERS))
    auth = args.auth or prompt("Authentication: github (both web apps), token (Claude only)", config.get("auth", "github") if CONFIG.exists() else "github")
    if auth not in ("github", "token"):
        raise ValueError("Choose github or token")
    config.update(canvas_url=canvas_url, browser=browser, auth=auth, port=args.port or config["port"])
    for option in ('include_personal', 'include_groups'):
        if getattr(args, option, None) is not None:
            config[option] = getattr(args, option)
    if args.public_url:
        config["public_url"] = https_origin(args.public_url)
    private_json(CONFIG, config)
    (PROJECT / "downloads").mkdir(exist_ok=True)
    token_file = PROJECT / ".mcp-token"
    if not token_file.exists():
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(48) + "\n")
    print("Saved private local settings. Your Canvas password is not requested.")
    return config


def configure_oauth(config, force=False):
    if not config.get("public_url"):
        raise ValueError("Run ./setup.sh quick first, or configure --public-url https://your-host.example")
    url = https_origin(config["public_url"])
    if SECRETS.is_file() and config.get("github_login") and not force:
        print(f"GitHub callback URL must be: {url}/auth/callback")
        stored = json.loads(SECRETS.read_text())
        if stored.get("callback_url") != url + "/auth/callback":
            input("Update the GitHub OAuth app callback to that exact URL, then press Enter: ")
            stored["callback_url"] = url + "/auth/callback"
            private_json(SECRETS, stored)
        return
    print("\nCreate a free GitHub OAuth app at https://github.com/settings/applications/new")
    print("Application name: Canvas Files (your own installation)")
    print(f"Homepage URL: {url}")
    print(f"Authorization callback URL: {url}/auth/callback")
    print("Register the application and generate a client secret. Keep that page open.")
    login = prompt("Your GitHub username (only this account will be allowed)")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login):
        raise ValueError("Enter your GitHub username, not an email address")
    client_id = prompt("GitHub OAuth app Client ID")
    client_secret = getpass.getpass("GitHub OAuth app Client Secret (hidden): ").strip()
    if not client_id or not client_secret:
        raise ValueError("Both the GitHub Client ID and Client Secret are required")
    previous = json.loads(SECRETS.read_text()) if SECRETS.exists() else {}
    private_json(SECRETS, {
        "github_client_id": client_id, "github_client_secret": client_secret,
        "jwt_signing_key": previous.get("jwt_signing_key") or secrets.token_urlsafe(48),
        "storage_key": previous.get("storage_key") or Fernet.generate_key().decode(),
        "callback_url": url + "/auth/callback",
    })
    config.update(auth="github", github_login=login)
    private_json(CONFIG, config)
    print("OAuth configured with encrypted persistent token storage and owner-only access.")


def cloudflared():
    binary = shutil.which("cloudflared")
    if not binary and shutil.which("brew"):
        print("Installing the free Cloudflare Tunnel client with Homebrew...")
        subprocess.run(["brew", "install", "cloudflared"], check=True)
        binary = shutil.which("cloudflared")
    if not binary:
        raise ValueError("Install cloudflared first: https://developers.cloudflare.com/tunnel/downloads/")
    return binary


def services(config):
    if config["auth"] == "github":
        configure_oauth(config)
    subprocess.run([sys.executable, str(PROJECT / "mac_services.py"), "install-server"], check=True)
    subprocess.run([sys.executable, str(PROJECT / "mac_services.py"), "install-sync",
                    "--interval", str(config["interval"])], check=True)


def wait_for_endpoint(url, server, tunnel, timeout=90):
    """A printed Cloudflare hostname is not proof the origin is reachable."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None or tunnel.poll() is not None:
            raise ValueError("The server or tunnel exited during startup; check .connector-logs")
        try:
            with urlopen(url + "/mcp", timeout=3):
                pass
        except HTTPError as error:
            if error.code == 401 and error.headers.get("WWW-Authenticate", "").lower().startswith("bearer"):
                return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.5)
    raise ValueError("The tunnel did not become reachable; check .connector-logs/quick-tunnel.log")


def quick(config):
    import socket
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", config["port"])) == 0:
            raise ValueError("The connector port is already in use. Stop that installation or configure a different --port.")
    directory = PROJECT / ".connector-logs"
    directory.mkdir(exist_ok=True, mode=0o700)
    processes = []
    log_files = []
    try:
        path = directory / "quick-tunnel.log"
        tunnel_log = path.open("w")
        log_files.append(tunnel_log)
        tunnel = subprocess.Popen([cloudflared(), "tunnel", "--no-autoupdate", "--url",
                                   f"http://127.0.0.1:{config['port']}"], stdout=tunnel_log, stderr=tunnel_log)
        processes.append(tunnel)
        deadline = time.monotonic() + 60
        public_url = None
        while time.monotonic() < deadline and tunnel.poll() is None:
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", path.read_text())
            if match:
                public_url = match.group()
                break
            time.sleep(0.2)
        if not public_url:
            raise ValueError(f"No temporary tunnel URL was received. Check {path}")
        config["public_url"] = public_url
        private_json(CONFIG, config)
        if config["auth"] == "github":
            configure_oauth(config)
        server_log = (directory / "server.log").open("a")
        log_files.append(server_log)
        server = subprocess.Popen([sys.executable, str(PROJECT / "canvas_mcp.py"), "--transport", "http"],
                                  stdout=server_log, stderr=server_log)
        processes.append(server)
        print("Waiting for the authenticated HTTPS endpoint to become reachable...")
        wait_for_endpoint(public_url, server, tunnel)
        print(f"\nConnector URL: {public_url}/mcp")
        if config["auth"] == "github":
            print("ChatGPT: add a custom MCP server, choose OAuth, then sign into your GitHub account.")
            print("Claude: add a custom connector, choose Sign in now and Register automatically.")
            print("Leave OAuth Client ID / Secret fields in the chat apps blank.")
        else:
            print("Claude: choose No sign in and add Authorization: Bearer <your .mcp-token> under Request headers.")
            print("ChatGPT's public-server route requires GitHub OAuth; token mode is for Claude or local testing.")
        print("Keep this Terminal open. Control-C stops this temporary setup.")
        print("New quick tunnel URLs require updating the GitHub callback URL and re-adding the connector.")
        next_sync = 0
        sync = None
        next_trim = 0
        while tunnel.poll() is None and server.poll() is None:
            if time.monotonic() >= next_trim:
                trim_connector_logs(PROJECT)
                next_trim = time.monotonic() + 30
            if time.monotonic() >= next_sync and (sync is None or sync.poll() is not None):
                sync_log = (directory / "sync.log").open("a")
                try:
                    sync = subprocess.Popen([sys.executable, str(PROJECT / "canvas_sync.py")],
                                            stdout=sync_log, stderr=sync_log)
                finally:
                    sync_log.close()
                processes = [process for process in processes if process.poll() is None]
                processes.append(sync)
                next_sync = time.monotonic() + config["interval"]
            time.sleep(1)
        raise ValueError(f"The server or tunnel exited. Check logs in {directory}")
    except KeyboardInterrupt:
        print("\nStopping temporary connector processes.")
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for stream in log_files:
            stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="quick",
                        choices=["quick", "configure", "oauth", "services", "tunnel", "status", "sync", "run", "uninstall"])
    parser.add_argument("--canvas-url")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS)
    parser.add_argument("--auth", choices=["github", "token"])
    parser.add_argument("--public-url")
    parser.add_argument("--port", type=int)
    parser.add_argument('--include-personal', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--include-groups', action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()
    if args.port is not None and not 1024 <= args.port <= 65535:
        parser.error("Choose a port between 1024 and 65535")
    try:
        config = load_config()
        if args.action == "configure" or not config.get("canvas_url") or any(
                [args.canvas_url, args.browser, args.auth, args.public_url, args.port]) or args.include_personal is not None or args.include_groups is not None:
            config = configure(args)
        if args.action == "quick":
            quick(config)
        elif args.action == "oauth":
            configure_oauth(config, force=True)
        elif args.action == "services":
            services(config)
        elif args.action == "tunnel":
            cloudflared()
            subprocess.run([sys.executable, str(PROJECT / "mac_services.py"), "install-tunnel"], check=True)
        elif args.action in ("status", "uninstall"):
            subprocess.run([sys.executable, str(PROJECT / "mac_services.py"), args.action], check=True)
        elif args.action == "sync":
            subprocess.run([sys.executable, str(PROJECT / "canvas_sync.py")], check=True)
        elif args.action == "run":
            subprocess.run([sys.executable, str(PROJECT / "canvas_mcp.py"), "--transport", "http"], check=True)
        else:
            print("Next: ./setup.sh quick (free temporary URL), or ./setup.sh oauth then ./setup.sh services (stable URL).")
    except (ValueError, OSError, subprocess.CalledProcessError, EOFError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
