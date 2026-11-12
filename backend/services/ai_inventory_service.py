"""
PHASE 2 - AI photo -> inventory update.
=========================================
Status: architecture + working matching algorithm, NOT wired to a trained
model yet. This needs your real product photos to become live - see
"WHAT'S NEEDED TO ACTIVATE THIS" at the bottom of this file. It is not
switched on in main.py by default.

------------------------------------------------------------------------
THE HONEST VERSION OF WHAT THIS CAN AND CAN'T DO
------------------------------------------------------------------------
IDENTIFYING which SKU a garment is, from a photo, works well with today's
vision models (CLIP-style image embeddings). Given a reference photo per
SKU (you already store one per product), matching a new photo to "which
SKU is this" is a solved, reliable problem.

COUNTING exact pieces in a photo of a rack - especially folded/stacked
kurta sets that look identical from outside - is NOT reliably solved by
computer vision alone. There's no visible boundary between piece #3 and
piece #4 in a stack of 10 folded, identical sets. Any vendor who tells you
"point a camera at the shelf and get an exact count" for stacked apparel
is overselling. Being upfront about this now saves you from a system that
silently drifts from the truth.

RECOMMENDED REAL-WORLD WORKFLOW (what this code is built for):
  1. Camera identifies WHICH SKU is in front of it (reliable).
  2. System shows its best-guess COUNT (best-effort - e.g. counting
     visible folded edges, or if bins are used, assuming one photo = one
     bin's known fixed capacity).
  3. A human taps to confirm or correct the count before it's saved -
     one tap, not manual excel entry. This is still a big speed-up over
     today's fully-manual counting, without pretending the camera is more
     certain than it is.
  4. For genuinely reliable automatic COUNTING with no human check, the
     real answer is a cheap QR/barcode sticker per shelf-bin (scan bin ->
     type/confirm count - seconds, ~100% accurate) or, if the inventory
     value justifies the cost, RFID tags per garment (near-100% automatic
     counting, no photo needed at all). Both are described in the README.

------------------------------------------------------------------------
ARCHITECTURE (implemented below)
------------------------------------------------------------------------
1. Reference index: one CLIP embedding per product, precomputed once from
   its stored product image. Stored as a flat matrix for nearest-neighbor
   search (ReferenceEmbeddingIndex - brute-force cosine similarity, which
   is plenty fast for a catalog of hundreds/low-thousands of SKUs; swap
   in FAISS/HNSW only if the catalog grows into the tens of thousands).
2. Detection: a rack photo is split into candidate regions (one per
   detected garment group) using an object detector.
3. Matching: each region's embedding is compared against the reference
   index -> best-matching SKU + a confidence score.
4. Aggregation: matches are grouped by SKU -> {sku: suggested_count},
   returned for human confirmation (see update_inventory_from_photo).
"""
from dataclasses import dataclass

import numpy as np


