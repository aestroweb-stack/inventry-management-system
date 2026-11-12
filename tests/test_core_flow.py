"""
Run from the project root:   python tests/test_core_flow.py
Real API, real database (a throw-away SQLite file), no mocking.
"""
import os
import sys
import tempfile
from io import BytesIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

os.environ["DATABASE_URL"] = "sqlite:///./test_inventory.db"
if os.path.exists("test_inventory.db"):
    os.remove("test_inventory.db")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook, load_workbook  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402

import main  # noqa: E402
import inventory_mcp  # noqa: E402
from database import SessionLocal  # noqa: E402
from models import ReorderBatch  # noqa: E402
from scripts.import_seller_listings import import_csv, _clean_product_title  # noqa: E402

client = TestClient(main.app)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def make_xlsx(headers, rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(list(r))
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def post_orders(content: bytes, filename: str):
    mime = "text/csv" if filename.endswith(".csv") else XLSX
    return client.post("/api/orders/upload", files={"file": (filename, content, mime)})


def expect(condition, message):
    if not condition:
        raise AssertionError("FAIL: " + message)
    print("  ok:", message)


def reorder_batches() -> int:
    db = SessionLocal()
    try:
        return db.query(ReorderBatch).count()
    finally:
        db.close()


# ====================================================================== #
print("1. Create a product with several size variants")
r = client.post("/api/products", json={
    "sku": "T1", "name": "Test Kurta A",
    "variants": [
        {"size": "S", "initial_stock": 10, "reorder_threshold": 5},
        {"size": "M", "initial_stock": 6, "reorder_threshold": 5},
        {"size": "L", "initial_stock": 2, "reorder_threshold": 5},
    ],
})
expect(r.status_code == 200, "create product T1 with 3 size variants")
body = r.json()
expect(len(body["variants"]) == 3 and body["total_stock"] == 18, "3 variants, total_stock 10+6+2 = 18")
by_size = {v["size"]: v for v in body["variants"]}
expect(by_size["S"]["variant_code"] == "T1_S", "variant_code is canonical SKU_Size")
expect(by_size["S"]["status"] == "OK", "T1-S status OK (10 > 5)")
expect(by_size["L"]["status"] == "LOW", "T1-L status LOW (2 <= 5): typed-in starting stock counts as counted")
mcp_search = inventory_mcp.search_inventory("Please tell me Test Kurta ka size M stock batao")
expect(mcp_search["total"] == 1 and mcp_search["items"][0]["sku"] == "T1",
       "Inventory MCP ignores English/Hinglish request words and size labels when matching a product")

r = client.post("/api/products", json={"sku": "T2", "name": "Test Suit B",
                                        "variants": [{"size": "M", "initial_stock": 6, "reorder_threshold": 5}]})
expect(r.status_code == 200, "create product T2")
r = client.post("/api/products", json={"sku": "T1", "name": "dup", "variants": [{"size": "S"}]})
expect(r.status_code == 400, "duplicate product SKU rejected")
expect("brand" not in body, "product API does not expose a brand field")

# ====================================================================== #
print("1b. Optional legacy brand cleanup is an explicit migration, not an app-startup side effect")
legacy_engine = create_engine("sqlite://")
with legacy_engine.begin() as conn:
    conn.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, brand TEXT)"))
    conn.execute(text("INSERT INTO products (name, brand) VALUES ('Catalog Label Kurta Set', 'Catalog Label')"))
main.migrate_legacy_brand_data_explicitly(legacy_engine)
legacy_columns = {column["name"] for column in inspect(legacy_engine).get_columns("products")}
with legacy_engine.begin() as conn:
    migrated_name = conn.execute(text("SELECT name FROM products WHERE id = 1")).scalar_one()
expect("brand" not in legacy_columns and migrated_name == "Kurta Set", "legacy label column and title prefix removed")
legacy_engine.dispose()
expect(_clean_product_title("Catalog Label Kurta", "Catalog Label") == "Kurta", "source label prefix is stripped from imported title")

# ====================================================================== #
print("2. Order upload reserves SIZE-WISE demand without changing physical stock")
excel = make_xlsx(["SKU", "Size", "Quantity"], [
    ("t1", "s", 3), ("T2", "M", 4), ("t1", "S", 4), ("t1", "M", 2), ("UNKNOWN99", "M", 1),
])
r = post_orders(excel, "orders.xlsx")
expect(r.status_code == 200, "order upload succeeds")
body = r.json()
expect(body["unique_skus"] == 4, f"4 unique variants/codes from 5 rows, got {body['unique_skus']}")
expect(body["pending_count"] == 3 and body["fulfilled_count"] == 0 and body["not_found_count"] == 1,
       "3 order lines pending, UNKNOWN99 not found")
t1s = next(l for l in body["lines"] if l["sku"] == "T1" and l["size"] == "S")
expect(t1s["qty_ordered"] == 7 and t1s["remaining_stock"] == 10 and t1s["status"] == "PENDING",
       "T1-S repeated rows aggregate to 7 and remain pending against stock 10")
t1m = next(l for l in body["lines"] if l["sku"] == "T1" and l["size"] == "M")
expect(t1m["qty_ordered"] == 2 and t1m["remaining_stock"] == 6, "T1-M remains size-isolated and physical stock is reserved")
reorder = {i["variant_code"]: i for i in client.get("/api/reorder-list").json()}
expect(reorder["T1_S"]["current_stock"] == 10 and reorder["T1_S"]["ordered_qty"] == 7
       and reorder["T1_S"]["required_qty"] == 0, "covered pending demand stays visible with zero required")
expect(reorder["UNKNOWN99"]["required_qty"] == 1, "unmapped order is listed as a one-piece shortage")

# ====================================================================== #
print("3. Physical alerts remain size-specific; covered orders are not shortage alerts")
codes = [a["variant_code"] for a in client.get("/api/alerts").json()]
expect("T1_L" in codes and not ({"T1_S", "T1_M", "T2_M"} & set(codes)), f"only physically low T1-L alerted, got {codes}")

# ====================================================================== #
print("4. Dispatch deducts physical stock once; shipped lines remain visible with zero pending demand")
upload_id = body["order_upload_id"]
dispatch = client.post(f"/api/orders/{upload_id}/dispatch")
expect(dispatch.status_code == 200 and dispatch.json()["dispatched_units"] == 13, "initial batch dispatch succeeds for 13 units")
expect(client.post(f"/api/orders/{upload_id}/dispatch").status_code == 404,
       "the same dispatched batch cannot be deducted twice")
