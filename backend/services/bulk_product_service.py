"""Excel template and transactional bulk product/size import."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Alert, InventoryTransaction, Product, Variant, TransactionType
from services.table_reader import read_table


MAX_IMPORT_ROWS = 20000
MAX_FILE_BYTES = 12 * 1024 * 1024

COLUMN_ALIASES = {
    "sku": {"sku", "master sku", "style code", "sku / style code", "product sku"},
    "name": {"product name", "name", "design name", "design"},
    "category": {"category", "type"},
    "size": {"size", "size name", "variant size"},
    "stock": ("stock", "opening stock", "initial stock", "current inventory", "total inventory (=)", "inventory", "quantity", "qty"),
    "threshold": {"alert below", "alert threshold", "reorder threshold"},
    "target": {"target", "target stock", "target stock level"},
}


def _find_column(columns, aliases):
    normalized = {" ".join(str(column).strip().lower().split()): column for column in columns}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return None


def _cell_text(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _nonnegative_int(value, *, default=None):
    text = _cell_text(value)
    if not text:
        if default is not None:
            return default
        raise ValueError("Stock is required.")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"'{text}' must be a whole number 0 or greater.")
    if not number.is_finite() or number != number.to_integral_value() or number < 0:
        raise ValueError(f"'{text}' must be a whole number 0 or greater.")
    return int(number)


def _strip_size_suffix(value: str, size: str) -> str:
    """Remove a terminal size token from a variant SKU/name, if present.

    Catalog exports commonly use codes such as ``STYLE-BLACK-XL`` or
    ``STYLE_BLACK_26``. Keep the color/design portion and group the sizes on
    the shared Product record.
    """
    import re

    value = value.strip()
    if not value or not size:
        return value
    return re.sub(rf"[-_\s]+{re.escape(size.strip())}$", "", value, flags=re.IGNORECASE).strip()


def _design_sku(source_sku: str, size: str) -> str:
    return _strip_size_suffix(source_sku, size).upper() or source_sku.upper()


def build_bulk_product_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Products"
    headers = ["SKU", "Product Name", "Category", "Size", "Stock", "Alert Below", "Target Stock"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="18324F")
        cell.alignment = Alignment(horizontal="center")
    for row in range(2, 502):
        for col in range(1, len(headers) + 1):
            ws.cell(row=row, column=col).number_format = "0"
    for column, width in {"A": 24, "B": 32, "C": 22, "D": 14, "E": 14, "F": 16, "G": 16}.items():
        ws.column_dimensions[column].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:G{MAX_IMPORT_ROWS + 1}"

    help_ws = wb.create_sheet("Instructions")
    instructions = [
        ["Bulk product import"],
        ["Enter one row per size. Repeat SKU, Product Name and Category for every size of the same design."],
        ["Required columns: SKU, Size and Stock. Product Name is optional and defaults to the design SKU. Stock must be a whole number 0 or greater."],
        ["Category may be blank. Alert Below defaults to 5 and Target Stock defaults to 20 when blank."],
        ["Do not include an image column. Add photos later from the product Edit screen."],
        ["An existing design can receive missing sizes; existing sizes and their stock are left unchanged. Restore a design from Trash before importing it."],
        ["If one row for a SKU is invalid, all sizes for that SKU are skipped together."],
        ["A final size suffix in SKU (for example STYLE-XL or STYLE_XL) is removed for design grouping. Repeated size rows are matched as SKU aliases when safe; conflicting stock is shown for review."],
        ["Rows with a blank Size are added as FREE SIZE. Check those rows after import."],
        ["Supported files: .xlsx, .xls and .csv. Maximum 20,000 rows and 12 MB."],
    ]
    for row in instructions:
        help_ws.append(row)
    help_ws["A1"].font = Font(bold=True, color="FFFFFF", size=15)
    help_ws["A1"].fill = PatternFill("solid", fgColor="18324F")
    help_ws.column_dimensions["A"].width = 112
    for row in help_ws.iter_rows(min_row=2):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
        row[0].font = Font(color="334155", size=11)
        help_ws.row_dimensions[row[0].row].height = 28

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def import_bulk_products(db: Session, file_bytes: bytes, filename: str = ""):
    if len(file_bytes) > MAX_FILE_BYTES:
        raise ValueError("File is larger than 12 MB. Split the inventory into smaller files.")
    try:
        df = read_table(file_bytes, filename)
    except ValueError:
        raise
    if df.empty:
        raise ValueError("The file has no product rows.")
    if len(df.index) > MAX_IMPORT_ROWS:
        raise ValueError(f"The file has {len(df.index)} rows. Upload at most {MAX_IMPORT_ROWS:,} at a time.")

    columns = {key: _find_column(df.columns, aliases) for key, aliases in COLUMN_ALIASES.items()}
    required = ("sku", "size", "stock")
    missing = [key for key in required if columns[key] is None]
    if missing:
        names = {"sku": "SKU", "size": "Size", "stock": "Stock"}
        raise ValueError("Missing required columns: " + ", ".join(names[key] for key in missing) + ". Download the template and copy your data into it.")

    groups: dict[str, list[dict]] = {}
    row_errors: dict[int, str] = {}
    row_skus: dict[int, str] = {}
    invalid_skus: set[str] = set()
    blank_rows = 0
    free_size_rows = 0
    duplicate_rows_merged = 0
    for index, record in enumerate(df.to_dict("records"), start=2):
        values = [_cell_text(value) for value in record.values()]
        if not any(values):
            blank_rows += 1
            continue

        source_sku = _cell_text(record.get(columns["sku"])).upper()
        if not source_sku:
            row_errors[index] = "SKU is required."
            continue
        raw_size = _cell_text(record.get(columns["size"])).upper()
        if not raw_size:
            # Some catalog rows are sold without a size. Keep them in the
            # inventory as a single FREE SIZE variant instead of dropping them.
            raw_size = "FREE SIZE"
            free_size_rows += 1
        sku = _design_sku(source_sku, raw_size)
        row_skus[index] = sku
        groups.setdefault(sku, [])
        try:
            source_name = _cell_text(record.get(columns["name"])) if columns["name"] else ""
            name = _strip_size_suffix(source_name, raw_size) if source_name else sku
            size = raw_size
            if len(sku) > 64 or len(name) > 300 or len(size) > 20:
                raise ValueError("SKU, Product Name or Size exceeds the allowed length.")
            category = _cell_text(record.get(columns["category"])) if columns["category"] else ""
            if len(category) > 100:
                raise ValueError("Category must be 100 characters or fewer.")
            stock = _nonnegative_int(record.get(columns["stock"]))
            threshold = _nonnegative_int(record.get(columns["threshold"]), default=5) if columns["threshold"] else 5
            target = _nonnegative_int(record.get(columns["target"]), default=20) if columns["target"] else 20
            groups[sku].append({
                "row": index, "sku": sku, "source_sku": source_sku, "name": name, "category": category,
                "size": size, "stock": stock, "threshold": threshold, "target": target,
            })
        except ValueError as exc:
            row_errors[index] = str(exc)
            invalid_skus.add(sku)

    # Validate each SKU as one unit so a typo on one size never leaves a
    # partially-created design in the catalog.
    group_errors: dict[str, str] = {}
    for sku, entries in groups.items():
        if sku in invalid_skus:
            group_errors[sku] = "One or more rows for this SKU are invalid; all sizes for this SKU were skipped."
            continue
        names = {entry["name"].casefold() for entry in entries}
        sizes = [entry["size"] for entry in entries]
        if len(names) > 1:
            group_errors[sku] = "Product Name must match on every row for this SKU."
        elif len(sizes) != len(set(sizes)):
            # A few catalogs include both a base listing and the explicit
            # size-SKU for the same stock row. Keep the explicit size SKU and
            # retain the alternate SKU as an order lookup alias.
            by_size: dict[str, list[dict]] = {}
            for entry in entries:
                by_size.setdefault(entry["size"], []).append(entry)
            if any(
                left["source_sku"] == right["source_sku"] and left["stock"] != right["stock"]
                for same_size in by_size.values()
                for index, left in enumerate(same_size)
                for right in same_size[index + 1:]
            ):
                group_errors[sku] = "This SKU has a repeated size with conflicting stock. Keep one row per SKU + Size."
                continue
            unique_entries = []
            for size, same_size in by_size.items():
                same_size.sort(key=lambda entry: (entry["source_sku"] == sku, entry["row"]))
                chosen = same_size[0]
                duplicate_rows_merged += len(same_size) - 1
                chosen["alias_codes"] = []
                for duplicate in same_size[1:]:
                    if duplicate["source_sku"] != chosen["source_sku"]:
                        chosen["alias_codes"].append(duplicate["source_sku"])
                    if duplicate["stock"] != chosen["stock"]:
                        row_errors[duplicate["row"]] = (
                            f"Duplicate {size} row has stock {duplicate['stock']}; kept "
                            f"{chosen['source_sku']} stock {chosen['stock']} and saved the other SKU as an alias."
                        )
                        row_skus[duplicate["row"]] = sku
                chosen["alias_codes"] = list(dict.fromkeys(chosen["alias_codes"]))
                unique_entries.append(chosen)
            groups[sku] = unique_entries

    valid_skus = [sku for sku in groups if sku not in group_errors]
    source_skus = sorted({
        code for sku in valid_skus for entry in groups[sku]
        for code in (entry["source_sku"], *entry.get("alias_codes", []))
    })
    lookup_skus = sorted(set(valid_skus) | set(source_skus))
    existing_products = {
        product.sku: product for product in db.query(Product).filter(Product.sku.in_(lookup_skus)).all()
    } if lookup_skus else {}
    variants_by_product: dict[int, list[Variant]] = {}
    if existing_products:
        for variant in db.query(Variant).filter(Variant.product_id.in_([p.id for p in existing_products.values()])).all():
            variants_by_product.setdefault(variant.product_id, []).append(variant)
    legacy_rows: dict[str, list[tuple[Product, Variant]]] = {}
    for sku, product in existing_products.items():
        if sku in valid_skus and product.is_deleted:
            group_errors[sku] = "This design is in Trash. Restore it before importing its sizes."
    for sku in valid_skus:
        if sku in group_errors:
            continue
        for entry in groups[sku]:
            old = existing_products.get(entry["source_sku"])
            if not old or old.sku == sku or old.is_deleted:
                continue
            variants = [v for v in variants_by_product.get(old.id, []) if not v.is_deleted]
            if len(variants) != 1 or variants[0].size.upper() != entry["size"]:
                continue
            legacy_rows.setdefault(sku, []).append((old, variants[0]))

    valid_groups = [(sku, entries) for sku, entries in groups.items() if sku not in group_errors]
    created_products = created_variants = merged_products = merged_variants = 0
    now = datetime.now(timezone.utc)
    try:
        products_by_sku = {}
        for sku, entries in valid_groups:
            first = entries[0]
            existing = existing_products.get(sku)
            if existing and not existing.is_deleted:
                products_by_sku[sku] = existing
                continue
            category_counts: dict[str, tuple[int, str]] = {}
            for entry in entries:
                category = entry["category"].strip()
                if category:
                    key = category.casefold()
                    count, display = category_counts.get(key, (0, category))
                    category_counts[key] = (count + 1, display)
            category = max(category_counts.values(), default=(0, ""), key=lambda item: item[0])[1]
            product = Product(sku=sku, name=first["name"], category=category or None)
            db.add(product)
            products_by_sku[sku] = product
            created_products += 1
        db.flush()

        variants_to_add = []
        for sku, entries in valid_groups:
            product = products_by_sku[sku]
            known_sizes = {v.size.upper() for v in variants_by_product.get(product.id, []) if not v.is_deleted}
            for old, variant in legacy_rows.get(sku, []):
                if variant.size.upper() in known_sizes:
                    continue
                variant.product = product
                preferred = next(e for e in entries if e["size"] == variant.size.upper())
                variant.variant_code = preferred["source_sku"] if preferred["source_sku"] != sku else f"{sku}_{variant.size}"
                aliases = [*preferred.get("alias_codes", []), *(e["source_sku"] for e in entries if e["size"] == variant.size.upper() and e["source_sku"] != preferred["source_sku"])]
                aliases = [code for code in dict.fromkeys(aliases) if code != variant.variant_code]
                if aliases:
                    variant.seller_sku_code = aliases[0]
                if len(aliases) > 1:
                    variant.marketplace_sku_code = aliases[1]
                known_sizes.add(variant.size.upper())
                if product.name == sku and not columns["name"]:
                    product.name = _strip_size_suffix(old.name, variant.size)
                merged_variants += 1
                merged_products += 1
                # The former one-size product is now empty. Remove it once its
                # variant has been reassigned; all ledger/order references stay
                # attached to the same variant id.
                db.delete(old)
            for entry in entries:
                if entry["size"] in known_sizes:
                    continue
                aliases = entry.get("alias_codes", [])
                variant = Variant(
                    product_id=product.id,
                    size=entry["size"],
                    variant_code=entry["source_sku"] if entry["source_sku"] != sku or entry["size"] == "FREE SIZE" else f"{sku}_{entry['size']}",
                    seller_sku_code=aliases[0] if aliases else None,
                    marketplace_sku_code=aliases[1] if len(aliases) > 1 else None,
                    current_stock=entry["stock"],
                    reorder_threshold=entry["threshold"],
                    target_stock_level=entry["target"],
                    last_counted_at=now,
                )
                variants_to_add.append((variant, entry))
                known_sizes.add(entry["size"])
                created_variants += 1
        for variant, _ in variants_to_add:
            db.add(variant)
        db.flush()

        transactions = []
        alerts = []
        for variant, entry in variants_to_add:
            if entry["stock"]:
                transactions.append(InventoryTransaction(
                    variant_id=variant.id, change_qty=entry["stock"],
                    transaction_type=TransactionType.INITIAL_STOCK,
                    reference="bulk product import", balance_after=entry["stock"],
                    created_by="bulk_product_import",
                ))
            if entry["stock"] <= entry["threshold"]:
                alerts.append(Alert(
                    variant_id=variant.id,
                    alert_type="OUT_OF_STOCK" if entry["stock"] <= 0 else "LOW_STOCK",
                    stock_at_alert=entry["stock"],
                ))
        db.add_all(transactions)
        db.add_all(alerts)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Import could not be saved because a SKU or size already exists. Refresh the inventory and upload again.") from exc

    errors = []
    for row, message in row_errors.items():
        errors.append({"row": row, "sku": row_skus.get(row), "message": message})
    for sku, message in group_errors.items():
        for entry in groups[sku]:
            if entry["row"] not in row_errors:
                errors.append({"row": entry["row"], "sku": sku, "message": message})
    errors.sort(key=lambda item: item["row"])

    processed_rows = len(df.index)
    # The source row count minus distinct size variants, harmless duplicate
    # aliases, and migrated legacy variants records the rows left unimported.
    rows_skipped = processed_rows - blank_rows - created_variants - merged_variants - duplicate_rows_merged
    return {
        "rows_processed": processed_rows,
        "products_created": created_products,
        "products_merged": merged_products,
        "variants_created": created_variants,
        "variants_merged": merged_variants,
        "duplicate_rows_merged": duplicate_rows_merged,
        "free_size_rows": free_size_rows,
        "rows_skipped": rows_skipped,
        "blank_rows": blank_rows,
        "errors": errors[:100],
        "errors_truncated": len(errors) > 100,
    }
