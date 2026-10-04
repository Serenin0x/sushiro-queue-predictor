"""Runtime defaults outside the checkout and commonly synced Desktop folders."""

import os
from pathlib import Path
import sys


def local_data_directory() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SUSHIWAIT" / "local"
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        base = Path(root) if root and Path(root).is_absolute() else Path.home() / "AppData" / "Local"
    else:
        root = os.environ.get("XDG_DATA_HOME")
        base = Path(root) if root and Path(root).is_absolute() else Path.home() / ".local" / "share"
    return base / "SUSHIWAIT" / "local"
