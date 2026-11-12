"""Streamable HTTP MCP tools for MYRAA's inventory assistant.

Writes are two-step: prepare_inventory_action returns a short-lived signed
confirmation token, and confirm_inventory_action applies that exact action.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, or_
from sqlalchemy.orm import aliased, joinedload, selectinload
from mcp.server.fastmcp import FastMCP

from database import SessionLocal
import models
from services import alert_service, excel_service, inventory_service, reorder_service


mcp = FastMCP(
    "MYRAA Inventory MCP",
    instructions=(
        "You are MYRAA, the inventory assistant. Use these tools for all live inventory facts. "
        "Never guess stock or report a write as complete before confirm_inventory_action succeeds. "
        "For every stock, product, size, order import, or dispatch change, call prepare_inventory_action, show "
        "the exact details, obtain the user's explicit confirmation, then call "
        "confirm_inventory_action with the returned token. Tokens are short-lived and bound to "
        "prepared data. WhatsApp only creates a user-reviewed draft; it never sends a message."
    ),
)

ACTIVE_ORDER_STATUSES = (models.OrderLineStatus.PENDING, models.OrderLineStatus.INSUFFICIENT_STOCK)
TOKEN_TTL_SECONDS = 600
logger = logging.getLogger(__name__)

_INVENTORY_QUERY_STOPWORDS = {
    # English and common Roman Hindi/Hinglish question words. Keep catalog
    # identity terms (design, colour, brand fragments, and SKU words) intact.
    "a", "an", "and", "are", "available", "batao", "bataye", "can", "check",
    "chahiye", "de", "dikhao", "do", "does", "for", "hai", "hain", "how",
    "in", "inventory", "is", "ka", "kar", "ke", "ki", "kitna", "kitne",
    "kitni", "ko", "me", "mein", "mujhe", "my", "of", "please", "show",
    "size", "sizes", "stock", "tell", "the", "what", "which", "with", "your",
}
_INVENTORY_SIZE_TOKENS = {
    "xs", "s", "m", "l", "xl", "xxl", "xxxl", "2xs", "xxs", "2xl",
    "3xl", "4xl", "5xl", "3xs", "small", "medium", "large", "size",
}


def _session():
    return SessionLocal()


def _clean_sku(value: str) -> str:
    return str(value or "").strip().upper()


def _size(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().upper())
    return {
        "SMALL": "S", "MEDIUM": "M", "LARGE": "L", "2XL": "XXL",
        "DOUBLE XL": "XXL", "XXXL": "3XL", "FREE-SIZE": "FREE SIZE",
        "ONE SIZE": "FREE SIZE",
    }.get(normalized, normalized)


def _product(db, sku: str, include_deleted: bool = False):
    query = db.query(models.Product).options(
        selectinload(models.Product.variants), joinedload(models.Product.vendor)
    ).filter(models.Product.sku == _clean_sku(sku))
    if not include_deleted:
        query = query.filter(models.Product.is_deleted.is_(False))
    return query.first()


def _product_data(product) -> dict[str, Any]:
    variants = sorted(
        (variant for variant in product.variants if not variant.is_deleted),
        key=lambda variant: (variant.size.casefold(), variant.size),
    )
    return {
        "sku": product.sku, "name": product.name, "category": product.category,
        "vendor": product.vendor.name if product.vendor else None,
        "image_path": product.image_path,
        "sizes": [{
            "variant_code": variant.variant_code, "size": variant.size,
            "current_stock": int(variant.current_stock),
            "counted": variant.last_counted_at is not None,
            "reorder_threshold": int(variant.reorder_threshold),
            "target_stock_level": int(variant.target_stock_level),
        } for variant in variants],
        "total_stock": sum(int(variant.current_stock) for variant in variants),
    }


def _find_variant(db, variant_code: str, include_deleted: bool = False):
    code = str(variant_code or "").strip().upper()
    candidates = [code]
    if "-" in code:
        candidates.append(code.replace("-", "_"))
    if "_" in code:
        candidates.append(code.rsplit("_", 1)[0] + "-" + code.rsplit("_", 1)[1])
    query = db.query(models.Variant).options(joinedload(models.Variant.product)).filter(
        models.Variant.variant_code.in_(list(dict.fromkeys(candidates)))
    )
    if not include_deleted:
        query = query.filter(models.Variant.is_deleted.is_(False))
    return query.first()


def _dispatchable_by_line(db, target_lines) -> dict[int, int]:
    target_ids = {line.id for line in target_lines if line.status in ACTIVE_ORDER_STATUSES}
    variant_ids = {line.variant_id for line in target_lines if line.variant_id is not None}
    if not target_ids or not variant_ids:
        return {}
    pending = (
        db.query(models.OrderLine)
        .join(models.OrderUpload, models.OrderLine.order_upload_id == models.OrderUpload.id)
        .options(joinedload(models.OrderLine.variant))
        .filter(models.OrderLine.status.in_(ACTIVE_ORDER_STATUSES), models.OrderLine.variant_id.in_(variant_ids))
        .order_by(models.OrderUpload.uploaded_at.asc(), models.OrderUpload.id.asc(), models.OrderLine.id.asc())
        .all()
    )
    stock_left: dict[int, int] = {}
    result: dict[int, int] = {}
    for line in pending:
        variant = line.variant
        if variant is None or variant.is_deleted or variant.product.is_deleted:
            continue
        if variant.id not in stock_left:
            stock_left[variant.id] = max(int(variant.current_stock or 0), 0) if variant.last_counted_at else 0
        remaining = max(int(line.qty_ordered or 0) - int(line.qty_fulfilled or 0), 0)
        dispatchable = min(remaining, stock_left[variant.id])
        if line.id in target_ids:
            result[line.id] = dispatchable
        stock_left[variant.id] -= dispatchable
    return result


def _order_import_csv(lines: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["SKU", "Size", "Quantity"])
    for line in lines:
        writer.writerow([line["sku"], line.get("size") or "", int(line["quantity"])])
    return buffer.getvalue().encode("utf-8-sig")


def _mcp_api_secret() -> bytes:
    value = os.getenv("INVENTORY_MCP_API_KEY") or os.getenv("DISHA_MCP_API_KEY") or ""
    return value.strip().encode("utf-8")


def _confirmation_token(action: str, payload: dict[str, Any]) -> str:
    secret = _confirmation_signing_secret()
    if len(secret) < 32:
        raise ValueError("The MCP API key is not configured or is too short.")
    body = json.dumps({"v": 1, "action": action, "payload": payload,
                       "exp": int(time.time()) + TOKEN_TTL_SECONDS},
                      separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    signature = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _decode_confirmation_token(token: str) -> tuple[str, dict[str, Any]]:
    try:
        encoded, supplied_signature = str(token or "").split(".", 1)
        secret = _confirmation_signing_secret()
        expected = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if len(secret) < 32 or not hmac.compare_digest(supplied_signature, expected):
            raise ValueError("Invalid confirmation token.")
        body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        claims = json.loads(body.decode("utf-8"))
        if claims.get("v") != 1 or int(claims.get("exp", 0)) < int(time.time()):
            raise ValueError("Confirmation expired. Prepare the action again.")
        if not isinstance(claims.get("payload"), dict):
            raise ValueError("Invalid confirmation payload.")
        return str(claims["action"]), claims["payload"]
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(str(exc) or "Invalid or expired confirmation token.") from exc


def _confirmation_signing_secret() -> bytes:
    """Use a separate purpose-derived key for signed user confirmation tokens.

    Cloud chat can prepare and confirm actions on the same inventory service
    using its server-side provider key, without exposing either secret to the
    browser or making the MCP endpoint public.
    """
    root = (
        os.getenv("MYRAA_CONFIRMATION_SECRET")
        or os.getenv("INVENTORY_MCP_API_KEY")
        or os.getenv("DISHA_MCP_API_KEY")
        or os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or ""
    ).strip().encode("utf-8")
    if len(root) < 32:
        return b""
    return hmac.new(root, b"MYRAA-inventory-confirmation-v1", hashlib.sha256).digest()


@mcp.tool()
def search_inventory(query: str = "", limit: int = 25, offset: int = 0) -> dict[str, Any]:
    """Search product/name/SKU/color terms and return live stock by size.

    Natural-language request words in English or Roman Hindi and size labels are
    ignored so callers may pass a full question such as "Mewari Gold ka size M
    stock batao" without those words preventing a product match.
    """
    limit, offset = min(max(int(limit), 1), 50), max(int(offset), 0)
    raw_query = str(query or "").strip()
    tokens = list(dict.fromkeys(
        token for token in re.findall(r"[\w]+", raw_query.casefold(), flags=re.UNICODE)
        if token not in _INVENTORY_QUERY_STOPWORDS and token not in _INVENTORY_SIZE_TOKENS
    ))
    if raw_query and not tokens:
        return {"items": [], "total": 0, "limit": limit, "offset": offset,
                "message": "Include a product name, colour, category, or SKU."}
    db = _session()
    try:
        q = db.query(models.Product).options(selectinload(models.Product.variants)).filter(models.Product.is_deleted.is_(False))
        for token in tokens:
            like = f"%{token}%"
            q = q.filter(or_(models.Product.sku.ilike(like), models.Product.name.ilike(like), models.Product.category.ilike(like)))
        total = q.count()
        products = q.order_by(models.Product.name.asc(), models.Product.id.asc()).offset(offset).limit(limit).all()
        return {"items": [_product_data(product) for product in products], "total": total, "limit": limit, "offset": offset}
    finally:
        db.close()


@mcp.tool()
def get_product(sku: str) -> dict[str, Any]:
    """Get one product's metadata and current stock for every size by product SKU."""
    db = _session()
    try:
        product = _product(db, sku)
        return _product_data(product) if product else {"error": f"Product {sku!r} not found."}
    finally:
        db.close()


