"""
Alert rule: an OPEN (unresolved) alert must exist for a variant iff its
current stock <= reorder_threshold. Idempotent by design - call
check_variant_alert() after every stock change; it's a no-op if the correct
alert state already exists, so callers never have to remember whether an
alert is "already open" before calling.

Alerts are per VARIANT (e.g. "392, size M"), not per design - size M can be
low while XL is fine, and that's a real, distinct signal to act on.
"""
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from models import Variant, Alert, Product


def check_variant_alert(db: Session, variant: Variant) -> Alert | None:
    """Open a new alert if stock just dropped to/below threshold, or resolve
    an existing one if stock has since recovered above it. Returns the newly
    created Alert (if one was opened this call), else None."""
    open_alert = (
        db.query(Alert)
        .filter(Alert.variant_id == variant.id, Alert.resolved == False)  # noqa: E712
        .first()
    )

    if variant.last_counted_at is None:
        # Never counted: the 0 in current_stock means "unknown", not "empty".
        # No alert until someone has actually counted this size.
        return None

    is_low = variant.current_stock <= variant.reorder_threshold

    if is_low and not open_alert:
        alert_type = "OUT_OF_STOCK" if variant.current_stock <= 0 else "LOW_STOCK"
        alert = Alert(
            variant_id=variant.id,
            alert_type=alert_type,
            stock_at_alert=variant.current_stock,
        )
        db.add(alert)
        db.flush()
        return alert

    if is_low and open_alert:
        # Keep the live alert accurate when stock crosses from LOW to OUT (or
        # recovers within the low range and falls again).
        open_alert.alert_type = "OUT_OF_STOCK" if variant.current_stock <= 0 else "LOW_STOCK"
        open_alert.stock_at_alert = variant.current_stock
        db.flush()
        return None

    if not is_low and open_alert:
        open_alert.resolved = True
        open_alert.resolved_at = datetime.now(timezone.utc)
        db.flush()

    return None


def get_open_alerts(db: Session, limit: int | None = None, offset: int = 0, q: str | None = None):
    query = (
        db.query(Alert)
        .options(selectinload(Alert.variant).selectinload(Variant.product))
        .join(Variant).join(Product)
        .filter(Alert.resolved == False, Variant.is_deleted == False, Product.is_deleted == False)  # noqa: E712
    )
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(
            Product.sku.ilike(like), Product.name.ilike(like), Variant.size.ilike(like),
            Variant.variant_code.ilike(like), Alert.alert_type.ilike(like),
        ))
    query = query.order_by(Variant.current_stock.asc(), Alert.id.asc()).offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return query.all()
