"""Optional bearer-token authentication for the OpenAI-compatible API."""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from ..config import settings


async def require_api_key(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency enforcing ``Authorization: Bearer <key>``.

    A no-op when ``CHAT2API_API_KEYS`` is unset, so local development needs no
    credentials.
    """
    allowed = settings.api_key_set
    if not allowed:
        return

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    if token not in allowed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key."
        )
