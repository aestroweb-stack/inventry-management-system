"""Private Railway service that owns SigLIP weights and inference memory."""
from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Header, HTTPException, UploadFile

from services import visual_match_service


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Download/cache pretrained weights and initialize once before marking
    # this service ready. HF_HOME should point at its persistent Railway volume.
    visual_match_service._load_encoder()
    yield


app = FastAPI(title="Twins Lady Visual Model", docs_url=None, redoc_url=None, lifespan=lifespan)


def _authorize(authorization: str | None) -> None:
    expected = os.getenv("VISUAL_AI_API_KEY", "").strip()
    if not expected:
        raise HTTPException(503, "Visual model service authentication is not configured.")
    supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "Unauthorized")


@app.get("/health")
def health():
    # The lifespan hook only completes after weights load successfully.
    return {"status": "ready", "model": visual_match_service.MODEL_VERSION}


@app.post("/v1/embed")
async def embed_image(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    content = await file.read(visual_match_service.MAX_SCAN_BYTES + 1)
    try:
        visual_match_service.validate_image(content)
        values = await asyncio.to_thread(visual_match_service.image_embedding, content)
    except visual_match_service.InvalidProductImage as exc:
        raise HTTPException(400, str(exc)) from exc
    except visual_match_service.ImageModelUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"model": visual_match_service.MODEL_VERSION, "embedding": values}
