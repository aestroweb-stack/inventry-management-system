import asyncio
import hmac
import os
import shutil
import uuid
import re
import logging
from contextlib import asynccontextmanager
from importlib.util import find_spec
from difflib import SequenceMatcher
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Query, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, RedirectResponse, Response, JSONResponse
from sqlalchemy import case, func, or_, delete as sa_delete, inspect as sa_inspect, text
from sqlalchemy.orm import Session, joinedload, selectinload, contains_eager, aliased

from database import Base, engine, get_db, SessionLocal
import models
import schemas
from services import (
    inventory_service, alert_service, excel_service, reorder_service,
    barcode_service, bulk_stock_service, bulk_product_service,
    visual_match_service, visual_search_service, cloud_assistant_service, claude_photo_service,
)
import inventory_mcp
from services import app_auth
from mcp.server.transport_security import TransportSecuritySettings

Base.metadata.create_all(bind=engine)
logger = logging.getLogger(__name__)
ACTIVE_ORDER_STATUSES = (
    models.OrderLineStatus.PENDING,
    models.OrderLineStatus.INSUFFICIENT_STOCK,
)


def _index_product_photo_background(product_id: int, image_id: int, content: bytes, image_path: str) -> None:
    """Index a saved catalog photo after the upload response has been returned.

    SigLIP's first model load can take several seconds on a small Railway
    service. Photo saving and inventory creation must not wait for that work.
    """
    db = SessionLocal()
    path = Path(image_path)
    try:
        visual_search_service.index_product_image(db, product_id, image_id, content, path)
        db.commit()
        visual_search_service.clear_visual_cache()
    except Exception as exc:
        db.rollback()
        try:
            visual_search_service.remove_product_embedding(db, product_id, image_id)
            db.commit()
        except Exception:
            db.rollback()
        logger.warning("Product photo saved; visual indexing queued for later rebuild (%s).", type(exc).__name__)
    finally:
        db.close()


def migrate_legacy_brand_data_explicitly(target_engine=engine):
    """Optional legacy cleanup; never invoke automatically during app startup.

    This changes saved product names and drops a database column. Run it only
    as a separately reviewed migration after taking a restorable backup.
    """
    columns = {column["name"] for column in sa_inspect(target_engine).get_columns("products")}
    if "brand" not in columns:
        return
    # Preserve product descriptions while removing an identical leading brand
    # prefix (for example, "CATALOG LABEL Women..." becomes "Women...").
    with target_engine.begin() as connection:
        connection.execute(text("""
            UPDATE products
            SET name = LTRIM(SUBSTR(name, LENGTH(brand) + 1))
            WHERE brand IS NOT NULL AND TRIM(brand) != ''
              AND LOWER(SUBSTR(name, 1, LENGTH(brand))) = LOWER(brand)
        """))
        connection.execute(text("ALTER TABLE products DROP COLUMN brand"))

def _mcp_transport_security():
    allowed_hosts = {"localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"}
    public_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().lower().rstrip(".")
    if public_domain:
        allowed_hosts.update({public_domain, f"{public_domain}:*"})
    allowed_hosts.update(
        value.strip().lower() for value in os.getenv("INVENTORY_MCP_ALLOWED_HOSTS", "").split(",") if value.strip()
    )
    allowed_origins = {
        f"https://{public_domain}", f"http://{public_domain}"} if public_domain else set()
    allowed_origins.update(
        value.strip() for value in os.getenv("INVENTORY_MCP_ALLOWED_ORIGINS", "").split(",") if value.strip()
    )
    return TransportSecuritySettings(allowed_hosts=sorted(allowed_hosts), allowed_origins=sorted(allowed_origins))


# FastMCP 1.x takes transport configuration in its constructor/settings;
# streamable_http_app() itself takes no keyword arguments in the deployed SDK.
# Configure the mounted endpoint before the session manager is initialized.
inventory_mcp.mcp.settings.streamable_http_path = "/"
inventory_mcp.mcp.settings.json_response = True
inventory_mcp.mcp.settings.transport_security = _mcp_transport_security()
_inventory_mcp_http_app = inventory_mcp.mcp.streamable_http_app()


@asynccontextmanager
async def _app_lifespan(_app):
    async with inventory_mcp.mcp.session_manager.run():
        yield


app = FastAPI(title="Inventory Management System", version="1.4", lifespan=_app_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("APP_ALLOWED_ORIGINS", "").split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)


@app.middleware("http")
async def protect_inventory_mcp(request: Request, call_next):
    """Require a private key for the entire MCP transport; never log credentials."""
    if (request.url.path == "/api/mcp" or request.url.path.startswith("/api/mcp/")) and request.method != "OPTIONS":
        # DISHA_MCP_API_KEY is the legacy Railway variable name; keep it as a
        # fallback so existing deployments can authenticate until renamed.
        expected = (os.getenv("INVENTORY_MCP_API_KEY") or os.getenv("DISHA_MCP_API_KEY") or "").strip()
        if len(expected) < 32:
            return Response(status_code=404)
        supplied = request.headers.get("x-api-key", "").strip()
        if not supplied:
            authorization = request.headers.get("authorization", "")
            if authorization.lower().startswith("bearer "):
                supplied = authorization[7:].strip()
        if not supplied or not hmac.compare_digest(supplied, expected):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    # Stored product images are private inventory data; require the same login
    # as inventory reads (the generic static frontend remains public).
    if request.url.path.startswith("/static/product_images/"):
        if app_auth.deployment_missing_password():
            return Response(status_code=503)
        if app_auth.required() and not app_auth.verify_session(request.cookies.get(app_auth.COOKIE, "")):
            return Response(status_code=401)
    # Other API routes use a separate operator login, not the remote MCP key.
    if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/mcp"):
        if request.url.path not in ("/api/auth/login", "/api/auth/status", "/api/health"):
            if app_auth.deployment_missing_password():
                return JSONResponse({"detail": "Set APP_PASSWORD in Railway Variables to enable the inventory API."}, status_code=503)
            if app_auth.required() and not app_auth.verify_session(request.cookies.get(app_auth.COOKIE, "")):
                return JSONResponse({"detail": "Login required."}, status_code=401)
            if app_auth.required() and request.method not in ("GET", "HEAD", "OPTIONS") and not app_auth.origin_allowed(request):
                return JSONResponse({"detail": "Invalid request origin."}, status_code=403)
    return await call_next(request)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
PRODUCT_IMAGE_DIR = Path(os.getenv("PRODUCT_IMAGE_DIR") or str(STATIC_DIR / "product_images")).resolve()
PRODUCT_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
# Mount the persistent Railway image directory before the general static mount.
app.mount("/static/product_images", StaticFiles(directory=PRODUCT_IMAGE_DIR), name="product-images")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

FRONTEND_DIR = BASE_DIR.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

app.mount("/api/mcp", _inventory_mcp_http_app, name="inventory-mcp")


@app.get("/api/auth/status")
def auth_status(request: Request):
    return {"login_required": app_auth.required(),
            "authenticated": app_auth.verify_session(request.cookies.get(app_auth.COOKIE, "")) if app_auth.required() else True,
            "configuration_required": app_auth.deployment_missing_password()}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    if not app_auth.origin_allowed(request):
        raise HTTPException(403, "Invalid login origin.")
    if app_auth.deployment_missing_password():
        raise HTTPException(503, "Set APP_PASSWORD in Railway Variables.")
    if not app_auth.required():
        return {"ok": True}
    client_key = request.client.host if request.client else "unknown"
    if not app_auth.login_allowed(client_key):
        raise HTTPException(429, "Too many failed logins. Retry later.")
    data = await request.json()
    if not isinstance(data, dict) or not app_auth.verify_password(str(data.get("password", ""))):
        app_auth.record_failed_login(client_key)
        # Generic error to avoid leaking credentials.
        raise HTTPException(401, "Incorrect password.")
    app_auth.clear_failed_logins(client_key)
    result = JSONResponse({"ok": True})
    result.set_cookie(app_auth.COOKIE, app_auth.create_session(), httponly=True,
                      secure=bool(os.getenv("RAILWAY_ENVIRONMENT") or request.url.scheme == "https"),
                      samesite="strict", max_age=app_auth.SESSION_AGE, path="/")
    return result


@app.post("/api/auth/logout")
def auth_logout():
    result = JSONResponse({"ok": True})
    result.delete_cookie(app_auth.COOKIE, path="/")
    return result


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/assistant/status")
def assistant_cloud_status():
    """Expose cloud readiness only; never return any server-side credential."""
    configured = cloud_assistant_service.is_configured()
    return {
        "cloud_enabled": configured,
        "provider": cloud_assistant_service.provider_name().capitalize() if configured else None,
        "model": cloud_assistant_service.model_name() if configured else None,
        "inventory_tools": configured,
        "writes_require_confirmation": True,
    }


@app.post("/api/assistant/chat")
async def assistant_cloud_chat(request: Request):
    """Run MYRAA on Railway so browser clients do not need a local companion."""
    client_key = request.client.host if request.client else "unknown"
    if not cloud_assistant_service.check_rate_limit(client_key):
        raise HTTPException(status_code=429, detail="Too many assistant requests. Wait a minute and retry.")
    try:
        payload = await request.json()
        result = await cloud_assistant_service.chat(payload.get("messages") if isinstance(payload, dict) else None)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        status = 503 if "not configured" in str(exc).lower() else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.post("/api/assistant/confirm")
