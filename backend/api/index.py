"""Vercel serverless entry point: exposes the FastAPI app (all routes are under /api)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402,F401
