# Product-photo dataset and fine-tuning plan

## Labels confirmed so far

| Photos | Confirmed catalog identity | Use |
| --- | --- | --- |
| `37c129d7df2e42629a09eec3f671dc67.jpg`, `5437582df27248b1bca8f37e5f24cc65.jpg`, `ee415222f09c42de937228447bea2595.jpg` | SKU `KIDS_NK_101` | Product reference views; the first two are similar front views and the third is a back/side view. |
| `IMG_0686_1.webp`, `IMG_0687.webp`, `IMG_0688_2.webp`, `IMG_0689.webp`, `IMG_0691.webp`, `IMG_0698.webp` | SKU `TWINS_O_SUZUKA_YELLOW`, name `SUZUKA YELLOW` | Product reference views, after verifying the live catalog record has that exact SKU. |
| `IMG_0763.webp`, `IMG_0764.webp`, `IMG_0765.webp` | **Unlabeled** | Keep out of training and matching until the exact SKU is confirmed. |
| `IMG_0688_2.jpg.jpeg` | **Unclear close crop / possible duplicate** | Exclude until a human verifies what design it depicts. |
| `Sunny Yellow Kurta Lookbook.png` | Multi-panel image with text/graphics | Do not use as a product photo; it can teach the model collage text and borders instead of the garment. |

These are labels only; the private pictures are not copied into Git. Add verified front/back/close-up views from **Products → Edit product → Photos** (or the add-product photo picker), then rebuild the image index. Product photo references are not model training: they improve retrieval by comparing a scan against real catalog views without altering stock.

## Fine-tuning reality check

SigLIP 2 is a pretrained, general image retrieval model. The supplied photos are enough to add genuine reference views for the two confirmed designs, but not enough to claim a reliable fine-tuned garment classifier. There are only two labeled designs, a few correlated photos per design, no independently labeled validation set, and one ambiguous group. Fine-tuning this small sample can memorize model poses/background/color and increase false matches. Do not point a tuned checkpoint at live stock until it passes a separate holdout set containing different camera angles, lighting, other mustard/yellow designs, and unknown garments.

The app therefore defaults to a separate SigLIP model service and database-backed nearest-reference retrieval. It fails closed for low/ambiguous matches. This is safer than returning a forced SKU. Fine-tuning is a separate, opt-in experiment; this change does not train or replace model weights by itself.

## Dataset format for a future supervised contrastive run

Put original photos into one folder per exact, already-existing SKU, with at least 10 genuinely different pictures per SKU as an initial target; include different people/poses, front/back, close-ups, phone cameras, indoor/outdoor light, and visually similar negative designs. Keep multiple crops of the same original in the same split. Do not use collage/lookbook images. Split by original photo/session, not by random copies, into train/validation/test. A training script must reject unknown SKUs and report per-SKU recall and false-positive matches before any adapter is promoted.

The current code does not include an automatic fine-tune command because the uncertain `IMG_0763–0765` labels and the tiny two-design set are not a safe production training set. The supplied two known groups can already be added as product photo references in the UI.
