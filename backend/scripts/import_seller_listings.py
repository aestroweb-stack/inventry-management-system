"""
Import a marketplace "Seller Listings Report" CSV into the Product/Variant
catalog. Built against the real export format: one row per (style, size),
with columns including style id, style name, size, article type, van,
seller sku code, sku code, and listing status. Any source brand column is ignored.

Usage (run from the backend/ folder):
    python scripts/import_seller_listings.py ../sample_data/seller_listings_export.csv [--include-inactive]

By default only listing status 'A' (Active) rows are imported. Pass
--include-inactive to also import DA (Deactive) / PDL (Platform Delist)
rows - useful if you still hold physical stock of a delisted design.

Every imported size starts as "NOT COUNTED": a listings report carries no
stock numbers, so stock is unknown, not zero. Unknown sizes raise no alerts
and never appear on reorder lists until they are counted (Bulk Stock Count
or Scan & Recount).

WHY grouping is done by `style id`, not by `van` or `seller sku code`:
`style id` is the one column that is 100% reliably constant across every
size-row of a design - verified
against the real 8,313-row export. `van` looks like the base SKU without the
size, and usually is, but for ~28% of styles it was entered identically to the
full per-size code, so used alone it would split one design into several
"products". The base code is therefore derived defensively (_derive_base_code).

All codes are stored UPPERCASE so that lookups from the API, the scanner and
uploaded order sheets are case-insensitive by construction.
"""
import sys
import argparse
from pathlib import Path
from collections import Counter

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal, Base, engine
from models import Product, Variant

DEFAULT_THRESHOLD = 5
DEFAULT_TARGET = 20