t1 = next(p for p in client.get("/api/products").json() if p["sku"] == "T1")
expect(next(v for v in t1["variants"] if v["size"] == "S")["current_stock"] == 3, "T1-S stock deducted once: 10 - 7 = 3")
reorder = {i["variant_code"]: i for i in client.get("/api/reorder-list").json()}
expect(reorder["T1_S"]["ordered_qty"] == 0 and reorder["T1_S"]["dispatched_qty"] == 7
       and reorder["T1_S"]["required_qty"] == 0, "dispatch is shown separately and never double-counted as pending")

# ====================================================================== #
print("5. Same file twice is rejected, not double-deducted")
same = make_xlsx(["SKU", "Size", "Quantity"], [("T2", "M", 1)])
same_upload = post_orders(same, "same.xlsx")
expect(same_upload.status_code == 200, "first upload succeeds")
expect(post_orders(same, "same.xlsx").status_code == 409, "identical re-upload rejected (409)")
expect(client.post(f"/api/orders/{same_upload.json()['order_upload_id']}/dispatch").status_code == 200,
       "separately identified pending batch can be dispatched")

# ====================================================================== #
print("6. Manual recount by variant_code updates stock and resolves the alert")
r = client.post("/api/inventory/adjust", json={"variant_code": "T1-S", "new_count": 20, "note": "physical recount"})
expect(r.json()["current_stock"] == 20 and r.json()["status"] == "OK", "T1-S recounted to 20, status OK")
codes = [a["variant_code"] for a in client.get("/api/alerts").json()]
expect("T1_S" not in codes and "T1_M" in codes, "T1-S alert resolved; T1-M untouched (size isolation)")
expect(client.post("/api/inventory/adjust", json={"variant_code": "T1-S", "new_count": -1}).status_code == 400,
       "negative count rejected")

# ====================================================================== #
print("7. Reorder list reports physical stock, order counts, and requirement")
reorder = {i["variant_code"]: i for i in client.get("/api/reorder-list").json()}
expect(reorder["T1_S"]["current_stock"] == 20 and reorder["T1_S"]["dispatched_qty"] == 7
       and reorder["T1_S"]["required_qty"] == 0, "T1-S shows stock 20, dispatched 7, no pending restock")
expect("T1_L" not in reorder, "variants without an order history are not shown in the order-based restock list")

# ====================================================================== #
print("8. Variant lookup (scan screen)")
r = client.get("/api/variants/t1-l")
expect(r.status_code == 200 and r.json()["sku"] == "T1" and r.json()["counted"] is True, "lookup is case-insensitive")
expect(client.get("/api/variants/NOPE-X").status_code == 404, "unknown code -> 404")

print("9. Audit trail per variant")
expect(len(client.get("/api/variants/T1_S/transactions").json()) >= 3, "T1-S ledger: initial, dispatch deduction, recount")

print("10. Reorder export is a real file")
r = client.get("/api/reorder-list/export")
expect(r.status_code == 200 and len(r.content) > 1000, "export xlsx has content")

print("11. QR labels")
r = client.get("/api/variants/T1-L/label")
expect(r.status_code == 200 and r.headers["content-type"] == "image/png" and len(r.content) > 500, "single label PNG")
r = client.get("/api/labels/sheet", params={"variant_codes": "T1_S,T1_M,T1_L"})
expect(r.status_code == 200 and len(r.content) > 1000, "label sheet PNG")

# ====================================================================== #
print("12. Real-catalog import: mixed-case codes normalised, sizes start NOT COUNTED")
csv_rows = [
    # style, name, size, article, van, seller sku code, sku code, status
    (1001, "Kurta Set Pink", "M", "Kurta Sets", "Twins_Test_Pink", "Twins_Test_Pink_M", "TWLAKASS900001", "A"),
    (1001, "Kurta Set Pink", "L", "Kurta Sets", "Twins_Test_Pink", "Twins_Test_Pink_L", "TWLAKASS900002", "A"),
    (1002, "GIRL CNTRL Dress", "M", "Dresses", "SW999_M", "SW999_M", "TWLAKASS900003", "A"),   # van repeats the size
    (1002, "GIRL CNTRL Dress", "L", "Dresses", "SW999_L", "SW999_L", "TWLAKASS900004", "A"),
    (1003, "Duplicate Design", "M", "Kurtas", "DUP", "DUP_M", "TWLAKASS900005", "A"),
    (1003, "Duplicate Design", "M", "Kurtas", "DUP", "DUP_L", "TWLAKASS900006", "A"),            # same size twice
    (1004, "Old Design", "S", "Kurtas", "OLD", "OLD_S", "TWLAKASS900007", "DA"),            # deactive
]
csv_df = pd.DataFrame(csv_rows, columns=["style id", "style name", "size", "article type",
                                          "van", "seller sku code", "sku code", "listing status"])
csv_path = os.path.join(tempfile.mkdtemp(), "listings.csv")
csv_df.to_csv(csv_path, index=False)
res = import_csv(csv_path)
expect(res["products_created"] == 3 and res["variants_created"] == 5, f"3 active products / 5 sizes imported, got {res['products_created']}/{res['variants_created']}")
expect(len(res["duplicate_size_rows"]) == 1, "duplicate-size row in the source is flagged, not a crash")
expect(client.get("/api/variants/twins_test_pink-m").status_code == 200, "mixed-case source SKU is found via lowercase lookup")
lk = client.get("/api/variants/TWINS_TEST_PINK-M").json()
expect(lk["counted"] is False and lk["current_stock"] == 0, "imported size reports counted = False")
expect(client.get("/api/variants/SW999-M").status_code == 200, "size-suffixed van collapsed to base SKU SW999")
expect(client.get("/api/variants/OLD-S").status_code == 404, "deactive listing skipped by default")
pink_product = next(p for p in client.get("/api/products").json() if p["sku"] == "TWINS_TEST_PINK")
expect(pink_product["name"] == "Kurta Set Pink", "source catalog prefix does not leak into product title")

print("13. Not-counted sizes never raise alerts or reorder lines")
codes = {a["variant_code"] for a in client.get("/api/alerts").json()}
expect(not any(c.startswith(("TWINS_TEST_PINK", "SW999", "DUP")) for c in codes), "no alerts for never-counted sizes")
reorder_codes = {i["variant_code"] for i in client.get("/api/reorder-list").json()}
expect(not any(c.startswith(("TWINS_TEST_PINK", "SW999", "DUP")) for c in reorder_codes), "no reorder lines for never-counted sizes")
s = client.get("/api/summary").json()
expect(s["uncounted"] == 5 and s["variants"] == 9 and s["products"] == 5, f"summary: 5 uncounted of 9 variants, 5 products, got {s}")

print("14. Filtering, search and pagination")
product_page = client.get("/api/products", params={"limit": 12, "offset": 0, "paged": "true"}).json()
expect(product_page["total"] == len(client.get("/api/products").json()) and product_page["items"],
       "unfiltered Products page returns an exact total for pagination")
