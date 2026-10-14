"""Liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from core.config import SETTINGS

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "model": SETTINGS.model}
