"""Load local secret files into the process environment. Never log values."""

from __future__ import annotations

import os
from pathlib import Path

SECRET_FILES = (
    Path("/root/.secrets/flask-common.env"),
    Path("/root/.secrets/erudore-plugin.env"),
)


def load_secrets() -> None:
    for path in SECRET_FILES:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key:
                os.environ.setdefault(key, value)
