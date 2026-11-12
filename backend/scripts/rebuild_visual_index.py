"""Backfill product photo references and build the SigLIP 2 pgvector index.

Run as a one-off after deploy, once the persistent image volume is mounted:
    cd backend && python scripts/rebuild_visual_index.py
"""
from pathlib import Path
import os
import sys

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from database import SessionLocal, engine  # noqa: E402
import models  # noqa: E402
from services import visual_search_service  # noqa: E402


def main() -> None:
    if engine.dialect.name != "postgresql":
        raise SystemExit("Visual index rebuild requires PostgreSQL with pgvector; SQLite is local-development only.")
    visual_search_service.ensure_postgres_index(engine)
    image_dir = Path(os.getenv("PRODUCT_IMAGE_DIR") or str(BASE_DIR / "static" / "product_images")).resolve()
    db = SessionLocal()
    indexed = skipped = legacy_backfilled = 0
    try:
        products = db.query(models.Product).filter(
            models.Product.is_deleted == False, models.Product.image_path.isnot(None)  # noqa: E712
        ).yield_per(250)
        for product in products:
            exists = db.query(models.ProductImage.id).filter(
                models.ProductImage.product_id == product.id,
                models.ProductImage.image_path == product.image_path,
            ).first()
            if not exists:
                db.add(models.ProductImage(product_id=product.id, image_path=product.image_path, is_primary=True))
                legacy_backfilled += 1
        db.commit()
        if legacy_backfilled:
            print(f"Linked {legacy_backfilled} legacy main photos to their product IDs")

        last_id = 0
        while True:
            page = db.query(models.ProductImage).join(models.Product).filter(
                models.Product.is_deleted == False, models.ProductImage.id > last_id  # noqa: E712
            ).order_by(models.ProductImage.id).limit(250).all()
            if not page:
                break
            for image in page:
                last_id = image.id
                path = (image_dir / Path(str(image.image_path)).name).resolve()
                try:
                    path.relative_to(image_dir)
                    visual_search_service.index_product_image(db, image.product_id, image.id, path.read_bytes(), path)
                    indexed += 1
                except Exception as exc:
                    skipped += 1
                    print(f"SKIP product-image:{image.id}: {type(exc).__name__}")
                    try:
                        visual_search_service.remove_product_embedding(db, image.product_id, image.id)
                    except Exception:
                        db.rollback()
            db.commit()
            print(f"Processed {indexed + skipped}: indexed={indexed}, skipped={skipped}")
    finally:
        db.close()
    visual_search_service.clear_visual_cache()
    print(f"SigLIP 2 visual index complete: indexed={indexed}, skipped={skipped}")
    if skipped:
        raise SystemExit("Some reference photos were skipped. Review the messages above before relying on visual search.")


if __name__ == "__main__":
    main()
