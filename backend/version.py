"""Single source of truth for the deployed application version."""
from __future__ import annotations

import os


APP_VERSION = os.getenv("APP_VERSION", "1.1.0").strip() or "1.1.0"
