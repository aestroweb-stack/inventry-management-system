"""Bulk stock-count Excel workflow.

The downloaded sheet contains every current product/size so the warehouse can
count the full catalog. Users may also add a new size row in the same sheet:
- existing SKU + new Size -> the size variant is created automatically;
- new SKU + Size + Product -> the product and size variant are created.

SKU and Size are always normalized to UPPERCASE. Blank Count cells are skipped.
"""
from datetime import datetime, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill
from sqlalchemy.orm import Session, joinedload

from models import Product, Variant, Alert, InventoryTransaction, TransactionType
from services.table_reader import read_table, to_int, clean_code
from services import inventory_service, alert_service

SIZE_ORDER = {"XS": 0, "S": 1, "M": 2, "L": 3, "XL": 4, "XXL": 5, "3XL": 6, "4XL": 7, "5XL": 8}

VARIANT_CODE_CANDIDATES = ["variant code", "variant_code", "code"]
SKU_COLUMN_CANDIDATES = ["sku", "style code", "stylecode", "product sku"]
SIZE_COLUMN_CANDIDATES = ["size", "size name", "variant size"]
PRODUCT_COLUMN_CANDIDATES = ["product", "product name", "name", "design", "design name"]
CATEGORY_COLUMN_CANDIDATES = ["category"]
COUNT_COLUMN_CANDIDATES = ["count", "counted", "actual count", "current stock", "stock", "qty"]


def size_rank(size: str) -> int:
    return SIZE_ORDER.get(str(size).upper(), 99)


def _is_repeated_header_variant(variant: Variant) -> bool:
    """Hide accidental header rows imported as catalog data by old sheets."""
    sku = str(variant.product.sku or "").strip().casefold()
    size = str(variant.size or "").strip().casefold()
    name = str(variant.product.name or "").strip().casefold()
    return sku in {"sku", "style code", "product sku"} and size in {"size", "variant size"} and name in {"product", "product name", "name", "design"}


def _find_column(columns, candidates):
    lower_map = {str(c).strip().lower(): c for c in columns}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
    return None


