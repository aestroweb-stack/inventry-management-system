"""
Every stock change in the system - order deduction, manual recount, restock,
AI photo update - goes through apply_stock_change(). This is the one place
that touches Variant.current_stock, which keeps the "ledger + running
balance stay in sync" guarantee in one spot instead of scattered across
every caller.

CONCURRENCY: two staff could process an order-upload and a manual recount
at the same time. A naive "read stock, subtract in Python, write stock back"
is a classic race condition - two concurrent deductions can both read
stock=5, both decide "5 >= 3, ok", and both write back 2, silently losing
one deduction (final stock should have been -1 short i.e. an error, not 2).

Fix: do the subtraction IN the UPDATE statement, guarded by a WHERE clause,
and let the database's row-level locking make it atomic:

    UPDATE variants SET current_stock = current_stock - :qty
    WHERE id = :id AND current_stock >= :qty

If 0 rows are affected, there wasn't enough stock at the moment the database
actually applied it (not when Python first checked) - so we know for certain,
not just "probably".
"""
from datetime import datetime, timezone
from sqlalchemy import update
from sqlalchemy.orm import Session

from models import Variant, InventoryTransaction, TransactionType


class InsufficientStockError(Exception):
    def __init__(self, variant_code: str, available: int, requested: int):
        self.variant_code = variant_code
        self.available = available
        self.requested = requested
        super().__init__(f"{variant_code}: requested {requested}, only {available} available")


class StaleStockError(Exception):
    def __init__(self, variant_code: str, current: int):
        self.variant_code = variant_code
        self.current = current
        super().__init__(f"{variant_code}: stock changed; current count is {current}")


def apply_stock_change(
    db: Session,
    variant: Variant,
    delta: int,
    transaction_type: TransactionType,
    reference: str = None,
    created_by: str = "system",
    allow_negative: bool = False,
    expected_current_stock: int = None,
) -> Variant:
    """
    Apply `delta` to variant.current_stock atomically (delta can be + or -).
    Raises InsufficientStockError if delta is negative and would take stock
    below zero (unless allow_negative=True, used by manual recounts which
    set an absolute value rather than a signed change).
    Logs an InventoryTransaction row in the same DB transaction.
    """
    conditions = [Variant.id == variant.id]
    if expected_current_stock is not None:
        conditions.append(Variant.current_stock == expected_current_stock)
    if delta < 0 and not allow_negative:
        conditions.append(Variant.current_stock >= -delta)
        result = db.execute(
            update(Variant)
            .where(*conditions)
            .values(current_stock=Variant.current_stock + delta)
        )
        if result.rowcount == 0:
            db.refresh(variant)
            if expected_current_stock is not None and variant.current_stock != expected_current_stock:
                raise StaleStockError(variant.variant_code, variant.current_stock)
            raise InsufficientStockError(variant.variant_code, variant.current_stock, -delta)
    else:
        result = db.execute(
            update(Variant)
            .where(*conditions)
            .values(current_stock=Variant.current_stock + delta)
        )
        if result.rowcount == 0:
            db.refresh(variant)
            raise StaleStockError(variant.variant_code, variant.current_stock)

    db.refresh(variant)

    txn = InventoryTransaction(
        variant_id=variant.id,
        change_qty=delta,
        transaction_type=transaction_type,
        reference=reference,
        balance_after=variant.current_stock,
        created_by=created_by,
    )
    db.add(txn)
    db.flush()
    return variant


def set_absolute_stock(
    db: Session,
    variant: Variant,
    new_count: int,
    created_by: str = "manual",
    note: str = "manual recount",
    expected_current_stock: int = None,
) -> Variant:
    """Manual physical-count update: set stock to an exact known value
    (not a +/- change). Used by the 'someone counted the rack' workflow.
    Also stamps last_counted_at - this is the moment a variant stops being
    "not counted" and starts taking part in alerts and reorder lists."""
    delta = new_count - variant.current_stock
    apply_stock_change(
        db, variant, delta,
        transaction_type=TransactionType.MANUAL_ADJUSTMENT,
        reference=note,
        created_by=created_by,
        allow_negative=True,  # an absolute recount is always valid, even if it's a big drop
        expected_current_stock=expected_current_stock,
    )
    variant.last_counted_at = datetime.now(timezone.utc)
    db.flush()
    return variant
