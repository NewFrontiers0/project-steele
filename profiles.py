"""Local project-steele user profiles and session tokens."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional


APP_ROOT = Path(__file__).resolve().parent
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")


class ProfileError(ValueError):
    """Raised when a profile cannot be created or read."""


class AuthError(ValueError):
    """Raised when a username/password or session token is invalid."""


def _profile_path() -> Path:
    configured = os.environ.get("PROJECT_STEELE_USERS_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return APP_ROOT / "data" / "users.json"


def _session_seconds() -> int:
    try:
        hours = float(os.environ.get("PROFILE_SESSION_HOURS", "12"))
    except ValueError:
        hours = 12
    return max(1, int(hours * 3600))


def _normalise_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise ProfileError("Username must be 3-64 characters using letters, numbers, dot, dash, underscore, or @")
    return username.lower()


def _hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        260_000,
    )
    return digest.hex()


class UserStore:
    def __init__(self):
        self.path = _profile_path()
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "users": {}}
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as e:
            raise ProfileError(f"Profile store is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ProfileError("Profile store has an invalid format")
        data.setdefault("version", 1)
        data.setdefault("users", {})
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".users-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def create_user(self, username: str, password: str, api_key: str) -> str:
        username = _normalise_username(username)
        password = password or ""
        api_key = (api_key or "").strip()
        if len(password) < 8:
            raise ProfileError("Password must be at least 8 characters")
        if len(api_key) < 16:
            raise ProfileError("Enter a valid dashboard API key")

        with self._lock:
            data = self._load()
            users = data["users"]
            if username in users:
                raise ProfileError("A profile with that username already exists")
            salt = secrets.token_hex(16)
            now = int(time.time())
            users[username] = {
                "password_salt": salt,
                "password_hash": _hash_password(password, salt),
                "api_key": api_key,
                "created_at": now,
                "updated_at": now,
            }
            self._save(data)
        return username

    def authenticate(self, username: str, password: str) -> str:
        username = _normalise_username(username)
        with self._lock:
            data = self._load()
            user = data["users"].get(username)
        if not user:
            raise AuthError("Invalid username or password")
        expected = user.get("password_hash", "")
        actual = _hash_password(password or "", user.get("password_salt", ""))
        if not hmac.compare_digest(expected, actual):
            raise AuthError("Invalid username or password")
        return username

    def api_key_for_user(self, username: str) -> str:
        username = _normalise_username(username)
        with self._lock:
            data = self._load()
            user = data["users"].get(username)
        if not user:
            raise AuthError("Profile no longer exists")
        api_key = (user.get("api_key") or "").strip()
        if not api_key:
            raise AuthError("Profile has no linked API key")
        return api_key

    def create_session(self, username: str) -> str:
        username = _normalise_username(username)
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {
                "username": username,
                "expires_at": time.time() + _session_seconds(),
            }
        return token

    def invalidate_session(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def api_key_for_session(self, token: Optional[str]) -> str:
        if not token:
            raise AuthError("Sign in to continue")
        with self._lock:
            session = self._sessions.get(token)
            if not session or session["expires_at"] < time.time():
                if session:
                    self._sessions.pop(token, None)
                raise AuthError("Session expired")
            data = self._load()
            user = data["users"].get(session["username"])
        if not user:
            raise AuthError("Profile no longer exists")
        api_key = (user.get("api_key") or "").strip()
        if not api_key:
            raise AuthError("Profile has no linked API key")
        return api_key


user_store = UserStore()
