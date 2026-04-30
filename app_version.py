"""Application version helpers."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
VERSION_FILE = APP_ROOT / "VERSION"


def get_version() -> str:
    env_version = os.environ.get("APP_VERSION", "").strip()
    if env_version:
        return env_version
    try:
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"
    return version or "0.0.0"


def get_build() -> str:
    env_build = os.environ.get("APP_BUILD", "").strip()
    if env_build:
        return env_build
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=APP_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()
