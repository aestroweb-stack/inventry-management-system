"""
Run once to populate a few sample products (each with several size variants)
so the dashboard isn't empty on first launch:

    python seed_data.py

Uses the numeric Style Code convention already in use for AJIO/Myntra
listings (next free block starts at 392) as example SKUs - replace with a
real product export whenever ready (see README "Importing your full
catalog").
"""
from datetime import datetime, timezone
from database import SessionLocal, Base, engine
from models import Product, Variant, Vendor, TransactionType
from services.inventory_service import apply_stock_change
from services.alert_service import check_variant_alert

Base.metadata.create_all(bind=engine)
db = SessionLocal()

if db.query(Product).count() > 0:
    print("Products already exist - skipping seed (delete inventory.db to reset).")
else:
    vendor1 = Vendor(name="Shree Textiles", contact="9XXXXXXXXX")
    vendor2 = Vendor(name="Kapodra Fabric House", contact="9XXXXXXXXX")
    db.add_all([vendor1, vendor2])
    db.flush()

    # Each product: base fields + a per-size stock map (S/M/L/XL/XXL where relevant)
    sample_products = [
        dict(sku="392", name="Rani Pink Embroidered Kurta Set",
             category="Kurta Set", vendor_id=vendor1.id,
             sizes={"S": 4, "M": 6, "L": 5, "XL": 2, "XXL": 1}, threshold=5, target=25),
        dict(sku="393", name="Mustard Yellow Printed Salwar Suit",
             category="Salwar Suit", vendor_id=vendor1.id,
             sizes={"S": 1, "M": 2, "L": 0, "XL": 0, "XXL": 0}, threshold=5, target=20),
        dict(sku="394", name="Navy Blue Chikankari Kurta Set",
             category="Kurta Set", vendor_id=vendor2.id,
             sizes={"S": 2, "M": 1, "L": 2, "XL": 1, "XXL": 0}, threshold=8, target=30),
        dict(sku="395", name="Maroon Bandhani Print Suit Set",
             category="Salwar Suit", vendor_id=vendor2.id,
             sizes={"S": 0, "M": 0, "L": 0, "XL": 0, "XXL": 0}, threshold=5, target=20),
        dict(sku="396", name="Sea Green Cotton Kurta Set",
             category="Kurta Set", vendor_id=vendor1.id,
             sizes={"S": 3, "M": 4, "L": 3, "XL": 2, "XXL": 0}, threshold=5, target=20),
    ]

    for data in sample_products:
        sizes = data.pop("sizes")
        threshold = data.pop("threshold")
        target = data.pop("target")
        p = Product(sku=data["sku"], name=data["name"],
                     category=data["category"], vendor_id=data["vendor_id"])
        db.add(p)
        db.flush()
        for size, qty in sizes.items():
            v = Variant(
                product_id=p.id, size=size, variant_code=f"{p.sku}-{size}",
                current_stock=0, reorder_threshold=threshold, target_stock_level=target,
                last_counted_at=datetime.now(timezone.utc),
            )
            db.add(v)
            db.flush()
            if qty:
                apply_stock_change(db, v, qty, TransactionType.INITIAL_STOCK, reference="seed data")
            check_variant_alert(db, v)

    db.commit()
    print(f"Seeded {len(sample_products)} products x 5 sizes each across 2 vendors.")

db.close()
