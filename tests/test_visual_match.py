"""Regression coverage for safe visual match decisions and live stock output.

Encoder outputs are deterministic fixtures here: these tests verify API and
decision behavior, not fashion-model accuracy on real garment photographs.
"""
import hashlib
import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///./test_visual_match.db"
os.environ["PRODUCT_IMAGE_DIR"] = tempfile.mkdtemp(prefix="inventory-visual-test-")
BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
import main  # noqa: E402
from services import visual_match_service, visual_search_service  # noqa: E402
import visual_model_service  # noqa: E402


def _photo(colour):
    image = Image.new("RGB", (180, 240), colour)
    for x in range(55, 125):
        for y in range(45, 205):
            image.putpixel((x, y), (245, 245, 245))
    output = BytesIO()
    image.save(output, "JPEG")
    return output.getvalue()


def _vector(position, sign=1.0):
    values = [0.0] * visual_match_service.VECTOR_DIMENSIONS
    values[position] = sign
    return tuple(values)


def _set_encoder_for(content_to_vector):
    return patch.object(
        visual_match_service,
        "local_visual_embedding",
        side_effect=lambda content: content_to_vector[hashlib.sha256(content).hexdigest()],
    )


def _reset_test_state():
    # This test module points only at test_visual_match.db and its own temp image folder.
    main.Base.metadata.drop_all(bind=main.engine)
    main.Base.metadata.create_all(bind=main.engine)
    visual_search_service._local_embeddings.clear()


def test_verified_match_returns_real_catalog_identity_and_live_stock():
    _reset_test_state()
    client = TestClient(main.app)
    client.post("/api/products", json={"sku": "VISUAL-RED", "name": "Red Test Kurta", "variants": [{"size": "M", "initial_stock": 7}]})
    client.post("/api/products", json={"sku": "VISUAL-BLUE", "name": "Blue Test Kurta", "variants": [{"size": "M", "initial_stock": 3}]})
    red, blue = _photo((220, 30, 35)), _photo((30, 60, 220))
    with _set_encoder_for({hashlib.sha256(red).hexdigest(): _vector(0), hashlib.sha256(blue).hexdigest(): _vector(1)}):
        assert client.post("/api/products/VISUAL-RED/image", files={"file": ("red.jpg", red, "image/jpeg")}).status_code == 200
        assert client.post("/api/products/VISUAL-BLUE/image", files={"file": ("blue.jpg", blue, "image/jpeg")}).status_code == 200
        result = client.post("/api/products/visual-match", files={"file": ("scan.jpg", red, "image/jpeg")})
    assert result.status_code == 200
    data = result.json()
    assert data["status"] == "matched"
    assert data["score_type"] == "cosine_similarity_not_probability"
    assert len(data["matches"]) == 1
    assert data["matches"][0]["product"]["sku"] == "VISUAL-RED"
    assert data["matches"][0]["reference_image_path"] == data["matches"][0]["product"]["image_path"]
    assert data["matches"][0]["product"]["variants"][0]["current_stock"] == 7
    assert data["matches"][0]["similarity"] == 1.0

    # A weakly similar image returns no candidate, never the nearest wrong SKU.
    unrelated = _photo((20, 210, 90))
    with _set_encoder_for({hashlib.sha256(unrelated).hexdigest(): _vector(0, -1.0)}):
        no_match = client.post("/api/products/visual-match", files={"file": ("unrelated.jpg", unrelated, "image/jpeg")})
    assert no_match.status_code == 200
    assert no_match.json()["status"] == "no_match"
    assert no_match.json()["matches"] == []

    # Index outages must not discard a product photo that was already committed.
    replacement = _photo((220, 31, 36))
    with patch.object(main.visual_search_service, "index_product_image", side_effect=RuntimeError("index unavailable")):
        uploaded = client.post("/api/products/VISUAL-RED/image", files={"file": ("replacement.jpg", replacement, "image/jpeg")})
    assert uploaded.status_code == 200
    image_path = client.get("/api/products/VISUAL-RED").json()["image_path"]
    assert image_path == uploaded.json()["image_path"]
    assert client.get(image_path).status_code == 200


def test_near_tied_designs_are_ambiguous_not_auto_selected():
    _reset_test_state()
    client = TestClient(main.app)
    first = client.post("/api/products", json={"sku": "VISUAL-TIE-A", "name": "Tie A", "variants": [{"size": "S", "initial_stock": 1}]})
    second = client.post("/api/products", json={"sku": "VISUAL-TIE-B", "name": "Tie B", "variants": [{"size": "S", "initial_stock": 2}]})
    assert first.status_code == second.status_code == 200
    photo_a, photo_b = _photo((200, 10, 10)), _photo((201, 10, 10))
    query = _photo((199, 10, 10))
    mapping = {hashlib.sha256(image).hexdigest(): _vector(10) for image in (photo_a, photo_b, query)}
    with _set_encoder_for(mapping):
        assert client.post("/api/products/VISUAL-TIE-A/image", files={"file": ("a.jpg", photo_a, "image/jpeg")}).status_code == 200
        assert client.post("/api/products/VISUAL-TIE-B/image", files={"file": ("b.jpg", photo_b, "image/jpeg")}).status_code == 200
        result = client.post("/api/products/visual-match", files={"file": ("query.jpg", query, "image/jpeg")})
    assert result.status_code == 200
    payload = result.json()
    assert payload["status"] == "ambiguous"
    assert {match["product"]["sku"] for match in payload["matches"]} == {"VISUAL-TIE-A", "VISUAL-TIE-B"}


