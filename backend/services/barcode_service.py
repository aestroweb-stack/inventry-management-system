"""Generate high-contrast printable Code128 barcode labels for each variant."""
from io import BytesIO

import barcode
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont
import qrcode


def _font(size=18):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _code128_image(
    value: str,
    *,
    module_width: float = 0.45,
    module_height: float = 22,
    quiet_zone: float = 8,
    dpi: int = 300,
) -> Image.Image:
    """Render a high-contrast Code128 barcode at a chosen compactness."""
    buf = BytesIO()
    code = barcode.get("code128", value, writer=ImageWriter())
    code.write(buf, options={
        "module_width": module_width,
        "module_height": module_height,
        "quiet_zone": quiet_zone,
        "font_size": 0,
        "text_distance": 2,
        "write_text": False,
        "dpi": dpi,
    })
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def generate_variant_label(variant_code: str, product_name: str, size: str) -> bytes:
    """Barcode is the primary scannable area; all human text stays below it."""
    code_img = _code128_image(variant_code)
    width = max(640, code_img.width + 60)
    height = code_img.height + 125
    canvas = Image.new("RGB", (width, height), "white")
    x = (width - code_img.width) // 2
    canvas.paste(code_img, (x, 12))

    draw = ImageDraw.Draw(canvas)
    title_font = _font(22)
    code_font = _font(24)
    title = f"{product_name} | Size {size}"
    bbox = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((width - (bbox[2] - bbox[0])) / 2, code_img.height + 28), title, fill="black", font=title_font)
    bbox = draw.textbbox((0, 0), variant_code, font=code_font)
    draw.text(((width - (bbox[2] - bbox[0])) / 2, code_img.height + 65), variant_code, fill="black", font=code_font)

    out = BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def generate_product_barcode_label(sku: str, product_name: str) -> bytes:
    """Create one design-level Code128 label; scanner lookup opens all sizes."""
    value = f"PRODUCT:{sku.strip().upper()}"
    code_img = _code128_image(value)
    width = max(680, code_img.width + 60)
    height = code_img.height + 132
    canvas = Image.new("RGB", (width, height), "white")
    canvas.paste(code_img, ((width - code_img.width) // 2, 10))
    draw = ImageDraw.Draw(canvas)
    title_font, code_font = _font(22), _font(19)
    for text, font, y in ((product_name[:48], title_font, code_img.height + 25), (value, code_font, code_img.height + 66)):
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text(((width - (bbox[2] - bbox[0])) / 2, y), text, fill="black", font=font)
    out = BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def generate_product_combo_label(sku: str, product_name: str) -> bytes:
    """Create one compact product label with scannable QR and Code128 codes.

    Both symbols contain the same PRODUCT:SKU value, so either one opens the
    full design in the existing camera scanner.
    """
    value = f"PRODUCT:{sku.strip().upper()}"
    barcode_img = _code128_image(
        value, module_width=0.22, module_height=13, quiet_zone=2, dpi=150
    )
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=5,
        border=2,
    )
    qr.add_data(value)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_size = 140
    qr_img.thumbnail((qr_size, qr_size), Image.Resampling.LANCZOS)

    pad = 16
    width = max(520, barcode_img.width + qr_img.width + pad * 4)
    height = 226
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)
    title_font, detail_font = _font(17), _font(13)
    title = product_name.strip()[:56] or sku.upper()
    draw.text((pad, 10), title, fill="black", font=title_font)

    # Keep the barcode's native aspect ratio, while fitting the compact label.
    code_max_w = width - qr_img.width - pad * 4
    code_max_h = 112
    scale = min(code_max_w / barcode_img.width, code_max_h / barcode_img.height, 1.0)
    if scale < 1.0:
        barcode_img = barcode_img.resize(
            (max(1, round(barcode_img.width * scale)), max(1, round(barcode_img.height * scale))),
            Image.Resampling.LANCZOS,
        )
    code_y = 48 + max(0, (code_max_h - barcode_img.height) // 2)
    canvas.paste(barcode_img, (pad, code_y))
    qr_x = width - qr_img.width - pad
    qr_y = 48 + max(0, (code_max_h - qr_img.height) // 2)
    canvas.paste(qr_img, (qr_x, qr_y))

    draw = ImageDraw.Draw(canvas)
    draw.text((pad, height - 36), sku.strip().upper()[:58], fill="black", font=detail_font)
    draw.text((qr_x, height - 36), "QR · PRODUCT", fill="black", font=detail_font)
    out = BytesIO()
    canvas.save(out, format="PNG", optimize=True, dpi=(220, 220))
    return out.getvalue()


def generate_product_qr_label(sku: str, product_name: str) -> bytes:
    """Create one design-level QR that opens every size in the inventory UI."""
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=3)
    qr.add_data(f"PRODUCT:{sku.strip().upper()}")
    qr.make(fit=True)
    qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    width = max(520, qr_image.width + 80)
    height = qr_image.height + 150
    canvas = Image.new("RGB", (width, height), "white")
    canvas.paste(qr_image, ((width - qr_image.width) // 2, 12))
    draw = ImageDraw.Draw(canvas)
    title_font, code_font = _font(22), _font(20)
    title = product_name[:48]
    for text, font, y in ((title, title_font, qr_image.height + 32), (sku.upper(), code_font, qr_image.height + 76)):
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text(((width - (bbox[2] - bbox[0])) / 2, y), text, fill="black", font=font)
    out = BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def generate_labels_sheet(variants: list[dict]) -> bytes:
    labels = [generate_variant_label(v["variant_code"], v["product_name"], v["size"]) for v in variants]
    images = [Image.open(BytesIO(b)).convert("RGB") for b in labels]
    if not images:
        raise ValueError("No variants to generate labels for.")

    cols = 2
    pad = 24
    cell_w = max(im.width for im in images) + pad
    cell_h = max(im.height for im in images) + pad
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), "white")
    for i, im in enumerate(images):
        r, c = divmod(i, cols)
        sheet.paste(im, (c * cell_w + pad // 2, r * cell_h + pad // 2))

    out = BytesIO()
    sheet.save(out, format="PNG", optimize=True)
    return out.getvalue()
