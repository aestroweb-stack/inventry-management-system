"""Additive MYRAA finance, marketplace alias, and purchase-challan tables.
No ALTER TABLE or destructive migration is required for existing catalog records.
"""
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Numeric, ForeignKey, DateTime, UniqueConstraint, Text
from sqlalchemy.orm import relationship
from database import Base


def utcnow():
    return datetime.now(timezone.utc)


class ProductCost(Base):
    __tablename__ = "myraa_product_costs"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), unique=True, nullable=False, index=True)
    unit_cost = Column(Numeric(12, 2), nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    product = relationship("Product", back_populates="cost_record")


class VariantCost(Base):
    __tablename__ = "myraa_variant_costs"
    id = Column(Integer, primary_key=True)
    variant_id = Column(Integer, ForeignKey("variants.id"), unique=True, nullable=False, index=True)
    unit_cost = Column(Numeric(12, 2), nullable=False)
    variant = relationship("Variant", back_populates="cost_record")


class MarketplaceSkuAlias(Base):
    __tablename__ = "myraa_marketplace_sku_aliases"
    id = Column(Integer, primary_key=True)
    variant_id = Column(Integer, ForeignKey("variants.id"), nullable=False, index=True)
    marketplace = Column(String(80), nullable=False, default="OTHER")
    alias_code = Column(String(180), nullable=False)
    normalized_code = Column(String(180), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    variant = relationship("Variant", back_populates="marketplace_aliases")


class PurchaseChallan(Base):
    __tablename__ = "myraa_purchase_challans"
    id = Column(Integer, primary_key=True)
    party_name = Column(String(180), nullable=False)
    challan_no = Column(String(120), nullable=False)
    invoice_no = Column(String(120), nullable=True)
    order_date = Column(String(12), nullable=False)
    source_filename = Column(String(255), nullable=False)
    file_hash = Column(String(64), unique=True, nullable=False)
    status = Column(String(20), nullable=False, default="OPEN")
    created_at = Column(DateTime(timezone=True), default=utcnow)
    posted_at = Column(DateTime(timezone=True), nullable=True)
    lines = relationship("PurchaseChallanLine", back_populates="challan", cascade="all, delete-orphan", order_by="PurchaseChallanLine.id")
    attachments = relationship("PurchaseChallanAttachment", back_populates="challan", cascade="all, delete-orphan", order_by="PurchaseChallanAttachment.id")


class PurchaseChallanLine(Base):
    __tablename__ = "myraa_purchase_challan_lines"
    id = Column(Integer, primary_key=True)
    challan_id = Column(Integer, ForeignKey("myraa_purchase_challans.id"), nullable=False, index=True)
    sku = Column(String(64), nullable=False)
    name = Column(String(300), nullable=False)
    category = Column(String(100), nullable=True)
    size = Column(String(20), nullable=False)
    qty_ordered = Column(Integer, nullable=False)
    qty_received = Column(Integer, nullable=False, default=0)
    unit_cost = Column(Numeric(12, 2), nullable=True, default=None)
    challan = relationship("PurchaseChallan", back_populates="lines")
    __table_args__ = (UniqueConstraint("challan_id", "sku", "size", name="uq_challan_sku_size"),)


class ProductMarketplaceAlias(Base):
    """Whole-product external SKU: requires explicit/inferable Size to match an order.

    Dedicated additive table avoids an ALTER on the existing size-scoped mappings.
    """
    __tablename__ = "myraa_product_marketplace_aliases"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    marketplace = Column(String(80), nullable=False, default="OTHER")
    alias_code = Column(String(180), nullable=False)
    normalized_code = Column(String(180), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    product = relationship("Product", back_populates="product_aliases")


class PurchaseChallanAttachment(Base):
    """Private scanned invoice/challan image, shared by all items in one bill."""
    __tablename__ = "myraa_challan_attachments"
    id = Column(Integer, primary_key=True)
    challan_id = Column(Integer, ForeignKey("myraa_purchase_challans.id"), nullable=False, index=True)
    original_filename = Column(String(255), nullable=False)
    storage_filename = Column(String(120), unique=True, nullable=False)
    content_type = Column(String(80), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    challan = relationship("PurchaseChallan", back_populates="attachments")