async def assistant_cloud_confirm(request: Request):
    """Commit one server-prepared change only after the UI's explicit confirm."""
    try:
        payload = await request.json()
        result = await cloud_assistant_service.confirm_action(payload.get("confirmation_token") if isinstance(payload, dict) else "")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/", include_in_schema=False)
def app_home():
    """Open the inventory UI directly from the Railway service root."""
    return RedirectResponse(url="/app/", status_code=307)

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------ #
def _variant_status(v: models.Variant) -> str:
    if v.last_counted_at is None:
        return "UNCOUNTED"
    if v.current_stock <= 0:
        return "OUT"
    if v.current_stock <= v.reorder_threshold:
        return "LOW"
    return "OK"


def _variant_out(v: models.Variant) -> dict:
    return {
        "variant_code": v.variant_code, "size": v.size, "current_stock": v.current_stock,
        "reorder_threshold": v.reorder_threshold, "target_stock_level": v.target_stock_level,
        "status": _variant_status(v),
    }


def _order_line_dispatchability(db: Session, target_lines) -> dict[int, int]:
    """Allocate counted physical stock to pending order lines in FIFO order.

    The returned quantity is informational for the UI and also limits a
    dispatch request to the quantity currently available for each selected
    line. Looking across batches prevents the same physical pieces from making
    every pending batch appear ready at once.
    """
    target_ids = {line.id for line in target_lines if line.status in ACTIVE_ORDER_STATUSES}
    variant_ids = {line.variant_id for line in target_lines if line.variant_id is not None}
    if not target_ids or not variant_ids:
        return {}

    pending_lines = (
        db.query(models.OrderLine)
        .join(models.OrderUpload, models.OrderLine.order_upload_id == models.OrderUpload.id)
        .options(joinedload(models.OrderLine.variant))
        .filter(
            models.OrderLine.status.in_(ACTIVE_ORDER_STATUSES),
            models.OrderLine.variant_id.in_(variant_ids),
        )
        .order_by(models.OrderUpload.uploaded_at.asc(), models.OrderUpload.id.asc(), models.OrderLine.id.asc())
        .all()
    )
    stock_left: dict[int, int] = {}
    dispatchable: dict[int, int] = {}
    for line in pending_lines:
        variant = line.variant
        if variant is None:
            continue
        if variant.id not in stock_left:
            stock_left[variant.id] = max(int(variant.current_stock or 0), 0) if variant.last_counted_at else 0
        remaining = max(int(line.qty_ordered or 0) - int(line.qty_fulfilled or 0), 0)
        quantity = min(remaining, stock_left[variant.id])
        if line.id in target_ids:
            dispatchable[line.id] = quantity
        stock_left[variant.id] -= quantity
    return dispatchable


def _product_out(p: models.Product) -> dict:
    live_variants = [v for v in p.variants if not v.is_deleted]
    image_paths = []
    if p.image_path:
        image_paths.append(p.image_path)
    image_paths.extend(
        image.image_path for image in sorted(
            p.reference_images,
            key=lambda image: (not image.is_primary, image.id),
        )
        if image.image_path and image.image_path not in image_paths
    )
    return {
        "id": p.id, "sku": p.sku, "name": p.name, "category": p.category,
        "image_path": p.image_path,
        "images": image_paths,
        "vendor": {"id": p.vendor.id, "name": p.vendor.name, "contact": p.vendor.contact} if p.vendor else None,
        "variants": [_variant_out(v) for v in sorted(live_variants, key=lambda v: (bulk_stock_service.SIZE_ORDER.get(v.size, 99), v.size))],
        "total_stock": sum(v.current_stock for v in live_variants),
    }


def _variant_row(v: models.Variant) -> dict:
    p = v.product
    return {
        "variant_code": v.variant_code, "sku": p.sku, "size": v.size, "product_name": p.name,
        "category": p.category, "vendor": p.vendor.name if p.vendor else None,
        "image_path": p.image_path, "current_stock": v.current_stock,
        "reorder_threshold": v.reorder_threshold, "target_stock_level": v.target_stock_level,
        "status": _variant_status(v), "last_counted_at": v.last_counted_at,
    }


def _get_product_by_sku(db: Session, sku: str, include_deleted: bool = False) -> models.Product:
    q = db.query(models.Product).options(
        selectinload(models.Product.variants),
        selectinload(models.Product.reference_images),
        joinedload(models.Product.vendor),
    ) \
        .filter(models.Product.sku == sku.strip().upper())
    if not include_deleted:
        q = q.filter(models.Product.is_deleted == False)  # noqa: E712
    product = q.first()
    if not product:
        raise HTTPException(404, f"Product '{sku}' not found")
    return product


def _get_variant_by_code(db: Session, variant_code: str, include_deleted: bool = False) -> models.Variant:
    code = variant_code.strip().upper()
    # Accept both legacy `SKU-SIZE` labels and the canonical `SKU_SIZE`
    # format. New variants are always created with the underscore format.
    candidates = [code]
    if "-" in code:
        candidates.append(code.replace("-", "_"))
    if "_" in code:
        candidates.append(code.rsplit("_", 1)[0] + "-" + code.rsplit("_", 1)[1])
    q = db.query(models.Variant).options(joinedload(models.Variant.product)) \
        .filter(models.Variant.variant_code.in_(list(dict.fromkeys(candidates))))
    if not include_deleted:
        q = q.filter(models.Variant.is_deleted == False)  # noqa: E712
    variant = q.first()
    if not variant:
        raise HTTPException(404, f"Variant '{variant_code}' not found")
    return variant


def _make_variant_code(sku: str, size: str) -> str:
    # Canonical stockable SKU: BASE_SKU_SIZE. Size is always the final token.
    return f"{sku.strip().upper()}_{size.strip().upper()}"


def _apply_variant_filters(query, q, status):
    V = models.Variant
    query = query.filter(V.is_deleted == False, models.Product.is_deleted == False)  # noqa: E712
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(models.Product.sku.ilike(like), models.Product.name.ilike(like), V.variant_code.ilike(like)))
    if status:
        s = status.strip().upper()
        counted = V.last_counted_at.isnot(None)
        if s == "UNCOUNTED":
            query = query.filter(V.last_counted_at.is_(None))
        elif s == "OUT":
            query = query.filter(counted, V.current_stock <= 0)
        elif s == "LOW":
            query = query.filter(counted, V.current_stock > 0, V.current_stock <= V.reorder_threshold)
        elif s == "OK":
            query = query.filter(counted, V.current_stock > V.reorder_threshold)
        elif s == "ATTENTION":
            query = query.filter(counted, V.current_stock <= V.reorder_threshold)
        else:
            raise HTTPException(400, "status must be one of UNCOUNTED, OUT, LOW, OK, ATTENTION")
    return query


