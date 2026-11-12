"""
Database schema.

Design note on Product vs. Variant:
Stock is tracked SIZE-WISE, not just per design. "SKU 392" alone isn't a
stockable unit - "392, size M" is. So the schema splits:
  Product  = the design (style code, name, image - shared across sizes)
  Variant  = one stockable row per (product, size) - this is what actually
             carries current_stock, gets scanned via its own variant_code,
             and is what every transaction/alert/order line references.
A "SKU" the business talks about (e.g. "392") is a Product; a "unit of
stock" is always a Variant.

Design note on "current_stock":
Variant.current_stock is a fast-read cache (denormalized), always updated
INSIDE the same DB transaction as an InventoryTransaction row. The
InventoryTransaction table is the append-only source of truth / audit ledger
- if current_stock is ever doubted, it can be rebuilt by summing transactions
for that variant. This is the standard ledger + running-balance pattern used
by real inventory and accounting systems.
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Enum, Text, UniqueConstraint
)
from sqlalchemy.orm import relationship

from database import Base


def utcnow():
    return datetime.now(timezone.utc)


class TransactionType(str, enum.Enum):
    INITIAL_STOCK = "INITIAL_STOCK"
    ORDER_DEDUCTION = "ORDER_DEDUCTION"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"
    RESTOCK = "RESTOCK"
    AI_PHOTO_UPDATE = "AI_PHOTO_UPDATE"


class OrderLineStatus(str, enum.Enum):
    PENDING = "PENDING"
    FULFILLED = "FULFILLED"
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    SKU_NOT_FOUND = "SKU_NOT_FOUND"


class Vendor(Base):
    __tablename__ = "vendors"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    contact = Column(String(200))  # phone / whatsapp / email
    products = relationship("Product", back_populates="vendor")


class Product(Base):
    """The design / style. Not stockable by itself - see Variant."""
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    sku = Column(String(64), unique=True, nullable=False, index=True)   # style code, e.g. "392"
    name = Column(String(300), nullable=False)
    category = Column(String(100))         # kurta set, salwar suit, etc.
    image_path = Column(String(500))       # relative path under /static/product_images

    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    vendor = relationship("Vendor", back_populates="products")

    is_deleted = Column(Boolean, nullable=False, default=False, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    variants = relationship("Variant", back_populates="product", cascade="all, delete-orphan")
    reference_images = relationship("ProductImage", back_populates="product", cascade="all, delete-orphan")


class ProductImage(Base):
    """A genuine reference photo belonging to one catalog design."""
    __tablename__ = "product_images"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    image_path = Column(String(500), nullable=False, unique=True)
    is_primary = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    product = relationship("Product", back_populates="reference_images")


class Variant(Base):
    """One stockable row per (product, size). This is what actually holds
    current_stock, gets a scannable code, and is what every transaction,
    alert, order line and reorder line references."""
    __tablename__ = "variants"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    size = Column(String(20), nullable=False)                 # "S", "M", "L", "XL", "XXL", or free text
    variant_code = Column(String(80), unique=True, nullable=False, index=True)  # e.g. "392-M" - what gets scanned

    current_stock = Column(Integer, nullable=False, default=0)
    reorder_threshold = Column(Integer, nullable=False, default=5)   # alert fires at/below this
    target_stock_level = Column(Integer, nullable=False, default=20)  # reorder tops up to this

    # NULL = this size's stock has never been physically counted/entered
    # (e.g. right after a catalog import, which carries no stock numbers).
    # "Not counted" is NOT "out of stock": such a variant raises no alerts and
    # never appears on a reorder list, so an unknown 0 can't be mistaken for a
    # real 0 and trigger a vendor order for the entire catalog.
    last_counted_at = Column(DateTime(timezone=True), nullable=True)

    # The marketplace's own codes for this exact size, stored UPPERCASE so an
    # uploaded order sheet can be matched on whichever code it carries.
    seller_sku_code = Column(String(160), index=True, nullable=True)
    marketplace_sku_code = Column(String(80), index=True, nullable=True)

    is_deleted = Column(Boolean, nullable=False, default=False, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    product = relationship("Product", back_populates="variants")
    transactions = relationship("InventoryTransaction", back_populates="variant")

    __table_args__ = (UniqueConstraint("product_id", "size", name="uq_product_size"),)


class InventoryTransaction(Base):
    """Append-only ledger. Never updated or deleted - only inserted."""
    __tablename__ = "inventory_transactions"
    id = Column(Integer, primary_key=True)
    variant_id = Column(Integer, ForeignKey("variants.id"), nullable=False, index=True)
    change_qty = Column(Integer, nullable=False)   # negative = stock out, positive = stock in
    transaction_type = Column(Enum(TransactionType), nullable=False)
    reference = Column(String(300))   # e.g. "order_upload:14" or a manual note
    balance_after = Column(Integer, nullable=False)
    created_by = Column(String(100), default="system")
    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)

    variant = relationship("Variant", back_populates="transactions")


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    variant_id = Column(Integer, ForeignKey("variants.id"), nullable=False, index=True)
    alert_type = Column(String(30), nullable=False)   # LOW_STOCK / OUT_OF_STOCK
    stock_at_alert = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    resolved = Column(Boolean, default=False, index=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    variant = relationship("Variant")


class OrderUpload(Base):
    """One row per Excel file uploaded (a batch of orders)."""
    __tablename__ = "order_uploads"
    id = Column(Integer, primary_key=True)
    filename = Column(String(300))
    file_hash = Column(String(64), index=True)   # sha256 - used to block accidental double-upload
    uploaded_at = Column(DateTime(timezone=True), default=utcnow)
    total_rows = Column(Integer, default=0)
    unique_skus = Column(Integer, default=0)
    fulfilled_count = Column(Integer, default=0)
    insufficient_count = Column(Integer, default=0)
    not_found_count = Column(Integer, default=0)

    lines = relationship("OrderLine", back_populates="order_upload")

    __table_args__ = (UniqueConstraint("file_hash", name="uq_order_upload_hash"),)


class OrderLine(Base):
    __tablename__ = "order_lines"
    id = Column(Integer, primary_key=True)
    order_upload_id = Column(Integer, ForeignKey("order_uploads.id"), nullable=False)
    sku = Column(String(64), nullable=False)      # raw SKU text as it appeared in the file
    size = Column(String(20), nullable=True)      # raw size text as it appeared in the file
    variant_id = Column(Integer, ForeignKey("variants.id"), nullable=True)
    qty_ordered = Column(Integer, nullable=False)
    qty_fulfilled = Column(Integer, nullable=False, default=0)
    status = Column(Enum(OrderLineStatus), nullable=False)

    order_upload = relationship("OrderUpload", back_populates="lines")
    variant = relationship("Variant")


class ReorderBatch(Base):
    """A snapshot of a reorder list at the moment it was generated/sent to parties."""
    __tablename__ = "reorder_batches"
    id = Column(Integer, primary_key=True)
    generated_at = Column(DateTime(timezone=True), default=utcnow)
    note = Column(Text)

    items = relationship("ReorderBatchItem", back_populates="batch")


class ReorderBatchItem(Base):
    __tablename__ = "reorder_batch_items"
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("reorder_batches.id"), nullable=False)
    variant_id = Column(Integer, ForeignKey("variants.id"), nullable=False)
    stock_at_generation = Column(Integer, nullable=False)
    required_qty = Column(Integer, nullable=False)

    batch = relationship("ReorderBatch", back_populates="items")
    variant = relationship("Variant")
