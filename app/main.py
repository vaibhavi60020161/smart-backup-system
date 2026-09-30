"""Team B REST API (FastAPI). Auto-generates the OpenAPI contract at /openapi.json and a test page at /docs."""
import base64
import binascii
import hashlib
import json
import os
import uuid
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .engine import Engine
from .errors import ApiError


class DedupRequest(BaseModel):
    file_id: str = Field(..., min_length=1, examples=["F1"])
    file_path: str = Field(..., min_length=1, examples=["files/report.pdf"])
    backup_id: str = Field(..., min_length=1, examples=["B1"])
    operation: str = "backup"
    chunking: Literal["fixed", "content"] = "fixed"
    chunk_size: int = Field(4096, ge=256, le=1_048_576)
    hash_algorithm: str = "sha256"
    expected_index_version: Optional[int] = None
    previous_result_id: Optional[str] = None


class VerifyRequest(BaseModel):
    dedup_result_id: Optional[str] = None
    file_id: Optional[str] = None
    algorithm: Literal["merkle", "checksum"] = "merkle"
    repetitions: int = Field(1, ge=1, le=20)


class ChunkPut(BaseModel):
    data_base64: str


class InvalidateRequest(BaseModel):
    key: Optional[str] = None
    all: bool = False


def create_app(engine=None, token=None):
    engine = engine or Engine.from_env()
    token = token or os.getenv("TEAMB_TOKEN", "dev-token")
    app = FastAPI(title="Team B - Deduplication & Integrity Engine", version="1.0.0",
                  description="Chunking, SHA-256, hash index (hash map / AVL), dedup and Merkle-tree verification.")
    app.state.engine = engine

    def meta(request):
        return {"correlation_id": request.state.cid, "api_version": "v1"}

    def ok(request, data, status=200, headers=None):
        return JSONResponse({"data": data, "meta": meta(request)}, status_code=status, headers=headers)

    def err(request, status, code, message, details=None):
        return JSONResponse({"error": {"code": code, "message": message, "details": details or []},
                             "meta": meta(request)}, status_code=status)

    # ----- cross-cutting: correlation id, metrics, error envelope -----
    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        try:
            response = await call_next(request)
        except Exception as e:                                   # unexpected -> 500 envelope
            response = err(request, 500, "INTERNAL_ERROR", "Unexpected server error", [type(e).__name__])
        engine.metrics.requests += 1
        if response.status_code >= 500:
            engine.metrics.failed += 1
        response.headers["X-Correlation-ID"] = request.state.cid
        return response

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return err(request, exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        details = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return err(request, 422, "VALIDATION_ERROR", "Request body/parameters are invalid", details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return err(request, exc.status_code, "HTTP_ERROR", str(exc.detail))

    def auth(authorization: Optional[str] = Header(None)):
        if authorization != f"Bearer {token}":
            raise ApiError(401, "UNAUTHORIZED", "Missing or invalid bearer token")

    secured = [Depends(auth)]

    # ----- routes -----
    @app.get("/health")
    def health(request: Request):
        return ok(request, {"status": "UP", "index_type": engine.idx.index.name, "index_version": engine.index_version})

    @app.post("/api/v1/dedup/compute", dependencies=secured, tags=["B1/B2 chunk + dedup"])
    def dedup_compute(body: DedupRequest, request: Request, idempotency_key: Optional[str] = Header(None)):
        if not idempotency_key:
            raise ApiError(400, "MISSING_IDEMPOTENCY_KEY", "Idempotency-Key header is required")
        req_hash = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
        out, replay = engine.run_idempotent(idempotency_key, req_hash, lambda: engine.compute(body.model_dump()))
        return ok(request, out, 200, {"Idempotent-Replay": "true"} if replay else None)

    @app.get("/api/v1/dedup/{result_id}", dependencies=secured, tags=["B1/B2 chunk + dedup"])
    def dedup_get(result_id: str, request: Request, include_chunks: bool = False):
        return ok(request, engine.get_result(result_id, include_chunks))

    @app.get("/api/v1/chunks/index", dependencies=secured, tags=["B1/B2 chunk + dedup"])
    def chunks_index(request: Request, limit: int = Query(50, ge=1, le=1000), offset: int = Query(0, ge=0)):
        return ok(request, engine.chunk_index(limit, offset))

    @app.put("/internal/v1/chunks/{chunk_id}", dependencies=secured, tags=["B1/B2 chunk + dedup"])
    def chunk_put(chunk_id: str, body: ChunkPut, request: Request):
        try:
            data = base64.b64decode(body.data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(422, "INVALID_BASE64", "data_base64 is not valid base64")
        return ok(request, engine.put_chunk(chunk_id, data))

    @app.get("/internal/v1/hash-index/{key}", dependencies=secured, tags=["B3 hash index"])
    def hash_index_get(key: str, request: Request):
        return ok(request, engine.index_lookup(key))

    @app.post("/internal/v1/hash-cache/invalidate", dependencies=secured, tags=["B3 hash index"])
    def cache_invalidate(body: InvalidateRequest, request: Request):
        if not body.all and not body.key:
            raise ApiError(422, "VALIDATION_ERROR", "Give a key or set all=true")
        return ok(request, engine.invalidate_cache(None if body.all else body.key))

    @app.post("/internal/v1/hash-index/rebuild", dependencies=secured, tags=["B3 hash index"])
    def index_rebuild(request: Request):
        return ok(request, engine.rebuild_index())

    @app.post("/api/v1/integrity/verify", dependencies=secured, tags=["B4 integrity"])
    def integrity_verify(body: VerifyRequest, request: Request):
        if not body.dedup_result_id and not body.file_id:
            raise ApiError(422, "VALIDATION_ERROR", "Give dedup_result_id or file_id")
        return ok(request, engine.verify(body.dedup_result_id, body.file_id, body.algorithm, body.repetitions))

    @app.get("/api/v1/metrics/dedup", dependencies=secured, tags=["B4 integrity"])
    def metrics(request: Request):
        return ok(request, engine.metrics_report())

    return app


app = create_app() if os.getenv("TEAMB_NO_AUTOSTART") != "1" else None