page = client.get("/api/variants", params={"status": "UNCOUNTED", "limit": 2, "offset": 1}).json()
expect(page["total"] == 5 and len(page["items"]) == 2 and page["offset"] == 1, "UNCOUNTED page: total 5, 2 items from offset 1")
expect(client.get("/api/variants", params={"q": "twins_test"}).json()["total"] == 2, "search by SKU text")
expect("brand" not in client.get("/api/products").json()[0], "brand removed from product listings")
expect(client.get("/api/brands").status_code == 404, "brand endpoint removed")
sizes = [i["size"] for i in client.get("/api/variants", params={"q": "T1_"}).json()["items"]]
expect(sizes == ["S", "M", "L"], f"sizes sorted S, M, L (not alphabetical), got {sizes}")
expect(client.get("/api/variants", params={"status": "BOGUS"}).status_code == 400, "bad status -> 400")

# ====================================================================== #
print("15. Order sheet matched on REAL marketplace codes (no Size column), never-counted -> clear note")
orders = make_xlsx(["Seller SKU Code", "Quantity"], [
    ("Twins_Test_Pink_M", 2),      # seller code, mixed case
    ("TWLAKASS900002", 1),        # marketplace's internal code for Twins_Test_Pink_L
    ("SW999_M", 1),
])
r = post_orders(orders, "myntra_orders.xlsx")
expect(r.status_code == 200, "order sheet with only a seller-sku column accepted")
body = r.json()
expect(body["not_found_count"] == 0 and body["pending_count"] == 3, "all 3 codes resolve and remain pending because stock is not counted")
expect(all("never counted" in (l["note"] or "") for l in body["lines"]), "each line says the stock was never counted")
expect(body["new_alerts"] == [], "no alerts from never-counted sizes")

print("16. CSV order file, base SKU + Size columns")
csv_orders = b"SKU,Size,Qty\ntwins_test_pink,m,1\n"
r = post_orders(csv_orders, "orders.csv")
expect(r.status_code == 200 and r.json()["lines"][0]["product_name"] == "Kurta Set Pink", "CSV order matched by SKU + Size")

print("17. Unreadable file is a clean 400, not a crash")
r = post_orders(b"this is not an excel file", "bad.xlsx")
expect(r.status_code == 400 and "Could not read" in r.json()["detail"], "garbage upload -> 400 with a readable message")

print("17b. No Quantity column: an order/shipment manifest where every row IS one piece")
manifest = make_xlsx(["Seller SKU Code"], [("Twins_Test_Pink_M",), ("Twins_Test_Pink_M",), ("SW999_L",)])
r = post_orders(manifest, "manifest.xlsx")
expect(r.status_code == 200, "file with no quantity column is accepted, not rejected")
mbody = r.json()
expect(mbody["file_note"] and "1 piece" in mbody["file_note"], f"response says qty defaulted to 1, got {mbody['file_note']!r}")
pink_line = next(l for l in mbody["lines"] if l["sku"] == "TWINS_TEST_PINK")
expect(pink_line["qty_ordered"] == 2, f"two repeated manifest rows for the same size = qty 2, got {pink_line['qty_ordered']}")

print("17c. A real quantity column still overrides the 1-per-row default (no regression)")
with_qty = make_xlsx(["SKU", "Size", "Qty"], [("T2", "M", 3)])
r = post_orders(with_qty, "with_qty.xlsx")
expect(r.json()["file_note"] is None, "file_note absent when a quantity column exists")
expect(r.json()["lines"][0]["qty_ordered"] == 3, "real quantity column value used, not defaulted to 1")

print("17d. Legacy .xls (old binary Excel format) is readable")
import xlwt
xls_buf = BytesIO()
xls_wb = xlwt.Workbook()
xls_ws = xls_wb.add_sheet("Orders")
for c, h in enumerate(["SKU", "Size", "Quantity"]):
    xls_ws.write(0, c, h)
xls_ws.write(1, 0, "T2"); xls_ws.write(1, 1, "M"); xls_ws.write(1, 2, 2)
xls_wb.save(xls_buf)
r = client.post("/api/orders/upload", files={"file": ("orders.xls", xls_buf.getvalue(), "application/vnd.ms-excel")})
expect(r.status_code == 200, f"legacy .xls file is read correctly, got {r.status_code}: {r.text[:200]}")

print("17e. A real .xlsx uploaded with a misleading/missing extension is still sniffed correctly")
mislabeled = make_xlsx(["SKU", "Size", "Quantity"], [("T2", "M", 777)])  # distinct content - must not hash-collide with test 5's file
r = client.post("/api/orders/upload", files={"file": ("export", mislabeled, "application/octet-stream")})
expect(r.status_code == 200, f"content is sniffed by its real bytes when the extension is missing/wrong, got {r.status_code}: {r.text[:200]}")

# ====================================================================== #
print("18. Bulk stock count: set, skip blanks, report unknown codes, ignore duplicate rows")
counts = make_xlsx(["Variant Code", "Count"], [
    ("TWINS_TEST_PINK-M", 10),
    ("TWINS_TEST_PINK-L", 3),
    ("SW999-M", None),               # blank -> skipped
    ("NOPE-X", 4),                   # unknown -> reported
    ("TWINS_TEST_PINK-M", 99),       # duplicate row -> first wins
])
r = client.post("/api/inventory/bulk-set", files={"file": ("counts.xlsx", counts, XLSX)})
expect(r.status_code == 200, "bulk-set accepted")
b = r.json()
expect(b["updated_count"] == 3 and b["not_found_count"] == 0 and b["skipped_blank_count"] == 1
       and b["duplicate_rows_ignored"] == 1 and b["created_products_count"] == 1 and b["created_variants_count"] == 1,
       f"2 known sizes updated and missing NOPE-X created, 1 blank, 1 duplicate ignored, got {b}")
lk = client.get("/api/variants/TWINS_TEST_PINK-M").json()
expect(lk["counted"] is True and lk["current_stock"] == 10, "TWINS_TEST_PINK-M counted, stock 10 (duplicate 99 ignored)")
expect(client.get("/api/variants/SW999-M").json()["counted"] is False, "blank row left SW999-M not counted")
codes = {a["variant_code"] for a in client.get("/api/alerts").json()}
expect("TWINS_TEST_PINK-L" in codes and "TWINS_TEST_PINK-M" not in codes, "counted low size alerts (3 <= 5); size with 10 does not")
reorder = {i["variant_code"]: i for i in client.get("/api/reorder-list").json()}
expect(reorder["TWINS_TEST_PINK-L"]["ordered_qty"] == 1 and reorder["TWINS_TEST_PINK-L"]["current_stock"] == 3
       and reorder["TWINS_TEST_PINK-L"]["required_qty"] == 0,
       "one pending order is covered by three physical units; no restock required")