@mcp.tool()
def get_inventory_summary() -> dict[str, Any]:
    """Get live dashboard counts: products, sizes, stock, uncounted, low, out, alerts, and pending orders."""
    db = _session()
    try:
        P, V = models.Product, models.Variant
        live = (V.is_deleted.is_(False), P.is_deleted.is_(False))
        counted = V.last_counted_at.isnot(None)
        def count(*conditions):
            return int(db.query(func.count(V.id)).join(P).filter(*live, *conditions).scalar() or 0)
        pending = db.query(models.OrderLine).filter(models.OrderLine.status.in_(ACTIVE_ORDER_STATUSES))
        units = pending.with_entities(func.coalesce(func.sum(models.OrderLine.qty_ordered - models.OrderLine.qty_fulfilled), 0)).scalar() or 0
        total_stock = db.query(func.coalesce(func.sum(V.current_stock), 0)).join(P).filter(*live, counted).scalar() or 0
        physical_alert_codes = {
            code for (code,) in db.query(V.variant_code).join(models.Alert, models.Alert.variant_id == V.id).join(P, V.product_id == P.id)
            .filter(models.Alert.resolved.is_(False), V.is_deleted.is_(False), P.is_deleted.is_(False)).all()
        }
        shortage_codes = {
            row["variant_code"] for row in reorder_service.generate_reorder_list(db, save_batch=False)
            if row["required_qty"] > 0 and not row.get("is_new")
        }
        return {
            "products": int(db.query(func.count(P.id)).filter(P.is_deleted.is_(False)).scalar() or 0),
            "sizes": count(), "counted_sizes": count(counted), "uncounted_sizes": count(V.last_counted_at.is_(None)),
            "out_sizes": count(counted, V.current_stock <= 0),
            "low_sizes": count(counted, V.current_stock > 0, V.current_stock <= V.reorder_threshold),
            "healthy_sizes": count(counted, V.current_stock > V.reorder_threshold),
            "open_alerts": len(physical_alert_codes | shortage_codes),
            "counted_units": int(total_stock), "pending_order_lines": int(pending.count()),
            "pending_order_units": int(units),
        }
    finally:
        db.close()


