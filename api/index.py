from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
SOURCE_DATA = BACKEND / "data"
RUNTIME_DATA = Path(os.getenv("GRAPHRAG_DATA_DIR", "/tmp/graphrag-data"))

os.environ.setdefault("GRAPHRAG_DATA_DIR", str(RUNTIME_DATA))

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _ensure_runtime_data() -> None:
    if RUNTIME_DATA.exists():
        return
    if SOURCE_DATA.exists():
        shutil.copytree(SOURCE_DATA, RUNTIME_DATA, ignore=shutil.ignore_patterns("uploads"))
    else:
        (RUNTIME_DATA / "kg").mkdir(parents=True, exist_ok=True)
        (RUNTIME_DATA / "jobs").mkdir(parents=True, exist_ok=True)


_ensure_runtime_data()

from main import app  # noqa: E402
