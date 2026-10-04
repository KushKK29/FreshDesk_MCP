"""Vercel entrypoint. Vercel's Python runtime looks for api/index.py exposing
an ASGI `app` object — this just re-exports the real app from src/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcp_http_server import app  # noqa: E402
