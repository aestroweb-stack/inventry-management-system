# MYRAA Inventory MCP CRUD Upgrade — Railway + Gemini

**Based on:** user-uploaded Inventory Management source with existing FastMCP (`backend/inventory_mcp.py`) and existing Gemini tool bridge (`backend/services/cloud_assistant_service.py`). This is an **incremental upgrade**, not a new inventory backend. It does not modify `backend/main.py`, database models, order import/dispatch flow, frontend styling, or photo search.

## What this adds

Existing FastMCP: `search_inventory`, `get_product`, `get_inventory_summary`, alerts, stock history, reorder/order reads, `prepare_inventory_action`, and `confirm_inventory_action`.

New **easy-to-call** typed MCP preview tools:

| Tool | Behavior |
|---|---|
| `prepare_create_product` | Product CREATE preview |
| `prepare_update_product` | Product UPDATE preview |
| `prepare_archive_product` | Product DELETE (moves to Trash) preview |
| `prepare_restore_product` | Restore product preview |
| `prepare_add_size` | Create size preview |
| `prepare_archive_size` | Archive size preview |
| `prepare_restore_size` | Restore size preview |
| `prepare_change_stock` | Stock `add`, `remove`, `set` preview |

**No preview tool commits data.** Each uses the existing `_validate_prepare` and 10-minute signed confirmation token pipeline. The MYRAA chat returns a preview; the operator reviews it and presses the existing **Confirm change** button. The existing separate `/api/assistant/confirm` route commits after revalidation. Product "delete" remains recoverable in Trash; it is **not** a hard delete.

`Process Orders` stays exactly as before: marketplace import → SKU matching → dispatch. Existing `prepare_inventory_action` can still prepare order import and dispatch; no order business code was changed.

## Copy/paste files in VS Code

1. **Back up** your existing `backend/inventory_mcp.py` and `backend/services/cloud_assistant_service.py`.
2. Replace/merge just these files from the package:
   - `backend/inventory_mcp.py` (updated existing FastMCP server)
   - `backend/services/cloud_assistant_service.py` (updated Gemini tool declaration/dispatcher)
3. Add optional diagnostic file: `tools/check_myraa_mcp.py`.
4. `backend/main.py`, `frontend/`, `requirements.txt`, and `railway.toml` **do not need modification**, provided your existing deploy already mounts `/api/mcp/` and uses `mcp>=1.23,<2`.
5. If your current production source has newer edits (especially prior voice patch), review `MYRAA_MCP_CRUD_DIFF.patch` and **merge only the changed hunks**, rather than blindly overwriting.

## GitHub / Railway

```powershell
git status
git add backend/inventory_mcp.py backend/services/cloud_assistant_service.py tools/check_myraa_mcp.py
git commit -m "Add MYRAA typed MCP CRUD tools with confirmation"
git push origin main
```

Railway should auto-deploy from GitHub. Keep the already-working `MYRAA_CLOUD_MODEL` value; do not change it for this MCP upgrade.

Railway **Web Service → Variables**:

```dotenv
MYRAA_CLOUD_PROVIDER=gemini
GEMINI_API_KEY=<YOUR_EXISTING_PRIVATE_GEMINI_KEY>
MYRAA_CLOUD_MODEL=<YOUR_WORKING_MODEL>
DATABASE_URL=<REFERENCE_TO_RAILWAY_POSTGRES>
APP_PASSWORD=<PRIVATE_LOGIN_PASSWORD>
APP_SESSION_SECRET=<RANDOM_SECRET_32_PLUS_CHARS>
MYRAA_CONFIRMATION_SECRET=<DIFFERENT_RANDOM_SECRET_32_PLUS_CHARS>
INVENTORY_MCP_API_KEY=<DIFFERENT_RANDOM_SECRET_32_PLUS_CHARS>
```

Do not commit `.env` or real secrets. `INVENTORY_MCP_API_KEY` is for **external** MCP clients; browser MYRAA tool calls are done server-side and do not need this secret exposed in JavaScript. The in-app confirm tokens should use a different `MYRAA_CONFIRMATION_SECRET`.

If your public Railway hostname does **not** match `RAILWAY_PUBLIC_DOMAIN`, set `INVENTORY_MCP_ALLOWED_HOSTS` to the exact hostname **without** `https://`, e.g.:

```dotenv
INVENTORY_MCP_ALLOWED_HOSTS=inventry-management-system-production-787ff64f.up.railway.app
```

## Protected MCP URL

```
https://inventry-management-system-production-787ff64f.up.railway.app/api/mcp/
```

Transport: **Streamable HTTP**. Pass the private `INVENTORY_MCP_API_KEY` in an **Authorization: Bearer** header (or `x-api-key`) from a trusted external MCP client. The URL is not a normal webpage. It is deliberately inaccessible without authorization.

## Read-only verification from Windows PowerShell

```powershell
py -m pip install "mcp>=1.23,<2" httpx
$env:INVENTORY_MCP_API_KEY = "PASTE_YOUR_EXISTING_RAILWAY_MCP_KEY_HERE"
py tools/check_myraa_mcp.py https://inventry-management-system-production-787ff64f.up.railway.app
Remove-Item Env:INVENTORY_MCP_API_KEY
```

Expected: 8 new `prepare_*` tools appear, then the inventory summary read succeeds. The check script **does not commit writes**. If it fails with 401, verify the key; if 404, verify the 32+ character length and server path/allowed host; if 403, verify allowed hosts; if connection fails, inspect Railway Deploy Logs.

## MYRAA browser chat / microphone examples

- READ: `Mere inventory mein total kitne products hain?`
- READ: `SKU TEST-001 ke saare sizes ka stock dikhao.` (use a real SKU)
- CREATE: `SKU TEST-001, Test Kurta, sizes S M L, ek naya product banao.`
- UPDATE: `SKU TEST-001 ka naam Test Cotton Kurta karo.`
- STOCK: `SKU TEST-001 size M mein 5 pieces add karo.`
- COUNT: `SKU TEST-001 size M ka verified stock 25 set karo.`
- DELETE (archive): `SKU TEST-001 ko Trash mein bhejo.`
- RESTORE: `SKU TEST-001 ko Trash se restore karo.`

Only run examples on test inventory and **check the exact preview** before clicking Confirm. For stock write use the exact variant code that MYRAA finds. SKU/size ambiguity should cause a clarifying question, not an automatic guess. If you use the small Gemini free-tier model, keep prompts specific; free-tier model capabilities and quotas can vary.

## Security boundaries

- MYRAA's Gemini model can **prepare** actions; its offered function declarations do not include a commit tool.
- The browser's Confirm button is separate; the API rechecks expected stock before accepting changes.
- External trusted MCP clients may call `confirm_inventory_action` if they hold a valid confirmation token **and** API key. They must enforce explicit operator approval themselves; the server cannot independently prove that a human clicked approval.
- Product delete means archive to Trash. **Permanent deletion is not exposed in this patch.**
- Signed tokens expire after 10 minutes. As with any write API, back up PostgreSQL before production changes and avoid retrying a successful confirmation blindly.

## Verification scope

Offline tests cover eight CRUD wrappers, Gemini tool availability, dispatcher routing and confirmation-preview extraction. They do **not** use a live Railway/Gemini secret, so actual remote bookability/availability cannot be claimed until the read-only test is run against your deployed service.
