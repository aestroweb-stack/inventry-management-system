# Railway: SigLIP 2 visual product search

## Current deployment: one Inventory service

The PostgreSQL production setup runs the inventory API and SigLIP 2 in the **same Railway Inventory service**. When the app is in SQLite local mode, it can use the private model service in the private-service section below instead; this does not require changing the inventory database. In either semantic mode, both the packaging/query photo and every saved catalog photo are embedded by the same SigLIP model.

1. Keep the existing Inventory service's `DATABASE_URL` and product-image volume unchanged. Do not point it at a new empty PostgreSQL database: SQLite rows and uploaded photos do not move automatically. Back up and migrate catalog, variants, orders, history, and image files before any database change.
2. Set Railway **Root Directory** to blank (repository root). Mount a persistent volume at `/data`, set `HF_HOME=/data/huggingface`, and keep the existing product-photo volume mounted at its current path. Set `PRODUCT_IMAGE_DIR` to the absolute mount path used for product photos (for example, `/data/product_images` only when that is where the photo volume is mounted). Without this variable the app saves photos under `backend/static/product_images`, which is local to the deploy and can disappear on redeploy.
3. Use the repository root as **Root Directory**. The root `requirements.txt` installs the CPU PyTorch wheel and SigLIP runtime dependencies during the normal Railway Python build. If you have a custom Build Command, use:

   ```sh
   pip install -r requirements.txt
   ```

4. Set **Start Command**:

   ```sh
   cd backend && python -m uvicorn main:app --host 0.0.0.0 --port $PORT
   ```

   Do not use `visual_model_service:app` as this service's start command; that would run only the model API and hide the inventory UI/API. Leave `VISUAL_AI_URL` unset to use the in-process model on PostgreSQL deployments. For SQLite deployments that need packaging-photo matching, set `VISUAL_AI_URL` and `VISUAL_AI_API_KEY` as described in the private-service section below. The first image search downloads weights and can take several minutes. PyTorch uses additional CPU/RAM. Mount a persistent volume at `/data` and set `HF_HOME=/data/huggingface` to cache weights across deploys.
5. Deploy, open **Visual Product Search**, and read the setup status shown above the photo-search message. If the status says old catalog photos are not indexed, run from this same Inventory service:

   ```sh
   cd /app/backend && python scripts/rebuild_visual_index.py
   ```

   This script must see the app's existing PostgreSQL database and mounted product photos. It only builds photo embeddings; it does not change stock or orders. Newly uploaded photos are indexed in the background automatically.

The model downloads from the public Hugging Face checkpoint; no token/API key is needed. The model is general-purpose, not fine-tuned on Twins Lady photos.

---

## Private split-service instructions (use when SQLite app needs semantic package-photo matching)

The app calls a separate, private Railway service for Google's public `google/siglip2-base-patch16-224` image encoder. The app remains on its small core dependency set, so model startup/inference does not block stock and order requests. The service downloads public model weights automatically at startup; no Hugging Face token is needed. Model files persist in a Railway volume. The model is general-purpose, not fine-tuned on Twins Lady photos.

## Before enabling on production

1. Deploy first to a staging Railway environment with a copy of the catalog and image volume. Do not run test uploads or index rebuilds against a production database without a restorable backup and an approved maintenance plan.
2. A PostgreSQL service showing **Online** is not yet proof that the app is using it. The previous Railway shell output showed `DATABASE_URL` was unset and `/app/backend/inventory.db` existed. Do not simply point the app at a new empty Postgres database: SQLite rows and uploaded photos do not move automatically. Back up and migrate the catalog, variants, orders, transaction history, and photo files first; verify counts in the target DB; only then set the app variable `DATABASE_URL=${{pgvector.DATABASE_URL}}` (replace `pgvector` with the exact service name). Do this in staging first. This visual-model service never connects to or edits the inventory database.

3. Keep product photos on a persistent volume mounted to the app service. Do not put the model cache on the app service. For the model service create a separate Railway service from this same GitHub repo and set its **Root Directory** to `/backend`.