# ------------------------------------------------------------------ #
# Live inventory (flat, filterable, paginated)
# ------------------------------------------------------------------ #
@app.get("/api/variants", response_model=schemas.VariantPage)
def list_variants(
    q: str | None = None, status: str | None = None,
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    V = models.Variant
    filtered = _apply_variant_filters(db.query(V).join(V.product), q, status)
    total = filtered.with_entities(func.count(V.id)).scalar()
    rows = (
        filtered.options(contains_eager(V.product).joinedload(models.Product.vendor))
        .order_by(models.Product.sku, case(bulk_stock_service.SIZE_ORDER, value=func.upper(V.size), else_=99), V.size)
        .offset(offset).limit(limit).all()
    )
    return {"total": total, "limit": limit, "offset": offset, "items": [_variant_row(v) for v in rows]}


def _open_alert_count(db: Session) -> int:
    physical_codes = {
        code for (code,) in db.query(models.Variant.variant_code)
        .join(models.Alert, models.Alert.variant_id == models.Variant.id)
        .join(models.Product, models.Variant.product_id == models.Product.id)
        .filter(
            models.Alert.resolved == False,
            models.Variant.is_deleted == False,
            models.Product.is_deleted == False,
        ).all()
    }
    shortage_codes = {
        item["variant_code"] for item in reorder_service.generate_reorder_list(db, save_batch=False)
        # Unmapped order lines cannot be tied to a stock size, so they are
        # shown in Process Orders as SKU NOT FOUND rather than as Alerts.
        # Keep the badge consistent with _collect_alerts(), which omits them.
        if item["required_qty"] > 0 and not item.get("is_new")
    }
    return len(physical_codes | shortage_codes)


@app.get("/api/summary", response_model=schemas.SummaryOut)
def summary(db: Session = Depends(get_db)):
    V, P = models.Variant, models.Product
    live = (V.is_deleted == False, P.is_deleted == False)  # noqa: E712
    counted = V.last_counted_at.isnot(None)

    def count(*conds):
        return db.query(func.count(V.id)).join(P).filter(*live, *conds).scalar()

    return {
        "products": db.query(func.count(P.id)).filter(P.is_deleted == False).scalar(),  # noqa: E712
        "stock_units": int(db.query(func.coalesce(func.sum(V.current_stock), 0)).join(P).filter(*live, counted).scalar() or 0),
        "variants": count(),
        "uncounted": count(V.last_counted_at.is_(None)),
        "out": count(counted, V.current_stock <= 0),
        "low": count(counted, V.current_stock > 0, V.current_stock <= V.reorder_threshold),
        "ok": count(counted, V.current_stock > V.reorder_threshold),
        # One visible alert per affected variant. Physical low-stock alerts
        # and pending-order shortages can refer to the same variant, so do not
        # double-count them in the badge.
        "open_alerts": _open_alert_count(db),
    }


# ------------------------------------------------------------------ #
# Products (+ their size variants)
# ------------------------------------------------------------------ #
def _smart_search_tokens(value: str | None) -> list[str]:
    return list(dict.fromkeys(token for token in re.split(r"[^\w]+", (value or "").strip().casefold()) if len(token) > 1))


def _apply_smart_search(query, columns, value: str | None):
    """Match each search word against any identity/name field."""
    for token in _smart_search_tokens(value):
        like = f"%{token}%"
        query = query.filter(or_(*(column.ilike(like) for column in columns)))
    return query


def _fuzzy_product_score(value: str, product) -> float:
    """Score typo-tolerant product-name/SKU matches after exact SQL search misses."""
    wanted = _smart_search_tokens(value)
    candidates = _smart_search_tokens(" ".join((product.sku or "", product.name or "", product.category or "")))
    if not wanted or not candidates:
        return 0.0
    scores = []
    for token in wanted:
        best = 1.0 if any(token in candidate for candidate in candidates) else max(
            (SequenceMatcher(None, token, candidate).ratio() for candidate in candidates),
            default=0.0,
        )
        minimum = 1.0 if len(token) <= 2 else (0.82 if len(token) <= 4 else 0.70)
        if best < minimum:
            return 0.0
        scores.append(best)
    return sum(scores) / len(scores)


@app.get("/api/products")
def list_products(q: str | None = None, limit: int | None = Query(None, ge=1, le=500), offset: int = Query(0, ge=0), paged: bool = False, db: Session = Depends(get_db)):
    base = db.query(models.Product).options(
        selectinload(models.Product.variants),
        selectinload(models.Product.reference_images),
        joinedload(models.Product.vendor),
    ) \
        .filter(models.Product.is_deleted == False)  # noqa: E712
    query = _apply_smart_search(base, (models.Product.sku, models.Product.name, models.Product.category), q)
    # Paged clients need an exact count even when the search box is empty.
    # Without this, Products renders "null designs" and "1–0 of null" while
    # the first page itself contains valid products.
    exact_total = query.count() if paged or (q and q.strip()) else None
    total = exact_total if paged else None
    if q and q.strip() and exact_total == 0:
        fuzzy = [(score, product) for product in base.all() if (score := _fuzzy_product_score(q, product)) > 0]
        fuzzy.sort(key=lambda item: (item[0], item[1].created_at.isoformat() if item[1].created_at else "", item[1].id), reverse=True)
        total = len(fuzzy)
        if limit:
            fuzzy = fuzzy[offset:offset + limit]
        items = [_product_out(product) for _, product in fuzzy]
        return {"items": items, "total": total, "offset": offset, "limit": limit} if paged else items
    query = query.order_by(models.Product.created_at.desc(), models.Product.id.desc())
    if limit:
        query = query.offset(offset).limit(limit)
    items = [_product_out(p) for p in query.all()]
    return {"items": items, "total": total, "offset": offset, "limit": limit} if paged else items


def _daily_sales_summary(db: Session) -> dict:
    """Summarize units actually dispatched today in India Standard Time."""
    ist = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(ist).date()
    start_utc = datetime.combine(today, datetime.min.time(), tzinfo=ist).astimezone(timezone.utc)
    end_utc = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=ist).astimezone(timezone.utc)
    T, V, P = models.InventoryTransaction, models.Variant, models.Product
    void_marker = aliased(models.InventoryTransaction)
    not_voided = ~db.query(void_marker.id).filter(
        void_marker.reference == func.replace(T.reference, ":dispatch", ":delete-reversal")
    ).exists()
    base = db.query(T).filter(
        T.transaction_type == models.TransactionType.ORDER_DEDUCTION,
        T.created_at >= start_utc,
        T.created_at < end_utc,
        T.change_qty < 0,
        T.reference.like("order_upload:%:dispatch"),
        not_voided,
    )
    units = base.with_entities(func.coalesce(-func.sum(T.change_qty), 0)).scalar()
    line_count = base.with_entities(func.count(T.id)).scalar()
    design_count = (
        db.query(func.count(func.distinct(P.id)))
        .join(V, V.product_id == P.id).join(T, T.variant_id == V.id)
        .filter(
            T.transaction_type == models.TransactionType.ORDER_DEDUCTION,
            T.created_at >= start_utc, T.created_at < end_utc, T.change_qty < 0,
            T.reference.like("order_upload:%:dispatch"), not_voided,
        ).scalar()
    )
    rows = (
        db.query(P.sku, P.name, func.sum(-T.change_qty).label("units"))
        .join(V, V.product_id == P.id)
        .join(T, T.variant_id == V.id)
        .filter(
            T.transaction_type == models.TransactionType.ORDER_DEDUCTION,
            T.created_at >= start_utc,
            T.created_at < end_utc,
            T.change_qty < 0,
            T.reference.like("order_upload:%:dispatch"),
            not_voided,
        )
        .group_by(P.id, P.sku, P.name)
        .order_by(func.sum(-T.change_qty).desc(), P.sku.asc())
        .limit(5).all()
    )
    return {
        "date": today.isoformat(),
        "timezone": "Asia/Kolkata",
        "dispatched_units": int(units or 0),
        "dispatch_lines": int(line_count or 0),
        "designs_sold": int(design_count or 0),
        "top_products": [{"sku": row.sku, "name": row.name, "units": int(row.units or 0)} for row in rows],
    }


@app.get("/api/analytics/daily-sales")
def daily_sales_summary(db: Session = Depends(get_db)):
    return _daily_sales_summary(db)


@app.get("/api/products/bulk-template")
def bulk_product_template():
    content = bulk_product_service.build_bulk_product_template()
    return StreamingResponse(
        BytesIO(content), media_type=XLSX_MEDIA,
        headers={"Content-Disposition": "attachment; filename=bulk_product_template.xlsx"},
    )