expect(client.get("/api/summary").json()["uncounted"] == 3, "summary: 3 sizes still not counted")

print("19. After counting, order demand stays pending until dispatch")
r = post_orders(make_xlsx(["Seller SKU Code", "Quantity"], [("TWINS_TEST_PINK_M", 4)]), "orders_after_count.xlsx")
line = r.json()["lines"][0]
expect(line["status"] == "PENDING" and line["remaining_stock"] == 10, "pending demand leaves physical count unchanged")
dispatch = client.post(f"/api/orders/{r.json()['order_upload_id']}/dispatch")
expect(dispatch.status_code == 200 and client.get("/api/variants/TWINS_TEST_PINK-M").json()["current_stock"] == 6,
       "dispatch deducts the four units once: 10 - 4 = 6")

print("20. Count sheet export -> fill -> upload round trip")
r = client.get("/api/inventory/count-sheet", params={"only_uncounted": "true"})
expect(r.status_code == 200, "count sheet downloads")
wb = load_workbook(BytesIO(r.content))
ws = wb.active
rows = list(ws.iter_rows(values_only=True))
expect(rows[0][0] == "Variant Code" and rows[0][5] == "Count", "header row as documented")
expect(len(rows) - 1 == 3 and all(row[4] is None for row in rows[1:]), "only the 3 uncounted sizes, System Stock blank")
header_product = client.post("/api/products", json={"sku": "SKU", "name": "Product", "variants": [{"size": "SIZE", "initial_stock": 1}]})
expect(header_product.status_code == 200, "header-like legacy row can be reproduced for count-sheet regression")
clean_sheet = load_workbook(BytesIO(client.get("/api/inventory/count-sheet").content)).active
expect(not any(row[1] == "SKU" and row[2] == "SIZE" for row in clean_sheet.iter_rows(min_row=2, values_only=True)),
       "count sheet hides accidental repeated header rows from catalog data")
repeated_header_upload = client.post("/api/inventory/bulk-set", files={"file": ("headers.xlsx", make_xlsx(
    ["Variant Code", "SKU", "Size", "Product", "System Stock", "Count"],
    [("Variant Code", "SKU", "Size", "Product", 0, 0)],
), XLSX)})
expect(repeated_header_upload.status_code == 200 and repeated_header_upload.json()["created_products_count"] == 0,
       "repeated header row in a count upload is ignored instead of imported as inventory")
for i in range(2, ws.max_row + 1):
    ws.cell(row=i, column=6, value=i)     # "count" each size
buf = BytesIO()
wb.save(buf)
r = client.post("/api/inventory/bulk-set", files={"file": ("filled.xlsx", buf.getvalue(), XLSX)})
expect(r.json()["updated_count"] == 3, "filled sheet uploads back: 3 updated")
expect(client.get("/api/summary").json()["uncounted"] == 0, "nothing left uncounted")

print("21. Viewing the reorder list is read-only; exporting saves one snapshot")
before = reorder_batches()
client.get("/api/reorder-list")
client.get("/api/reorder-list")
expect(reorder_batches() == before, "opening the list twice created no snapshots")
client.get("/api/reorder-list/export")
expect(reorder_batches() == before + 1, "export saved exactly one snapshot")

print("22. Delete a whole product: sizes, ledger, alerts all cleaned up, nothing orphaned")
client.post("/api/products", json={"sku": "DEL1", "name": "Delete Me", "variants": [
    {"size": "S", "initial_stock": 3, "reorder_threshold": 5}, {"size": "M", "initial_stock": 20},
]})
expect({a["variant_code"] for a in client.get("/api/alerts").json()} >= {"DEL1_S"}, "DEL1-S alerted before delete")
r = client.delete("/api/products/DEL1")
expect(r.status_code == 200 and r.json()["sizes_removed"] == 2, f"delete reports 2 sizes removed, got {r.json()}")
expect(client.get("/api/variants/DEL1-S").status_code == 404, "DEL1-S gone")
expect(not any(a["sku"] == "DEL1" for a in client.get("/api/alerts").json()), "DEL1's alert cleaned up, not orphaned")
expect(not any(p["sku"] == "DEL1" for p in client.get("/api/products").json()), "DEL1 gone from product list")
expect(client.delete("/api/products/DEL1").status_code == 404, "deleting again -> 404, not a crash")

print("23. Delete a single size: the rest of the product is untouched")
client.post("/api/products", json={"sku": "DEL2", "name": "Partial Delete", "variants": [
    {"size": "S", "initial_stock": 5}, {"size": "M", "initial_stock": 7},
]})
r = client.delete("/api/variants/DEL2-S")
expect(r.status_code == 200 and r.json()["product_also_deleted"] is False, "DEL2-S removed, product kept (M remains)")
expect(client.get("/api/variants/DEL2-M").status_code == 200, "DEL2-M still there")
r = client.delete("/api/variants/DEL2-M")
expect(r.json()["product_also_deleted"] is True, "removing the LAST size also removes the now-empty product")
expect(not any(p["sku"] == "DEL2" for p in client.get("/api/products").json()), "DEL2 gone entirely")

print("24. Soft delete: item disappears from normal views but is restorable from Trash")
client.post("/api/products", json={"sku": "TR1", "name": "Trash Test", "variants": [
    {"size": "S", "initial_stock": 3, "reorder_threshold": 5}, {"size": "M", "initial_stock": 20},
]})
expect("TR1_S" in {a["variant_code"] for a in client.get("/api/alerts").json()}, "TR1-S alerted before delete")
r = client.delete("/api/products/TR1")
expect(r.json()["trashed"] is True, "delete response says trashed")
expect(client.get("/api/variants/TR1-S").status_code == 404, "TR1-S hidden from lookup after delete")
expect(not any(p["sku"] == "TR1" for p in client.get("/api/products").json()), "TR1 hidden from product list")
expect("TR1_S" not in {a["variant_code"] for a in client.get("/api/alerts").json()}, "TR1-S alert cleared on delete")
trash = client.get("/api/trash").json()
expect(any(t["sku"] == "TR1" and t["variant_count"] == 2 for t in trash), f"TR1 appears in Trash with 2 sizes, got {trash}")
r = client.post("/api/products/TR1/restore")
expect(r.status_code == 200 and r.json()["variants_restored"] == 2, "restore brings back both sizes")
expect(client.get("/api/variants/TR1-S").status_code == 200, "TR1-S visible again after restore")
expect("TR1_S" in {a["variant_code"] for a in client.get("/api/alerts").json()}, "TR1-S alert re-opens on restore (still low)")
expect(not any(t["sku"] == "TR1" for t in client.get("/api/trash").json()), "TR1 gone from Trash after restore")
# existing SKU re-use while in Trash is rejected with a clear signal, not a silent duplicate
client.delete("/api/products/TR1")
expect(client.post("/api/products", json={"sku": "TR1", "name": "x", "variants": [{"size": "S"}]}).status_code == 409,
       "creating a SKU that's in Trash is rejected (409), telling you to restore instead")
