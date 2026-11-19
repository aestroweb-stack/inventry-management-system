"""MYRAA catalog costing, marketplace alias and inbound challan APIs.
All writes occur behind existing /api authentication and in DB transactions.
"""
from __future__ import annotations

import hashlib
import re
import os
import io
import uuid
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape as xml_escape
from PIL import Image, UnidentifiedImageError
from collections import defaultdict
from datetime import datetime, timezone, date
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from database import get_db
import models
from business_models import ProductCost, VariantCost, MarketplaceSkuAlias, ProductMarketplaceAlias, PurchaseChallan, PurchaseChallanLine, PurchaseChallanAttachment
from services import excel_service, inventory_service, alert_service
from services.table_reader import read_table, clean_code, to_int

router = APIRouter(prefix="/api", tags=["Finance and purchase challans"])
MAX_EXCEL_SIZE = 12 * 1024 * 1024
MAX_INVOICE_BYTES = 10 * 1024 * 1024
MAX_CHALLAN_ATTACHMENTS = 12
# PRIVATE directory: never mount it with FastAPI StaticFiles.
CHALLAN_IMAGE_DIR = Path(os.getenv("PURCHASE_CHALLAN_FILE_DIR") or str(Path(__file__).resolve().parent / "private_challan_files")).resolve()



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
    product_mapping = db.query(ProductMarketplaceAlias).filter_by(normalized_code=normalized).first()
    if product_mapping:
        raise HTTPException(409, "SKU already used as a whole-product mapping; choose another SKU")
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


def add_product_alias(db, product: models.Product, marketplace: str, alias: str):
    """Whole-product SKU. Orders MUST supply an explicit or inferred size."""
    normalized = normalize_alias(alias)
    existing = db.query(ProductMarketplaceAlias).filter_by(normalized_code=normalized).first()
    if existing:
        if existing.product_id != product.id:
            raise HTTPException(409, "Marketplace SKU already belongs to another product")
        existing.marketplace = (marketplace or "OTHER").strip().upper()[:80]
        return existing
    if db.query(MarketplaceSkuAlias).filter_by(normalized_code=normalized).first():
        raise HTTPException(409, "SKU already mapped to a specific size")
    if db.query(models.Variant).filter(
        or_(models.Variant.variant_code == normalized,
            models.Variant.seller_sku_code == normalized,
            models.Variant.marketplace_sku_code == normalized),
    ).first():
        raise HTTPException(409, "SKU already assigned to a stockable size")
    direct = db.query(models.Product).filter(models.Product.sku == normalized).first()
    if direct:
        if direct.id == product.id:
            return None  # canonical Master SKU already matched by product + Size
        raise HTTPException(409, "SKU is another product's Master SKU")
    item = ProductMarketplaceAlias(product_id=product.id,
        marketplace=(marketplace or "OTHER").strip().upper()[:80],
        alias_code=alias.strip(), normalized_code=normalized)
    db.add(item)
    return item


class CostsAndAliases(BaseModel):
    unit_cost: Optional[Decimal] = Field(default=None, ge=0, le=99999999)
    size_costs: dict[str, Optional[Decimal]] = Field(default_factory=dict)


class AliasRequest(BaseModel):
    size: Optional[str] = None
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
    if payload.size:
        variant = next((v for v in product.variants if not v.is_deleted and v.size.upper() == payload.size.strip().upper()), None)
        if not variant:
            raise HTTPException(422, "Choose a size belonging to this product")
        add_alias(db, variant, payload.marketplace, payload.sku)
        resolved_size = variant.size
    else:
        add_product_alias(db, product, payload.marketplace, payload.sku)
        resolved_size = None
    db.commit()
    excel_service.reconcile_unmapped_order_lines(db)
    return {"status": "saved", "sku": product.sku, "size": resolved_size, "scope": "size" if resolved_size else "product"}


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


@router.delete("/products/{sku}/product-marketplace-aliases/{alias_id}")
def remove_whole_product_alias(sku: str, alias_id: int, db: Session = Depends(get_db)):
    alias = db.query(ProductMarketplaceAlias).join(models.Product).filter(
        ProductMarketplaceAlias.id == alias_id,
        models.Product.sku == sku.strip().upper(),
    ).first()
    if not alias:
        raise HTTPException(404, "Product-wide SKU mapping not found")
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


