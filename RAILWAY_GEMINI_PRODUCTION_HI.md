# MYRAA Inventory — Railway + Gemini (production guide)

## Kya update hua?

- Existing FastAPI / SQLite–PostgreSQL models, inventory CRUD, order processing, reorder, alerts, barcode scanning, print labels, archive/trash, inventory history and size-specific operations **preserved**.
- Hosted MYRAA now prefers **Google Gemini API**, not a local Gemini desktop app or localhost Node/Python companion. FastAPI calls the Gemini generateContent API from Railway. Default model `gemini-3.8-flash` (verified at time of implementation, configurable).
- Inventory MCP exists already: `backend/inventory_mcp.py`, mounted at `/api/mcp/` with its separate private API key. The hosted Gemini integration uses the same guarded Python tool handlers directly; it does not need an external MCP host or local companion.
- **Voice:** Chrome/Edge (where `SpeechRecognition` is available) records/transcribes on user click, then sends text through the *same* hosted Gemini tool loop. Optional browser text-to-speech speaks answers. This is browser STT/TTS, not server-side streaming speech; browser speech services may use their own infrastructure. Microphone permissions and HTTPS are required.
- **Vision:** `POST /api/assistant/identify-photo` lets Gemini identify garment attributes and search live catalog names/categories. It returns **possible suggestions only**, never a verified SKU. The original `/api/products/visual-match` retains SigLIP matching when configured and falls back to Gemini suggestions if SigLIP is unavailable. Do not auto-change inventory from image recognition.
- **UI:** Green/cream apparel dashboard, responsive 11 sidebar views, product cards, persistent large-screen assistant panel, voice/language/photo controls, dark mode, real API-driven metrics, image hero; *no fabricated sales or product data*.
- **Authentication:** Cookie-based operator login for app API with `APP_PASSWORD`, strict SameSite HttpOnly cookie, same-origin check on writes; `/api/mcp/` still requires 32+ character `INVENTORY_MCP_API_KEY`. This is a **single-operator login**, not role-based access control. For multiple staff roles use an external identity provider/proxy.

## Railway setup (from this ZIP or GitHub)

1. Unzip source, push repository to your private GitHub repository. Do **not** commit `.env` or real keys.
2. In Railway: New Project → GitHub Repo → select repository; Railway uses root `requirements.txt` and `railway.toml`. Root directory is repository root, not `/backend`.
3. Add **PostgreSQL** service in the same Railway project. In web service Variables set `DATABASE_URL` as a **reference variable** to PostgreSQL `DATABASE_URL` (Railway UI reference picker). Do not use the ephemeral SQLite file for real inventory.
4. Add **Railway Volume** mounted at `/data`, then web service Variables `PRODUCT_IMAGE_DIR=/data/product_images` so images survive redeploys. Take independent database and image backups.
5. Set Variables (minimum production set):

   ```text
   MYRAA_CLOUD_PROVIDER=gemini
   GEMINI_API_KEY=<secret from Google AI Studio>
   MYRAA_CLOUD_MODEL=gemini-3.8-flash
   APP_PASSWORD=<long unique operator password>
   APP_SESSION_SECRET=<random long secret>
   MYRAA_CONFIRMATION_SECRET=<random 32+ character secret>
   INVENTORY_MCP_API_KEY=<random 32+ character secret>
   DATABASE_URL=<Railway Postgres reference variable>
   PRODUCT_IMAGE_DIR=/data/product_images
   ```

   Get your Gemini API key at **Google AI Studio** (`https://aistudio.google.com/apikey`). API quota and billing depend on your model and Google account; enable billing if required for the selected tier. **Never** put the key into frontend code. No Anthropic key is needed.