r = client.delete("/api/trash/TR1")
expect(r.status_code == 200, "permanently deleting from Trash works")
expect(client.get("/api/trash").json() == [] or not any(t["sku"] == "TR1" for t in client.get("/api/trash").json()), "TR1 gone from Trash for good")
expect(client.post("/api/products", json={"sku": "TR1", "name": "y", "variants": [{"size": "S"}]}).status_code == 200,
       "SKU is free to reuse once permanently deleted")

print("25. Add a size to an existing product (the 'forgot a size' fix)")
client.post("/api/products", json={"sku": "AS1", "name": "Add Size Test", "variants": [{"size": "M", "initial_stock": 5}]})
r = client.post("/api/products/AS1/variants", json={"size": "XL", "initial_stock": 8, "reorder_threshold": 3})
expect(r.status_code == 200, "adding a new size succeeds")
sizes = {v["size"]: v for v in r.json()["variants"]}
expect("XL" in sizes and sizes["XL"]["current_stock"] == 8, "new XL size carries its starting stock")
expect(client.post("/api/products/AS1/variants", json={"size": "XL"}).status_code == 400, "adding the same size twice is rejected")
# deleting a size then re-adding it should un-trash it, not create a second row
client.delete("/api/variants/AS1-XL")
r = client.post("/api/products/AS1/variants", json={"size": "XL", "initial_stock": 2})
expect(r.status_code == 200, "re-adding a trashed size revives it instead of erroring")
expect(len([v for v in r.json()["variants"] if v["size"] == "XL"]) == 1, "exactly one XL row exists, not a duplicate")

print("26. Scanning one size resolves the WHOLE product, every size included")
r = client.get("/api/variants/AS1-M/product")
expect(r.status_code == 200 and r.json()["sku"] == "AS1", "lookup by one variant_code returns its parent product")
expect({v["size"] for v in r.json()["variants"]} == {"M", "XL"}, "both of AS1's sizes are included, not just the scanned one")

print("27. Global transaction history reads like an activity log")
r = client.get("/api/transactions", params={"q": "AS1", "limit": 10})
expect(r.status_code == 200 and len(r.json()) >= 2, "AS1's transactions are listed")
expect(all(t["sku"] == "AS1" for t in r.json()), "search filters to just AS1's history")
expect(r.json()[0]["created_at"] >= r.json()[-1]["created_at"], "newest first")

print("28. Movers (for the dashboard chart): units actually sold, highest first")
client.post("/api/products", json={"sku": "MV1", "name": "Mover Test", "variants": [{"size": "M", "initial_stock": 50}]})
post_orders(make_xlsx(["SKU", "Size", "Quantity"], [("MV1", "M", 12)]), "mover_order.xlsx")
r = client.get("/api/analytics/movers", params={"days": 365, "limit": 5})
expect(r.status_code == 200, "movers endpoint responds")
mv1 = next((m for m in r.json() if m["sku"] == "MV1"), None)
expect(mv1 is not None and mv1["units_moved"] == 12, f"MV1 shows 12 units moved, got {mv1}")

print("29. Barcode labels are real, scannable Code128 (not QR) and decode back to the exact variant_code")
import barcode as barcode_lib
from pyzbar.pyzbar import decode as zbar_decode
from PIL import Image as PILImage
r = client.get("/api/variants/AS1-M/label")
expect(r.status_code == 200 and r.headers["content-type"] == "image/png", "label PNG generated")
decoded = zbar_decode(PILImage.open(BytesIO(r.content)))
expect(len(decoded) == 1 and decoded[0].type == "CODE128" and decoded[0].data.decode() == "AS1_M",
       f"barcode decodes back to AS1_M, got {[(d.type, d.data) for d in decoded]}")
r = client.get("/api/products/AS1/barcode")
product_decoded = zbar_decode(PILImage.open(BytesIO(r.content)))
expect(r.status_code == 200 and len(product_decoded) == 1
       and product_decoded[0].type == "CODE128"
       and product_decoded[0].data.decode() == "PRODUCT:AS1",
       f"single product barcode decodes to design lookup, got {[(d.type, d.data) for d in product_decoded]}")
r = client.get("/api/products/AS1/qr")
qr_decoded = zbar_decode(PILImage.open(BytesIO(r.content)))
expect(r.status_code == 200 and len(qr_decoded) == 1 and qr_decoded[0].type == "QRCODE"
       and qr_decoded[0].data.decode() == "PRODUCT:AS1",
       f"separate product QR decodes to the same design lookup, got {[(d.type, d.data) for d in qr_decoded]}")

print("30. Restock requirement is the shortfall between pending orders and physical stock")
client.post("/api/products", json={"sku": "RS1", "name": "Restock Shortfall", "variants": [
    {"size": "M", "initial_stock": 2, "reorder_threshold": 1},
]})
shortfall_upload = post_orders(make_xlsx(["SKU", "Size", "Quantity"], [("RS1", "M", 5)]), "restock_shortfall.xlsx")
shortfall = next(i for i in client.get("/api/reorder-list").json() if i["sku"] == "RS1")
expect(shortfall["current_stock"] == 2 and shortfall["ordered_qty"] == 5 and shortfall["required_qty"] == 3,
       "physical stock 2 against pending orders 5 yields restock required 3")
shortfall_id = shortfall_upload.json()["order_upload_id"]
ready_history = next(i for i in client.get("/api/orders/history", params={"limit": 200}).json()
                     if i["order_upload_id"] == shortfall_id)
expect(ready_history["dispatch_state"] == "PARTIAL" and ready_history["dispatchable_units"] == 2
       and ready_history["lines"][0]["dispatchable_qty"] == 2,
       "order history shows a partial ready amount when only two of five pieces are stocked")
partial_dispatch = client.post(f"/api/orders/{shortfall_id}/dispatch")
expect(partial_dispatch.status_code == 200 and partial_dispatch.json()["dispatched_units"] == 2
       and partial_dispatch.json()["pending_units"] == 3,
       "dispatch deducts available pieces instead of blocking the whole batch")
expect(client.get("/api/variants/RS1-M").json()["current_stock"] == 0,
       "partial dispatch deducts only the two physically available pieces")
partial_history = next(i for i in client.get("/api/orders/history", params={"limit": 200}).json()
                       if i["order_upload_id"] == shortfall_id)