@mcp.tool()
def get_inventory_alerts(query: str = "", limit: int = 50) -> dict[str, Any]:
    """List current size-level low stock, out of stock, and pending order shortage alerts."""
    limit = min(max(int(limit), 1), 100)
    db = _session()
    try:
        rows = alert_service.get_open_alerts(db, limit=None, offset=0, q=query or None)
        items = [{"sku": row.variant.product.sku, "product": row.variant.product.name,
                  "variant_code": row.variant.variant_code, "size": row.variant.size,
                  "alert_type": row.alert_type, "current_stock": int(row.variant.current_stock),
                  "stock_at_alert": int(row.stock_at_alert)} for row in rows]
        existing_codes = {item["variant_code"] for item in items}
        shortages = reorder_service.generate_reorder_list(db, save_batch=False, part=query or None)
        for shortage in shortages:
            if shortage.get("is_new") or shortage["required_qty"] <= 0 or shortage["variant_code"] in existing_codes:
                continue
            items.append({"sku": shortage["sku"], "product": shortage["name"],
                "variant_code": shortage["variant_code"], "size": shortage["size"],
                "alert_type": "ORDER_SHORTAGE", "current_stock": int(shortage["current_stock"]),
                "stock_at_alert": int(shortage["current_stock"]), "ordered_qty": int(shortage["ordered_qty"]),
                "required_qty": int(shortage["required_qty"])})
            existing_codes.add(shortage["variant_code"])
        if query.strip():
            tokens = re.findall(r"[\w]+", query.casefold(), flags=re.UNICODE)
            items = [item for item in items if all(token in " ".join(str(item.get(field) or "").casefold()
                for field in ("sku", "product", "variant_code", "size", "alert_type")) for token in tokens)]
        items.sort(key=lambda item: (item["alert_type"] != "OUT_OF_STOCK", item["current_stock"], item["variant_code"]))
        return {"items": items[:limit], "total": len(items), "limit": limit}
    finally:
        db.close()


@mcp.tool()
def get_reorder_shortages(query: str = "", limit: int = 100) -> dict[str, Any]:
    """Get live reorder picklist rows, preserving each exact stockable size."""
    limit = min(max(int(limit), 1), 200)
    db = _session()
    try:
        rows = reorder_service.generate_reorder_list(db, save_batch=False, part=query or None)
        return {"items": rows[:limit], "total": len(rows), "limit": limit}
    finally:
        db.close()


@mcp.tool()
def get_stock_history(query: str = "", limit: int = 50) -> dict[str, Any]:
    """Read append-only stock transactions newest first, optionally filtered by SKU/name/size."""
    limit = min(max(int(limit), 1), 100)
    db = _session()
    try:
        q = db.query(models.InventoryTransaction).join(models.Variant).join(models.Product).options(
            joinedload(models.InventoryTransaction.variant).joinedload(models.Variant.product))
        if query.strip():
            like = f"%{query.strip()}%"
            q = q.filter(or_(models.Product.sku.ilike(like), models.Product.name.ilike(like), models.Variant.variant_code.ilike(like)))
        rows = q.order_by(models.InventoryTransaction.created_at.desc(), models.InventoryTransaction.id.desc()).limit(limit).all()
        return {"items": [{"id": row.id, "sku": row.variant.product.sku, "product": row.variant.product.name,
            "size": row.variant.size, "variant_code": row.variant.variant_code, "change_qty": row.change_qty,
            "balance_after": row.balance_after, "type": row.transaction_type.value, "reference": row.reference,
            "created_by": row.created_by, "created_at": row.created_at.isoformat() if row.created_at else None} for row in rows]}
    finally:
        db.close()


@mcp.tool()
def get_daily_dispatch_summary() -> dict[str, Any]:
    """Summarize actual dispatched units today in India time; pending orders are excluded."""
    ist = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(ist).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=ist).astimezone(timezone.utc)
    end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=ist).astimezone(timezone.utc)
    db = _session()
    try:
        T, V, P = models.InventoryTransaction, models.Variant, models.Product
        void_marker = aliased(T)
        not_voided = ~db.query(void_marker.id).filter(
            void_marker.reference == func.replace(T.reference, ":dispatch", ":delete-reversal")
        ).exists()
        base = db.query(T).filter(T.transaction_type == models.TransactionType.ORDER_DEDUCTION,
            T.created_at >= start, T.created_at < end, T.change_qty < 0,
            T.reference.like("order_upload:%:dispatch"), not_voided)
        units = base.with_entities(func.coalesce(-func.sum(T.change_qty), 0)).scalar() or 0
        products = db.query(P.sku, P.name, func.sum(-T.change_qty).label("units")).join(
            V, V.product_id == P.id).join(T, T.variant_id == V.id).filter(
            T.transaction_type == models.TransactionType.ORDER_DEDUCTION,
            T.created_at >= start, T.created_at < end, T.change_qty < 0,
            T.reference.like("order_upload:%:dispatch"), not_voided).group_by(P.id, P.sku, P.name).order_by(
            func.sum(-T.change_qty).desc()).limit(10).all()
        return {"date": today.isoformat(), "timezone": "Asia/Kolkata", "dispatched_units": int(units),
                "products": [{"sku": row.sku, "name": row.name, "units": int(row.units or 0)} for row in products]}
    finally:
        db.close()


