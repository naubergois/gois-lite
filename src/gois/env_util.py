from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path) -> int:
    """Tiny KEY=VALUE loader (no deps). Strips quotes, skips comments and
    blank lines, and refuses to overwrite values already in os.environ."""
    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded += 1
    return loaded