def _text(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _clean_product_title(value: str | None, source_label: str | None) -> str | None:
    """Drop a source catalog label when it is only a title prefix."""
    title = _text(value)
    label = _text(source_label)
    if not title or not label or not title.casefold().startswith(label.casefold()):
        return title
    remainder = title[len(label):]
    if remainder and not (remainder[0].isspace() or remainder[0] in "-:|·"):
        return title
    return remainder.lstrip(" \t-:|·") or title


def _strip_size_suffix(value: str, size: str) -> str:
    suffix = "_" + str(size)
    if str(value).upper().endswith(suffix.upper()):
        return str(value)[: -len(suffix)]
    return str(value)


def _derive_base_code(group: pd.DataFrame) -> str | None:
    """One design's rows in -> the best single base code to use as the
    Product SKU. Prefers a van that is already constant across the group;
    otherwise strips each row's own size from its van (then from its seller
    sku code) and takes the most common result."""
    vans = [_text(v) for v in group["van"]]
    if all(v is not None for v in vans) and len(set(vans)) == 1:
        return vans[0]

    stripped = []
    for _, row in group.iterrows():
        size = _text(row["size"]) or ""
        van, ssc = _text(row["van"]), _text(row["seller sku code"])
        candidate = None
        if van:
            candidate = _strip_size_suffix(van, size)
            if candidate == van and ssc:          # van had no size suffix to strip - try the seller code
                candidate = _strip_size_suffix(ssc, size)
        elif ssc:
            candidate = _strip_size_suffix(ssc, size)
        if candidate:
            stripped.append(candidate)

    return Counter(stripped).most_common(1)[0][0] if stripped else None


def import_csv(csv_path: str, include_inactive: bool = False):
    df = pd.read_csv(csv_path, dtype=str)
    required = {"style id", "style name", "size", "article type", "van", "seller sku code", "listing status"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV is missing expected columns: {sorted(missing)}")
    has_marketplace_code = "sku code" in df.columns

    if not include_inactive:
        df = df[df["listing status"] == "A"].copy()

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    existing_skus = {row[0].upper() for row in db.query(Product.sku).all()}
    used_skus = set(existing_skus)

    products_created = variants_created = 0
    skipped_existing_products = skipped_existing_variants = 0
    code_collisions, duplicate_size_rows, no_size_rows = [], [], []
    status_counts = Counter()

    for style_id, group in df.groupby("style id"):
        base_code = _derive_base_code(group)
        if not base_code:
            base_code = f"STYLE-{style_id}"
        base_code = base_code.strip().upper()

        sku = base_code
        if sku in used_skus and sku not in existing_skus:
            # collided with another style processed earlier in this same run
            sku = f"{base_code}-{style_id}"
            code_collisions.append((base_code, style_id, sku))
        used_skus.add(sku)

        first = group.iloc[0]
        status_counts.update(group["listing status"].tolist())

        product = db.query(Product).filter(Product.sku == sku).first()
        if product:
            skipped_existing_products += 1
        else:
            product = Product(
                sku=sku,
                name=_clean_product_title(first["style name"], first.get("brand")) or sku,
                category=_text(first["article type"]),
            )
            db.add(product)
            db.flush()
            products_created += 1

        sizes_seen = set()
        for _, row in group.iterrows():
            size = (_text(row["size"]) or "").upper()
            if not size:
                no_size_rows.append((sku, _text(row["seller sku code"])))
                continue
            if size in sizes_seen:
                # Source-data problem, not an import bug: two rows of one style claim the
                # same size (seen in the real export - one row's size column disagreed with
                # its own SKU's size suffix). Keep the first, skip the rest, surface it.
                duplicate_size_rows.append((sku, size, _text(row["seller sku code"])))
                continue
            sizes_seen.add(size)

            if db.query(Variant.id).filter(Variant.product_id == product.id, Variant.size == size).first():
                skipped_existing_variants += 1
                continue

            seller_code = _text(row["seller sku code"])
            market_code = _text(row["sku code"]) if has_marketplace_code else None
            db.add(Variant(
                product_id=product.id,
                size=size,
                variant_code=f"{sku}-{size}",
                current_stock=0,                     # unknown - see module docstring
                reorder_threshold=DEFAULT_THRESHOLD,
                target_stock_level=DEFAULT_TARGET,
                last_counted_at=None,                # NOT COUNTED
                seller_sku_code=seller_code.upper() if seller_code else None,
                marketplace_sku_code=market_code.upper() if market_code else None,
            ))
            variants_created += 1

        if products_created and products_created % 300 == 0:
            db.commit()   # periodic commit so one huge transaction doesn't stay open for the whole run

    db.commit()
    db.close()

    return {
        "rows_processed": len(df),
        "products_created": products_created,
        "variants_created": variants_created,
        "skipped_existing_products": skipped_existing_products,
        "skipped_existing_variants": skipped_existing_variants,
        "code_collisions": code_collisions,
        "duplicate_size_rows": duplicate_size_rows,
        "no_size_rows": no_size_rows,
        "status_counts": dict(status_counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--include-inactive", action="store_true",
                        help="Also import DA (Deactive) and PDL (Platform Delist) listings")
    args = parser.parse_args()

    result = import_csv(args.csv_path, include_inactive=args.include_inactive)

    print(f"Rows processed:              {result['rows_processed']}")
    print(f"Products created:            {result['products_created']}")
    print(f"Sizes (variants) created:    {result['variants_created']}   (all start as NOT COUNTED)")
    print(f"Products already existed:    {result['skipped_existing_products']}")
    print(f"Sizes already existed:       {result['skipped_existing_variants']}")
    print(f"SKU collisions resolved:     {len(result['code_collisions'])}")
    for base, style_id, resolved in result["code_collisions"]:
        print(f"    '{base}' (style {style_id}) -> '{resolved}'")
    print(f"Duplicate-size rows in source (first kept): {len(result['duplicate_size_rows'])}")
    for sku, size, ssc in result["duplicate_size_rows"]:
        print(f"    {sku} size {size}: extra row '{ssc}' - check this in your seller panel")
    if result["no_size_rows"]:
        print(f"Rows with no size (skipped): {len(result['no_size_rows'])}")