def parse_challan_file(blob: bytes, filename: str, db: Session | None = None):
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
        name = (pick(raw, NAME_FIELDS) or "").strip()
        size = (pick(raw, SIZE_FIELDS) or "").strip().upper()
        # Simple 3-column supplier sheet: Product Name, Size, Qty.
        if not sku and name:
            if db is not None:
                matches = db.query(models.Product).filter(
                    func.lower(models.Product.name) == name.lower(),
                    models.Product.is_deleted.is_(False),
                ).limit(3).all()
                if len(matches) > 1:
                    raise HTTPException(422, f"Row {idx}: product name '{name}' matches multiple Master SKUs; use SKU column to disambiguate")
                if matches:
                    sku = matches[0].sku
            if not sku:
                sku = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")[:64]
                if not sku:
                    raise HTTPException(422, f"Row {idx}: Product Name must contain letters or digits")
                if db is not None:
                    conflict = db.query(models.Product).filter_by(sku=sku).first()
                    if conflict and conflict.name.lower() != name.lower():
                        raise HTTPException(422, f"Row {idx}: generated SKU collision; supply Master SKU column")
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
        name = (name or sku).strip()[:300]
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
            "attachments": [{"id": a.id, "filename": a.original_filename,
                             "content_type": a.content_type, "size_bytes": a.size_bytes,
                             "url": f"/api/purchase-challans/{batch.id}/attachments/{a.id}"}
                            for a in batch.attachments],
            "received_unpriced_pieces": sum(x["qty_received"] for x in lines if x["unit_cost"] is None),
            "received_value_known": float(sum((Decimal(str(x["unit_cost"])) * x["qty_received"] for x in lines if x["unit_cost"] is not None), Decimal("0"))),
            "pending_value_known": float(sum((Decimal(str(x["unit_cost"])) * x["pending"] for x in lines if x["unit_cost"] is not None), Decimal("0"))),
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
            "received_pieces": sum(b["received"] for b in result),
            "received_value_known": sum(b["received_value_known"] for b in result),
            "received_unpriced_pieces": sum(b["received_unpriced_pieces"] for b in result),
            "pending_pieces": sum(b["pending"] for b in result if b["status"] != "POSTED"),
            "ordered_pieces": sum(b["ordered"] for b in result),
            "pending_cost_known": float(pending_cost_known),
            "pending_unpriced_pieces": pending_unpriced}