@mcp.tool()
def get_dispatch_analytics(start_date: str = "", end_date: str = "", limit: int = 10) -> dict[str, Any]:
    """Summarize dispatched units and top products/sizes for inclusive India-local dates (YYYY-MM-DD, up to 366 days)."""
    ist = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(ist).date()
    try:
        parsed_start = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
        parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None
        start_day = parsed_start or parsed_end or today
        end_day = parsed_end or start_day
    except ValueError as exc:
        raise ValueError("Dates must use YYYY-MM-DD format.") from exc
    if end_day < start_day or (end_day - start_day).days > 365:
        raise ValueError("Choose an inclusive date range of 1 to 366 days.")
    limit = min(max(int(limit), 1), 50)
    start_utc = datetime.combine(start_day, datetime.min.time(), tzinfo=ist).astimezone(timezone.utc)
    end_utc = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=ist).astimezone(timezone.utc)
    db = _session()
    try:
        T, V, P = models.InventoryTransaction, models.Variant, models.Product
        void_marker = aliased(T)
        not_voided = ~db.query(void_marker.id).filter(
            void_marker.reference == func.replace(T.reference, ":dispatch", ":delete-reversal")
        ).exists()
        base = db.query(T).filter(T.transaction_type == models.TransactionType.ORDER_DEDUCTION,
            T.created_at >= start_utc, T.created_at < end_utc, T.change_qty < 0,
            T.reference.like("order_upload:%:dispatch"), not_voided)
        units = base.with_entities(func.coalesce(-func.sum(T.change_qty), 0)).scalar() or 0
        lines = base.with_entities(func.count(T.id)).scalar() or 0
        products = db.query(P.sku, P.name, func.sum(-T.change_qty).label("units")).join(
            V, V.product_id == P.id).join(T, T.variant_id == V.id).filter(
            T.transaction_type == models.TransactionType.ORDER_DEDUCTION,
            T.created_at >= start_utc, T.created_at < end_utc, T.change_qty < 0,
            T.reference.like("order_upload:%:dispatch"), not_voided).group_by(
            P.id, P.sku, P.name).order_by(func.sum(-T.change_qty).desc(), P.sku.asc()).limit(limit).all()
        sizes = db.query(P.sku, P.name, V.size, func.sum(-T.change_qty).label("units")).join(
            V, V.product_id == P.id).join(T, T.variant_id == V.id).filter(
            T.transaction_type == models.TransactionType.ORDER_DEDUCTION,
            T.created_at >= start_utc, T.created_at < end_utc, T.change_qty < 0,
            T.reference.like("order_upload:%:dispatch"), not_voided).group_by(
            P.id, P.sku, P.name, V.size).order_by(func.sum(-T.change_qty).desc(), P.sku.asc(), V.size.asc()).limit(limit).all()
        return {"start_date": start_day.isoformat(), "end_date": end_day.isoformat(),
            "timezone": "Asia/Kolkata", "dispatched_units": int(units), "dispatch_transactions": int(lines),
            "top_products": [{"sku": row.sku, "name": row.name, "units": int(row.units or 0)} for row in products],
            "top_sizes": [{"sku": row.sku, "name": row.name, "size": row.size, "units": int(row.units or 0)} for row in sizes]}
    finally:
        db.close()


@mcp.tool()
def list_pending_order_batches(limit: int = 30) -> dict[str, Any]:
    """List recent order batches with remaining quantities and currently dispatchable units."""
    limit = min(max(int(limit), 1), 100)
    db = _session()
    try:
        batches = db.query(models.OrderUpload).options(selectinload(models.OrderUpload.lines)).order_by(
            models.OrderUpload.uploaded_at.desc(), models.OrderUpload.id.desc()).limit(limit).all()
        lines = [line for batch in batches for line in batch.lines if line.status in ACTIVE_ORDER_STATUSES and line.variant_id]
        available = _dispatchable_by_line(db, lines)
        items = []
        for batch in batches:
            pending = [line for line in batch.lines if line.status in ACTIVE_ORDER_STATUSES]
            units = sum(max(int(line.qty_ordered) - int(line.qty_fulfilled), 0) for line in pending)
            if not units:
                continue
            items.append({"batch_id": batch.id, "filename": batch.filename,
                "uploaded_at": batch.uploaded_at.isoformat() if batch.uploaded_at else None,
                "pending_lines": len(pending), "pending_units": units,
                "dispatchable_units": sum(available.get(line.id, 0) for line in pending),
                "not_found_lines": sum(1 for line in batch.lines if line.status == models.OrderLineStatus.SKU_NOT_FOUND)})
        return {"items": items}
    finally:
        db.close()


@mcp.tool()
def get_order_batch(batch_id: int) -> dict[str, Any]:
    """Show batch lines with product, exact size, ordered/fulfilled/remaining quantities, and current availability."""
    db = _session()
    try:
        batch = db.query(models.OrderUpload).options(
            selectinload(models.OrderUpload.lines).joinedload(models.OrderLine.variant).joinedload(models.Variant.product)
        ).filter(models.OrderUpload.id == int(batch_id)).first()
        if batch is None:
            return {"error": f"Batch {batch_id} not found."}
        pending = [line for line in batch.lines if line.status in ACTIVE_ORDER_STATUSES]
        available = _dispatchable_by_line(db, pending)
        return {"batch_id": batch.id, "filename": batch.filename,
            "pending_units": sum(max(line.qty_ordered - line.qty_fulfilled, 0) for line in pending),
            "lines": [{"line_id": line.id, "sku": line.sku,
                "product": line.variant.product.name if line.variant and line.variant.product else None,
                "size": line.size, "ordered": int(line.qty_ordered), "fulfilled": int(line.qty_fulfilled),
                "remaining": max(int(line.qty_ordered) - int(line.qty_fulfilled), 0),
                "dispatchable_now": int(available.get(line.id, 0)), "status": line.status.value}
                for line in sorted(batch.lines, key=lambda item: item.id)]}
    finally:
        db.close()


