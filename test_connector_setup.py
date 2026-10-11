import argparse
import asyncio
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError

from cryptography.fernet import Fernet
import httpx
from fastmcp.server.auth.providers.github import GitHubTokenVerifier
from fastmcp.server.auth.auth import AccessToken

from canvas_mcp import Archive, build_server
from connector_auth import OwnerVerifier, github_auth
from connector_config import DEFAULTS, https_origin, load_config, private_json
import connector_setup


class SetupTests(unittest.TestCase):
    def test_readiness_waits_past_tunnel_error_and_requires_bearer_challenge(self):
        from unittest.mock import Mock
        server, tunnel = Mock(), Mock()
        server.poll.return_value = tunnel.poll.return_value = None
        pending = HTTPError("https://example/mcp", 502, "not ready", {}, None)
        ready = HTTPError("https://example/mcp", 401, "protected", {"WWW-Authenticate": "Bearer realm=Canvas"}, None)
        with patch.object(connector_setup, "urlopen", side_effect=[pending, ready]) as request, \
                patch.object(connector_setup.time, "sleep"):
            connector_setup.wait_for_endpoint("https://example", server, tunnel)
            self.assertEqual(request.call_count, 2)

    def test_host_validation(self):
        self.assertEqual(https_origin("https://canvas.example.edu/"), "https://canvas.example.edu")
        for invalid in ["http://canvas.example.edu", "https://user:secret@example.edu", "https://example.edu/courses/1", "https://example.edu?token=secret"]:
            with self.assertRaises(ValueError):
                https_origin(invalid)

    def test_portable_config_and_token_permissions(self):
        with tempfile.TemporaryDirectory(prefix="canvas setup with spaces ") as tmp:
            root = Path(tmp)
            args = argparse.Namespace(canvas_url="https://canvas.example.edu", browser="firefox",
                                      auth="token", port=8976, public_url="https://mcp.example.edu")
            with patch.object(connector_setup, "PROJECT", root), patch.object(connector_setup, "CONFIG", root / ".connector-config.json"), \
                    patch.object(connector_setup, "load_config", return_value=dict(DEFAULTS)):
                config = connector_setup.configure(args)
            self.assertEqual(config["canvas_url"], "https://canvas.example.edu")
            self.assertEqual(config["port"], 8976)
            for file in [".connector-config.json", ".mcp-token"]:
                self.assertEqual(stat.S_IMODE((root / file).stat().st_mode), 0o600)
            self.assertNotIn("elliottleow", (root / ".connector-config.json").read_text())

    def test_github_owner_gate(self):
        async def check():
            verifier = OwnerVerifier("Owner")
            for login, allowed in [("owner", True), ("other-user", False), (None, False)]:
                token = AccessToken(token="test", client_id="1", scopes=["read:user"], claims={"login": login})
                with patch.object(GitHubTokenVerifier, "verify_token", new=AsyncMock(return_value=token)):
                    self.assertEqual(await verifier.verify_token("test") is not None, allowed)
        asyncio.run(check())

    def test_oauth_discovery_registration_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secrets = root / "secrets.json"
            private_json(secrets, {"github_client_id": "test-id", "github_client_secret": "test-secret",
                                  "jwt_signing_key": "j" * 64, "storage_key": Fernet.generate_key().decode()})
            config = {"public_url": "https://mcp.example", "github_login": "owner"}
            async def check():
                auth = github_auth(config, secrets, root / "storage")
                server = build_server(Archive(root), auth)
                app = server.http_app(stateless_http=True, json_response=True)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://mcp.example") as client:
                    denied = await client.post("/mcp", json={})
                    self.assertEqual(denied.status_code, 401)
                    self.assertIn("resource_metadata", denied.headers["www-authenticate"])
                    metadata = await client.get("/.well-known/oauth-authorization-server")
                    self.assertEqual(metadata.status_code, 200)
                    self.assertIn("S256", metadata.json()["code_challenge_methods_supported"])
                    endpoint = metadata.json()["registration_endpoint"]
                    registered = await client.post(endpoint, json={
                        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"], "token_endpoint_auth_method": "none"})
                    self.assertEqual(registered.status_code, 201, registered.text)
                    client_id = registered.json()["client_id"]
                fresh = github_auth(config, secrets, root / "storage")
                self.assertIsNotNone(await fresh.get_client(client_id))
                self.assertEqual((await fresh.get_client(client_id)).redirect_uris[0].host, "claude.ai")
            asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