def build_count_sheet(db: Session, only_uncounted: bool = False) -> bytes:
    query = (
        db.query(Variant).join(Product).options(joinedload(Variant.product))
        .filter(Variant.is_deleted == False, Product.is_deleted == False)  # noqa: E712
    )
    if only_uncounted:
        query = query.filter(Variant.last_counted_at.is_(None))

    variants = [v for v in query.all() if not _is_repeated_header_variant(v)]
    variants.sort(key=lambda v: (v.product.sku.upper(), size_rank(v.size), v.size.upper()))

    wb = Workbook()
    ws = wb.active
    ws.title = "Count Sheet"
    headers = ["Variant Code", "SKU", "Size", "Product", "System Stock", "Count"]
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.fill = PatternFill(
            start_color="A6415B" if h == "Count" else "2F3B52",
            end_color="A6415B" if h == "Count" else "2F3B52", fill_type="solid",
        )
    ws.cell(row=1, column=6).comment = Comment(
        "Count is pre-filled with the latest known stock for convenience. Replace it with the physical count; leave blank to skip. Enter 0 when the size was physically counted and is empty. "
        "You may add a new row with SKU + Size + Product; a missing size will be created automatically.",
        "Inventory Management System",
    )

    for r, v in enumerate(variants, start=2):
        ws.cell(r, 1, v.variant_code)
        ws.cell(r, 2, v.product.sku.upper())
        ws.cell(r, 3, v.size.upper())
        ws.cell(r, 4, v.product.name)
        ws.cell(r, 5, None if v.last_counted_at is None else v.current_stock)
        # Pre-fill Count with the known current stock so the downloaded
        # workbook is immediately usable and contains every existing size.
        # Warehouse staff can overwrite Count with the physical recount.
        ws.cell(r, 6, None if v.last_counted_at is None else v.current_stock)
        for c in range(1, 7):
            ws.cell(r, c).font = Font(name="Arial")

    for col, width in enumerate([40, 34, 10, 42, 15, 12], start=1):
        ws.column_dimensions[chr(64 + col)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:F{max(2, len(variants) + 1)}"

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _normal_variant_code(sku: str, size: str) -> str:
    return f"{sku.upper()}-{size.upper()}"


def bulk_set_stock(db: Session, file_bytes: bytes, filename: str = "", created_by: str = "bulk_upload"):
    df = read_table(file_bytes, filename)
    if df.empty:
        raise ValueError("The file has no rows.")

    variant_code_col = _find_column(df.columns, VARIANT_CODE_CANDIDATES)
    sku_col = _find_column(df.columns, SKU_COLUMN_CANDIDATES)
    size_col = _find_column(df.columns, SIZE_COLUMN_CANDIDATES)
    product_col = _find_column(df.columns, PRODUCT_COLUMN_CANDIDATES)
    category_col = _find_column(df.columns, CATEGORY_COLUMN_CANDIDATES)
    count_col = _find_column(df.columns, COUNT_COLUMN_CANDIDATES)

    if not count_col or not (variant_code_col or (sku_col and size_col)):
        raise ValueError(
            "Need a Count column plus either Variant Code or SKU + Size. "
            f"Found columns: {list(df.columns)}"
        )

    records = df.to_dict("records")
    now = datetime.now(timezone.utc)
    targets: dict[int, tuple[Variant, int]] = {}
    created_products = 0
    created_variants = 0
    not_found, skipped_blank, skipped_incomplete, duplicates = [], 0, 0, 0

    # Resolve rows one by one only for the rows that actually have a Count.
    # Normal warehouse sheets are modest, and this path also supports creating
    # a missing size/product from a manually added Excel row.
    for rec in records:
        raw_variant = clean_code(rec.get(variant_code_col)) if variant_code_col else None
        sku = clean_code(rec.get(sku_col)) if sku_col else None
        size = clean_code(rec.get(size_col)) if size_col else None
        if (
            str(raw_variant or "").casefold() in {"variant code", "variant_code", "code"}
            and str(sku or "").casefold() in {"sku", "style code", "product sku"}
            and str(size or "").casefold() in {"size", "variant size"}
        ):
            skipped_incomplete += 1
            continue

        count = to_int(rec.get(count_col))
        if count is None:
            skipped_blank += 1
            continue
        if count < 0:
            raise ValueError("Stock Count cannot be negative. Use 0 for an empty size.")

        if raw_variant and (not sku or not size):
            if "-" in raw_variant:
                base, maybe_size = raw_variant.rsplit("-", 1)
                sku = sku or clean_code(base)
                size = size or clean_code(maybe_size)

        if not sku or not size:
            skipped_incomplete += 1
            continue

        sku = sku.upper()
        size = size.upper()
        variant_code = raw_variant or _normal_variant_code(sku, size)
        variant_code = variant_code.upper()

        # Exact variant code first; then SKU + size.
        variant = (
            db.query(Variant).options(joinedload(Variant.product))
            .filter(Variant.variant_code == variant_code).first()
        )
        if not variant:
            variant = (
                db.query(Variant).options(joinedload(Variant.product)).join(Product)
                .filter(Product.sku == sku, Variant.size == size).first()
            )

        if not variant:
            product = db.query(Product).filter(Product.sku == sku).first()
            if product and product.is_deleted:
                product.is_deleted = False
                product.deleted_at = None
            if not product:
                product_name = str(rec.get(product_col) or sku).strip()
                category = str(rec.get(category_col) or "").strip() or None
                product = Product(sku=sku, name=product_name, category=category)
                db.add(product)
                db.flush()
                created_products += 1

            existing_size = db.query(Variant).filter(
                Variant.product_id == product.id, Variant.size == size
            ).first()
            if existing_size:
                variant = existing_size
            else:
                variant = Variant(
                    product_id=product.id,
                    size=size,
                    variant_code=_normal_variant_code(sku, size),
                    current_stock=0,
                    reorder_threshold=5,
                    target_stock_level=20,
                    last_counted_at=None,
                )
                db.add(variant)
                db.flush()
                created_variants += 1

        if variant.id in targets:
            duplicates += 1
            continue
        targets[variant.id] = (variant, count)

    # Apply absolute counts and alerts using the same single-source inventory service.
    for variant, new_count in targets.values():
        inventory_service.set_absolute_stock(
            db, variant, new_count, created_by=created_by, note="bulk stock upload"
        )
        alert_service.check_variant_alert(db, variant)

    db.commit()
    return {
        "rows_processed": len(records),
        "updated_count": len(targets),
        "not_found_count": len(not_found),
        "not_found_sample": not_found[:20],
        "skipped_blank_count": skipped_blank + skipped_incomplete,
        "duplicate_rows_ignored": duplicates,
        "created_products_count": created_products,
        "created_variants_count": created_variants,
    }