def _validate_prepare(action: str, payload: dict[str, Any], db) -> tuple[str, dict[str, Any]]:
    """Validate and freeze an action payload before asking the user to confirm."""
    if action == "stock_change":
        code = str(payload.get("variant_code") or "").strip().upper()
        operation, quantity = str(payload.get("operation") or "").strip().lower(), payload.get("quantity")
        if operation not in {"add", "set", "remove"} or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0 or quantity > 1_000_000:
            raise ValueError("Use operation add, set, or remove and a non-negative whole-number quantity.")
        if operation in {"add", "remove"} and quantity == 0:
            raise ValueError("Add/remove quantity must be greater than zero.")
        variant = _find_variant(db, code)
        if not variant:
            raise ValueError(f"Active size {code!r} not found.")
        if operation == "remove" and quantity > variant.current_stock:
            raise ValueError(f"Only {variant.current_stock} pieces are in stock for {variant.variant_code}.")
        if operation == "add" and not variant.last_counted_at:
            raise ValueError("This size has not been physically counted. Set the verified total stock instead.")
        current = int(variant.current_stock)
        after = quantity if operation == "set" else current + quantity if operation == "add" else current - quantity
        return action, {"variant_id": variant.id, "variant_code": variant.variant_code, "operation": operation,
            "quantity": quantity, "expected_stock": current, "expected_counted": variant.last_counted_at is not None,
            "expected_updated_at": variant.updated_at.isoformat() if variant.updated_at else None,
            "sku": variant.product.sku, "product": variant.product.name, "size": variant.size, "result_stock": after}

    if action == "product_create":
        sku, name = _clean_sku(payload.get("sku")), str(payload.get("name") or "").strip()
        raw_sizes = payload.get("sizes", [])
        if not isinstance(raw_sizes, list) or any(not isinstance(item, str) for item in raw_sizes):
            raise ValueError("Product sizes must be a list of size names.")
        sizes = list(dict.fromkeys(_size(item) for item in raw_sizes if item.strip()))
        if not sku or not name or not sizes:
            raise ValueError("Product creation needs an SKU, product name, and at least one size.")
        if len(sku) > 64 or len(name) > 300 or any(len(item) > 20 for item in sizes):
            raise ValueError("SKU, name, or size exceeds the catalog field length.")
        if any(len(sku) + 1 + len(item) > 80 for item in sizes):
            raise ValueError("SKU plus size would exceed the stockable variant-code limit.")
        if _product(db, sku, include_deleted=True):
            raise ValueError(f"SKU {sku} already exists, including in Trash.")
        return action, {"sku": sku, "name": name, "category": str(payload.get("category") or "").strip() or None, "sizes": sizes}

    if action in {"product_update", "product_archive", "product_restore"}:
        sku = _clean_sku(payload.get("sku"))
        product = _product(db, sku, include_deleted=(action == "product_restore"))
        if not product:
            raise ValueError(f"Product {sku} not found.")
        if action == "product_restore" and not product.is_deleted:
            raise ValueError(f"Product {sku} is already active.")
        changes = {}
        if action == "product_update":
            for source, target, maximum in (("new_sku", "sku", 64), ("new_name", "name", 300), ("new_category", "category", 100)):
                if source in payload and payload[source] is not None:
                    value = _clean_sku(payload[source]) if target == "sku" else str(payload[source]).strip()
                    current_value = product.sku if target == "sku" else getattr(product, target)
                    if value == (current_value or ""):
                        continue
                    if not value and target != "category":
                        raise ValueError(f"{target} cannot be empty.")
                    if len(value) > maximum:
                        raise ValueError(f"{target} is too long.")
                    changes[target] = value or None
            if not changes:
                raise ValueError("Provide at least one changed field.")
            if "sku" in changes and changes["sku"] != product.sku and _product(db, changes["sku"], include_deleted=True):
                raise ValueError(f"SKU {changes['sku']} already exists.")
            if "sku" in changes and any(
                len(changes["sku"]) + 1 + len(variant.size) > 80
                for variant in product.variants if not variant.is_deleted
            ):
                raise ValueError("The new SKU plus an existing size would exceed the stockable variant-code limit.")
        return action, {"product_id": product.id, "sku": product.sku, "was_deleted": bool(product.is_deleted),
            "updated_at": product.updated_at.isoformat() if product.updated_at else None, "changes": changes}

    if action in {"size_add", "size_archive", "size_restore"}:
        code = str(payload.get("variant_code") or "").strip().upper()
        variant = _find_variant(db, code, include_deleted=True)
        if action == "size_add":
            sku, size = _clean_sku(payload.get("sku")), _size(payload.get("size"))
            product = _product(db, sku)
            if not product or not size:
                raise ValueError("Adding a size needs an active product SKU and size.")
            if len(sku) + 1 + len(size) > 80:
                raise ValueError("SKU plus size would exceed the stockable variant-code limit.")
            existing = next((item for item in product.variants if item.size.upper() == size), None)
            if existing and not existing.is_deleted:
                raise ValueError(f"Size {size} already exists on {sku}.")
            initial = payload.get("initial_stock", 0)
            if isinstance(initial, bool) or not isinstance(initial, int) or initial < 0 or initial > 1_000_000:
                raise ValueError("Initial stock must be a non-negative whole number.")
            return action, {"product_id": product.id, "sku": sku, "size": size, "initial_stock": initial,
                "existing_variant_id": existing.id if existing else None}
        if not variant:
            raise ValueError(f"Size {code} not found.")
        if action == "size_archive" and variant.is_deleted:
            raise ValueError(f"Size {code} is already in Trash.")
        if action == "size_restore" and not variant.is_deleted:
            raise ValueError(f"Size {code} is already active.")
        return action, {"variant_id": variant.id, "variant_code": variant.variant_code, "sku": variant.product.sku,
            "was_deleted": bool(variant.is_deleted), "updated_at": variant.updated_at.isoformat() if variant.updated_at else None}

    if action == "order_import":
        raw_lines = payload.get("lines")
        if not isinstance(raw_lines, list) or not raw_lines or len(raw_lines) > 500:
            raise ValueError("Order import needs between 1 and 500 rows.")
        lines = []
        for index, row in enumerate(raw_lines, start=1):
            if not isinstance(row, dict):
                raise ValueError(f"Order row {index} must include SKU, optional size, and quantity.")
            sku = str(row.get("variant_code") or row.get("sku") or "").strip()
            size = _size(row.get("size")) if row.get("size") else ""
            quantity = row.get("quantity", row.get("qty", 1))
            if not sku or len(sku) > 64 or len(size) > 20:
                raise ValueError(f"Order row {index} has an invalid SKU or size.")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1 or quantity > 1_000_000:
                raise ValueError(f"Order row {index} quantity must be a whole number from 1 to 1000000.")
            lines.append({"sku": sku, "size": size, "quantity": quantity})
        filename = str(payload.get("filename") or "MYRAA orders.csv").strip().replace("/", "_").replace("\\", "_")[:250]
        file_bytes = _order_import_csv(lines)
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        if db.query(models.OrderUpload.id).filter(models.OrderUpload.file_hash == file_hash).first():
            raise ValueError("This exact order list was already imported.")
        return action, {"filename": filename, "lines": lines, "row_count": len(lines),
            "total_quantity": sum(row["quantity"] for row in lines), "content_hash": file_hash}

    if action == "order_dispatch":
        batch_id = payload.get("batch_id")
        if isinstance(batch_id, bool) or not isinstance(batch_id, int) or batch_id < 1:
            raise ValueError("Provide a valid order batch ID.")
        batch = db.query(models.OrderUpload).filter(models.OrderUpload.id == batch_id).first()
        if batch is None:
            raise ValueError(f"Order batch {batch_id} not found.")
        pending = db.query(models.OrderLine).options(joinedload(models.OrderLine.variant)).filter(
            models.OrderLine.order_upload_id == batch_id, models.OrderLine.status.in_(ACTIVE_ORDER_STATUSES),
            models.OrderLine.variant_id.isnot(None)).order_by(models.OrderLine.id.asc()).all()
        wanted = payload.get("line_ids")
        if wanted is not None:
            if not isinstance(wanted, list) or not wanted or any(isinstance(item, bool) or not isinstance(item, int) for item in wanted):
                raise ValueError("line_ids must be a non-empty list of pending line IDs.")
            by_id = {line.id: line for line in pending}
            if set(wanted) - by_id.keys():
                raise ValueError("Some selected lines are no longer pending. Refresh the batch first.")
            pending = [by_id[line_id] for line_id in sorted(set(wanted))]
        available = _dispatchable_by_line(db, pending)
        plan = [{"line_id": line.id, "expected_fulfilled": int(line.qty_fulfilled), "qty": int(available.get(line.id, 0))}
                for line in pending if available.get(line.id, 0) > 0]
        if not plan:
            raise ValueError("No counted stock is currently dispatchable for the selected batch lines.")
        return action, {"batch_id": batch_id, "filename": batch.filename, "plan": plan,
            "dispatchable_units": sum(item["qty"] for item in plan),
            "pending_units": sum(max(line.qty_ordered - line.qty_fulfilled, 0) for line in pending)}

    if action == "whatsapp_draft":
        phone = re.sub(r"[\s().-]", "", str(payload.get("phone") or "").translate(str.maketrans("०१२३४५६७८९", "0123456789")))
        if re.fullmatch(r"[0-9]{10}", phone):
            phone = "+91" + phone
        message = str(payload.get("message") or "").strip()
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
            raise ValueError("Provide a 10-digit Indian mobile or an international phone number.")
        if not message or len(message) > 1000:
            raise ValueError("Provide the exact WhatsApp message, up to 1000 characters.")
        return action, {"phone": phone, "name": str(payload.get("name") or "").strip()[:80], "message": message}

    raise ValueError("Unsupported action. Supported actions: stock_change, product_create/update/archive/restore, size_add/archive/restore, order_import, order_dispatch, whatsapp_draft.")