expect(partial_history["status"] == "PENDING" and partial_history["pending_units"] == 3
       and partial_history["dispatched_units"] == 2
       and partial_history["lines"][0]["status"] == "PARTIALLY_DISPATCHED",
       "the undispatched three pieces remain pending after a partial dispatch")
partial_reorder = next(i for i in client.get("/api/reorder-list").json() if i["sku"] == "RS1")
expect(partial_reorder["ordered_qty"] == 3 and partial_reorder["dispatched_qty"] == 2
       and partial_reorder["required_qty"] == 3,
       "reorder quantity follows the remaining order and does not double-count dispatched pieces")
client.post("/api/inventory/adjust", json={"variant_code": "RS1-M", "new_count": 5, "note": "restock before dispatch"})
ready_again = next(i for i in client.get("/api/orders/history", params={"limit": 200}).json()
                   if i["order_upload_id"] == shortfall_id)
expect(ready_again["dispatch_state"] == "READY" and ready_again["dispatchable_units"] == 3
       and ready_again["lines"][0]["dispatchable_qty"] == 3,
       "restocking changes the pending line to green-ready for its remaining quantity")
second_dispatch = client.post(f"/api/orders/{shortfall_id}/dispatch")
expect(second_dispatch.status_code == 200 and second_dispatch.json()["dispatched_units"] == 3
       and second_dispatch.json()["pending_units"] == 0
       and client.get("/api/variants/RS1-M").json()["current_stock"] == 2,
       "a later dispatch ships only the remaining three pieces and leaves surplus stock untouched")

print("30.1 Selected order lines dispatch independently and keep the rest pending")
client.post("/api/products", json={"sku": "SEL1", "name": "Selected Dispatch", "variants": [
    {"size": "S", "initial_stock": 1}, {"size": "M", "initial_stock": 0},
]})
selected_upload = post_orders(make_xlsx(["SKU", "Size", "Quantity"], [("SEL1", "S", 2), ("SEL1", "M", 3)]), "selected_dispatch.xlsx")
selected_id = selected_upload.json()["order_upload_id"]
selected_history = next(i for i in client.get("/api/orders/history", params={"limit": 200}).json()
                        if i["order_upload_id"] == selected_id)
selected_s_line = next(line for line in selected_history["lines"] if line["size"] == "S")
selected_m_line = next(line for line in selected_history["lines"] if line["size"] == "M")
selected_dispatch = client.post(f"/api/orders/{selected_id}/dispatch", json={"line_ids": [selected_s_line["id"]]})
expect(selected_dispatch.status_code == 200 and selected_dispatch.json()["dispatched_units"] == 1
       and selected_dispatch.json()["pending_units"] == 4,
       "selected line dispatch uses its available piece without dispatching another size")
selected_after = next(i for i in client.get("/api/orders/history", params={"limit": 200}).json()
                      if i["order_upload_id"] == selected_id)
expect(next(line for line in selected_after["lines"] if line["id"] == selected_s_line["id"])["pending_qty"] == 1
       and next(line for line in selected_after["lines"] if line["id"] == selected_m_line["id"])["pending_qty"] == 3
       and client.get("/api/variants/SEL1-S").json()["current_stock"] == 0
       and client.get("/api/variants/SEL1-M").json()["current_stock"] == 0,
       "unselected size demand stays pending and no stock is deducted from it")
selected_delete = client.delete(f"/api/orders/{selected_id}")
expect(selected_delete.status_code == 200 and selected_delete.json()["restored_units"] == 1
       and client.get("/api/variants/SEL1-S").json()["current_stock"] == 1,
       "deleting a partially dispatched batch restores only the pieces already sent")

print("30.2 Shared stock is allocated across pending batches without double-ready totals")
client.post("/api/products", json={"sku": "FIFO1", "name": "FIFO Dispatch", "variants": [
    {"size": "M", "initial_stock": 3},
]})
fifo_old = post_orders(make_xlsx(["SKU", "Size", "Quantity"], [("FIFO1", "M", 2)]), "fifo_older.xlsx")
fifo_new = post_orders(make_xlsx(["SKU", "Size", "Quantity"], [("FIFO1", "M", 3)]), "fifo_newer.xlsx")
fifo_history = client.get("/api/orders/history", params={"limit": 200}).json()
fifo_old_row = next(i for i in fifo_history if i["order_upload_id"] == fifo_old.json()["order_upload_id"])
fifo_new_row = next(i for i in fifo_history if i["order_upload_id"] == fifo_new.json()["order_upload_id"])
expect(fifo_old_row["dispatch_state"] == "READY" and fifo_old_row["dispatchable_units"] == 2
       and fifo_new_row["dispatch_state"] == "PARTIAL" and fifo_new_row["dispatchable_units"] == 1
       and fifo_old_row["dispatchable_units"] + fifo_new_row["dispatchable_units"] == 3,
       "older pending demand receives first claim and visible ready pieces never exceed physical stock")

print("30a. Legacy negative stock is carried into the reorder quantity")
client.post("/api/products", json={"sku": "NEGRESTOCK", "name": "Negative Balance Recovery", "variants": [
    {"size": "S", "initial_stock": 4},
]})
negative_upload = post_orders(make_xlsx(["SKU", "Size", "Quantity"], [("NEGRESTOCK", "S", 4)]), "negative_restock.xlsx")
expect(client.post(f"/api/orders/{negative_upload.json()['order_upload_id']}/dispatch").status_code == 200,
       "dispatch baseline batch before simulating a legacy negative balance")
db = SessionLocal()
try:
    negative_variant = db.query(main.models.Variant).filter_by(variant_code="NEGRESTOCK_S").one()
    negative_variant.current_stock = -2  # reproduce a pre-fix/legacy over-dispatch cache
    db.commit()
finally:
    db.close()
negative_reorder = next(i for i in client.get("/api/reorder-list").json() if i["variant_code"] == "NEGRESTOCK_S")
expect(negative_reorder["ordered_qty"] == 0 and negative_reorder["current_stock"] == -2
       and negative_reorder["required_qty"] == 2,
       "negative physical balance with no pending demand still requests enough restock to reach zero")

print("30b. Daily sales counts dispatched units and excludes pending orders")
client.post("/api/products", json={"sku": "SALESTODAY", "name": "Sales Summary Test", "variants": [
    {"size": "M", "initial_stock": 100},
]})
sales_file = make_xlsx(["SKU", "Size", "Quantity"], [("SALESTODAY", "M", 50)])
sales_upload = post_orders(sales_file, "sales_today.xlsx")
pending_sales_before = client.get("/api/analytics/daily-sales").json()
expect(pending_sales_before["dispatched_units"] == 0 or all(p["sku"] != "SALESTODAY" for p in pending_sales_before["top_products"]),
       "uploaded pending orders are not counted as today's sales")