@app.post("/api/products/bulk", response_model=schemas.BulkProductImportResult)
async def bulk_create_products(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read(bulk_product_service.MAX_FILE_BYTES + 1)
    try:
        result = bulk_product_service.import_bulk_products(db, content, file.filename or "")
        if result.get("products_created") or result.get("variants_created"):
            excel_service.reconcile_unmapped_order_lines(db)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@app.get("/api/products/{sku}", response_model=schemas.ProductOut)
def get_product(sku: str, db: Session = Depends(get_db)):
    return _product_out(_get_product_by_sku(db, sku))


@app.post("/api/products", response_model=schemas.ProductOut)
def create_product(payload: schemas.ProductCreate, db: Session = Depends(get_db)):
    sku = payload.sku.strip().upper()
    existing = db.query(models.Product).filter(models.Product.sku == sku).first()
    if existing and not existing.is_deleted:
        raise HTTPException(400, f"SKU '{sku}' already exists.")
    if existing and existing.is_deleted:
        raise HTTPException(409, f"SKU '{sku}' exists in Trash. Restore it instead of creating a new one.")

    sizes_seen = set()
    for v in payload.variants:
        if v.size.strip().upper() in sizes_seen:
            raise HTTPException(400, f"Duplicate size '{v.size}' in request.")
        sizes_seen.add(v.size.strip().upper())

    product = models.Product(sku=sku, name=payload.name, category=payload.category, vendor_id=payload.vendor_id)
    db.add(product)
    db.flush()

    now = datetime.now(timezone.utc)
    for v in payload.variants:
        variant = models.Variant(
            product_id=product.id, size=v.size.strip().upper(), variant_code=_make_variant_code(sku, v.size),
            current_stock=0, reorder_threshold=v.reorder_threshold, target_stock_level=v.target_stock_level,
            last_counted_at=now,
        )
        db.add(variant)
        db.flush()
        if v.initial_stock:
            inventory_service.apply_stock_change(db, variant, v.initial_stock, transaction_type=models.TransactionType.INITIAL_STOCK, reference="product created")
        alert_service.check_variant_alert(db, variant)

    db.commit()
    excel_service.reconcile_unmapped_order_lines(db)
    db.refresh(product)
    return _product_out(product)


@app.patch("/api/products/{sku}", response_model=schemas.ProductOut)
def update_product(sku: str, payload: schemas.ProductUpdate, db: Session = Depends(get_db)):
    product = _get_product_by_sku(db, sku)
    data = payload.model_dump(exclude_unset=True)

    # SKU is the single canonical identity used by Products, Bulk Stock,
    # Reorder and new marketplace-order matching. If the user edits it,
    # regenerate every size's stockable Variant code from the new SKU too.
    if "sku" in data:
        new_sku = str(data.pop("sku") or "").strip().upper()
        if not new_sku:
            raise HTTPException(400, "SKU cannot be empty.")
        if new_sku != product.sku:
            conflict = db.query(models.Product).filter(
                models.Product.sku == new_sku, models.Product.id != product.id
            ).first()
            if conflict:
                raise HTTPException(409, f"SKU '{new_sku}' already exists.")
            for variant in product.variants:
                if variant.is_deleted:
                    continue
                variant.variant_code = _make_variant_code(new_sku, variant.size)
            product.sku = new_sku

    for field, value in data.items():
        setattr(product, field, value)
    db.commit()
    db.refresh(product)
    return _product_out(product)


@app.post("/api/products/{sku}/variants", response_model=schemas.ProductOut)
def add_size(sku: str, payload: schemas.VariantCreate, db: Session = Depends(get_db)):
    """Adds one more size to a design that already exists - for a size that
    was missed when the product was first added."""
    product = _get_product_by_sku(db, sku)
    size = payload.size.strip().upper()
    code = _make_variant_code(sku, size)

    existing = db.query(models.Variant).filter(models.Variant.product_id == product.id, models.Variant.size == size).first()
    if existing and not existing.is_deleted:
        raise HTTPException(400, f"Size '{size}' already exists on {sku}.")
    if existing and existing.is_deleted:
        existing.is_deleted = False
        existing.deleted_at = None
        variant = existing
    else:
        variant = models.Variant(
            product_id=product.id, size=size, variant_code=code, current_stock=0,
            reorder_threshold=payload.reorder_threshold, target_stock_level=payload.target_stock_level,
        )
        db.add(variant)
    db.flush()
    if payload.initial_stock:
        inventory_service.apply_stock_change(db, variant, payload.initial_stock, transaction_type=models.TransactionType.INITIAL_STOCK, reference="size added")
        variant.last_counted_at = datetime.now(timezone.utc)
    alert_service.check_variant_alert(db, variant)
    db.commit()
    excel_service.reconcile_unmapped_order_lines(db)
    db.refresh(product)
    return _product_out(product)


@app.patch("/api/variants/{variant_code}", response_model=schemas.VariantOut)
def update_variant(variant_code: str, payload: schemas.VariantUpdate, db: Session = Depends(get_db)):
    variant = _get_variant_by_code(db, variant_code)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(variant, field, value)
    db.flush()
    alert_service.check_variant_alert(db, variant)
    db.commit()
    # Variant metadata may add a seller-SKU alias that lets old batches resolve.
    excel_service.reconcile_unmapped_order_lines(db)
    db.refresh(variant)
    return _variant_out(variant)


@app.post("/api/products/{sku}/image")
def upload_product_image(
    sku: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    product = _get_product_by_sku(db, sku)
    content = file.file.read(visual_match_service.MAX_SCAN_BYTES + 1)
    try:
        visual_match_service.validate_image(content)
        ext = visual_match_service.image_extension(content)
    except visual_match_service.InvalidProductImage as exc:
        raise HTTPException(400, str(exc)) from exc
    fname = f"{product.sku}_{uuid.uuid4().hex[:8]}{ext}"
    dest = PRODUCT_IMAGE_DIR / fname
    dest.write_bytes(content)
    old_path = product.image_path
    primary_ref = db.query(models.ProductImage).filter(
        models.ProductImage.product_id == product.id, models.ProductImage.is_primary == True  # noqa: E712
    ).first()
    product.image_path = f"/static/product_images/{fname}"
    if primary_ref:
        primary_ref.image_path = product.image_path
    else:
        primary_ref = models.ProductImage(product_id=product.id, image_path=product.image_path, is_primary=True)
        db.add(primary_ref)
    try:
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        dest.unlink(missing_ok=True)
        raise
    background_tasks.add_task(_index_product_photo_background, product.id, primary_ref.id, content, str(dest))
    if old_path:
        try:
            old_name = Path(old_path).name
            (PRODUCT_IMAGE_DIR / old_name).unlink(missing_ok=True)
        except Exception:
            pass
    return {"image_path": product.image_path}


@app.post("/api/products/{sku}/images")
async def upload_product_reference_images(
    sku: str,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """Attach up to eight genuine extra views to this product, never to stock rows."""
    product = _get_product_by_sku(db, sku)
    if not files or len(files) > 8:
        raise HTTPException(400, "Choose between 1 and 8 reference photos.")
    saved: list[tuple[models.ProductImage, bytes, Path]] = []
    try:
        for file in files:
            content = await file.read(visual_match_service.MAX_SCAN_BYTES + 1)
            visual_match_service.validate_image(content)
            ext = visual_match_service.image_extension(content)
            fname = f"{product.sku}_{uuid.uuid4().hex[:8]}{ext}"
            dest = PRODUCT_IMAGE_DIR / fname
            dest.write_bytes(content)
            image = models.ProductImage(
                product_id=product.id,
                image_path=f"/static/product_images/{fname}",
                is_primary=False,
            )
            db.add(image)
            saved.append((image, content, dest))
        db.flush()
        db.commit()
    except visual_match_service.InvalidProductImage as exc:
        db.rollback()
        for _, _, dest in saved:
            dest.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        db.rollback()
        for _, _, dest in saved:
            dest.unlink(missing_ok=True)
        raise

    for image, content, dest in saved:
        background_tasks.add_task(_index_product_photo_background, product.id, image.id, content, str(dest))
    return {"saved": len(saved), "queued_for_indexing": len(saved), "message": "Photos saved. Visual indexing will finish in the background."}


@app.post("/api/assistant/identify-photo")
async def assistant_identify_photo(
    request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
):
    """Server-side Gemini Vision suggestions (not authoritative SKU recognition)."""
    client_key = request.client.host if request.client else "unknown"
    if not visual_search_service.allow_scan(client_key):
        raise HTTPException(429, "Too many photo searches. Please try later.")
    content = await file.read(visual_match_service.MAX_SCAN_BYTES + 1)
    try:
        return await claude_photo_service.identify_photo(content, db)
    except visual_match_service.InvalidProductImage as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/products/visual-match")
async def visual_product_match(
    request: Request,
    file: UploadFile = File(...),
    limit: int = Query(5, ge=1, le=10),
    db: Session = Depends(get_db),
):
    """Return a verified, ambiguous, or no-match result; this route never changes stock."""
    client_key = request.client.host if request.client else "unknown"
    if not visual_search_service.allow_scan(client_key):
        raise HTTPException(429, "Too many image searches. Please wait a minute and try again.")
    content = await file.read(visual_match_service.MAX_SCAN_BYTES + 1)
    try:
        visual_match_service.validate_image(content)
        result = await asyncio.to_thread(visual_search_service.search, db, content, PRODUCT_IMAGE_DIR, limit)
    except visual_match_service.InvalidProductImage as exc:
        raise HTTPException(400, str(exc)) from exc
    except (visual_search_service.VisualSearchUnavailable, visual_match_service.ImageModelUnavailable) as exc:
        # A dedicated SigLIP/pgvector service is optional. On small Railway
        # plans, cloud Vision provides unverified catalog suggestions instead.
        if not cloud_assistant_service.is_configured():
            raise HTTPException(503, f"Image matching unavailable: {exc}") from exc
        try:
            suggestion = await claude_photo_service.identify_photo(content, db, limit=limit)
        except RuntimeError as reason:
            raise HTTPException(502, str(reason)) from reason
        selected = [item["sku"] for item in suggestion["matches"]]
        products = db.query(models.Product).options(selectinload(models.Product.variants)).filter(
            models.Product.sku.in_(selected), models.Product.is_deleted.is_(False)
        ).all() if selected else []
        by_sku = {product.sku: product for product in products}
        return {
            "status": "ambiguous" if selected else "no_match",
            "matches": [{"similarity": None, "reference_image_path": item.get("image_path"),
                         "product": _product_out(by_sku[item["sku"]])}
                        for item in suggestion["matches"] if item["sku"] in by_sku],
            "ranked_designs": len(selected), "matching_mode": f"{cloud_assistant_service.provider_name().capitalize()} Vision keyword suggestions",
            "score_type": "not_available",
            "message": f"{cloud_assistant_service.provider_name().capitalize()} recognized: " + suggestion["description"] + ". Review possible designs; AI did NOT verify the exact SKU.",
            "notice": suggestion["notice"],
        }
    product_ids = [hit.product_id for hit in result.matches]
    products = db.query(models.Product).options(selectinload(models.Product.variants)).filter(
        models.Product.id.in_(product_ids), models.Product.is_deleted == False  # noqa: E712
    ).all() if product_ids else []
    by_id = {product.id: product for product in products}
    matches = []
    for hit in result.matches:
        product = by_id.get(hit.product_id)
        if product:
            matches.append({
                "similarity": round(hit.similarity, 4),
                "reference_image_path": hit.reference_image_path,
                "product": _product_out(product),
            })
    status = result.status if matches else "no_match"
    message = result.message if matches else "No active catalog match is available. Check the product photos or try another photo."
    return {
        "ranked_designs": len(product_ids),
        "status": status,
        "matches": matches,
        "message": message,
        "score_type": "cosine_similarity_not_probability",
        "matching_mode": (
            "SigLIP 2 semantic image embeddings" if engine.dialect.name == "postgresql"
            or os.getenv("VISUAL_AI_URL", "").strip()
            else "local color/layout suggestions"
        ),
        "notice": "Only a matched result is a strong visual match. Ambiguous results are suggestions; photo search never changes stock.",
    }


@app.get("/api/visual-search/status")
def visual_search_status(db: Session = Depends(get_db)):
    """Report safe setup diagnostics for photo search without exposing secrets."""
    dialect = engine.dialect.name
    catalog_product_count = 0
    try:
        photo_count = db.query(models.ProductImage).join(models.Product).filter(
            models.Product.is_deleted == False  # noqa: E712
        ).count()
        catalog_product_count = db.query(models.Product).filter(
            models.Product.is_deleted == False  # noqa: E712
        ).count()
    except Exception:
        db.rollback()
        photo_count = 0

    pgvector_index_exists = False
    pgvector_extension_installed = False
    indexed_photo_count = 0
    postgres_connected = False
    if dialect == "postgresql":
        try:
            db.execute(text("SELECT 1"))
            postgres_connected = True
        except Exception:
            db.rollback()
    if postgres_connected:
        try:
            pgvector_extension_installed = bool(db.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
            )).scalar())
            pgvector_index_exists = bool(db.execute(
                text("SELECT to_regclass(:table_name) IS NOT NULL"),
                {"table_name": visual_search_service.TABLE_NAME},
            ).scalar())
            if pgvector_index_exists:
                indexed_photo_count = int(db.execute(
                    text(f"SELECT count(*) FROM {visual_search_service.TABLE_NAME} WHERE model_name = :model"),
                    {"model": visual_search_service.MODEL_NAME},
                ).scalar() or 0)
        except Exception:
            db.rollback()
            pgvector_index_exists = False

    runtime_installed = bool(find_spec("torch") and find_spec("transformers"))
    model_weights_loaded = visual_match_service._encoder is not None
    external_model_service = bool(os.getenv("VISUAL_AI_URL", "").strip())
    external_model_service_ready = False
    if external_model_service and os.getenv("VISUAL_AI_API_KEY", "").strip():
        try:
            import httpx
            service_url = os.getenv("VISUAL_AI_URL", "").strip().rstrip("/")
            response = httpx.get(f"{service_url}/health", timeout=1.5)
            response.raise_for_status()
            health = response.json()
            external_model_service_ready = (
                health.get("status") == "ready"
                and health.get("model") == visual_search_service.MODEL_NAME
            )
        except Exception:
            external_model_service_ready = False
    needs_rebuild = dialect == "postgresql" and (
        not pgvector_index_exists or indexed_photo_count < photo_count
    )
    local_matcher_available = dialect != "postgresql"
    model_available = (
        local_matcher_available and not external_model_service
        or runtime_installed and not external_model_service
        or external_model_service_ready
    )
    if dialect == "postgresql":
        index_ready = (
            postgres_connected
            and pgvector_extension_installed
            and pgvector_index_exists
            and photo_count > 0
            and indexed_photo_count >= photo_count
        )
    else:
        # SQLite uses the Pillow fallback unless the private SigLIP service is
        # configured. With the service, query and catalog photos share semantic
        # embeddings, allowing packaged and model-view photos to match.
        index_ready = photo_count > 0 and catalog_product_count <= visual_search_service.DEV_MAX_CATALOG
    if dialect != "postgresql":
        if not os.getenv("DATABASE_URL", "").strip():
            if external_model_service_ready:
                message = f"Database: SQLite local mode. SigLIP 2 semantic matching via the private model service is configured for up to {visual_search_service.DEV_MAX_CATALOG} designs. Catalog photos: {photo_count}."
            elif external_model_service:
                message = "SQLite is available, but the private SigLIP service is not ready or its key is missing. Check VISUAL_AI_URL, VISUAL_AI_API_KEY, and the model service /health endpoint."
            else:
                message = f"Database: SQLite local mode. Lightweight color/layout suggestions are available for up to {visual_search_service.DEV_MAX_CATALOG} designs. For packaged-photo to model-photo matching, configure the private SigLIP model service. Catalog photos: {photo_count}."
            if os.getenv("RAILWAY_ENVIRONMENT", "").strip():
                message += " Railway is not connected to PostgreSQL (DATABASE_URL is missing); connect the PostgreSQL service before relying on durable production inventory."
        else:
            mode_text = "SigLIP 2 semantic matching via the private model service" if external_model_service_ready else "lightweight color/layout suggestions; configure a ready private SigLIP model service for packaged-photo matching"
            message = f"Database: {dialect}. {mode_text} are available for up to {visual_search_service.DEV_MAX_CATALOG} designs; use PostgreSQL with pgvector for larger catalogs. Catalog photos: {photo_count}."
    elif not postgres_connected:
        message = "Database URL is PostgreSQL, but the app cannot connect to it. Check the Railway PostgreSQL service and DATABASE_URL reference."
    elif not pgvector_extension_installed:
        message = "Database: PostgreSQL connected; pgvector extension is missing. Enable vector on this database, then rebuild the image index."
    elif photo_count == 0:
        message = "Database: PostgreSQL with pgvector connected; no catalog reference photos are saved yet. Add clear product photos first."
    elif needs_rebuild:
        message = f"Database: PostgreSQL with pgvector connected; {indexed_photo_count} of {photo_count} photos are indexed. Rebuild the image index from this app service."
    elif not model_weights_loaded and not external_model_service:
        message = "Catalog photo index is ready. SigLIP weights will load the first time you search a photo."
    else:
        message = "Visual model runtime and catalog photo index are ready."
    cloud_vision_fallback = cloud_assistant_service.is_configured()
    if cloud_vision_fallback and not (index_ready and model_available):
        message += f" {cloud_assistant_service.provider_name().capitalize()} Vision descriptions and unverified catalog suggestions are available without SigLIP."
    if not model_available:
        if external_model_service:
            message += " The configured private SigLIP service is not ready or its key is missing."
        else:
            message += " SigLIP runtime is not installed and no private model service is configured."

    return {
        "status": "ready" if (index_ready and model_available) or cloud_vision_fallback else "unavailable",
        "database": dialect,
        "database_url_configured": bool(os.getenv("DATABASE_URL", "").strip()),
        "postgres_connected": postgres_connected,
        "pgvector_extension_installed": pgvector_extension_installed,
        "index_ready": index_ready or cloud_vision_fallback,
        "cloud_vision_fallback": cloud_vision_fallback,
        "claude_vision_fallback": cloud_vision_fallback and cloud_assistant_service.provider_name() == "claude",
        "runtime_installed": runtime_installed,
        "model_weights_loaded": model_weights_loaded,
        "local_matcher_available": local_matcher_available,
        "external_model_service_configured": external_model_service,
        "external_model_service_ready": external_model_service_ready,
        "semantic_matching_available": (dialect == "postgresql" and model_available) or external_model_service_ready,
        "pgvector_index_exists": pgvector_index_exists,
        "catalog_photos": photo_count,
        "indexed_photos": indexed_photo_count,
        "needs_rebuild": needs_rebuild,
        "message": message,
    }


# ------------------------------------------------------------------ #
# Trash: soft delete + restore
# ------------------------------------------------------------------ #
@app.delete("/api/products/{sku}", response_model=schemas.DeleteResult)
def delete_product(sku: str, db: Session = Depends(get_db)):
    """Moves a design and all its sizes to Trash. Stock, alerts and reorder
    lines stop counting it immediately; full history is kept and it can be
    restored later from the Trash view."""
    product = _get_product_by_sku(db, sku)
    now = datetime.now(timezone.utc)
    count = 0
    for v in product.variants:
        if not v.is_deleted:
            v.is_deleted = True
            v.deleted_at = now
            open_alert = db.query(models.Alert).filter(models.Alert.variant_id == v.id, models.Alert.resolved == False).first()  # noqa: E712
            if open_alert:
                open_alert.resolved = True
                open_alert.resolved_at = now
            count += 1
    product.is_deleted = True
    product.deleted_at = now
    db.commit()
    return {"deleted": product.sku, "trashed": True, "sizes_removed": count}


@app.post("/api/products/{sku}/restore", response_model=schemas.RestoreResult)
def restore_product(sku: str, db: Session = Depends(get_db)):
    product = _get_product_by_sku(db, sku, include_deleted=True)
    if not product.is_deleted:
        raise HTTPException(400, f"'{sku}' is not in Trash.")
    product.is_deleted = False
    product.deleted_at = None
    count = 0
    for v in product.variants:
        if v.is_deleted:
            v.is_deleted = False
            v.deleted_at = None
            count += 1
        alert_service.check_variant_alert(db, v)
    db.commit()
    excel_service.reconcile_unmapped_order_lines(db)
    return {"restored": product.sku, "variants_restored": count}


@app.delete("/api/variants/{variant_code}", response_model=schemas.DeleteResult)
def delete_variant(variant_code: str, db: Session = Depends(get_db)):
    """Moves a single size to Trash. If it was the design's last remaining
    size, the whole design goes to Trash too."""
    variant = _get_variant_by_code(db, variant_code)
    product = variant.product
    now = datetime.now(timezone.utc)
    variant.is_deleted = True
    variant.deleted_at = now
    open_alert = db.query(models.Alert).filter(models.Alert.variant_id == variant.id, models.Alert.resolved == False).first()  # noqa: E712
    if open_alert:
        open_alert.resolved = True
        open_alert.resolved_at = now
    db.flush()

    remaining = db.query(func.count(models.Variant.id)).filter(models.Variant.product_id == product.id, models.Variant.is_deleted == False).scalar()  # noqa: E712
    product_also_deleted = remaining == 0
    if product_also_deleted:
        product.is_deleted = True
        product.deleted_at = now
    db.commit()
    return {"deleted": variant.variant_code, "trashed": True, "product_also_deleted": product_also_deleted}


@app.post("/api/variants/{variant_code}/restore", response_model=schemas.RestoreResult)
def restore_variant(variant_code: str, db: Session = Depends(get_db)):
    variant = _get_variant_by_code(db, variant_code, include_deleted=True)
    if not variant.is_deleted:
        raise HTTPException(400, f"'{variant_code}' is not in Trash.")
    variant.is_deleted = False
    variant.deleted_at = None
    if variant.product.is_deleted:
        variant.product.is_deleted = False
        variant.product.deleted_at = None
    alert_service.check_variant_alert(db, variant)
    db.commit()
    excel_service.reconcile_unmapped_order_lines(db)
    return {"restored": variant.variant_code}


@app.get("/api/trash")
def list_trash(limit: int | None = Query(None, ge=1, le=100), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    query = (
        db.query(models.Product).options(selectinload(models.Product.variants))
        .filter(models.Product.is_deleted == True)  # noqa: E712
        .order_by(models.Product.deleted_at.desc())
    )
    total = query.count()
    products = query.offset(offset).limit(limit).all() if limit else query.all()
    items = [{
        "sku": p.sku, "name": p.name, "image_path": p.image_path,
        "variant_count": len(p.variants), "deleted_at": p.deleted_at,
    } for p in products]
    return {"total": total, "limit": limit, "offset": offset, "items": items} if limit is not None else items


@app.delete("/api/trash/{sku}")
def permanently_delete(sku: str, db: Session = Depends(get_db)):
    """Empties one item from Trash for good - no further undo. Use only to
    clean up Trash itself; everyday removal is the regular delete above."""
    product = _get_product_by_sku(db, sku, include_deleted=True)
    if not product.is_deleted:
        raise HTTPException(400, f"'{sku}' must be in Trash before it can be permanently deleted.")
    variant_ids = [v.id for v in db.query(models.Variant.id).filter(models.Variant.product_id == product.id)]
    if variant_ids:
        db.execute(sa_delete(models.OrderLine).where(models.OrderLine.variant_id.in_(variant_ids)))
        db.execute(sa_delete(models.ReorderBatchItem).where(models.ReorderBatchItem.variant_id.in_(variant_ids)))
        db.execute(sa_delete(models.Alert).where(models.Alert.variant_id.in_(variant_ids)))
        db.execute(sa_delete(models.InventoryTransaction).where(models.InventoryTransaction.variant_id.in_(variant_ids)))
    # Variant rows themselves are left to the ORM cascade below (Product.variants
    # is configured cascade="all, delete-orphan") - deleting them here too would
    # just make SQLAlchemy attempt the same DELETE twice and warn about it.
    db.delete(product)
    db.commit()
    return {"permanently_deleted": sku.strip().upper()}


# ------------------------------------------------------------------ #
# Variant lookup (scan screen) + counting / adjustment
# ------------------------------------------------------------------ #
@app.get("/api/variants/{variant_code}", response_model=schemas.VariantLookupOut)
def lookup_variant(variant_code: str, db: Session = Depends(get_db)):
    variant = _get_variant_by_code(db, variant_code)
    return {
        "variant_code": variant.variant_code, "sku": variant.product.sku, "size": variant.size,
        "product_name": variant.product.name, "current_stock": variant.current_stock,
        "counted": variant.last_counted_at is not None, "image_path": variant.product.image_path,
    }


@app.get("/api/variants/{variant_code}/product", response_model=schemas.ProductOut)
def product_for_variant(variant_code: str, db: Session = Depends(get_db)):
    """Resolve a scanned size to its parent design; the client limits barcode
    editing to that size while product QR/manual search show the full design."""
    variant = _get_variant_by_code(db, variant_code)
    return _product_out(_get_product_by_sku(db, variant.product.sku))


@app.post("/api/inventory/adjust", response_model=schemas.VariantOut)
def adjust_stock(payload: schemas.StockAdjustment, db: Session = Depends(get_db)):
    if payload.operation not in {"set", "delta"}:
        raise HTTPException(400, "Operation must be 'set' or 'delta'.")
    if payload.operation == "set":
        if payload.new_count is None:
            raise HTTPException(400, "Enter the final physical stock count.")
        if payload.new_count < 0:
            raise HTTPException(400, "Count cannot be negative.")
    else:
        if payload.delta is None or payload.delta == 0:
            raise HTTPException(400, "Enter a non-zero quantity to add or remove.")
        if payload.expected_current_stock is None:
            raise HTTPException(400, "Current stock is required for a safe add/remove update.")
    variant = _get_variant_by_code(db, payload.variant_code)
    variant = db.query(models.Variant).filter(models.Variant.id == variant.id).populate_existing().with_for_update().one()
    if payload.expected_current_stock is not None and variant.current_stock != payload.expected_current_stock:
        raise HTTPException(409, f"Stock changed since this update was prepared. Current stock is {variant.current_stock}; refresh and try again.")
    try:
        if payload.operation == "set":
            inventory_service.set_absolute_stock(
                db, variant, payload.new_count, note=payload.note,
                expected_current_stock=payload.expected_current_stock,
            )
        else:
            new_count = variant.current_stock + payload.delta
            if new_count < 0:
                raise HTTPException(400, f"Cannot remove {abs(payload.delta)} units; only {variant.current_stock} are in stock.")
            inventory_service.apply_stock_change(
                db,
                variant,
                payload.delta,
                transaction_type=(models.TransactionType.RESTOCK if payload.delta > 0 else models.TransactionType.MANUAL_ADJUSTMENT),
                reference=payload.note,
                created_by="stock adjustment",
                expected_current_stock=payload.expected_current_stock,
            )
            variant.last_counted_at = datetime.now(timezone.utc)
            db.flush()
    except inventory_service.StaleStockError as exc:
        raise HTTPException(409, f"Stock changed since this update was prepared. Current stock is {exc.current}; refresh and try again.") from exc
    alert_service.check_variant_alert(db, variant)
    db.commit()
    # Map older marketplace lines now that stock has been counted or adjusted;
    # the next history refresh can show precisely which pieces are dispatchable.
    excel_service.reconcile_unmapped_order_lines(db)
    db.refresh(variant)
    return _variant_out(variant)


@app.get("/api/inventory/count-sheet")
def count_sheet(only_uncounted: bool = False, db: Session = Depends(get_db)):
    xlsx_bytes = bulk_stock_service.build_count_sheet(db, only_uncounted=only_uncounted)
    return StreamingResponse(BytesIO(xlsx_bytes), media_type=XLSX_MEDIA, headers={"Content-Disposition": "attachment; filename=count_sheet.xlsx"})


@app.post("/api/inventory/bulk-set", response_model=schemas.BulkSetResult)
async def bulk_set(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        result = bulk_stock_service.bulk_set_stock(db, content, file.filename or "")
        if result.get("updated_count") or result.get("created_variants_count"):
            excel_service.reconcile_unmapped_order_lines(db)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/variants/{variant_code}/transactions", response_model=list[schemas.TransactionOut])
def variant_transactions(variant_code: str, db: Session = Depends(get_db)):
    variant = _get_variant_by_code(db, variant_code, include_deleted=True)
    return (
        db.query(models.InventoryTransaction).filter(models.InventoryTransaction.variant_id == variant.id)
        .order_by(models.InventoryTransaction.created_at.desc(), models.InventoryTransaction.id.desc()).limit(200).all()
    )


@app.get("/api/transactions")
def global_transactions(
    q: str | None = None, transaction_type: str | None = None,
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    paged: bool = False,
    db: Session = Depends(get_db),
):
    """Every stock change across the whole catalog, newest first - the
    'who/what/when/how much changed' activity log."""
    T, V, P = models.InventoryTransaction, models.Variant, models.Product
    query = db.query(T).join(V, T.variant_id == V.id).join(P, V.product_id == P.id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(P.sku.ilike(like), P.name.ilike(like), V.variant_code.ilike(like)))
    if transaction_type:
        query = query.filter(T.transaction_type == transaction_type)
    total = query.order_by(None).count() if paged else None
    rows = query.options(contains_eager(T.variant).contains_eager(V.product)) \
        .order_by(T.created_at.desc(), T.id.desc()).offset(offset).limit(limit).all()
    items = [{
        "id": t.id, "variant_code": t.variant.variant_code, "sku": t.variant.product.sku, "size": t.variant.size,
        "product_name": t.variant.product.name, "change_qty": t.change_qty, "transaction_type": t.transaction_type.value,
        "reference": t.reference, "balance_after": t.balance_after, "created_by": t.created_by, "created_at": t.created_at,
    } for t in rows]
    return {"items": items, "total": total, "offset": offset, "limit": limit} if paged else items


# ------------------------------------------------------------------ #
# Order history / batches
# ------------------------------------------------------------------ #
@app.get("/api/orders/history")
def order_history(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), paged: bool = False, db: Session = Depends(get_db)):
    total = db.query(func.count(models.OrderUpload.id)).scalar() if paged else None
    uploads = (
        db.query(models.OrderUpload)
        .options(
            selectinload(models.OrderUpload.lines).joinedload(models.OrderLine.variant).joinedload(models.Variant.product)
        )
        .order_by(models.OrderUpload.uploaded_at.desc(), models.OrderUpload.id.desc())
        .offset(offset).limit(limit).all()
    )
    page_lines = [line for upload in uploads for line in upload.lines]
    dispatchable_by_line = _order_line_dispatchability(db, page_lines)
    result = []
    for u in uploads:
        pending_lines = [line for line in u.lines if line.status in ACTIVE_ORDER_STATUSES]
        pending = len(pending_lines)
        fulfilled = sum(1 for line in u.lines if line.qty_fulfilled > 0)
        insufficient = sum(1 for line in u.lines if line.status == models.OrderLineStatus.INSUFFICIENT_STOCK)
        not_found = sum(1 for line in u.lines if line.status == models.OrderLineStatus.SKU_NOT_FOUND)
        pending_units = sum(max(line.qty_ordered - line.qty_fulfilled, 0) for line in pending_lines)
        dispatched_units = sum(line.qty_fulfilled for line in u.lines)
        dispatchable_units = sum(dispatchable_by_line.get(line.id, 0) for line in pending_lines)
        if pending:
            status = "PENDING"
        elif fulfilled:
            status = "DISPATCHED"
        elif insufficient:
            status = "INSUFFICIENT STOCK"
        else:
            status = "PROCESSED"
        lines = []
        for line in sorted(u.lines, key=lambda x: x.id):
            product_name = line.variant.product.name if line.variant and line.variant.product else None
            variant_code = line.variant.variant_code if line.variant else None
            pending_qty = max(line.qty_ordered - line.qty_fulfilled, 0) if line.status in ACTIVE_ORDER_STATUSES else 0
            dispatchable_qty = dispatchable_by_line.get(line.id, 0)
            line_status = (
                "PARTIALLY_DISPATCHED" if pending_qty and line.qty_fulfilled
                else "PENDING" if pending_qty and dispatchable_qty > 0
                else line.status.value
            )
            lines.append({
                "id": line.id, "sku": line.sku, "size": line.size, "variant_code": variant_code,
                "product_name": product_name, "qty_ordered": line.qty_ordered,
                "qty_fulfilled": line.qty_fulfilled, "pending_qty": pending_qty,
                "dispatchable_qty": dispatchable_qty, "status": line_status,
            })
        dispatch_state = "WAITING"
        if dispatchable_units and dispatchable_units >= pending_units:
            dispatch_state = "READY"
        elif dispatchable_units:
            dispatch_state = "PARTIAL"
        result.append({
            "order_upload_id": u.id, "filename": u.filename, "uploaded_at": u.uploaded_at,
            "total_rows": u.total_rows, "unique_skus": u.unique_skus,
            "pending_count": pending, "fulfilled_count": fulfilled,
            "pending_units": pending_units, "dispatched_units": dispatched_units,
            "dispatchable_lines": sum(1 for line in pending_lines if dispatchable_by_line.get(line.id, 0) > 0),
            "dispatchable_units": dispatchable_units, "dispatch_state": dispatch_state,
            "insufficient_count": insufficient, "not_found_count": not_found,
            "status": status, "lines": lines,
        })
    return {"items": result, "total": total, "offset": offset, "limit": limit} if paged else result


# ------------------------------------------------------------------ #
# Orders (Excel / CSV upload)
# ------------------------------------------------------------------ #
@app.post("/api/orders/upload", response_model=schemas.OrderUploadResult)
async def upload_orders(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        result = excel_service.process_order_excel(db, content, file.filename or "")
        # Return line ids and freshly computed readiness so the just-uploaded
        # batch can use the same per-line selection and dispatch flow as history.
        order_lines = (
            db.query(models.OrderLine)
            .options(joinedload(models.OrderLine.variant))
            .filter(models.OrderLine.order_upload_id == result["order_upload_id"])
            .order_by(models.OrderLine.id.asc())
            .all()
        )
        dispatchable_by_line = _order_line_dispatchability(db, order_lines)
        for line_result, line in zip(result["lines"], order_lines):
            pending_qty = max(line.qty_ordered - line.qty_fulfilled, 0) if line.status in ACTIVE_ORDER_STATUSES else 0
            line_result.update({
                "id": line.id,
                "pending_qty": pending_qty,
                "dispatchable_qty": dispatchable_by_line.get(line.id, 0),
            })
        return result
    except excel_service.DuplicateUploadError as e:
        raise HTTPException(409, str(e))
    except (excel_service.BadOrderFileError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/orders/reconcile-unmapped")
def reconcile_unmapped_orders(db: Session = Depends(get_db)):
    """Retry safe catalog matching for old SKU_NOT_FOUND lines; never deduct stock."""
    matched_lines = excel_service.reconcile_unmapped_order_lines(db)
    return {
        "matched_lines": matched_lines,
        "message": f"{matched_lines} previously unmatched order lines matched to inventory. No stock was deducted.",
    }


@app.delete("/api/orders/{order_upload_id}")
def delete_order_upload(order_upload_id: int, db: Session = Depends(get_db)):
    """Remove a processed file and all its order demand, safely reversing dispatch stock."""
    upload = db.query(models.OrderUpload).filter(models.OrderUpload.id == order_upload_id).with_for_update().first()
    if upload is None:
        raise HTTPException(status_code=404, detail="Order batch not found. It may already have been deleted.")

    lines = db.query(models.OrderLine).filter(models.OrderLine.order_upload_id == order_upload_id).all()
    fulfilled_by_variant: dict[int, int] = defaultdict(int)
    for line in lines:
        if line.variant_id and line.qty_fulfilled > 0:
            fulfilled_by_variant[line.variant_id] += line.qty_fulfilled

    try:
        restored_units = 0
        if fulfilled_by_variant:
            variants = db.query(models.Variant).filter(
                models.Variant.id.in_(fulfilled_by_variant.keys())
            ).with_for_update().all()
            by_id = {variant.id: variant for variant in variants}
            for variant_id, quantity in fulfilled_by_variant.items():
                variant = by_id.get(variant_id)
                if variant is None:
                    raise HTTPException(status_code=409, detail="A dispatched size was removed; this batch cannot be safely reversed.")
                inventory_service.apply_stock_change(
                    db, variant, quantity,
                    transaction_type=models.TransactionType.MANUAL_ADJUSTMENT,
                    reference=f"order_upload:{order_upload_id}:delete-reversal",
                    created_by="order_batch_delete",
                )
                alert_service.check_variant_alert(db, variant)
                restored_units += quantity

        removed_lines = len(lines)
        filename = upload.filename
        db.query(models.OrderLine).filter(models.OrderLine.order_upload_id == order_upload_id).delete(synchronize_session=False)
        db.delete(upload)  # Also releases its unique file hash for a corrected re-upload.
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    return {
        "deleted_order_upload_id": order_upload_id,
        "filename": filename,
        "removed_lines": removed_lines,
        "restored_units": restored_units,
        "message": "Order demand removed; dispatched quantities, if any, were restored to stock.",
    }


# ------------------------------------------------------------------ #
# Alerts
# ------------------------------------------------------------------ #
def _collect_alerts(db: Session, q: str | None = None):
    alerts = alert_service.get_open_alerts(db, limit=None, offset=0, q=q)
    result = [{
        "id": a.id, "variant_code": a.variant.variant_code, "sku": a.variant.product.sku, "size": a.variant.size,
        "product_name": a.variant.product.name, "alert_type": a.alert_type, "stock_at_alert": a.stock_at_alert,
        "current_stock": a.variant.current_stock,
        "created_at": a.created_at, "ordered_qty": None, "required_qty": None,
    } for a in alerts]

    # Pending marketplace orders can create a real purchase shortage even when
    # physical stock is still above the normal low-stock threshold. Show that
    # shortage in Alerts as a virtual alert; the database remains the source of
    # truth for physical-stock alerts.
    shortages = reorder_service.generate_reorder_list(db, save_batch=False, part=q)
    existing_codes = {a["variant_code"] for a in result}
    for item in shortages:
        if item.get("is_new") or item["required_qty"] <= 0:
            continue
        if item["variant_code"] in existing_codes:
            continue
        result.append({
            "id": -item["variant_code"].__hash__(),
            "variant_code": item["variant_code"],
            "sku": item["sku"],
            "size": item["size"],
            "product_name": item["name"],
            "alert_type": "ORDER_SHORTAGE",
            "stock_at_alert": item["current_stock"],
            "current_stock": item["current_stock"],
            "created_at": datetime.now(timezone.utc),
            "ordered_qty": item["ordered_qty"],
            "required_qty": item["required_qty"],
        })
    result.sort(key=lambda a: (a["alert_type"] != "OUT_OF_STOCK", a["stock_at_alert"], a["variant_code"]))
    if q and q.strip():
        tokens = _smart_search_tokens(q)
        result = [a for a in result if all(
            any(token in str(a.get(key) or "").casefold() for key in ("variant_code", "sku", "size", "product_name", "alert_type"))
            for token in tokens
        )]
    return result


@app.get("/api/alerts", response_model=list[schemas.AlertOut])
def list_alerts(limit: int = Query(1000, ge=1, le=5000), offset: int = Query(0, ge=0), q: str | None = None, db: Session = Depends(get_db)):
    alerts = _collect_alerts(db, q)
    return alerts[offset:offset + limit]


@app.get("/api/alerts/groups", response_model=schemas.AlertGroupPage)
def list_alert_groups(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0), q: str | None = None, db: Session = Depends(get_db)):
    alerts = _collect_alerts(db, q)
    rank = {"OUT_OF_STOCK": 0, "ORDER_SHORTAGE": 1, "LOW_STOCK": 2}
    size_rank = {name: index for index, name in enumerate(("FREE SIZE", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL"))}
    groups = {}
    for alert in alerts:
        group = groups.setdefault(alert["sku"], {
            "sku": alert["sku"], "product_name": alert["product_name"], "items": [],
            "highest_severity": alert["alert_type"], "_severity": 99, "_stock": alert["stock_at_alert"],
        })
        group["items"].append(alert)
        severity = rank.get(alert["alert_type"], 3)
        group["_severity"] = min(group["_severity"], severity)
        group["_stock"] = min(group["_stock"], alert["stock_at_alert"])
        if severity == group["_severity"]:
            group["highest_severity"] = alert["alert_type"]
    page_groups = list(groups.values())
    for group in page_groups:
        group["items"].sort(key=lambda a: (size_rank.get(a["size"].upper(), 100), a["size"].upper()))
        group["alert_count"] = len(group["items"])
        del group["_severity"], group["_stock"]
    page_groups.sort(key=lambda g: (rank.get(g["highest_severity"], 3), min(a["stock_at_alert"] for a in g["items"]), g["sku"].casefold()))
    total = len(page_groups)
    return {"total": total, "limit": limit, "offset": offset, "items": page_groups[offset:offset + limit]}


# ------------------------------------------------------------------ #
# Dispatch pending orders
# ------------------------------------------------------------------ #
@app.post("/api/orders/{order_upload_id}/dispatch")
def dispatch_order_upload(
    order_upload_id: int,
    payload: schemas.OrderDispatchRequest | None = None,
    db: Session = Depends(get_db),
):
    """Dispatch available pieces from a batch, leaving any shortage pending."""
    upload = db.query(models.OrderUpload).filter(models.OrderUpload.id == order_upload_id).with_for_update().first()
    if upload is None:
        raise HTTPException(status_code=404, detail="Order batch not found.")
    pending_lines = (
        db.query(models.OrderLine)
        .options(joinedload(models.OrderLine.variant).joinedload(models.Variant.product))
        .filter(
            models.OrderLine.order_upload_id == order_upload_id,
            models.OrderLine.status.in_(ACTIVE_ORDER_STATUSES),
            models.OrderLine.variant_id.isnot(None),
        )
        # Serialize concurrent attempts to dispatch the same batch on databases
        # that support row locks (PostgreSQL). The guarded stock update below
        # remains the final protection against a simultaneous recount/dispatch.
        .with_for_update(of=models.OrderLine)
        .order_by(models.OrderLine.id.asc())
        .all()
    )
    if not pending_lines:
        raise HTTPException(status_code=404, detail="No pending order lines found for this upload.")

    selected_ids = set(payload.line_ids) if payload is not None else None
    if selected_ids is not None:
        pending_by_id = {line.id: line for line in pending_lines}
        invalid_ids = selected_ids.difference(pending_by_id)
        if invalid_ids:
            raise HTTPException(status_code=409, detail="Some selected order lines are no longer pending. Refresh the order list and try again.")
        lines = [pending_by_id[line_id] for line_id in sorted(selected_ids)]
    else:
        lines = pending_lines

    dispatchable_by_line = _order_line_dispatchability(db, lines)
    plan = []
    for line in lines:
        remaining = max(line.qty_ordered - line.qty_fulfilled, 0)
        quantity = min(remaining, dispatchable_by_line.get(line.id, 0))
        if quantity > 0:
            plan.append((line, quantity))
    if not plan:
        raise HTTPException(
            status_code=409,
            detail="No counted stock is available for these pending order lines. Any undispatched quantity remains pending.",
        )

    try:
        for line, quantity in plan:
            variant = line.variant
            apply = inventory_service.apply_stock_change
            apply(
                db, variant, -quantity,
                transaction_type=models.TransactionType.ORDER_DEDUCTION,
                reference=f"order_upload:{order_upload_id}:dispatch",
                created_by="dispatch",
            )
            line.qty_fulfilled += quantity
            if line.qty_fulfilled >= line.qty_ordered:
                line.qty_fulfilled = line.qty_ordered
                line.status = models.OrderLineStatus.FULFILLED
            else:
                line.status = models.OrderLineStatus.PENDING
            alert_service.check_variant_alert(db, variant)
        db.commit()
    except inventory_service.InsufficientStockError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"{exc.variant_code}: stock changed during dispatch; requested {exc.requested}, only {exc.available} remain. The batch was not dispatched.",
        ) from exc
    except Exception:
        db.rollback()
        raise

    remaining_lines = [line for line in pending_lines if line.status in ACTIVE_ORDER_STATUSES]
    remaining_units = sum(max(line.qty_ordered - line.qty_fulfilled, 0) for line in remaining_lines)
    dispatched_units = sum(quantity for _, quantity in plan)
    return {
        "order_upload_id": order_upload_id,
        "dispatched_lines": len(plan),
        "dispatched_units": dispatched_units,
        "pending_lines": len(remaining_lines),
        "pending_units": remaining_units,
        "status": "PENDING" if remaining_units else "DISPATCHED",
    }


# ------------------------------------------------------------------ #
# Reorder list
# ------------------------------------------------------------------ #
@app.get("/api/reorder-list")
def reorder_list(part: str | None = None, limit: int | None = Query(None, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    # Merge cross-brand aliases before pagination so one product/colour/size
    # appears once and the page count reflects the consolidated picklist.
    items = reorder_service.generate_reorder_list(db, save_batch=False)
    items = reorder_service.consolidate_reorder_items(items, part=part)
    return {"total": len(items), "limit": limit, "offset": offset, "items": items[offset:offset + limit]} if limit is not None else items


@app.get("/api/reorder-list/export")
def reorder_list_export(part: str | None = None, db: Session = Depends(get_db)):
    if part and part.strip():
        # Keep the saved reorder batch scoped to the user's raw SKU search,
        # while building the downloadable group from all brands that match it.
        reorder_service.generate_reorder_list(db, save_batch=True, part=part)
        items = reorder_service.generate_reorder_list(db, save_batch=False)
    else:
        items = reorder_service.generate_reorder_list(db, save_batch=True)
    items = reorder_service.consolidate_reorder_items(items, part=part)
    items = [item for item in items if item["required_qty"] > 0 or item.get("is_new")]
    xlsx_bytes = reorder_service.reorder_list_to_excel(items)
    return StreamingResponse(BytesIO(xlsx_bytes), media_type=XLSX_MEDIA, headers={"Content-Disposition": "attachment; filename=reorder_list.xlsx"})


# ------------------------------------------------------------------ #
# Printable barcode labels
# ------------------------------------------------------------------ #
@app.get("/api/variants/{variant_code}/label")
def variant_label(variant_code: str, db: Session = Depends(get_db)):
    variant = _get_variant_by_code(db, variant_code)
    png_bytes = barcode_service.generate_variant_label(variant.variant_code, variant.product.name, variant.size)
    return StreamingResponse(BytesIO(png_bytes), media_type="image/png")


@app.get("/api/products/{sku}/qr")
def product_qr_label(sku: str, db: Session = Depends(get_db)):
    """One scannable design QR; scanning opens the product with every size."""
    product = _get_product_by_sku(db, sku)
    png_bytes = barcode_service.generate_product_qr_label(product.sku, product.name)
    return StreamingResponse(
        BytesIO(png_bytes), media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{product.sku}_product_qr.png"'},
    )


@app.get("/api/products/{sku}/barcode")
def product_barcode_label(sku: str, db: Session = Depends(get_db)):
    """One design-level barcode; the PRODUCT: prefix routes scanners to all sizes."""
    product = _get_product_by_sku(db, sku)
    png_bytes = barcode_service.generate_product_barcode_label(product.sku, product.name)
    return StreamingResponse(
        BytesIO(png_bytes), media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{product.sku}_product_barcode.png"'},
    )


@app.get("/api/products/{sku}/label")
def product_combo_label(sku: str, db: Session = Depends(get_db)):
    """One compact product label containing both QR and Code128 symbols."""
    product = _get_product_by_sku(db, sku)
    png_bytes = barcode_service.generate_product_combo_label(product.sku, product.name)
    return StreamingResponse(
        BytesIO(png_bytes), media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{product.sku}_product_label.png"'},
    )


@app.get("/api/labels/sheet")
def labels_sheet(variant_codes: str = Query(...), db: Session = Depends(get_db)):
    codes = [c.strip().upper() for c in variant_codes.split(",") if c.strip()]
    variants = (
        db.query(models.Variant).options(joinedload(models.Variant.product))
        .filter(models.Variant.variant_code.in_(codes), models.Variant.is_deleted == False).all()  # noqa: E712
    )
    if not variants:
        raise HTTPException(404, "No matching variants found for the given codes.")
    variants.sort(key=lambda v: (v.product.sku, bulk_stock_service.SIZE_ORDER.get(v.size, 99), v.size))
    png_bytes = barcode_service.generate_labels_sheet([{"variant_code": v.variant_code, "product_name": v.product.name, "size": v.size} for v in variants])
    return StreamingResponse(BytesIO(png_bytes), media_type="image/png", headers={"Content-Disposition": "attachment; filename=labels.png"})


# ------------------------------------------------------------------ #
# Analytics: movers, for the dashboard chart
# ------------------------------------------------------------------ #
@app.get("/api/analytics/movers", response_model=list[schemas.MoverOut])
def movers(days: int = Query(30, ge=1, le=365), limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db)):
    """Top ordered designs. Pending and dispatched orders both count as movement."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        db.query(
            models.Product.sku, models.Product.name, models.Product.image_path,
            func.sum(models.OrderLine.qty_ordered).label("units"),
        )
        .join(models.Variant, models.OrderLine.variant_id == models.Variant.id)
        .join(models.Product, models.Variant.product_id == models.Product.id)
        .join(models.OrderUpload, models.OrderLine.order_upload_id == models.OrderUpload.id)
        .filter(
            models.OrderUpload.uploaded_at >= since,
            models.Variant.is_deleted == False,  # noqa: E712
            models.Product.is_deleted == False,  # noqa: E712
            models.OrderLine.status.in_([*ACTIVE_ORDER_STATUSES, models.OrderLineStatus.FULFILLED]),
        )
        .group_by(models.Product.sku, models.Product.name, models.Product.image_path)
        .order_by(func.sum(models.OrderLine.qty_ordered).desc())
        .limit(limit).all()
    )
    return [{"sku": r.sku, "name": r.name, "image_path": r.image_path, "units_moved": int(r.units)} for r in rows]
