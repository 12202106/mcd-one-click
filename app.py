"""Vercel ASGI entrypoint. No startup/lifespan hook is needed for JSON MCP or OAuth."""
from gateway import app

__all__ = ["app"]
