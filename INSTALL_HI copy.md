# MYRAA Inventory — Costing, SKU Mapping & Purchase Challans v1

## IMPORTANT BEFORE DEPLOYING
**Back up your production inventory database and product images.** Your earlier Railway logs showed `DATABASE_URL: MISSING` and an SQLite file at `/app/backend/inventory.db`. Without a persistent Railway volume or Postgres, redeployment can lose inventory, costs and challans. Do **not** switch DATABASE_URL to a new empty database without migrating your old inventory. This patch does not perform a destructive migration.

## New files (6)
- `backend/business_models.py` — five new tables: product/size costs, exact marketplace alias, purchase challans, purchase lines
- `backend/business_api.py` — costing (including all-design search), alias, supplier upload/receipt/push REST endpoints
- `frontend/business-workspace.js` — costing chart + searchable paginated all-design costing, product edit controls, purchase workflow
- `frontend/business-workspace.css` — matching light/dark/mobile styling
- `frontend/assets/purchase-challan-template.xlsx` — user-downloadable Excel import template
- `tests/test_business_workflows.py` — offline tests

## Existing files changed (7)
- `backend/models.py` — additive ORM relations only
- `backend/schemas.py` — optional cost and SKU alias fields
- `backend/main.py` — registers new API and saves costs/aliases in product creation transaction
- `backend/services/excel_service.py` — exact external-SKU matching before fuzzy resolution
- `backend/services/packing_picklist_service.py` — explicit white fill and black borders, grid lines off
- `frontend/app.js` — add-product costs/aliases, size scroll, mobile drawer close, hooks
- `frontend/index.html` — purchase challan page/sidebar and additional form inputs/styles/scripts

## INSTALL (VS Code)
1. Open latest GitHub project, back up database/images.
2. Copy each patch file to the exact matching relative path, maintaining the folder tree. **Do not overwrite a newer `main.py`, `index.html`, `app.js` or `excel_service.py` blindly**; compare `MYRAA_Business_v1.diff` first if there have been other edits since your uploaded ZIP + Packing v3.
3. Ensure all original app files remain. New packages beyond the project's existing requirements are not needed.
4. Run offline tests: `python -m unittest discover -s tests -p test_business_workflows.py -v` and `python -m unittest discover -s tests -p test_packing_picklist.py -v`.
5. Git commands: `git status`, `git add backend/ frontend/ tests/test_business_workflows.py`, `git commit -m "MYRAA costing challans master SKU mapping"`, `git push origin main`.
6. Railway automatically creates **only the five new tables** on startup via existing `Base.metadata.create_all()`; existing stock/order tables are unchanged. Deployment still needs verification.

## How to use
**Products > Add Product**: Master SKU stays the internal design code. Optional default unit cost and size-specific unit costs; add multiple external SKU mappings, one line each: `S | Myntra | EXTERNAL-SKU-S`. Master SKU stays visible; marketplace codes map to the exact size.
**Products > Product Detail**: Edit default + size unit cost. Add/delete external SKU mappings. Upload more photographs after creation without replacing the original front image. Save costing separately from basic product name/category.
**Dashboard**: "Stock Investment at Cost" shows known-valued counted inventory, a per-design bar chart, a paginated searchable list of **all designs**, and a warning for pieces with missing costs. Valuation is purchasing cost × current stock, *not selling value, profit or revenue*.
**Purchase Challans**: Import an XLSX, XLS, or CSV with columns `Master SKU`, `Product Name`, `Size`, `Qty`, `Unit Cost`, optional `Category`. Supply Party/Supplier, challan/invoice number and order date. Duplicate uploads are rejected. Uploading only stages a supplier order. Enter or mark received quantities per size, save receipt. Pending pieces and known pending cost value update (unpriced pieces are flagged). Only when **all received and each cost set** does "Push" add stock once (with inventory transactions) or create missing products/sizes. The second push is rejected. Existing stock uses weighted-average unit cost. Challan prices are snapshots; no automatic supplier payments, GST/accounting, or invoice PDF generation.
**Packing Picklist**: Print remains 22pt bold and 38pt rows; changes are white fill, explicit black borders and no gridlines.

## Safeguards and limitations
- No existing Process Orders import/dispatch/deduction logic rewritten. Order aliases are now checked on exact match before fuzzy match. An alias cannot identify another variant.
- All purchase-inventory pushes operate in a database transaction. A partial receipt never pushes. Missing costs block push; price existing stock before receiving new pieces, so totals are not misleading.
- Old products with no costing show an explicit unpriced warning, never guessed values. Supplier amount total excludes lines without unit cost.
- Uploaded product photos require persistent `PRODUCT_IMAGE_DIR`. A Railway filesystem without a volume is ephemeral.
- These are offline-tested implementation files, not live-deployed changes. You must verify in Railway after copying.
- Avoid committing `.env`, API keys, DB files, or user-order Excel files to Git.

## Railway smoke tests
On your Railway shell:
`cd /app/backend && /app/.venv/bin/python -c "from database import engine; from sqlalchemy import inspect; print([x for x in inspect(engine).get_table_names() if x.startswith('myraa_')])"`
Should list five `myraa_` tables.
Open web UI and test with a temporary SKU + a small sample challan. First try partial receipt (push must reject); then full receipt (stock rises only after Push), check cost chart, mapping and additional photos. Test Process Orders using a known external SKU and verify it maps to one size; do not dispatch actual customer orders just for testing.