expect(client.post(f"/api/orders/{sales_upload.json()['order_upload_id']}/dispatch").status_code == 200,
       "today's sales test batch dispatches")
sales_today = client.get("/api/analytics/daily-sales").json()
sales_product = next((p for p in sales_today["top_products"] if p["sku"] == "SALESTODAY"), None)
expect(sales_today["timezone"] == "Asia/Kolkata" and sales_today["dispatched_units"] >= 50
       and sales_today["dispatch_lines"] >= 1 and sales_today["designs_sold"] >= 1
       and sales_product is not None and sales_product["units"] == 50,
       "daily sales reports IST date, dispatched units, lines, designs and per-product units")
today_history = next(i for i in client.get("/api/orders/history", params={"limit": 200}).json()
                    if i["order_upload_id"] == sales_upload.json()["order_upload_id"])
expect(today_history["status"] == "DISPATCHED" and today_history["fulfilled_count"] == 1
       and today_history["pending_count"] == 0
       and today_history["lines"][0]["qty_fulfilled"] == 50,
       "order history records the dispatch and fulfilled quantity accurately")
sales_before_delete = sales_today["dispatched_units"]
deleted_sales = client.delete(f"/api/orders/{sales_upload.json()['order_upload_id']}")
expect(deleted_sales.status_code == 200 and deleted_sales.json()["restored_units"] == 50,
       "deleting a dispatched batch reverses its inventory deduction")
expect(client.get("/api/variants/SALESTODAY-M").json()["current_stock"] == 100,
       "dispatch stock is restored when its batch is removed")
sales_after_delete = client.get("/api/analytics/daily-sales").json()
expect(sales_after_delete["dispatched_units"] == sales_before_delete - 50
       and all(p["sku"] != "SALESTODAY" for p in sales_after_delete["top_products"]),
       "deleted dispatch is removed from daily sales totals and product ranking")
expect(all(i["sku"] != "SALESTODAY" for i in client.get("/api/reorder-list").json()),
       "deleted batch no longer contributes pending or fulfilled reorder demand")
expect(client.delete(f"/api/orders/{sales_upload.json()['order_upload_id']}").status_code == 404,
       "a removed batch is no longer present for subsequent delete requests")
reprocessed_sales = post_orders(sales_file, "sales_today.xlsx")
expect(reprocessed_sales.status_code == 200,
       "deleting a processed batch releases the file hash for a corrected re-upload")

print("30c. Dispatch rechecks atomic stock and rolls back the whole batch on a shortage")
client.post("/api/products", json={"sku": "RACE1", "name": "Concurrent Dispatch Guard", "variants": [
    {"size": "S", "initial_stock": 5}, {"size": "M", "initial_stock": 2},
]})
race_upload = post_orders(make_xlsx(["SKU", "Size", "Quantity"], [("RACE1", "S", 2), ("RACE1", "M", 2)]), "dispatch_race.xlsx")
original_apply_stock_change = main.inventory_service.apply_stock_change
def simulate_stock_change_then_dispatch(db, variant, delta, **kwargs):
    if variant.variant_code == "RACE1_M" and kwargs.get("created_by") == "dispatch":
        variant.current_stock = 1
        db.flush()
    return original_apply_stock_change(db, variant, delta, **kwargs)
main.inventory_service.apply_stock_change = simulate_stock_change_then_dispatch
try:
    guarded_dispatch = client.post(f"/api/orders/{race_upload.json()['order_upload_id']}/dispatch")
finally:
    main.inventory_service.apply_stock_change = original_apply_stock_change
expect(guarded_dispatch.status_code == 409 and "batch was not dispatched" in guarded_dispatch.json()["detail"].lower(),
       "stock shortage after preliminary validation returns a clear 409")
expect(client.get("/api/variants/RACE1_S").json()["current_stock"] == 5
       and client.get("/api/variants/RACE1_M").json()["current_stock"] == 2,
       "a failed multi-size dispatch rolls back every deduction")
race_history = next(i for i in client.get("/api/orders/history", params={"limit": 200}).json()
                   if i["order_upload_id"] == race_upload.json()["order_upload_id"])
expect(race_history["status"] == "PENDING" and race_history["pending_count"] == 2,
       "a blocked batch remains pending and visible in order history")

print("30b. Railway root opens the app and variant SKUs group by their final size")
root = client.get("/", follow_redirects=False)
expect(root.status_code == 307 and root.headers.get("location") == "/app/",
       "service root redirects directly to the inventory app")

print("31. Bulk product template and size-wise Excel import")
template = client.get("/api/products/bulk-template")
expect(template.status_code == 200 and "bulk_product_template.xlsx" in template.headers.get("content-disposition", ""),
       "bulk template downloads as an Excel workbook")
template_wb = load_workbook(BytesIO(template.content), read_only=True)
expect(list(next(template_wb["Products"].iter_rows(values_only=True))) ==
       ["SKU", "Product Name", "Category", "Size", "Stock", "Alert Below", "Target Stock"],
       "template has SKU, product, category and size-wise stock columns")
expect("Instructions" in template_wb.sheetnames, "template includes fill-in instructions")
bulk_products = make_xlsx(
    ["SKU", "Product Name", "Category", "Size", "Stock", "Alert Below", "Target Stock"],
    [
        ("BLK_OK", "Bulk Linen Set", "Ethnic", "S", 5, 2, 12),
        ("BLK_OK", "Bulk Linen Set", "Ethnic", "M", 0, 2, 12),
        ("BLK_BAD", "Incomplete Set", "Ethnic", "S", None, 2, 12),
        ("BLK_BAD", "Incomplete Set", "Ethnic", "M", 3, 2, 12),
        ("BLK_DUP", "Duplicate Size", "Ethnic", "L", 1, 2, 12),
        ("BLK_DUP", "Duplicate Size", "Ethnic", "L", 2, 2, 12),
        ("T1", "Existing SKU", "Ethnic", "XL", 2, 2, 12),
    ],
)
bulk_result = client.post("/api/products/bulk", files={"file": ("bulk_products.xlsx", bulk_products, XLSX)})
expect(bulk_result.status_code == 200, f"bulk import responds, got {bulk_result.status_code}: {bulk_result.text[:180]}")
bulk_body = bulk_result.json()
expect(bulk_body["products_created"] == 1 and bulk_body["variants_created"] == 3 and bulk_body["rows_skipped"] == 4,
       f"valid SKU grouped by size; invalid and existing groups skipped, got {bulk_body}")