4. On the model service add a persistent volume mounted at `/data`, and set:

   ```text
   HF_HOME=/data/huggingface
   VISUAL_AI_API_KEY=<generate-a-long-random-secret>
   ```

   Use the same random secret in both services. Keep it in Railway Variables, never `.env` committed to Git. `HF_HOME` stores automatically downloaded weights between deploys.

5. In the model service set Build Command:

   ```sh
   pip install --index-url https://download.pytorch.org/whl/cpu torch && pip install -r requirements-visual-ai.txt
   ```

   Set Start Command to `uvicorn visual_model_service:app --host 0.0.0.0 --port $PORT`.

6. In the app service set:

   ```text
   VISUAL_AI_URL=http://${{VisualModel.RAILWAY_PRIVATE_DOMAIN}}:${{VisualModel.PORT}}
   VISUAL_AI_API_KEY=<the-same-long-random-secret>
   ```

   Replace `VisualModel` with the exact Railway service name. Railway's private service reference variables are documented in [Working with Domains](https://docs.railway.com/networking/domains/working-with-domains). The model service should not have a public domain. Check `/health` from the service logs or Railway private network; readiness is returned only after model weights load. First deployment downloads the model and can take several minutes. Keep adequate RAM for PyTorch; do not add these heavy packages to the inventory app service.

7. Set the existing conservative starting values, then calibrate them on a labeled validation set:

   ```text
   VISUAL_MATCH_MIN_SCORE=0.90
   VISUAL_MATCH_MIN_MARGIN=0.10
   VISUAL_SEARCH_DEV_MAX_CATALOG=500
   VISUAL_SEARCH_RPM=30
   ```

   These scores are cosine similarities, not confidence percentages. The strict defaults auto-select only high-scoring, clearly separated results; weaker cross-view package/model results stay in the reviewable candidate list. Do not lower thresholds just to force an exact label. Include same-color near-duplicate designs, different colors, alternate camera angles, dark/blurred photos, non-garment images, and products absent from the catalog. Record top-1 accuracy, top-5 recall, false-accept rate, and no-match rate.

## Index existing catalog photos

After the app deploys with the `product_images` table and persistent photo volume mounted, run once from the **app service** container (not the model service):

```sh
cd backend
python scripts/rebuild_visual_index.py
```

The script links legacy `products.image_path` records to their actual product ID, then requests embeddings from `VISUAL_AI_URL` and writes them to `product_visual_embeddings_siglip2_v1`. It does not update stock, orders, or transaction history. It must use the app's real PostgreSQL `DATABASE_URL`, not its SQLite default. If it skips any photos, fix the volume/path/service issue and rerun it before relying on visual search.

Each product's first photo is the main catalog photo. Add up to seven extra genuine reference views using the product photo picker; they are linked to the same product and indexed separately. Replacing the main photo refreshes its vector. A failed index leaves the saved image in place, logs the failure, and causes the search to fail closed until reindexing succeeds.

## Staging acceptance

- Confirm product images still load after redeploy (persistent volume mounted to the same path).
- Try several real phone photos of each design and hard negatives. A close candidate must return `ambiguous`; unknown/poor images must return `no_match`.
- Verify each result shows the product identity and current size-wise stock from the database.
- Verify the scan endpoint never changes stock or creates products.
- If the model/index is unavailable, manual product search and recount remain available.

## Fine-tuning data

See [VISUAL_FINE_TUNING_HI.md](VISUAL_FINE_TUNING_HI.md) for the supplied photo labels and why they are not enough to claim a reliable fine-tuned model. Add labeled product references first; do not call reference indexing “training.”

## Rollback and limits

- The prior `product_visual_embeddings_clip_v1` table and product images are retained; this version creates a separate SigLIP 2 index. No existing inventory rows are migrated or deleted by index rebuild.
- Roll back the app build if model loading causes memory/startup problems; visual search will be unavailable on that version, but manual inventory workflows remain.
- Visual search is a candidate retrieval feature, not a guarantee of exact identification. A Twins Lady photo benchmark and calibrated thresholds are still required before claiming production accuracy.
- The service requires outbound access to download the public Hugging Face model on first use. No Hugging Face token is required for this public checkpoint.
