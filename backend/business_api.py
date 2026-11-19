"""MYRAA catalog costing, marketplace alias and inbound challan APIs.
All writes occur behind existing /api authentication and in DB transactions.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone, date
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from database import get_db
import models
from business_models import ProductCost, VariantCost, MarketplaceSkuAlias, PurchaseChallan, PurchaseChallanLine
from services import excel_service, inventory_service, alert_service
from services.table_reader import read_table, clean_code, to_int

router = APIRouter(prefix="/api", tags=["Finance and purchase challans"])
MAX_EXCEL_SIZE = 12 * 1024 * 1024


def cost_decimal(value, name="Unit cost"):
    if value is None or str(value).strip() == "":
        return None
    try:
        v = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(422, f"{name} must be a valid number")
    if not v.is_finite() or v < 0 or v > 99999999:
        raise HTTPException(422, f"{name} must be between 0 and 99999999")
    return v


def upsert_product_cost(db, product_id: int, cost):
    value = cost_decimal(cost)
    if value is None:
        return
    current = db.query(ProductCost).filter_by(product_id=product_id).first()
    if current:
        current.unit_cost = value
    else:
        db.add(ProductCost(product_id=product_id, unit_cost=value))


def upsert_variant_cost(db, variant_id: int, cost):
    value = cost_decimal(cost)
    if value is None:
        return
    current = db.query(VariantCost).filter_by(variant_id=variant_id).first()
    if current:
        current.unit_cost = value
    else:
        db.add(VariantCost(variant_id=variant_id, unit_cost=value))


def normalize_alias(s: str) -> str:
    value = str(s or "").strip().upper()
    if not value or len(value) > 180 or any(ord(ch) < 32 for ch in value):
        raise HTTPException(422, "Marketplace SKU must be 1–180 printable characters")
    return value


def add_alias(db, variant: models.Variant, marketplace: str, alias: str):
    normalized = normalize_alias(alias)
    if normalized in {variant.variant_code.upper(), variant.product.sku.upper()}:
        return None  # the master code is already matched by the catalog
    existing = db.query(MarketplaceSkuAlias).filter_by(normalized_code=normalized).first()
    if existing:
        if existing.variant_id != variant.id:
            raise HTTPException(409, f"Marketplace SKU '{normalized}' already belongs to another size/product")
        existing.marketplace = (marketplace or "OTHER").strip().upper()[:80]
        return existing
    # Cannot create a silent ambiguous alias against any existing canonical SKU.
    direct = db.query(models.Variant).filter(
        or_(models.Variant.variant_code == normalized,
            models.Variant.seller_sku_code == normalized,
            models.Variant.marketplace_sku_code == normalized),
        models.Variant.id != variant.id,
        models.Variant.is_deleted.is_(False),
    ).first()
    if direct:
        raise HTTPException(409, f"SKU '{normalized}' already identifies another variant")
    direct_product = db.query(models.Product).filter(models.Product.sku == normalized, models.Product.id != variant.product_id).first()
    if direct_product:
        raise HTTPException(409, f"SKU '{normalized}' is already a different master SKU")
    item = MarketplaceSkuAlias(variant_id=variant.id, marketplace=(marketplace or "OTHER").strip().upper()[:80], alias_code=alias.strip(), normalized_code=normalized)
    db.add(item)
    return item


class CostsAndAliases(BaseModel):
    unit_cost: Optional[Decimal] = Field(default=None, ge=0, le=99999999)
    size_costs: dict[str, Optional[Decimal]] = Field(default_factory=dict)


class AliasRequest(BaseModel):
    size: str
    marketplace: str = "OTHER"
    sku: str


@router.put("/products/{sku}/costing")
def save_product_costing(sku: str, payload: CostsAndAliases, db: Session = Depends(get_db)):
    product = db.query(models.Product).filter(models.Product.sku == sku.strip().upper(), models.Product.is_deleted.is_(False)).first()
    if not product:
        raise HTTPException(404, "Product not found")
    sizes = {v.size.upper(): v for v in product.variants if not v.is_deleted}
    for size in payload.size_costs:
        if size.strip().upper() not in sizes:
            raise HTTPException(422, f"Size {size} does not belong to {product.sku}")
    upsert_product_cost(db, product.id, payload.unit_cost)
    for size, value in payload.size_costs.items():
        if value is not None:
            upsert_variant_cost(db, sizes[size.strip().upper()].id, value)
    db.commit()
    return {"status": "saved", "sku": product.sku}


@router.post("/products/{sku}/marketplace-aliases")
def save_product_alias(sku: str, payload: AliasRequest, db: Session = Depends(get_db)):
    product = db.query(models.Product).filter(models.Product.sku == sku.strip().upper(), models.Product.is_deleted.is_(False)).first()
    if not product:
        raise HTTPException(404, "Product not found")
    variant = next((v for v in product.variants if not v.is_deleted and v.size.upper() == payload.size.strip().upper()), None)
    if not variant:
        raise HTTPException(422, "Choose a size belonging to this product")
    add_alias(db, variant, payload.marketplace, payload.sku)
    db.commit()
    excel_service.reconcile_unmapped_order_lines(db)
    return {"status": "saved", "sku": product.sku, "size": variant.size}


@router.delete("/products/{sku}/marketplace-aliases/{alias_id}")
def remove_product_alias(sku: str, alias_id: int, db: Session = Depends(get_db)):
    alias = db.query(MarketplaceSkuAlias).join(models.Variant).join(models.Product).filter(
        MarketplaceSkuAlias.id == alias_id, models.Product.sku == sku.strip().upper()
    ).first()
    if not alias:
        raise HTTPException(404, "SKU mapping not found")
    db.delete(alias)
    db.commit()
    return {"status": "deleted"}


@router.get("/costing/summary")
def costing_summary(db: Session = Depends(get_db)):
    products = db.query(models.Product).options(
        selectinload(models.Product.variants).selectinload(models.Variant.cost_record),
        joinedload(models.Product.cost_record),
    ).filter(models.Product.is_deleted.is_(False)).all()
    rows = []
    total_known = Decimal("0")
    unpriced = 0
    pieces_unpriced = 0
    for p in products:
        stock = 0
        amount = Decimal("0")
        for v in p.variants:
            if v.is_deleted or v.last_counted_at is None or v.current_stock <= 0:
                continue
            stock += v.current_stock
            cost = v.cost_record.unit_cost if v.cost_record else (p.cost_record.unit_cost if p.cost_record else None)
            if cost is None:
                unpriced += 1
                pieces_unpriced += v.current_stock
            else:
                amount += cost * v.current_stock
        total_known += amount
        if stock:
            rows.append({"sku": p.sku, "name": p.name, "stock": stock, "cost_value": float(amount)})
    rows.sort(key=lambda x: -x["cost_value"])
    return {"total_known_cost": float(total_known), "currency": "INR", "unpriced_sizes": unpriced, "unpriced_pieces": pieces_unpriced, "products": rows[:30]}


@router.get("/costing/designs")
def costing_designs(q: str = "", limit: int = Query(20, ge=1, le=100),
                    offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    """Every design, searchable and paginated; never truncate the catalog to a top-N chart."""
    query = db.query(models.Product).options(
        joinedload(models.Product.cost_record),
        selectinload(models.Product.variants).selectinload(models.Variant.cost_record),
    ).filter(models.Product.is_deleted.is_(False))
    if q.strip():
        query = query.filter((models.Product.sku.ilike(f"%{q.strip()}%")) |
                             (models.Product.name.ilike(f"%{q.strip()}%")))
    total = query.count()
    products = query.order_by(models.Product.sku.asc()).offset(offset).limit(limit).all()
    items = []
    for p in products:
        stock = 0
        amount = Decimal("0")
        missing = 0
        for v in p.variants:
            if v.is_deleted or v.last_counted_at is None or v.current_stock <= 0:
                continue
            stock += v.current_stock
            cost = v.cost_record.unit_cost if v.cost_record else (p.cost_record.unit_cost if p.cost_record else None)
            if cost is None:
                missing += v.current_stock
            else:
                amount += cost * v.current_stock
        items.append({"sku": p.sku, "name": p.name, "stock": stock,
                      "cost_value": float(amount), "unpriced_pieces": missing})
    return {"total": total, "offset": offset, "limit": limit, "items": items}


SKU_FIELDS = ("sku", "master sku", "master_sku", "style", "style code", "style id", "design", "design code", "product sku")
NAME_FIELDS = ("product name", "name", "design name", "product")
SIZE_FIELDS = ("size", "variant size", "size name")
QTY_FIELDS = ("qty", "quantity", "order qty", "ordered qty", "pieces", "order quantity", "total qty")
COST_FIELDS = ("unit cost", "cost", "rate", "cost price", "purchase rate", "purchase price", "price")
CATEGORY_FIELDS = ("category", "product category")


def pick(row: dict, keys: tuple):
    # Normalize column header punctuation and whitespace, but NOT cell SKU values.
    fields = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in row.items()}
    for k in keys:
        v = fields.get(re.sub(r"[^a-z0-9]", "", k.lower()))
        if v is not None and str(v).strip().lower() not in ("", "nan", "none"):
            return str(v).strip()
    return None


def parse_challan_file(blob: bytes, filename: str):
    if not blob or len(blob) > MAX_EXCEL_SIZE:
        raise HTTPException(422, "Excel file must be between 1 byte and 12 MB")
    try:
        df = read_table(blob, filename)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    if df.empty or len(df) > 10000:
        raise HTTPException(422, "Challan must have 1–10,000 rows")
    merged = defaultdict(lambda: {"quantity": 0})
    for idx, raw in enumerate(df.to_dict("records"), 2):
        sku = (pick(raw, SKU_FIELDS) or "").strip().upper()
        size = (pick(raw, SIZE_FIELDS) or "").strip().upper()
        if not size:
            match = re.search(r"(?:[_\-\s]+)(5XL|4XL|3XL|XXL|XL|XS|S|M|L|\d{2,3})$", sku)
            if match:
                size = match.group(1)
                sku = sku[:match.start()].rstrip("_- ")
        rawqty = pick(raw, QTY_FIELDS)
        if not sku and not rawqty:
            continue
        qty = to_int(rawqty)
        if not sku or not size or qty is None or qty <= 0:
            raise HTTPException(422, f"Row {idx}: SKU, size and positive qty are required. Keep any unknown size in the Size column.")
        if len(sku) > 64 or len(size) > 20:
            raise HTTPException(422, f"Row {idx}: SKU/size too long")
        cost = cost_decimal(pick(raw, COST_FIELDS), f"Row {idx} cost")
        key = (sku, size)
        name = (pick(raw, NAME_FIELDS) or sku).strip()[:300]
        category = (pick(raw, CATEGORY_FIELDS) or "").strip()[:100]
        existing = merged[key]
        if existing["quantity"] and (existing["unit_cost"] != cost or existing["name"] != name):
            raise HTTPException(422, f"Row {idx}: duplicate {sku}/{size} has different name or cost. Correct before importing.")
        existing.update({"sku": sku, "size": size, "name": name, "category": category, "unit_cost": cost})
        existing["quantity"] += qty
    if not merged:
        raise HTTPException(422, "No valid challan rows found")
    return list(merged.values())


def serialize_challan(batch: PurchaseChallan):
    lines = [{"id": x.id, "sku": x.sku, "name": x.name, "size": x.size,
              "qty_ordered": x.qty_ordered, "qty_received": x.qty_received,
              "pending": x.qty_ordered - x.qty_received,
              "unit_cost": float(x.unit_cost) if x.unit_cost is not None else None} for x in batch.lines]
    return {"id": batch.id, "party_name": batch.party_name, "challan_no": batch.challan_no,
            "invoice_no": batch.invoice_no, "order_date": batch.order_date,
            "status": batch.status, "created_at": batch.created_at,
            "posted_at": batch.posted_at, "filename": batch.source_filename,
            "ordered": sum(x["qty_ordered"] for x in lines),
            "received": sum(x["qty_received"] for x in lines),
            "pending": sum(x["pending"] for x in lines),
            "cost_total_known": float(sum((Decimal(str(x["unit_cost"])) * x["qty_ordered"] for x in lines if x["unit_cost"] is not None), Decimal("0"))),
            "lines": lines}


@router.get("/purchase-challans")
def list_purchase_challans(db: Session = Depends(get_db)):
    batches = db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).order_by(PurchaseChallan.id.desc()).limit(200).all()
    result = [serialize_challan(b) for b in batches]
    pending_cost_known = Decimal("0")
    pending_unpriced = 0
    for batch in result:
        if batch["status"] == "POSTED":
            continue
        for line in batch["lines"]:
            if line["unit_cost"] is None:
                pending_unpriced += line["pending"]
            else:
                pending_cost_known += Decimal(str(line["unit_cost"])) * line["pending"]
    return {"items": result,
            "pending_pieces": sum(b["pending"] for b in result if b["status"] != "POSTED"),
            "ordered_pieces": sum(b["ordered"] for b in result),
            "pending_cost_known": float(pending_cost_known),
            "pending_unpriced_pieces": pending_unpriced}


@router.get("/purchase-challans/{batch_id}")
def get_purchase_challan(batch_id: int, db: Session = Depends(get_db)):
    b = db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=batch_id).first()
    if not b:
        raise HTTPException(404, "Challan not found")
    return serialize_challan(b)


@router.post("/purchase-challans/import")
async def import_challan(file: UploadFile = File(...), party_name: str = Form(...), challan_no: str = Form(...), invoice_no: str = Form(""), order_date: str = Form(""), db: Session = Depends(get_db)):
    party, number = party_name.strip()[:180], challan_no.strip()[:120]
    if not party or not number:
        raise HTTPException(422, "Party and challan number required")
    d = order_date.strip() or date.today().isoformat()
    try:
        date.fromisoformat(d)
    except ValueError:
        raise HTTPException(422, "Date must be YYYY-MM-DD")
    data = await file.read(MAX_EXCEL_SIZE + 1)
    rows = parse_challan_file(data, file.filename or "")
    filehash = hashlib.sha256(data + party.upper().encode() + number.upper().encode()).hexdigest()
    if db.query(PurchaseChallan).filter_by(file_hash=filehash).first():
        raise HTTPException(409, "This challan file was imported earlier")
    if db.query(PurchaseChallan).filter(func.upper(PurchaseChallan.party_name) == party.upper(), func.upper(PurchaseChallan.challan_no) == number.upper()).first():
        raise HTTPException(409, "Party + challan number already exists")
    batch = PurchaseChallan(party_name=party, challan_no=number, invoice_no=invoice_no.strip()[:120], order_date=d, source_filename=(file.filename or "upload.xlsx")[:255], file_hash=filehash)
    db.add(batch)
    db.flush()
    for x in rows:
        db.add(PurchaseChallanLine(challan_id=batch.id, sku=x["sku"], name=x["name"], size=x["size"], category=x["category"], qty_ordered=x["quantity"], qty_received=0, unit_cost=x["unit_cost"]))
    db.commit()
    return serialize_challan(db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=batch.id).one())


class ReceiptLine(BaseModel):
    line_id: int
    received: int = Field(ge=0)
    unit_cost: Optional[Decimal] = Field(default=None, ge=0, le=99999999)


class ReceiptUpdate(BaseModel):
    lines: list[ReceiptLine] = Field(min_length=1)


@router.patch("/purchase-challans/{batch_id}/receipt")
def update_receipt(batch_id: int, payload: ReceiptUpdate, db: Session = Depends(get_db)):
    batch = db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=batch_id).first()
    if not batch:
        raise HTTPException(404, "Challan not found")
    if batch.status == "POSTED":
        raise HTTPException(409, "Already pushed to inventory; receipt is locked")
    line_index = {x.id: x for x in batch.lines}
    if len({x.line_id for x in payload.lines}) != len(payload.lines):
        raise HTTPException(422, "Duplicate receipt line")
    for item in payload.lines:
        line = line_index.get(item.line_id)
        if not line or item.received > line.qty_ordered:
            raise HTTPException(422, "Invalid line or received quantity exceeds ordered quantity")
    for item in payload.lines:
        line = line_index[item.line_id]
        line.qty_received = item.received
        if item.unit_cost is not None:
            line.unit_cost = cost_decimal(item.unit_cost)
    batch.status = "READY" if all(x.qty_received == x.qty_ordered for x in batch.lines) else "PARTIAL" if any(x.qty_received for x in batch.lines) else "OPEN"
    db.commit()
    return serialize_challan(batch)


@router.post("/purchase-challans/{batch_id}/push")
def push_challan_to_inventory(batch_id: int, db: Session = Depends(get_db)):
    batch = db.query(PurchaseChallan).filter_by(id=batch_id).with_for_update().first()
    if not batch:
        raise HTTPException(404, "Challan not found")
    if batch.status == "POSTED":
        raise HTTPException(409, "This challan has already been pushed; inventory not changed twice")
    lines = db.query(PurchaseChallanLine).filter_by(challan_id=batch_id).order_by(PurchaseChallanLine.id).all()
    if not lines or any(x.qty_received != x.qty_ordered for x in lines):
        raise HTTPException(409, "All sizes must be fully received before pushing the challan. Pending quantities remain visible.")
    if any(x.unit_cost is None for x in lines):
        raise HTTPException(409, "Fill Unit Cost for every challan line before pushing, to keep inventory costing accurate.")
    try:
        for item in lines:
            product = db.query(models.Product).filter_by(sku=item.sku).first()
            if product and product.is_deleted:
                raise HTTPException(409, f"{item.sku} is in Trash: restore manually first")
            if not product:
                product = models.Product(sku=item.sku, name=item.name, category=item.category or None)
                db.add(product)
                db.flush()
            variant = db.query(models.Variant).filter_by(product_id=product.id, size=item.size).first()
            if variant and variant.is_deleted:
                raise HTTPException(409, f"{item.sku} size {item.size} is in Trash")
            if not variant:
                code = f"{item.sku}_{item.size}"
                if db.query(models.Variant).filter_by(variant_code=code).first():
                    raise HTTPException(409, f"Variant code {code} already exists")
                variant = models.Variant(product_id=product.id, size=item.size, variant_code=code, current_stock=0, last_counted_at=datetime.now(timezone.utc))
                db.add(variant)
                db.flush()
            # Moving-average landed unit cost: preserve valuation of existing
            # pieces instead of replacing every old piece with latest price.
            old_qty = max(int(variant.current_stock), 0) if variant.last_counted_at is not None else 0
            old_cost = (variant.cost_record.unit_cost if variant.cost_record else
                        product.cost_record.unit_cost if product.cost_record else None)
            if old_qty and old_cost is None:
                raise HTTPException(409, f"Set costing for existing {item.sku}/{item.size} ({old_qty} pieces) before receiving more stock")
            old_value = Decimal(old_qty) * old_cost if old_cost is not None else Decimal("0")
            new_avg_cost = ((old_value + Decimal(item.qty_received) * item.unit_cost) /
                            Decimal(old_qty + item.qty_received)).quantize(Decimal("0.01"))
            inventory_service.apply_stock_change(db, variant, item.qty_received, transaction_type=models.TransactionType.RESTOCK, reference=f"purchase_challan:{batch.id}:{batch.challan_no}", created_by="challan_receipt")
            variant.last_counted_at = datetime.now(timezone.utc)
            upsert_variant_cost(db, variant.id, new_avg_cost)
            alert_service.check_variant_alert(db, variant)
        batch.status = "POSTED"
        batch.posted_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        db.rollback()
        raise
    excel_service.reconcile_unmapped_order_lines(db)
    return {"status": "POSTED", "id": batch.id, "received_pieces": sum(x.qty_received for x in lines)}