expect(len(bulk_body["errors"]) == 4, "row errors explain the invalid stock and duplicate size")
bulk_product = next(p for p in client.get("/api/products").json() if p["sku"] == "BLK_OK")
bulk_sizes = {variant["size"]: variant for variant in bulk_product["variants"]}
expect(bulk_product["image_path"] is None and bulk_sizes["S"]["current_stock"] == 5
       and bulk_sizes["M"]["current_stock"] == 0, "size-wise opening stock imports and image stays ready for later editing")
expect("BLK_OK_M" in {alert["variant_code"] for alert in client.get("/api/alerts").json()},
       "zero-stock imported size enters normal alert flow")
expect("XL" in {variant["size"] for variant in next(p for p in client.get("/api/products").json() if p["sku"] == "T1")["variants"]},
       "a new size can be added to an existing design without changing its old sizes")
repeat_bulk = client.post("/api/products/bulk", files={"file": ("repeat.xlsx", make_xlsx(
    ["SKU", "Product Name", "Size", "Stock"], [("BLK_OK", "Bulk Linen Set", "M", 4)]), XLSX)})
expect(repeat_bulk.status_code == 200 and repeat_bulk.json()["products_created"] == 0
       and repeat_bulk.json()["variants_created"] == 0 and repeat_bulk.json()["rows_skipped"] == 1,
       "re-upload leaves an existing size and its stock unchanged")
suffix_products = make_xlsx(
    ["SKU", "Category", "Size", "Current Inventory"],
    [("ZIA-MUSTARD-XXL", "Kurta Set", "XXL", 2), ("ZIA-MUSTARD-XL", "Kurta Set", "XL", 4), ("ZIA-MUSTARD-S", "Kurta Set", "S", 7)],
)
suffix_result = client.post("/api/products/bulk", files={"file": ("master_inventory.xlsx", suffix_products, XLSX)})
expect(suffix_result.status_code == 200 and suffix_result.json()["products_created"] == 1
       and suffix_result.json()["variants_created"] == 3, "size-suffixed master SKUs create one design with size-wise current inventory")
zia = next(p for p in client.get("/api/products").json() if p["sku"] == "ZIA-MUSTARD")
expect({v["size"]: v["current_stock"] for v in zia["variants"]} == {"XXL": 2, "XL": 4, "S": 7},
       "Current Inventory, Category and size columns import without a Product Name column")
alias_result = client.post("/api/products/bulk", files={"file": ("aliases.xlsx", make_xlsx(
    ["SKU", "Category", "Size", "Current Inventory"],
    [("ALIAS-DESIGN", "Kurta Set", "XXL", 10), ("ALIAS-DESIGN_XXL", "KURTA SET", "XXL", 10)],
), XLSX)})
expect(alias_result.status_code == 200 and alias_result.json()["duplicate_rows_merged"] == 1,
       "base SKU and explicit size SKU for the same stock row are combined")
alias_order = post_orders(make_xlsx(["SKU", "Size", "Quantity"], [("ALIAS-DESIGN", "XXL", 1)]), "alias_order.xlsx")
expect(alias_order.status_code == 200 and alias_order.json()["not_found_count"] == 0,
       "the alternate source SKU remains usable when matching incoming orders")
free_size_result = client.post("/api/products/bulk", files={"file": ("free_size.xlsx", make_xlsx(
    ["SKU", "Category", "Size", "Current Inventory"], [("M-217", "Kurta Set", None, 0)],
), XLSX)})
expect(free_size_result.status_code == 200 and free_size_result.json()["free_size_rows"] == 1,
       "a blank size is retained as a FREE SIZE variant instead of dropped")
free_size_product = client.get("/api/products/M-217").json()
expect(free_size_product["variants"][0]["size"] == "FREE SIZE"
       and free_size_product["variants"][0]["variant_code"] == "M-217",
       "one-size variants remain scannable and order-matchable by their source SKU")
legacy = client.post("/api/products", json={"sku": "OLD-DESIGN-XL", "name": "Old Design", "variants": [{"size": "XL", "initial_stock": 8}]})
expect(legacy.status_code == 200, "legacy one-size product created for merge regression")
merge_result = client.post("/api/products/bulk", files={"file": ("legacy.xlsx", make_xlsx(
    ["SKU", "Product Name", "Size", "Stock"], [("OLD-DESIGN-XL", "Old Design", "XL", 0)]), XLSX)})
expect(merge_result.status_code == 200 and merge_result.json()["variants_merged"] == 1,
       "old one-size product joins its design while preserving the existing size record")
old_product = client.get("/api/products/OLD-DESIGN").json()
expect(old_product["variants"][0]["current_stock"] == 8 and old_product["variants"][0]["variant_code"] == "OLD-DESIGN-XL",
       "legacy stock and variant identity survive product grouping")
expect(client.get("/api/alerts?q=ZIA-MUSTARD-XXL").status_code == 200
       and any(a["variant_code"] == "ZIA-MUSTARD-XXL" for a in client.get("/api/alerts?q=ZIA-MUSTARD-XXL").json()),
       "alerts can be searched by the exact imported variant SKU")
grouped_alerts = client.get("/api/alerts/groups?q=ZIA-MUSTARD&limit=1").json()
expect(grouped_alerts["total"] == 1 and grouped_alerts["items"][0]["sku"] == "ZIA-MUSTARD"
       and {a["size"] for a in grouped_alerts["items"][0]["items"]} == {"XL", "XXL"},
       "alerts group multiple affected sizes under one design and paginate by design")
expect(len(client.get("/api/products?limit=1&offset=1").json()) == 1,
       "product search supports previous/next page offsets")
product_page = client.get("/api/products", params={"limit": 2, "offset": 1, "paged": "true"}).json()
expect(product_page["total"] >= 3 and len(product_page["items"]) == 2 and product_page["offset"] == 1,
       "product pager returns exact total and page range data")
reorder_page = client.get("/api/reorder-list?limit=1&offset=0").json()
expect(reorder_page["limit"] == 1 and len(reorder_page["items"]) == 1,
       "reorder list supports page-sized responses while legacy list remains available")
history_page = client.get("/api/orders/history", params={"limit": 2, "offset": 0, "paged": "true"}).json()
expect(history_page["total"] >= 2 and len(history_page["items"]) == 2,
       "order history pager returns exact total count")
transaction_page = client.get("/api/transactions", params={"limit": 2, "offset": 0, "paged": "true"}).json()
expect(transaction_page["total"] >= 2 and len(transaction_page["items"]) == 2,
       "stock history pager returns exact total count")
bad_template = client.post("/api/products/bulk", files={"file": ("wrong.xlsx", make_xlsx(["SKU", "Size"], [("X", "M")]), XLSX)})
expect(bad_template.status_code == 400 and "Missing required columns" in bad_template.json()["detail"],
       "wrong format returns a clear template-column error")

print("\nALL CHECKS PASSED")
