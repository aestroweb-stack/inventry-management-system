"""SigLIP 2 product-photo indexing with fail-closed visual match decisions."""
from __future__ import annotations

import hashlib
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from sqlalchemy import text

from services import visual_match_service


MODEL_NAME = visual_match_service.MODEL_VERSION
VECTOR_DIMENSIONS = visual_match_service.VECTOR_DIMENSIONS
TABLE_NAME = "product_visual_embeddings_siglip2_v1"
DEV_MAX_CATALOG = int(os.getenv("VISUAL_SEARCH_DEV_MAX_CATALOG", "500"))
RATE_LIMIT_PER_MINUTE = int(os.getenv("VISUAL_SEARCH_RPM", "30"))
CACHE_TTL_SECONDS = int(os.getenv("VISUAL_SEARCH_CACHE_SECONDS", "60"))
REFERENCE_SHORTLIST_SIZE = max(25, int(os.getenv("VISUAL_SEARCH_REFERENCE_SHORTLIST", "100")))


def _setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        return max(lower, min(upper, float(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


# These are conservative starting values, not calibrated probabilities. The
# actual Twins Lady photo benchmark is needed before claiming production accuracy.
# Strict auto-match defaults favor precision. Lower-scoring cross-view results
# remain visible as reviewable candidates instead of being silently selected.
MIN_SIMILARITY = _setting("VISUAL_MATCH_MIN_SCORE", 0.90, -1.0, 1.0)
MIN_MARGIN = _setting("VISUAL_MATCH_MIN_MARGIN", 0.10, 0.0, 1.0)


class VisualSearchUnavailable(RuntimeError):
    """The app refused to guess because its model or index is not ready."""


@dataclass(frozen=True)
class SearchHit:
    product_id: int
    similarity: float
    reference_image_path: str | None = None


@dataclass(frozen=True)
class SearchResult:
    status: str
    matches: tuple[SearchHit, ...]
    message: str


_local_requests: dict[str, deque[float]] = {}
_local_rate_lock = Lock()
_local_embedding_lock = Lock()
_local_embeddings: dict[int, tuple[tuple[str, str, int, int], tuple[float, ...]]] = {}
_redis_client = None
_redis_checked = False


def _database_is_postgres(db) -> bool:
    return db.bind.dialect.name == "postgresql"


def _semantic_local_mode() -> bool:
    """Use the same SigLIP space for local catalogs when the private service exists.

    The Pillow descriptor is useful as an offline fallback, but cannot reliably
    link a folded, packaged garment to a model photo. A configured model service
    must therefore be used for both the query and every catalog reference.
    """
    return bool(os.getenv("VISUAL_AI_URL", "").strip())


def _vector_literal(values: tuple[float, ...]) -> str:
    if len(values) != VECTOR_DIMENSIONS:
        raise VisualSearchUnavailable(f"Visual model returned {len(values)} dimensions; expected {VECTOR_DIMENSIONS}.")
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def _image_embedding(content: bytes) -> tuple[float, ...]:
    """Get image features from the isolated model service in production.

    Local development may use the in-process encoder. Railway should set
    VISUAL_AI_URL so model memory/CPU cannot starve inventory transactions.
    """
    url = os.getenv("VISUAL_AI_URL", "").strip().rstrip("/")
    if not url:
        return visual_match_service.image_embedding(content)

    import httpx

    api_key = os.getenv("VISUAL_AI_API_KEY", "").strip()
    if not api_key:
        raise visual_match_service.ImageModelUnavailable(
            "VISUAL_AI_API_KEY is required when VISUAL_AI_URL is configured."
        )
    try:
        response = httpx.post(
            f"{url}/v1/embed",
            files={"file": ("product-image.jpg", content, "image/jpeg")},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=float(os.getenv("VISUAL_AI_TIMEOUT_SECONDS", "120")),
        )
        response.raise_for_status()
        raw = response.json().get("embedding")
        if not isinstance(raw, list) or len(raw) != VECTOR_DIMENSIONS:
            raise ValueError("Visual model service returned an invalid embedding shape.")
        values = tuple(float(value) for value in raw)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Visual model service returned non-finite embedding values.")
        _vector_literal(values)
        return values
    except Exception as exc:
        raise visual_match_service.ImageModelUnavailable(
            "The visual model service is unavailable or returned an invalid response."
        ) from exc


def _redis():
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        import redis
        client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
        client.ping()
        _redis_client = client
    except Exception:
        _redis_client = None
    return _redis_client


def allow_scan(client_key: str) -> bool:
    key = f"visual-search:rate:{client_key or 'unknown'}:{int(time.time() // 60)}"
    client = _redis()
    if client:
        try:
            count = int(client.incr(key))
            if count == 1:
                client.expire(key, 70)
            return count <= RATE_LIMIT_PER_MINUTE
        except Exception:
            pass
    now = time.monotonic()
    with _local_rate_lock:
        entries = _local_requests.setdefault(client_key or "unknown", deque())
        while entries and now - entries[0] >= 60:
            entries.popleft()
        if len(entries) >= RATE_LIMIT_PER_MINUTE:
            return False
        entries.append(now)
    return True


def _cached_hits(content: bytes) -> list[SearchHit] | None:
    client = _redis()
    if not client:
        return None
    key = f"visual-search:match:{MODEL_NAME}:{hashlib.sha256(content).hexdigest()}"
    try:
        raw = client.get(key)
        if not raw:
            return None
        return [SearchHit(int(product_id), float(score)) for product_id, score in (item.split(":", 1) for item in raw.split(",") if ":" in item)]
    except Exception:
        return None


def _cache_hits(content: bytes, hits: list[SearchHit]) -> None:
    client = _redis()
    if not client:
        return
    key = f"visual-search:match:{MODEL_NAME}:{hashlib.sha256(content).hexdigest()}"
    try:
        client.setex(key, CACHE_TTL_SECONDS, ",".join(f"{hit.product_id}:{hit.similarity:.8f}" for hit in hits))
    except Exception:
        pass


def clear_visual_cache() -> None:
    client = _redis()
    if not client:
        return
    try:
        for key in client.scan_iter(match="visual-search:match:*", count=200):
            client.delete(key)
    except Exception:
        pass


def ensure_postgres_index(engine) -> None:
    """Create a new model-versioned reference-image index, retaining old vectors."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                image_id INTEGER PRIMARY KEY REFERENCES product_images(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                model_name VARCHAR(160) NOT NULL,
                embedding vector({VECTOR_DIMENSIONS}) NOT NULL,
                indexed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{TABLE_NAME}_model ON {TABLE_NAME} (model_name)"))
        connection.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{TABLE_NAME}_product ON {TABLE_NAME} (product_id)"))
        connection.execute(text(f"""
            CREATE INDEX IF NOT EXISTS ix_{TABLE_NAME}_hnsw
            ON {TABLE_NAME} USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 128)
        """))


def index_product_image(db, product_id: int, image_id: int, content: bytes, image_path: Path | None = None) -> None:
    """Index one saved reference image; called after its DB row is created."""
    postgres = _database_is_postgres(db)
    # Keep heavyweight transformer inference out of the SQLite app process.
    values = _image_embedding(content) if postgres or _semantic_local_mode() else visual_match_service.local_visual_embedding(content)
    if postgres:
        # New uploads should be indexable without a separate schema setup step.
        # Existing catalog photos still need the one-off rebuild script.
        ensure_postgres_index(db.get_bind())
        embedding = _vector_literal(values)
        db.execute(text(f"""
            INSERT INTO {TABLE_NAME} (image_id, product_id, model_name, embedding, indexed_at)
            VALUES (:image_id, :product_id, :model_name, CAST(:embedding AS vector), CURRENT_TIMESTAMP)
            ON CONFLICT (image_id) DO UPDATE
            SET product_id = EXCLUDED.product_id, model_name = EXCLUDED.model_name,
                embedding = EXCLUDED.embedding, indexed_at = CURRENT_TIMESTAMP
        """), {"image_id": image_id, "product_id": product_id, "model_name": MODEL_NAME, "embedding": embedding})
    else:
        mode = MODEL_NAME if _semantic_local_mode() else "local-color-layout:v1"
        signature = (mode, "inline", 0, len(content))
        if image_path:
            stat = image_path.stat()
            signature = (mode, str(image_path.resolve()), stat.st_mtime_ns, stat.st_size)
        with _local_embedding_lock:
            _local_embeddings[image_id] = (signature, values)


def remove_product_embedding(db, product_id: int, image_id: int | None = None) -> None:
    if _database_is_postgres(db):
        if image_id is None:
            db.execute(text(f"DELETE FROM {TABLE_NAME} WHERE product_id = :product_id"), {"product_id": product_id})
        else:
            db.execute(text(f"DELETE FROM {TABLE_NAME} WHERE image_id = :image_id"), {"image_id": image_id})
    with _local_embedding_lock:
        if image_id is None:
            for key in [key for key in _local_embeddings if key < 0]:
                _local_embeddings.pop(key, None)
        else:
            _local_embeddings.pop(image_id, None)
    clear_visual_cache()


def _postgres_search(db, embedding: tuple[float, ...], limit: int) -> list[SearchHit]:
    vector = _vector_literal(embedding)
    rows = db.execute(text(f"""
        WITH nearest_references AS (
            SELECT e.product_id, e.image_id,
                   1 - (e.embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM {TABLE_NAME} AS e
            JOIN products AS p ON p.id = e.product_id
            JOIN product_images AS i ON i.id = e.image_id AND i.product_id = e.product_id
            WHERE e.model_name = :model_name AND p.is_deleted = FALSE
            ORDER BY e.embedding <=> CAST(:embedding AS vector)
            LIMIT :shortlist
        ), best_reference_per_product AS (
            SELECT product_id, image_id, similarity,
                   ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY similarity DESC) AS rank
            FROM nearest_references
        )
        SELECT b.product_id, b.similarity, i.image_path
        FROM best_reference_per_product AS b
        JOIN product_images AS i ON i.id = b.image_id
        WHERE b.rank = 1
        ORDER BY b.similarity DESC
        LIMIT :limit
    """), {"embedding": vector, "model_name": MODEL_NAME, "shortlist": REFERENCE_SHORTLIST_SIZE, "limit": limit}).all()
    return [SearchHit(int(row.product_id), float(row.similarity), str(row.image_path)) for row in rows]


def _development_search(db, embedding: tuple[float, ...], image_dir: Path, limit: int) -> list[SearchHit]:
    from models import Product, ProductImage
    products = db.query(Product).filter(Product.is_deleted == False).all()  # noqa: E712
    if len(products) > DEV_MAX_CATALOG:
        raise VisualSearchUnavailable("Local visual search is limited above the development catalog limit. Use PostgreSQL with pgvector.")
    product_ids = {product.id for product in products}
    images = db.query(ProductImage).filter(ProductImage.product_id.in_(product_ids)).all() if product_ids else []
    images_by_product: dict[int, list[ProductImage]] = {}
    for image in images:
        images_by_product.setdefault(image.product_id, []).append(image)
    product_scores: dict[int, tuple[float, str]] = {}
    for product in products:
        for image in images_by_product.get(product.id, []):
            candidate = (image_dir / Path(str(image.image_path)).name).resolve()
            try:
                candidate.relative_to(image_dir.resolve())
                stat = candidate.stat()
                mode = MODEL_NAME if _semantic_local_mode() else "local-color-layout:v1"
                signature = (mode, str(candidate), stat.st_mtime_ns, stat.st_size)
                with _local_embedding_lock:
                    cached = _local_embeddings.get(image.id)
                if not cached or cached[0] != signature:
                    ref_content = candidate.read_bytes()
                    ref_embedding = _image_embedding(ref_content) if _semantic_local_mode() else visual_match_service.local_visual_embedding(ref_content)
                    with _local_embedding_lock:
                        _local_embeddings[image.id] = (signature, ref_embedding)
                else:
                    ref_embedding = cached[1]
                score = sum(left * right for left, right in zip(embedding, ref_embedding))
                if score > product_scores.get(product.id, (-1.0, ""))[0]:
                    product_scores[product.id] = (score, f"/static/product_images/{candidate.name}")
            except (OSError, ValueError, visual_match_service.InvalidProductImage):
                continue
    return [SearchHit(product_id, score, image_path) for product_id, (score, image_path) in sorted(product_scores.items(), key=lambda item: item[1][0], reverse=True)[:limit]]


def _decide(hits: list[SearchHit]) -> SearchResult:
    if not hits:
        return SearchResult("no_match", (), "No usable product reference photos are indexed.")
    top = hits[0]
    if top.similarity < 0.35:
        return SearchResult("no_match", (), "No catalog design looked sufficiently related. Try a clear, closer photo of the garment.")
    margin = top.similarity - hits[1].similarity if len(hits) > 1 else 1.0
    if top.similarity < MIN_SIMILARITY or margin < MIN_MARGIN:
        return SearchResult("ambiguous", tuple(hits[:5]), "Possible matches found. Review the product photos and confirm the exact design.")
    return SearchResult("matched", (top,), "A strong visual match was found. Sizes and stock are read live from the database.")


def _decide_local(hits: list[SearchHit]) -> SearchResult:
    """Use conservative thresholds for the non-semantic local descriptor."""
    if not hits or hits[0].similarity < 0.66:
        return SearchResult("no_match", (), "No local visual candidate was similar enough. Try a clear photo with the garment filling the frame.")
    top = hits[0]
    margin = top.similarity - hits[1].similarity if len(hits) > 1 else 1.0
    if top.similarity >= 0.995 and margin >= 0.15:
        return SearchResult("matched", (top,), "A near-identical catalog photo matched. Verify the design before using its stock.")
    return SearchResult("ambiguous", tuple(hits[:3]), "Local color/layout suggestions. Review the product photos before choosing a design.")


def _decide_semantic_local(hits: list[SearchHit]) -> SearchResult:
    """Keep cross-view semantic candidates visible for staff confirmation.

    SigLIP cosine values are not confidence percentages and packaged photos can
    score lower than model-to-model shots. Below the auto-match threshold, show
    ranked candidate photos for confirmation instead of hiding them as no-match.
    """
    if not hits or hits[0].similarity < 0.35:
        return SearchResult("no_match", (), "No catalog photo looks sufficiently related. Try a closer, clearer photo of the garment.")
    top = hits[0]
    margin = top.similarity - hits[1].similarity if len(hits) > 1 else 1.0
    if top.similarity >= MIN_SIMILARITY and margin >= MIN_MARGIN:
        return SearchResult("matched", (top,), "A strong visual match was found. Verify the product before using its live stock.")
    return SearchResult("ambiguous", tuple(hits[:5]), "Possible product matches found. Compare the saved photos and confirm the correct design.")


def search(db, content: bytes, image_dir: Path, limit: int) -> SearchResult:
    """Rank saved design reference photos. Recognition never modifies inventory."""
    postgres = _database_is_postgres(db)
    if postgres:
        embedding = _image_embedding(content)
        try:
            hits = _postgres_search(db, embedding, max(limit, 5))
        except Exception as exc:
            raise VisualSearchUnavailable(f"SigLIP 2 PostgreSQL index unavailable. Build {TABLE_NAME} and retry.") from exc
    else:
        semantic = _semantic_local_mode()
        embedding = _image_embedding(content) if semantic else visual_match_service.local_visual_embedding(content)
        hits = _development_search(db, embedding, image_dir, max(limit, 5))
        return _decide_semantic_local(hits) if semantic else _decide_local(hits)
    return _decide(hits)