# ---------------------------------------------------------------------
# Step 1: nearest-neighbor search over per-SKU reference embeddings
# ---------------------------------------------------------------------
class ReferenceEmbeddingIndex:
    """Holds one embedding vector per SKU and answers 'which SKU is this
    photo closest to?' via cosine similarity. Pure numpy - no external
    vector DB needed at this catalog size."""

    def __init__(self):
        self.skus: list[str] = []
        self._matrix: np.ndarray | None = None  # (n_skus, embedding_dim), L2-normalized

    def build(self, sku_embedding_pairs: list[tuple[str, np.ndarray]]):
        if not sku_embedding_pairs:
            self.skus, self._matrix = [], None
            return
        self.skus = [sku for sku, _ in sku_embedding_pairs]
        mat = np.vstack([emb for _, emb in sku_embedding_pairs]).astype(np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1  # avoid divide-by-zero on a degenerate embedding
        self._matrix = mat / norms

    def query(self, embedding: np.ndarray, top_k: int = 1) -> list[tuple[str, float]]:
        """Returns up to top_k (sku, cosine_similarity) pairs, best first."""
        if self._matrix is None or len(self.skus) == 0:
            return []
        q = embedding.astype(np.float32)
        norm = np.linalg.norm(q)
        if norm == 0:
            return []
        q = q / norm
        sims = self._matrix @ q  # (n_skus,) cosine similarity since both sides are unit vectors
        top_idx = np.argsort(-sims)[:top_k]
        return [(self.skus[i], float(sims[i])) for i in top_idx]


# ---------------------------------------------------------------------
# Step 2 & 3: detection + embedding (real implementation, needs models
# downloaded on YOUR machine - see bottom of file for exact commands)
# ---------------------------------------------------------------------
@dataclass
class DetectedRegion:
    bbox: tuple[int, int, int, int]   # x1, y1, x2, y2 in pixels
    crop: "np.ndarray"                # the cropped image region


def detect_garment_regions(image: "np.ndarray") -> list[DetectedRegion]:
    """
    Splits a rack photo into one region per visually distinct garment
    group. Real implementation (commented, not run here - see note below):

        from ultralytics import YOLO
        model = YOLO("yolov8n.pt")  # generic pretrained weights as a start;
                                     # fine-tune on ~100-200 labelled photos of
                                     # YOUR racks for reliable region proposals
        results = model(image)
        regions = []
        for box in results[0].boxes.xyxy.cpu().numpy():
            x1, y1, x2, y2 = box.astype(int)
            regions.append(DetectedRegion((x1, y1, x2, y2), image[y1:y2, x1:x2]))
        return regions

    NOT executed in this sandbox: no internet access here to download model
    weights, and no real rack photos to test against yet. This function is
    a clean seam to drop that code into once you're running on your own
    machine (see "WHAT'S NEEDED TO ACTIVATE THIS" below).
    """
    raise NotImplementedError(
        "Detection model not wired up yet - see the docstring above and the "
        "activation steps at the bottom of this file."
    )


def generate_embedding(image_crop: "np.ndarray") -> np.ndarray:
    """
    Real implementation (commented):

        import torch, open_clip
        model, _, preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="openai"
        )
        model.eval()
        with torch.no_grad():
            tensor = preprocess(Image.fromarray(image_crop)).unsqueeze(0)
            embedding = model.encode_image(tensor).squeeze(0).numpy()
        return embedding

    Same caveat as detect_garment_regions: needs `pip install open_clip_torch
    torch pillow` and a first-run internet connection to fetch the pretrained
    weights, on the machine this actually runs on.
    """
    raise NotImplementedError(
        "Embedding model not wired up yet - see the docstring above."
    )


# ---------------------------------------------------------------------
# Step 4: orchestration - what an API endpoint would call
# ---------------------------------------------------------------------
def update_inventory_from_photo(
    image: "np.ndarray",
    reference_index: ReferenceEmbeddingIndex,
    min_confidence: float = 0.75,
) -> dict:
    """
    Returns a SUGGESTION for human review - never writes to the database
    directly. The API layer should show this to the user and only call
    inventory_service.set_absolute_stock() per SKU after they confirm/edit
    the counts (exactly like a manual recount, just pre-filled by AI).
    """
    regions = detect_garment_regions(image)

    sku_counts: dict[str, int] = {}
    low_confidence_regions = 0

    for region in regions:
        embedding = generate_embedding(region.crop)
        matches = reference_index.query(embedding, top_k=1)
        if not matches:
            low_confidence_regions += 1
            continue
        sku, confidence = matches[0]
        if confidence < min_confidence:
            low_confidence_regions += 1
            continue
        sku_counts[sku] = sku_counts.get(sku, 0) + 1

    return {
        "suggested_counts": sku_counts,
        "unmatched_regions": low_confidence_regions,
        "requires_human_confirmation": True,
    }


"""
------------------------------------------------------------------------
WHAT'S NEEDED TO ACTIVATE THIS (do this on your own machine, not here)
------------------------------------------------------------------------
1. pip install open_clip_torch torch ultralytics pillow
   (first run downloads pretrained weights - needs internet access once)

2. One-time: for every existing Product, generate a reference embedding
   from its stored product image and save it (e.g. a new
   ProductEmbedding table: product_id, embedding BLOB). A ~20 line script
   using generate_embedding() above on each Product.image_path.

3. Uncomment the real bodies of detect_garment_regions() and
   generate_embedding() above.

4. Add a POST /inventory/photo-update endpoint that:
     a. accepts an uploaded photo
     b. loads the ReferenceEmbeddingIndex from the ProductEmbedding table
     c. calls update_inventory_from_photo()
     d. returns suggested_counts to the frontend for the "confirm counts"
        screen (mock this screen in the current frontend as a placeholder
        - see frontend/app.js)
     e. only on confirm, loops the confirmed counts through
        inventory_service.set_absolute_stock() per SKU

5. Start with ~100-200 of your own rack photos to fine-tune the detector
   (step 1's yolov8n.pt is generic - it doesn't know what a folded kurta
   set looks like out of the box). Until fine-tuned, expect it to work
   best as "point the camera at ONE bin/pile at a time" rather than a
   wide shot of a full rack.
"""