6. Deploy with the generated Railway public domain. Railway will supply `$PORT`; start command from `railway.toml` is `cd backend && uvicorn main:app --host 0.0.0.0 --port $PORT`. Health path `/api/health`. App home `/` redirects to `/app/`.
7. Open `https://<your-domain>/app/` → enter `APP_PASSWORD` → inspect live Dashboard. Test typed chat and browser microphone (Chrome/Edge):
   - “Ivory Kurta ka size M stock kitna hai?” → read-only live lookup.
   - “Ivory Kurta size M mein 5 pieces add karo” → Gemini proposes a *prepared* stock change. **Confirm change** button is required for commit. The change and history can then be verified in the UI.
   - “Ek naya product create karo SKU KUR-TEST-100, size S M L.” → preview and explicit confirmation. Similarly update, archive, restore, add/remove sizes, import and dispatch supported by the existing tool handlers.
   - Use **Identify photo** or sidebar **Visual Product Search** to inspect suggestions. Upload real reference photos to products for stronger matching.

## Backend architecture

`frontend/app.js` → `POST /api/assistant/chat` → `backend/services/cloud_assistant_service.py` → `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent` → `inventory_mcp.py` read tools / `prepare_inventory_action` → signed action preview; only `POST /api/assistant/confirm` commits.

`POST /api/assistant/identify-photo` → `backend/services/claude_photo_service.py` (provider-aware, supports Gemini) → Gemini Vision garment description → candidate products from PostgreSQL (operator reviews; no write).

`/api/mcp/` remains an authenticated Streamable HTTP MCP endpoint for optional external integrations. For the hosted assistant, internal Python tools are faster and avoid exposing MCP to Gemini via internet; **no new MCP server is necessary**.

## Important limitations and operations

- Railway app hosting and Gemini API usage are charged separately. Gemini app subscriptions do not automatically include Gemini API quota or billing. The model can be changed to a cheaper supported `MYRAA_CLOUD_MODEL` if usage grows.
- **Browser voice ≠ autonomous continuous speech service.** Browser speech recognition availability varies by engine, locale, permission and network. Chrome/Edge HTTPS recommended. If browser speech is unavailable, use typed chat. No local companion is required for browser voice-to-inventory commands.
- Direct desktop control (personal Chrome tabs, local files, WhatsApp desktop) still requires a local companion; Railway server **cannot** control a visitor's PC. This upgrade does **not** make that local-computer functionality remote.
- For exact image-to-image identification (front/model photo vs folded/packaged photo) a dedicated SigLIP 2 inference service, catalog embedding index/pgvector and a tested threshold are recommended. Gemini Vision fallback finds *keyword candidates*, not verified matches. The regular Railway app intentionally excludes Torch; optional `backend/requirements-visual-ai.txt` contains visual-model requirements.
- Existing `PRODUCT_IMAGE_DIR` must point to a persistent volume; `DATABASE_URL` must point to PostgreSQL. **Never run multiple SQLite web replicas**.
- Protect PostgreSQL and Railway variables; rotate credentials on suspected disclosure. The built-in password protects all `/api/*` routes except health and auth, while the MCP route has independent token authentication. If `APP_PASSWORD` is missing on Railway, app inventory API deliberately returns 503 instead of exposing write endpoints unauthenticated.
- This implementation supports one signed-in operator password, not per-user RBAC, two-factor auth or enterprise audit identity. For many warehouse staff, add centralized authentication, user-scoped roles, logout invalidation and permission checks per endpoint before wider rollout.
- Confirm tokens have time limits and stock precondition checks. Since there is no automatic stock adjustment from voice or image, a mistaken transcription can be reviewed before committing.

## Quick checks / test commands

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_myraa_production.py' -v
python tests/test_core_flow.py
cd backend && uvicorn main:app --host 127.0.0.1 --port 8080
```

Health: `GET /api/health` should return `{"status":"ok"}`. Assistant: after login, `GET /api/assistant/status` should return `cloud_enabled=true`, `provider="Gemini"` and model name. The UI will not attempt a localhost connection in hosted mode.

**Verification:** The offline unit tests and a mocked browser smoke test were run during modification. Live Gemini API calls, real Railway deploy, PostgreSQL and image volume connectivity need to be verified in *your* Railway project with your credentials; no live deployment claim is made here.