@mcp.tool()
def prepare_inventory_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Prepare a stock/product/size/dispatch write or WhatsApp draft. Writes return a confirmation token; do not commit until the user confirms."""
    db = _session()
    try:
        action, frozen = _validate_prepare(str(action).strip().lower(), payload or {}, db)
        if action == "whatsapp_draft":
            digits = frozen["phone"].lstrip("+")
            return {"status": "draft_ready", "recipient": frozen["name"], "phone": frozen["phone"],
                "message": frozen["message"], "url": f"https://wa.me/{digits}?text={quote(frozen['message'])}",
                "user_must_review_and_press_send": True}
        return {"status": "awaiting_user_confirmation", "confirmation_token": _confirmation_token(action, frozen),
            "expires_in_seconds": TOKEN_TTL_SECONDS, "action": action, "details": frozen,
            "instruction": "Show these exact details to the user. Confirm only after explicit approval."}
    except Exception as exc:
        db.rollback()
        return {"status": "error", "message": str(exc)}
    finally:
        db.close()


def _resolve_alerts(db, variant_id: int) -> None:
    now = datetime.now(timezone.utc)
    db.query(models.Alert).filter(models.Alert.variant_id == variant_id, models.Alert.resolved.is_(False)).update(
        {models.Alert.resolved: True, models.Alert.resolved_at: now}, synchronize_session=False)


def _reconcile_after_commit(db, result: dict[str, Any]) -> dict[str, Any]:
    """Do not report a committed write as failed if background order reconciliation errors."""
    try:
        result["reconciled_order_lines"] = int(excel_service.reconcile_unmapped_order_lines(db))
    except Exception as exc:
        db.rollback()
        logger.warning("MYRAA MCP action committed; unmapped order reconciliation failed (%s).", type(exc).__name__)
        result["warning"] = "The action succeeded, but some previously unmatched order lines could not be refreshed."
    return result


def _confirm_stock(payload: dict[str, Any]) -> dict[str, Any]:
    db = _session()
    try:
        variant = db.query(models.Variant).filter(models.Variant.id == int(payload["variant_id"])).with_for_update().first()
        if variant is None or variant.is_deleted or variant.product.is_deleted:
            raise ValueError("This size was removed after preparation.")
        if int(variant.current_stock) != int(payload["expected_stock"]):
            raise ValueError(f"Stock changed after preparation; current count is {variant.current_stock}. Prepare again.")
        operation, quantity = payload["operation"], int(payload["quantity"])
        actual_updated = variant.updated_at.isoformat() if variant.updated_at else None
        if actual_updated != payload.get("expected_updated_at"):
            raise ValueError("This size was updated after preparation. Prepare the action again.")
        if operation == "add":
            if not variant.last_counted_at:
                raise ValueError("This size has no physical count. Set its verified total first.")
            inventory_service.apply_stock_change(db, variant, quantity, models.TransactionType.RESTOCK,
                reference="MYRAA MCP confirmed stock add", created_by="myraa_mcp",
                expected_current_stock=int(payload["expected_stock"]))
            variant.last_counted_at = datetime.now(timezone.utc)
        elif operation == "remove":
            if quantity > int(variant.current_stock):
                raise ValueError("There is not enough stock for this removal.")
            inventory_service.apply_stock_change(db, variant, -quantity, models.TransactionType.MANUAL_ADJUSTMENT,
                reference="MYRAA MCP confirmed stock removal", created_by="myraa_mcp",
                expected_current_stock=int(payload["expected_stock"]))
        else:
            inventory_service.set_absolute_stock(db, variant, quantity, created_by="myraa_mcp",
                note="MYRAA MCP confirmed physical count", expected_current_stock=int(payload["expected_stock"]))
        alert_service.check_variant_alert(db, variant)
        result = {"status": "completed", "variant_code": variant.variant_code, "sku": variant.product.sku,
            "product": variant.product.name, "size": variant.size, "current_stock": int(variant.current_stock)}
        db.commit()
        return _reconcile_after_commit(db, result)
    except Exception as exc:
        db.rollback()
        return {"status": "error", "message": str(exc)}
    finally:
        db.close()


def _confirm_product_create(payload: dict[str, Any]) -> dict[str, Any]:
    db = _session()
    try:
        if _product(db, payload["sku"], include_deleted=True):
            raise ValueError(f"SKU {payload['sku']} now exists. Refresh before creating.")
        now = datetime.now(timezone.utc)
        product = models.Product(sku=payload["sku"], name=payload["name"], category=payload.get("category"))
        db.add(product)
        db.flush()
        for size in payload["sizes"]:
            variant = models.Variant(product_id=product.id, size=size, variant_code=f"{product.sku}_{size}",
                current_stock=0, reorder_threshold=5, target_stock_level=20, last_counted_at=now)
            db.add(variant)
            db.flush()
            alert_service.check_variant_alert(db, variant)
        result = {"status": "completed", "product": _product_data(product)}
        db.commit()
        return _reconcile_after_commit(db, result)
    except Exception as exc:
        db.rollback()
        return {"status": "error", "message": str(exc)}
    finally:
        db.close()


def _confirm_product_metadata(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    db = _session()
    try:
        product = db.query(models.Product).filter(models.Product.id == int(payload["product_id"])).first()
        if not product or product.sku != payload["sku"] or bool(product.is_deleted) != bool(payload["was_deleted"]):
            raise ValueError("Product changed after preparation. Prepare a fresh action.")
        actual_updated = product.updated_at.isoformat() if product.updated_at else None
        if actual_updated != payload.get("updated_at"):
            raise ValueError("Product changed after preparation. Prepare a fresh action.")
        if action == "product_update":
            changes = payload["changes"]
            if "sku" in changes:
                new_sku = changes["sku"]
                if _product(db, new_sku, include_deleted=True):
                    raise ValueError(f"SKU {new_sku} now exists.")
                for variant in product.variants:
                    if not variant.is_deleted:
                        variant.variant_code = f"{new_sku}_{variant.size}"
                product.sku = new_sku
            if "name" in changes:
                product.name = changes["name"]
            if "category" in changes:
                product.category = changes["category"]
            result = {"status": "completed", "product": _product_data(product)}
            db.commit()
            return _reconcile_after_commit(db, result)
        now = datetime.now(timezone.utc)
        if action == "product_archive":
            product.is_deleted, product.deleted_at = True, now
            for variant in product.variants:
                if not variant.is_deleted:
                    variant.is_deleted, variant.deleted_at = True, now
                _resolve_alerts(db, variant.id)
            result = {"status": "completed", "sku": product.sku, "trashed": True}
            db.commit()
            return result
        if not product.is_deleted:
            raise ValueError("Product is already active.")
        product.is_deleted, product.deleted_at = False, None
        for variant in product.variants:
            if variant.is_deleted:
                variant.is_deleted, variant.deleted_at = False, None
            alert_service.check_variant_alert(db, variant)
        result = {"status": "completed", "sku": product.sku, "restored": True}
        db.commit()
        return _reconcile_after_commit(db, result)
    except Exception as exc:
        db.rollback()
        return {"status": "error", "message": str(exc)}
    finally:
        db.close()


def _confirm_size(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    db = _session()
    try:
        now = datetime.now(timezone.utc)
        if action == "size_add":
            product = db.query(models.Product).filter(models.Product.id == int(payload["product_id"]),
                models.Product.is_deleted.is_(False)).first()
            if not product:
                raise ValueError("Product was removed after preparation.")
            variant = db.query(models.Variant).filter(models.Variant.product_id == product.id,
                models.Variant.size == payload["size"]).first()
            if variant and not variant.is_deleted:
                raise ValueError("This size already exists. Refresh the product first.")
            if variant:
                variant.is_deleted, variant.deleted_at = False, None
            else:
                variant = models.Variant(product_id=product.id, size=payload["size"],
                    variant_code=f"{product.sku}_{payload['size']}", current_stock=0,
                    reorder_threshold=5, target_stock_level=20)
                db.add(variant)
                db.flush()
            initial = int(payload["initial_stock"])
            if initial:
                inventory_service.apply_stock_change(db, variant, initial, models.TransactionType.INITIAL_STOCK,
                    reference="MYRAA MCP confirmed size creation", created_by="myraa_mcp")
                variant.last_counted_at = now
            alert_service.check_variant_alert(db, variant)
            result = {"status": "completed", "product": _product_data(product)}
            db.commit()
            return _reconcile_after_commit(db, result)
        variant = db.query(models.Variant).options(joinedload(models.Variant.product)).filter(
            models.Variant.id == int(payload["variant_id"])).first()
        if not variant or variant.variant_code != payload["variant_code"] or bool(variant.is_deleted) != bool(payload["was_deleted"]):
            raise ValueError("Size changed after preparation. Prepare the action again.")
        if variant.updated_at and variant.updated_at.isoformat() != payload.get("updated_at"):
            raise ValueError("Size changed after preparation. Prepare the action again.")
        if action == "size_archive":
            variant.is_deleted, variant.deleted_at = True, now
            _resolve_alerts(db, variant.id)
            remaining = db.query(func.count(models.Variant.id)).filter(models.Variant.product_id == variant.product_id,
                models.Variant.is_deleted.is_(False)).scalar() or 0
            if remaining == 0:
                variant.product.is_deleted, variant.product.deleted_at = True, now
            result = {"status": "completed", "variant_code": variant.variant_code, "trashed": True}
            db.commit()
            return result
        variant.is_deleted, variant.deleted_at = False, None
        if variant.product.is_deleted:
            variant.product.is_deleted, variant.product.deleted_at = False, None
        alert_service.check_variant_alert(db, variant)
        result = {"status": "completed", "variant_code": variant.variant_code, "restored": True}
        db.commit()
        return _reconcile_after_commit(db, result)
    except Exception as exc:
        db.rollback()
        return {"status": "error", "message": str(exc)}
    finally:
        db.close()


def _confirm_dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    db = _session()
    try:
        batch_id = int(payload["batch_id"])
        batch = db.query(models.OrderUpload).filter(models.OrderUpload.id == batch_id).with_for_update().first()
        if not batch:
            raise ValueError("Order batch was removed. Refresh order history.")
        planned = {int(item["line_id"]): item for item in (payload.get("plan") or [])}
        lines = db.query(models.OrderLine).options(
            joinedload(models.OrderLine.variant).joinedload(models.Variant.product)
        ).filter(models.OrderLine.order_upload_id == batch_id, models.OrderLine.id.in_(planned.keys()),
            models.OrderLine.status.in_(ACTIVE_ORDER_STATUSES)).with_for_update().all()
        if len(lines) != len(planned):
            raise ValueError("Selected order lines changed after preparation. Prepare a fresh dispatch.")
        for line in lines:
            if int(line.qty_fulfilled) != int(planned[line.id]["expected_fulfilled"]):
                raise ValueError("This batch was already dispatched or updated. Refresh before retrying.")
        available = _dispatchable_by_line(db, lines)
        actual_plan = [(line, min(int(planned[line.id]["qty"]), int(available.get(line.id, 0)))) for line in lines]
        actual_plan = [(line, qty) for line, qty in actual_plan if qty > 0]
        if not actual_plan:
            raise ValueError("No planned quantity remains dispatchable. Check stock and prepare again.")
        try:
            for line, quantity in actual_plan:
                inventory_service.apply_stock_change(db, line.variant, -quantity,
                    models.TransactionType.ORDER_DEDUCTION, reference=f"order_upload:{batch_id}:dispatch",
                    created_by="dispatch")
                line.qty_fulfilled += quantity
                line.status = models.OrderLineStatus.FULFILLED if line.qty_fulfilled >= line.qty_ordered else models.OrderLineStatus.PENDING
                alert_service.check_variant_alert(db, line.variant)
            db.flush()
            remaining = db.query(func.coalesce(func.sum(models.OrderLine.qty_ordered - models.OrderLine.qty_fulfilled), 0)).filter(
                models.OrderLine.order_upload_id == batch_id, models.OrderLine.status.in_(ACTIVE_ORDER_STATUSES)).scalar() or 0
            db.commit()
        except Exception:
            db.rollback()
            raise
        dispatched = sum(quantity for _, quantity in actual_plan)
        return {"status": "completed", "batch_id": batch_id, "dispatched_units": dispatched,
                "pending_units": int(remaining), "lines_dispatched": len(actual_plan)}
    except Exception as exc:
        db.rollback()
        return {"status": "error", "message": str(exc)}
    finally:
        db.close()


def _confirm_order_import(payload: dict[str, Any]) -> dict[str, Any]:
    db = _session()
    try:
        file_bytes = _order_import_csv(payload["lines"])
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        if file_hash != payload["content_hash"] or db.query(models.OrderUpload.id).filter(
            models.OrderUpload.file_hash == file_hash).first():
            raise ValueError("This order list changed or was already imported. Prepare it again.")
        result = excel_service.process_order_excel(db, file_bytes, payload["filename"], created_by="myraa_mcp")
        return {"status": "completed", "batch_id": result["order_upload_id"], "filename": result["filename"],
            "rows": result["total_rows"], "matched_lines": result["pending_count"],
            "unmatched_lines": result["not_found_count"], "pending_units": sum(
                int(line["qty_ordered"]) for line in result["lines"] if line["status"] != "SKU_NOT_FOUND"),
            "message": "Order demand imported. No physical stock was deducted; dispatch is a separate confirmed action."}
    except Exception as exc:
        db.rollback()
        return {"status": "error", "message": str(exc)}
    finally:
        db.close()


@mcp.tool()
def confirm_inventory_action(confirmation_token: str) -> dict[str, Any]:
    """Execute one prepared write after the user explicitly confirmed it. The signed token expires in 10 minutes; stale inventory is rejected."""
    try:
        action, payload = _decode_confirmation_token(confirmation_token)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    handlers = {
        "stock_change": _confirm_stock,
        "product_create": _confirm_product_create,
        "product_update": lambda data: _confirm_product_metadata("product_update", data),
        "product_archive": lambda data: _confirm_product_metadata("product_archive", data),
        "product_restore": lambda data: _confirm_product_metadata("product_restore", data),
        "size_add": lambda data: _confirm_size("size_add", data),
        "size_archive": lambda data: _confirm_size("size_archive", data),
        "size_restore": lambda data: _confirm_size("size_restore", data),
        "order_import": _confirm_order_import,
        "order_dispatch": _confirm_dispatch,
    }
    handler = handlers.get(action)
    if handler is None:
        return {"status": "error", "message": "This confirmation token has an unsupported action."}
    return handler(payload)