def test_multiple_reference_images_stay_attached_to_one_product_and_do_not_change_stock():
    _reset_test_state()
    client = TestClient(main.app)
    created = client.post("/api/products", json={
        "sku": "VISUAL-MULTI", "name": "Multi View Kurta",
        "variants": [{"size": "M", "initial_stock": 9}],
    })
    assert created.status_code == 200
    front, back, scan = _photo((120, 30, 120)), _photo((120, 40, 120)), _photo((120, 50, 120))
    mapping = {hashlib.sha256(photo).hexdigest(): _vector(12) for photo in (front, back, scan)}
    with _set_encoder_for(mapping):
        primary = client.post("/api/products/VISUAL-MULTI/image", files={"file": ("front.jpg", front, "image/jpeg")})
        extras = client.post("/api/products/VISUAL-MULTI/images", files=[
            ("files", ("back.jpg", back, "image/jpeg")),
            ("files", ("close-up.jpg", scan, "image/jpeg")),
        ])
        result = client.post("/api/products/visual-match", files={"file": ("camera.jpg", scan, "image/jpeg")})
    assert primary.status_code == 200
    assert extras.status_code == 200 and extras.json()["saved"] == 2
    assert result.status_code == 200 and result.json()["status"] == "matched"
    product = result.json()["matches"][0]["product"]
    assert product["id"] == created.json()["id"]
    assert product["variants"][0]["current_stock"] == 9
    with main.SessionLocal() as db:
        refs = db.query(main.models.ProductImage).filter_by(product_id=product["id"]).all()
        assert len(refs) == 3
        assert sum(1 for ref in refs if ref.is_primary) == 1


def test_invalid_image_is_rejected_and_empty_catalog_is_no_match():
    _reset_test_state()
    client = TestClient(main.app)
    invalid = client.post("/api/products/visual-match", files={"file": ("x.jpg", b"not a photo", "image/jpeg")})
    assert invalid.status_code == 400
    empty_photo = _photo((30, 50, 70))
    with _set_encoder_for({hashlib.sha256(empty_photo).hexdigest(): _vector(0)}):
        empty = client.post("/api/products/visual-match", files={"file": ("empty.jpg", empty_photo, "image/jpeg")})
    assert empty.status_code == 200
    assert empty.json()["status"] == "no_match" and empty.json()["matches"] == []


def test_sqlite_visual_search_works_without_siglip_weights():
    _reset_test_state()
    client = TestClient(main.app)
    photo = _photo((55, 65, 75))
    created = client.post("/api/products", json={"sku": "LOCAL-VISUAL", "name": "Local visual test", "variants": [{"size": "M", "initial_stock": 7}]})
    assert created.status_code == 200
    assert client.post("/api/products/LOCAL-VISUAL/image", files={"file": ("ref.jpg", photo, "image/jpeg")}).status_code == 200
    with patch.object(visual_match_service, "image_embedding", side_effect=visual_match_service.ImageModelUnavailable("SigLIP weights unavailable")):
        response = client.post("/api/products/visual-match", files={"file": ("scan.jpg", photo, "image/jpeg")})
    assert response.status_code == 200
    assert response.json()["status"] == "matched"
    assert response.json()["matches"][0]["product"]["sku"] == "LOCAL-VISUAL"
    assert response.json()["matching_mode"] == "local color/layout suggestions"
    status = client.get("/api/visual-search/status")
    assert status.status_code == 200 and status.json()["status"] == "ready"
    assert status.json()["local_matcher_available"] is True


def test_remote_visual_model_service_uses_bearer_key_and_validates_vector():
    photo = _photo((30, 40, 50))
    expected = [0.0] * visual_match_service.VECTOR_DIMENSIONS
    expected[0] = 1.0
    fake_response = type("Response", (), {
        "json": lambda self: {"model": visual_match_service.MODEL_VERSION, "embedding": expected},
        "raise_for_status": lambda self: None,
    })()
    with patch.dict(os.environ, {
        "VISUAL_AI_URL": "http://visual-model.railway.internal",
        "VISUAL_AI_API_KEY": "test-visual-token",
    }), patch("httpx.post", return_value=fake_response) as post:
        result = visual_search_service._image_embedding(photo)
    assert result == tuple(expected)
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-visual-token"
    assert post.call_args.args[0] == "http://visual-model.railway.internal/v1/embed"

    fake_response.json = lambda: {"embedding": [float("nan")] * visual_match_service.VECTOR_DIMENSIONS}
    with patch.dict(os.environ, {
        "VISUAL_AI_URL": "http://visual-model.railway.internal",
        "VISUAL_AI_API_KEY": "test-visual-token",
    }), patch("httpx.post", return_value=fake_response):
        try:
            visual_search_service._image_embedding(photo)
        except visual_match_service.ImageModelUnavailable:
            pass
        else:
            raise AssertionError("Non-finite remote vectors must fail closed")

    with patch.dict(os.environ, {"VISUAL_AI_API_KEY": "private-service-test-key"}):
        visual_model_service._authorize("Bearer private-service-test-key")
        try:
            visual_model_service._authorize("Bearer wrong-key")
        except visual_model_service.HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("Visual service must reject a wrong API key")


if __name__ == "__main__":
    test_verified_match_returns_real_catalog_identity_and_live_stock()
    test_near_tied_designs_are_ambiguous_not_auto_selected()
    test_invalid_image_is_rejected_and_empty_catalog_is_no_match()
    test_sqlite_visual_search_works_without_siglip_weights()
    test_remote_visual_model_service_uses_bearer_key_and_validates_vector()