@router.get("/purchase-challans/template")
def download_challan_template():
    rows = [["Product Name", "Size", "Qty"], ["URVISHA RED", "S", 20], ["URVISHA RED", "M", 25]]
    return StreamingResponse(io.BytesIO(_simple_xlsx(rows)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="MYRAA_Challan_3_Column_Template.xlsx"'})



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
    rows = parse_challan_file(data, file.filename or "", db=db)
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



def _invoice_file_path(storage_filename: str) -> Path:
    # Names are server-generated UUIDs. Reject unexpected path components anyway.
    if Path(storage_filename).name != storage_filename:
        raise HTTPException(500, "Unsafe stored invoice filename")
    return CHALLAN_IMAGE_DIR / storage_filename


@router.post("/purchase-challans/{batch_id}/attachments")
async def upload_challan_attachments(batch_id: int, files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    batch = db.query(PurchaseChallan).filter_by(id=batch_id).first()
    if not batch:
        raise HTTPException(404, "Challan not found")
    if not files or len(files) + db.query(PurchaseChallanAttachment).filter_by(challan_id=batch_id).count() > MAX_CHALLAN_ATTACHMENTS:
        raise HTTPException(422, f"Maximum {MAX_CHALLAN_ATTACHMENTS} images/documents per challan")
    CHALLAN_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    stored = []
    try:
        for upload in files:
            content = await upload.read(MAX_INVOICE_BYTES + 1)
            if not content or len(content) > MAX_INVOICE_BYTES:
                raise HTTPException(422, "Each invoice attachment must be 1 byte–10 MB")
            kind = None
            if content.startswith(b"%PDF-"):
                kind = ("pdf", "application/pdf")
            else:
                try:
                    with Image.open(io.BytesIO(content)) as im:
                        im.verify()
                        typ = im.format
                    kind = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"),
                            "WEBP": ("webp", "image/webp")}.get(typ)
                except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError):
                    pass
            if not kind:
                raise HTTPException(422, "Only valid JPG, PNG, WEBP or PDF challan scans are allowed")
            storage = f"{uuid.uuid4().hex}.{kind[0]}"
            target = _invoice_file_path(storage)
            with open(target, "xb") as fh:
                fh.write(content)
            stored.append(target)
            db.add(PurchaseChallanAttachment(
                challan_id=batch_id, original_filename=Path((upload.filename or "invoice").replace("\\", "/")).name[:255],
                storage_filename=storage, content_type=kind[1], size_bytes=len(content)))
        db.commit()
    except Exception:
        db.rollback()
        for target in stored:
            target.unlink(missing_ok=True)
        raise
    return serialize_challan(db.query(PurchaseChallan).options(
        selectinload(PurchaseChallan.lines), selectinload(PurchaseChallan.attachments)).filter_by(id=batch_id).one())


@router.get("/purchase-challans/{batch_id}/attachments/{attachment_id}")
def download_challan_attachment(batch_id: int, attachment_id: int, db: Session = Depends(get_db)):
    record = db.query(PurchaseChallanAttachment).filter_by(id=attachment_id, challan_id=batch_id).first()
    if not record:
        raise HTTPException(404, "Challan attachment not found")
    path = _invoice_file_path(record.storage_filename)
    if not path.is_file():
        raise HTTPException(404, "Saved scan unavailable: check persistent Railway file volume")
    return FileResponse(path, media_type=record.content_type, filename=record.original_filename,
                        content_disposition_type="inline" if record.content_type.startswith("image/") else "attachment")


@router.delete("/purchase-challans/{batch_id}/attachments/{attachment_id}")
def delete_challan_attachment(batch_id: int, attachment_id: int, db: Session = Depends(get_db)):
    """Remove a mistaken pre-posting document; retain posted bill evidence."""
    record = db.query(PurchaseChallanAttachment).filter_by(id=attachment_id, challan_id=batch_id).first()
    if not record:
        raise HTTPException(404, "Challan attachment not found")
    if record.challan.status == "POSTED":
        raise HTTPException(409, "Posted challan documents are retained for audit")
    path = _invoice_file_path(record.storage_filename)
    db.delete(record)
    db.commit()
    path.unlink(missing_ok=True)
    return {"status": "deleted", "id": attachment_id}


def _simple_xlsx(rows: list[list]):
    """Small standards-compliant XLSX without requiring new Railway dependencies."""
    def col(i):
        result = ""
        while i:
            i, r = divmod(i - 1, 26)
            result = chr(65 + r) + result
        return result
    cells = []
    for n, values in enumerate(rows, 1):
        contents = []
        for j, val in enumerate(values, 1):
            location = f"{col(j)}{n}"
            if isinstance(val, (float, int)) and not isinstance(val, bool):
                contents.append(f'<c r="{location}"><v>{val}</v></c>')
            else:
                txt = xml_escape(str(val if val is not None else ""), {'"':'&quot;'})
                contents.append(f'<c r="{location}" t="inlineStr"><is><t>{txt}</t></is></c>')
        cells.append(f'<row r="{n}">{"".join(contents)}</row>')
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
             '<sheetData>' + ''.join(cells) + '</sheetData></worksheet>')
    output = io.BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Challan Items" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', sheet)
    return output.getvalue()


@router.get("/purchase-challans/{batch_id}/export")
def export_challan(batch_id: int, db: Session = Depends(get_db)):
    batch = db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=batch_id).first()
    if not batch:
        raise HTTPException(404, "Challan not found")
    rows = [["Product Name", "Size", "Qty Ordered", "Qty Received", "Qty Pending", "Unit Cost INR", "Received Value INR", "Pending Value INR", "Master SKU"]]
    for line in batch.lines:
        cost = float(line.unit_cost) if line.unit_cost is not None else None
        rows.append([line.name, line.size, line.qty_ordered, line.qty_received,
                     line.qty_ordered - line.qty_received, cost if cost is not None else "",
                     round(cost * line.qty_received, 2) if cost is not None else "",
                     round(cost * (line.qty_ordered - line.qty_received), 2) if cost is not None else "", line.sku])
    filename = f"MYRAA_Challan_{batch.id}.xlsx"
    return StreamingResponse(io.BytesIO(_simple_xlsx(rows)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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
