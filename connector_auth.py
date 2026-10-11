"""GitHub OAuth bridge restricted to the owner of this local Canvas installation."""
import json
from pathlib import Path

from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubTokenVerifier
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from cryptography.fernet import Fernet

from connector_config import PROJECT, SECRETS, https_origin


class OwnerVerifier(GitHubTokenVerifier):
    def __init__(self, login):
        super().__init__(required_scopes=["read:user"])
        if not login or not login.strip():
            raise ValueError("A GitHub owner username is required")
        self.owner_login = login.strip().casefold()

    async def verify_token(self, token):
        verified = await super().verify_token(token)
        if verified and str(verified.claims.get("login", "")).casefold() == self.owner_login:
            return verified
        return None


def github_auth(config, secrets_file=SECRETS, storage_dir=None):
    secrets = json.loads(Path(secrets_file).read_text())
    directory = Path(storage_dir or PROJECT / ".connector-oauth")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    storage = FernetEncryptionWrapper(DiskStore(directory=directory),
                                      fernet=Fernet(secrets["storage_key"].encode()))
    return OAuthProxy(
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id=secrets["github_client_id"],
        upstream_client_secret=secrets["github_client_secret"],
        token_verifier=OwnerVerifier(config["github_login"]),
        base_url=https_origin(config["public_url"]),
        redirect_path="/auth/callback",
        valid_scopes=["read:user"],
        allowed_client_redirect_uris=["https://chatgpt.com/*", "https://chat.openai.com/*",
                                      "https://claude.ai/*", "https://claude.com/*",
                                      "http://localhost:*/*", "http://127.0.0.1:*/*"],
        client_storage=storage,
        jwt_signing_key=secrets["jwt_signing_key"],
        require_authorization_consent=True,
    )
