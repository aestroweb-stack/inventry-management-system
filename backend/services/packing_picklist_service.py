"""Read-only marketplace picklist -> consolidated party/brand packing Excel.

Never reads or writes product/stock tables, never dispatches orders.
Every valid positive source quantity survives in exactly one output group.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from io import BytesIO
from math import ceil
import re
from typing import Any
from xml.sax.saxutils import escape as xml_escape
from zipfile import ZipFile, ZIP_DEFLATED

from services.table_reader import read_table, to_int

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_SOURCE_ROWS = 20000
SIZE_ORDER = ("XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "3XL", "4XL", "5XL")
SIZE_CHOICES = set(SIZE_ORDER) | {"OS", "FREE SIZE", "FREESIZE", "ONE SIZE"}
PARTY_CODES = ("NEEL", "DEV", "UD", "LP", "NK", "V", "O")
PARTY_SEQUENCE = ("NO PARTY", "V", "NK", "O", "LP", "NEEL")
# Only trailing color words can be removed. Genuine design IDs and numbers remain.
COLOR_WORDS = {"BLACK", "WHITE", "BLUE", "RED", "GREEN", "PINK", "PURPLE", "YELLOW", "ORANGE", "BEIGE", "GREY", "GRAY", "NAVY", "RUST", "PEACH", "LEMON", "MUSTARD", "WINE", "MAROON", "CREAM", "CREME", "BROWN", "TEAL", "OLIVE", "RAMA", "PISTA", "GOLD", "GOLDEN", "CHIKU", "CHIKOO", "SKY", "LIGHT", "DARK", "OFF", "RANI", "LAVENDER", "FUCHSIA", "MINT", "KHAKI", "TAN", "MAGENTA", "EMERALD", "IVORY", "TURQUOISE", "AQUA", "MULTI", "MAUVE", "COFFEE", "CHOCOLATE", "SILVER", "LILAC", "NEON", "MEHNDI", "FIRUZI", "FIROZI", "FIROJI", "SAGE", "NUDE", "CHERRY", "PARROT", "SEA", "PURPLEE", "COFFE", "COFFEE", "MULTICOLOR", "MULTICOLOUR", "ROSE", "RANI", "OFFWHITE"}
# Exact, boundary-anchored brand aliases only. Do NOT strip arbitrary first words.
BRAND_ALIASES = {
    "KINDA DESI": "KINDA DESI", "KINDA": "KINDA", "TWINS LADY": "TWINS",
    "TWINSLADY": "TWINS", "TWINS": "TWINS", "MODA": "MODA", "MDOA": "MODA",
    "H&N": "H&N", "SZN": "SZN", "ANOUK": "ANOUK", "SNGR": "SNGR",
    "FUSION": "FUSION", "GIRL": "GIRL", "DRIP": "DRIP", "M": "M",
}
SOURCE_COLUMNS = ("master sku", "master_sku", "seller sku", "seller sku code", "sku code", "sku", "style code", "variant code", "product sku")
QTY_COLUMNS = ("qty", "quantity", "order qty", "ordered qty", "order quantity", "units", "pieces", "count")
SIZE_COLUMNS = ("size", "size name", "variant size")


class PicklistError(ValueError):
    pass


def _pick_column(keys: list[str], candidates: tuple[str, ...]) -> str | None:
    found = {str(k).strip().lower(): k for k in keys}
    return next((found[x] for x in candidates if x in found), None)


def _canonical(value: str) -> str:
    value = str(value).strip().upper().replace("\u00a0", " ")
    # Only explicit parenthesized variant letters/numbers collapse; intrinsic
    # codes such as NK-851, 22141, PINKPARI-2 are NEVER stripped.
    value = re.sub(r"\(\s*(?:[A-Z]|\d{1,2})\s*\)", "", value)
    value = re.sub(r"[\s_\-]+", "_", value)
    return value.strip("_")


def _strip_brand(value: str) -> tuple[str, str]:
    text = value.strip()
    for alias in sorted(BRAND_ALIASES, key=len, reverse=True):
        match = re.match(re.escape(alias) + r"(?=[_\-\s])[_\-\s]+", text, re.I)
        if match:
            return BRAND_ALIASES[alias], text[match.end():]
    return "UNBRANDED", text


def _split_party(value: str) -> tuple[str | None, str]:
    for code in PARTY_CODES:
        match = re.match(re.escape(code) + r"(?=[_\-\s])[_\-\s]+", value, re.I)
        if match:
            return code, value[match.end():]
    return None, value


def _size(value: Any) -> str:
    s = str(value or "").strip().upper().replace("\u00a0", " ")
    s = {"2XL": "XXL", "3 X L": "3XL", "FREE": "FREE SIZE"}.get(s, s)
    return s


def _extract_size(body: str, supplied_size: Any) -> tuple[str | None, str, str | None]:
    supplied = _size(supplied_size)
    known = bool(supplied and (supplied in SIZE_CHOICES or re.fullmatch(r"\d{1,3}(?:Y)?", supplied)))
    if supplied and not known:
        return None, body, f"Unrecognised size column: {supplied}"
    sizes_re = r"(?:XXXS|XXS|XS|XXL|XL|3XL|4XL|5XL|2XL|S|M|L|OS|FREE SIZE|FREESIZE|ONE SIZE|\d{1,3}Y?)"
    tail = re.search(r"(?:[_\-\s]+)(" + sizes_re + r")\s*$", body, re.I)
    if known:
        if tail:
            found = _size(tail.group(1))
            if found == supplied or {found, supplied} == {"2XL", "XXL"}:
                return supplied, body[:tail.start()], None
            return supplied, body, f"SKU suffix ({found}) differs from size column ({supplied})"
        # Marketplace SKU like GIRL_AENA_BEIGE34 with no separator.
        if re.search(r"(?<!\d)" + re.escape(supplied) + r"$", body, re.I):
            return supplied, body[:-len(supplied)], None
        return supplied, body, None
    if tail:
        return _size(tail.group(1)), body[:tail.start()], None
    return None, body, "No recognisable size; check source SKU/Size column"


def _parse_record(source_sku: str, supplied_size: Any, qty: int, row_number: int) -> dict:
    original = source_sku.strip()
    # Party may precede a seller brand, e.g. LP_Twins_Sapna_Olive_L.
    pre_party, pre_body = _split_party(original)
    pre_brand, after_brand = _strip_brand(pre_body) if pre_party else ("UNBRANDED", original)
    if pre_party and pre_brand != "UNBRANDED":
        brand, party, body = pre_brand, pre_party, after_brand
    else:
        brand, body = _strip_brand(original)
        party, body = _split_party(body)
    # Some sellers encode LP_TWINS_... (party before brand).
    if brand == "UNBRANDED" and party is not None:
        inner_brand, inner = _strip_brand(body)
        if inner_brand != "UNBRANDED":
            brand, body = inner_brand, inner
    size, design_raw, warning = _extract_size(body, supplied_size)
    design = _canonical(design_raw)
    if not design:
        warning = warning or "No design remains after parsing"
    if not size:
        size = "UNKNOWN"
    return {
        "row": row_number, "original_sku": original, "brand": brand,
        "explicit_party": party, "party": party, "design": design or _canonical(original),
        "size": size, "qty": qty, "note": warning or "",
    }


def _without_color(design: str) -> tuple[str, str]:
    """Remove suffix color *only*, not intrinsic numeric/design identity.

    EVIL_EYE_BLUE -> EVIL_EYE, NK_851 -> NK_851, PINKPARI_2 -> unchanged.
    """
    tokens = design.split("_")
    removed = []
    while len(tokens) > 1 and tokens[-1] in COLOR_WORDS and len(removed) < 3:
        removed.insert(0, tokens.pop())
    return "_".join(tokens), "_".join(removed)


def party_rank(party: str) -> tuple[int, str]:
    return (PARTY_SEQUENCE.index(party), "") if party in PARTY_SEQUENCE else (len(PARTY_SEQUENCE), party)


def _selected_date(source_filename: str, date_value: str | None) -> str:
    if date_value:
        try:
            return datetime.strptime(date_value, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError as exc:
            raise PicklistError("Invalid date; use YYYY-MM-DD") from exc
    found = re.search(r"(?:Picklist[_ -])?(20\d\d)[_-](\d\d)[_-](\d\d)", source_filename or "", re.I)
    if found:
        try:
            return datetime(int(found[1]), int(found[2]), int(found[3])).strftime("%d/%m/%Y")
        except ValueError:
            pass
    return datetime.now().strftime("%d/%m/%Y")


def transform(records: list[dict], *, brand_override: str = "AUTO", picklist_date: str | None = None, filename: str = "") -> dict:
    if not records:
        raise PicklistError("Excel mein koi data row nahi mili")
    brand_override = (brand_override or "AUTO").strip().upper()
    if brand_override not in {"AUTO", "TWINS", "MODA"}:
        raise PicklistError("Brand selection must be AUTO, TWINS or MODA")
    date_label = _selected_date(filename, picklist_date)
    columns = list(dict.fromkeys(k for r in records[:200] for k in r.keys()))
    sku_col = _pick_column(columns, SOURCE_COLUMNS)
    qty_col = _pick_column(columns, QTY_COLUMNS)
    brand_col = _pick_column(columns, ("brand", "seller brand", "seller_brand", "seller name", "marketplace brand"))
    size_col = _pick_column(columns, SIZE_COLUMNS)
    if not sku_col:
        raise PicklistError("Master SKU / Seller SKU / SKU column required. Upload original marketplace picklist, not formatted output.")
    lines = []
    review = []
    skipped_blank = 0
    reported_totals = []
    bad_qty_rows = []
    for row_number, r in enumerate(records, start=2):
        sku = str(r.get(sku_col) or "").strip()
        if sku.upper() in {"TOTAL", "GRAND TOTAL"}:
            if qty_col and to_int(r.get(qty_col)) is not None:
                reported_totals.append(to_int(r.get(qty_col)))
            skipped_blank += 1
            continue
        if not sku or sku.upper() == "SUBTOTAL":
            skipped_blank += 1
            continue
        raw_qty = r.get(qty_col) if qty_col else 1
        qty = to_int(raw_qty)
        if qty is None or qty <= 0 or str(raw_qty).strip() not in {str(qty), f"{qty}.0"} and not isinstance(raw_qty, (int, float)):
            bad_qty_rows.append(row_number)
            continue
        line = _parse_record(sku, r.get(size_col) if size_col else None, qty, row_number)
        line["sku_brand"] = line["brand"]
        explicit_brand = str(r.get(brand_col) or "").strip().upper() if brand_col else ""
        # Source Brand column or operator-selected brand is authoritative;
        # auto detection by SKU prefix remains conservative.
        line["brand"] = (brand_override if brand_override != "AUTO" else explicit_brand or line["brand"])
        line["original_design"] = line["design"]
        line["design"], line["color_removed"] = _without_color(line["design"])
        lines.append(line)
    if bad_qty_rows:
        raise PicklistError("Invalid or fractional quantity; correct these Excel rows: " + ", ".join(map(str, bad_qty_rows[:15])))
    if len(lines) + len(review) > MAX_SOURCE_ROWS:
        raise PicklistError(f"Max {MAX_SOURCE_ROWS} rows per picklist")
    if not lines:
        raise PicklistError("No valid SKU+quantity rows found")

    # Missing party stays NO PARTY so it prints FIRST as requested.
    # Do not infer a party from a different branded design.
    for line in lines:
        if not line["party"]:
            line["party"] = "NO PARTY"
        if line["note"] or line["brand"] == "UNBRANDED" or line["size"] == "UNKNOWN":
            issue = "; ".join(filter(None, [line["note"], "Brand not recognised" if line["brand"] == "UNBRANDED" else ""]))
            review.append({"row": line["row"], "sku": line["original_sku"], "qty": line["qty"], "issue": issue, "party": line["party"]})
        prefix = "" if line["party"] in {"V", "NO PARTY"} else line["party"] + "_"
        line["pick_sku"] = prefix + line["design"] + "_" + line["size"]

    aggregated = defaultdict(lambda: {"qty": 0, "brands": set(), "source_rows": 0})
    brand_agg = defaultdict(lambda: {"qty": 0, "source_rows": 0})
    for line in lines:
        identity = (line["party"], line["design"], line["size"])
        aggregated[identity]["qty"] += line["qty"]
        aggregated[identity]["brands"].add(line["brand"])
        aggregated[identity]["source_rows"] += 1
        brand_agg[(line["brand"], line["party"], line["design"], line["size"])]["qty"] += line["qty"]
        brand_agg[(line["brand"], line["party"], line["design"], line["size"])]["source_rows"] += 1

    merged = []
    for (party, design, size), v in aggregated.items():
        prefix = "" if party in {"V", "NO PARTY"} else party + "_"
        merged.append({"party": party, "design": design, "size": size,
                       "pick_sku": prefix + design + "_" + size, "qty": v["qty"],
                       "brands": ", ".join(sorted(v["brands"])), "source_rows": v["source_rows"]})
    branded = []
    for (brand, party, design, size), v in brand_agg.items():
        prefix = "" if party in {"V", "NO PARTY"} else party + "_"
        branded.append({"brand": brand, "party": party, "design": design, "size": size,
                        "pick_sku": prefix + design + "_" + size, "qty": v["qty"],
                        "source_rows": v["source_rows"]})
    total = sum(line["qty"] for line in lines)
    if any(reported != total for reported in reported_totals):
        raise PicklistError(f"Input Total ({reported_totals[-1]}) does not match parsed pieces ({total})")
    if sum(item["qty"] for item in merged) != total or sum(item["qty"] for item in branded) != total:
        raise PicklistError("Quantity reconciliation failed; download refused")
    party_totals = Counter()
    brand_totals = Counter()
    for line in lines:
        party_totals[line["party"]] += line["qty"]
        brand_totals[line["brand"]] += line["qty"]
    return {"total_qty": total, "source_rows": len(lines), "merged_rows": len(branded), "picklist_date": date_label, "brand_selection": brand_override,
            "skipped_non_item_rows": skipped_blank, "review_count": len(review),
            "party_totals": dict(sorted(party_totals.items())), "brand_totals": dict(sorted(brand_totals.items())),
            "items": merged, "brand_items": branded, "audit": lines, "review": review}


def parse_upload(payload: bytes, filename: str, *, brand_override: str = "AUTO", picklist_date: str | None = None) -> dict:
    if not payload or len(payload) > MAX_UPLOAD_BYTES:
        raise PicklistError("File empty or over 12 MB")
    if not filename.lower().endswith((".xlsx", ".xls", ".csv")):
        raise PicklistError("Upload .xlsx / .xls / .csv")
    try:
        frame = read_table(payload, filename)
    except Exception as exc:
        raise PicklistError("Excel read failed: " + str(exc)[:170]) from exc
    if len(frame) > MAX_SOURCE_ROWS + 1:
        raise PicklistError("Picklist has too many rows")
    # pandas is already used by the existing marketplace import reader. The
    # generator itself never queries a DB or mutates order/stock state.
    records = frame.where(frame.notna(), None).to_dict(orient="records")
    return transform(records, brand_override=brand_override, picklist_date=picklist_date, filename=filename)


def _size_order(value: str):
    try:
        return (0, SIZE_ORDER.index(value), "")
    except ValueError:
        if value.isdigit():
            return (1, int(value), "")
        return (2, 0, value)


def _sorted_items(items):
    # Required order: missing-party first, then V, NK, O, LP, NEEL, others.
    return sorted(items, key=lambda x: (party_rank(x["party"]), x["design"], _size_order(x["size"]), x["pick_sku"]))


def _pages(items: list[dict], *, row_limit: int = 39, lane_count: int = 5) -> list[list[list[dict]]]:
    """Five SKU/Qty pairs per printed page, no more than 39 data rows + header.

    A design's size lines stay together if the family fits in a lane. Spillover
    produces another PRINT PAGE for the same brand; nothing is omitted.
    """
    families = defaultdict(list)
    for item in _sorted_items(items):
        families[(item["party"], item["design"])].append(item)
    pages: list[list[list[dict]]] = []
    page = [[] for _ in range(lane_count)]
    lane = 0
    for family in families.values():
        remaining = list(family)
        while remaining:
            space = row_limit - len(page[lane])
            if space <= 0 or (len(remaining) > space and len(remaining) <= row_limit and page[lane]):
                lane += 1
                if lane == lane_count:
                    pages.append(page)
                    page = [[] for _ in range(lane_count)]
                    lane = 0
                space = row_limit - len(page[lane])
            part, remaining = remaining[:space], remaining[space:]
            page[lane].extend(part)
            if remaining:
                lane += 1
                if lane == lane_count:
                    pages.append(page)
                    page = [[] for _ in range(lane_count)]
                    lane = 0
    if any(page):
        pages.append(page)
    return pages


def _sheet_name(name: str, used: set[str]) -> str:
    stem = re.sub(r"[\\/*?:\[\]]", "-", name).strip()[:31] or "Sheet"
    candidate, suffix = stem, 2
    while candidate.lower() in used:
        candidate = f"{stem[:27]}_{suffix}"
        suffix += 1
    used.add(candidate.lower())
    return candidate


def create_sheet_data(result: dict, *, include_brand_pages: bool = True, include_audit: bool = False) -> list[tuple[str, list[list[Any]], str]]:
    """Brand/date print pages only, matching the supplied A:N reference layout.

    In printed sheets: row 1 date+brand, rows 2..40 SKU/Qty. No totals row.
    """
    d = result.get("picklist_date") or datetime.now().strftime("%d/%m/%Y")
    grouped = defaultdict(list)
    for item in result["brand_items"]:
        grouped[item["brand"]].append(item)
    output = []
    for brand in sorted(grouped, key=lambda b: (0 if b == "TWINS" else 1 if b == "MODA" else 2, b)):
        for part, lanes in enumerate(_pages(grouped[brand]), start=1):
            label = f"{brand}-{d}"
            sheet_name = f"{brand}-{d.replace('/', '-')}{' '+str(part) if part > 1 else ''}"
            header = [None] * 14
            for col in (0, 6, 12):
                header[col] = label
            rows = [header]
            for k in range(max(len(lane) for lane in lanes)):
                row = [None] * 14
                for i, lane in enumerate(lanes):
                    if k < len(lane):
                        row[i * 3] = lane[k]["pick_sku"]
                        row[i * 3 + 1] = lane[k]["qty"]
                rows.append(row)
            output.append((sheet_name, rows, "packing"))
    if not output:
        raise PicklistError("No packing rows after transformation")
    if include_audit:
        reviews = [["Source Row", "SKU", "Qty", "Issue", "Party"]]
        reviews += [[r["row"], r["sku"], r["qty"], r["issue"], r.get("party", "")] for r in result["review"]]
        output.append(("Review", reviews, "review"))
        audits = [["Source Row", "Original SKU", "Brand", "Party", "Original Design", "Merged Design", "Size", "Qty", "Pick SKU", "Notes"]]
        audits += [[r["row"], r["original_sku"], r["brand"], r["party"], r["original_design"], r["design"], r["size"], r["qty"], r["pick_sku"], r["note"]] for r in result["audit"]]
        output.append(("Source Audit", audits, "audit"))
    return output


def to_xlsx(result: dict, include_brand_pages: bool = True, include_audit: bool = False) -> bytes:
    """Emit an Excel workbook without extra Railway spreadsheet dependencies."""
    sheets = create_sheet_data(result, include_brand_pages=include_brand_pages, include_audit=include_audit)
    used = set()
    sheets = [(_sheet_name(n, used), rows, kind) for n, rows, kind in sheets]

    def attr(value):
        return xml_escape(str(value), {'"': '&quot;'})

    def col_letter(n):
        letters = ""
        while n:
            n, mod = divmod(n - 1, 26)
            letters = chr(65 + mod) + letters
        return letters

    def cell(rownum, colnum, value, style):
        if value is None or value == "":
            return ""
        ref = f"{col_letter(colnum+1)}{rownum}"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{attr(value)}</t></is></c>'

    def sheet_xml(rows, kind):
        output = ['<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
                  '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
        if kind == "packing":
            output += ['<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>',
                       '<sheetViews><sheetView showGridLines="0" workbookViewId="0"/></sheetViews>',
                       '<cols>' + ''.join(f'<col min="{i+1}" max="{i+1}" width="{44 if i%3==0 else 9 if i%3==1 else 3}" customWidth="1"/>' for i in range(14)) + '</cols>']
        else:
            output.append('<cols>' + ''.join(f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>' for i, w in enumerate([15,44,20,34,34,34,15,10,42,60])) + '</cols>')
        output.append('<sheetData>')
        for ridx, row in enumerate(rows, 1):
            cells = []
            for cidx, value in enumerate(row):
                style = (1 if ridx == 1 else 2) if kind == 'packing' else (3 if ridx == 1 else 4)
                v = cell(ridx, cidx, value, style)
                if v:
                    cells.append(v)
            height = 38 if kind == "packing" else 23
            output.append(f'<row r="{ridx}" ht="{height}" customHeight="1">{"".join(cells)}</row>')
        output.append('</sheetData>')
        if kind == "packing":
            output.extend(['<printOptions horizontalCentered="1"/>',
                '<pageMargins left="0.1" right="0.1" top="0.15" bottom="0.15" header="0" footer="0"/>',
                '<pageSetup paperSize="8" orientation="landscape" fitToWidth="1" fitToHeight="1"/>'])
        output.append('</worksheet>')
        return ''.join(output)

    types = '<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' \
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' \
        '<Default Extension="xml" ContentType="application/xml"/>' \
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' \
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' \
        + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1)) + '</Types>'
    workbook = '<?xml version="1.0" encoding="utf-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' \
        + ''.join(f'<sheet name="{attr(n)}" sheetId="{i}" r:id="rId{i}"/>' for i,(n,_,_) in enumerate(sheets,1)) + '</sheets></workbook>'
    rels = '<?xml version="1.0" encoding="utf-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' \
        + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1)) + '<Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'
    # Black text, white background, 22-point bold data; 26-point bold title.
    styles = '<?xml version="1.0" encoding="utf-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' \
        '<fonts count="4"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="26"/><name val="Calibri"/><color rgb="FF000000"/></font><font><b/><sz val="22"/><name val="Calibri"/><color rgb="FF000000"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>' \
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>' \
        '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/><diagonal/></border></borders>' \
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' \
        '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="3" fillId="0" borderId="1" xfId="0"/><xf numFmtId="0" fontId="3" fillId="0" borderId="1" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    root_rels = '<?xml version="1.0" encoding="utf-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    bio = BytesIO()
    with ZipFile(bio,"w",ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', types)
        z.writestr('_rels/.rels', root_rels)
        z.writestr('xl/workbook.xml', workbook)
        z.writestr('xl/_rels/workbook.xml.rels', rels)
        z.writestr('xl/styles.xml', styles)
        for i,(_name,data,kind) in enumerate(sheets,1):
            z.writestr(f'xl/worksheets/sheet{i}.xml',sheet_xml(data,kind))
    return bio.getvalue()
