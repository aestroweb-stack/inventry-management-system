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
PARTY_CODES = ("DEV", "UD", "LP", "NK", "V", "O")
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


def transform(records: list[dict]) -> dict:
    if not records:
        raise PicklistError("Excel mein koi data row nahi mili")
    columns = list(dict.fromkeys(k for r in records[:200] for k in r.keys()))
    sku_col = _pick_column(columns, SOURCE_COLUMNS)
    qty_col = _pick_column(columns, QTY_COLUMNS)
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
        lines.append(line)
    if bad_qty_rows:
        raise PicklistError("Invalid or fractional quantity; correct these Excel rows: " + ", ".join(map(str, bad_qty_rows[:15])))
    if len(lines) + len(review) > MAX_SOURCE_ROWS:
        raise PicklistError(f"Max {MAX_SOURCE_ROWS} rows per picklist")
    if not lines:
        raise PicklistError("No valid SKU+quantity rows found")

    # Infer a missing party only if this exact design is assigned to ONE party
    # elsewhere. Otherwise keep it separate and flag, never guess.
    known = defaultdict(set)
    for line in lines:
        if line["explicit_party"] and not line["note"]:
            known[line["design"]].add(line["explicit_party"])
    for line in lines:
        if not line["party"]:
            matches = known[line["design"]]
            if len(matches) == 1:
                line["party"] = next(iter(matches))
                line["note"] = "; ".join(filter(None, [line["note"], "Party inferred from same design"]))
            else:
                line["party"] = "NO PARTY"
                line["note"] = "; ".join(filter(None, [line["note"], "Party missing" if not matches else "Ambiguous parties: " + ",".join(sorted(matches))]))
        if line["note"] or line["brand"] == "UNBRANDED" or line["size"] == "UNKNOWN":
            issue = "; ".join(filter(None, [line["note"], "Brand not recognised" if line["brand"] == "UNBRANDED" else ""]))
            review.append({"row": line["row"], "sku": line["original_sku"], "qty": line["qty"], "issue": issue, "party": line["party"]})
        # Safe naming: V party is the base collection; others remain visible.
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
    return {"total_qty": total, "source_rows": len(lines), "merged_rows": len(merged),
            "skipped_non_item_rows": skipped_blank, "review_count": len(review),
            "party_totals": dict(sorted(party_totals.items())), "brand_totals": dict(sorted(brand_totals.items())),
            "items": merged, "brand_items": branded, "audit": lines, "review": review}


def parse_upload(payload: bytes, filename: str) -> dict:
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
    return transform(records)


def _size_order(value: str):
    try:
        return (0, SIZE_ORDER.index(value), "")
    except ValueError:
        if value.isdigit():
            return (1, int(value), "")
        return (2, 0, value)


def _sorted_items(items):
    return sorted(items, key=lambda x: (x["design"], _size_order(x["size"]), x["pick_sku"]))


def _columns(items: list[dict], count=5):
    """Keep all sizes of the same design together in one column."""
    families = defaultdict(list)
    for item in _sorted_items(items):
        families[item["design"]].append(item)
    groups = list(families.values())
    target = max(1, ceil(len(items) / count))
    result = [[] for _ in range(count)]
    index = 0
    for group in groups:
        if index < count - 1 and result[index] and len(result[index]) + len(group) > target:
            index += 1
        result[index].extend(group)
    return result


def _sheet_name(name: str, used: set[str]) -> str:
    stem = re.sub(r"[\\/*?:\[\]]", "-", name).strip()[:31] or "Sheet"
    candidate, suffix = stem, 2
    while candidate.lower() in used:
        candidate = f"{stem[:27]}_{suffix}"
        suffix += 1
    used.add(candidate.lower())
    return candidate


def create_sheet_data(result: dict, *, include_brand_pages: bool = True) -> list[tuple[str, list[list[Any]], str]]:
    """Return sheet definitions (name, table, kind). Also used in export tests."""
    sheets = []
    d = datetime.now().strftime("%d/%m/%Y")
    summary = [["MYRAA PICKLIST • PACKING SUMMARY", ""], ["Generated", d],
               ["Input SKU rows", result["source_rows"]], ["Total pieces", result["total_qty"]],
               ["Merged party-SKU-size lines", result["merged_rows"]], ["Review rows (including inferred parties)", result["review_count"]],
               ["", ""], ["PARTY", "PIECES"]]
    summary += [[key, value] for key, value in sorted(result["party_totals"].items())]
    summary += [["", ""], ["BRAND", "PIECES"]]
    summary += [[key, value] for key, value in sorted(result["brand_totals"].items())]
    summary += [["", ""], ["All quantities are read-only. This upload does NOT dispatch or deduct inventory.", ""]]
    sheets.append(("Summary", summary, "summary"))
    parties = defaultdict(list)
    for item in result["items"]:
        parties[item["party"]].append(item)
    for party in sorted(parties, key=lambda p: (p == "NO PARTY", p)):
        title = f"PARTY {party} • {d} • {sum(i['qty'] for i in parties[party])} pcs"
        col_groups = _columns(parties[party])
        max_h = max(len(g) for g in col_groups)
        table = [[title] + [None] * 13, ["SKU / SIZE", "QTY", None] * 4 + ["SKU / SIZE", "QTY"]]
        for k in range(max_h):
            row = []
            for g in col_groups:
                if k < len(g):
                    row.extend([g[k]["pick_sku"], g[k]["qty"]])
                else:
                    row.extend([None, None])
                if len(row) < 14:
                    row.append(None)
            table.append(row[:14])
        sheets.append(("Party " + party, table, "packing"))
    if include_brand_pages:
        brandgroups = defaultdict(list)
        for item in result["brand_items"]:
            brandgroups[item["brand"]].append(item)
        for brand in sorted(brandgroups):
            title = f"BRAND {brand} • {d} • {sum(i['qty'] for i in brandgroups[brand])} pcs"
            cols = _columns(brandgroups[brand])
            table = [[title] + [None] * 13, ["SKU / SIZE", "QTY", None] * 4 + ["SKU / SIZE", "QTY"]]
            for k in range(max(len(c) for c in cols)):
                row = []
                for g in cols:
                    row += ([g[k]["pick_sku"], g[k]["qty"]] if k < len(g) else [None, None])
                    if len(row) < 14:
                        row.append(None)
                table.append(row[:14])
            sheets.append(("Brand " + brand, table, "packing"))
    reviews = [["Source Row", "Original SKU", "Qty", "Issue / Reason", "Party / Suggested"]]
    for r in result["review"]:
        reviews.append([r["row"], r["sku"], r["qty"], r["issue"], r.get("party", "")])
    sheets.append(("Review", reviews, "review"))
    audits = [["Source Row", "Original SKU", "Brand", "Party", "Normalized Design", "Size", "Qty", "Generated Pick SKU", "Notes"]]
    audits += [[r["row"], r["original_sku"], r["brand"], r["party"], r["design"], r["size"], r["qty"], r["pick_sku"], r["note"]] for r in result["audit"]]
    sheets.append(("Source Audit", audits, "audit"))
    return sheets


def to_xlsx(result: dict, include_brand_pages: bool = True) -> bytes:
    """Small standard-library-only OOXML writer; no Railway dependency added."""
    sheets = create_sheet_data(result, include_brand_pages=include_brand_pages)
    used = set()
    sheets = [(_sheet_name(n, used), rows, kind) for n, rows, kind in sheets]

    def attr(value):
        return xml_escape(str(value), {'"': '&quot;'})

    def cell(rownum, colnum, value, style):
        if value is None or value == "":
            return ""
        letter = ""
        n = colnum + 1
        while n:
            n, mod = divmod(n - 1, 26)
            letter = chr(65 + mod) + letter
        ref = f"{letter}{rownum}"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
        string = attr(value)
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{string}</t></is></c>'

    def sheet_xml(rows, kind):
        out = ['<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
               '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
        if kind == "packing":
            out += ['<sheetViews><sheetView workbookViewId="0"><pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
                    '<cols>' + ''.join(f'<col min="{i+1}" max="{i+1}" width="{37 if i%3==0 else 9 if i%3==1 else 3}" customWidth="1"/>' for i in range(14)) + '</cols>']
        else:
            widths = [20, 34, 20, 22, 32, 14, 10, 35, 50]
            out += ['<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
                    '<cols>' + ''.join(f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)) + '</cols>']
        out.append('<sheetData>')
        for ridx, row in enumerate(rows, 1):
            cells = []
            for cidx, value in enumerate(row):
                if kind == "packing":
                    s = 1 if ridx == 1 else 2 if ridx == 2 else 4 if cidx % 3 == 1 else 3
                elif kind == "summary":
                    s = 1 if ridx == 1 else 2 if str(row[0]) in {"PARTY", "BRAND"} else 4 if isinstance(value, (int,float)) else 3
                else:
                    s = 2 if ridx == 1 else 4 if isinstance(value, (int,float)) else 3
                c = cell(ridx, cidx, value, s)
                if c:
                    cells.append(c)
            out.append(f'<row r="{ridx}" ht="{26 if ridx == 1 else 22 if ridx == 2 else 20}" customHeight="1">{"".join(cells)}</row>')
        out.append('</sheetData>')
        if kind == "packing":
            out.append('<mergeCells count="1"><mergeCell ref="A1:N1"/></mergeCells>')
            out.append(f'<pageMargins left="0.25" right="0.25" top="0.4" bottom="0.4" header="0.2" footer="0.2"/><pageSetup orientation="landscape" paperSize="9" fitToWidth="1"/>')
        out.append('</worksheet>')
        return ''.join(out)

    types = '<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' \
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' \
            '<Default Extension="xml" ContentType="application/xml"/>' \
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' \
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' \
            + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets)+1)) + '</Types>'
    workbook = '<?xml version="1.0" encoding="utf-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' \
               + ''.join(f'<sheet name="{attr(n)}" sheetId="{i}" r:id="rId{i}"/>' for i,(n,_,_) in enumerate(sheets, 1)) + '</sheets></workbook>'
    rels = '<?xml version="1.0" encoding="utf-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' \
           + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1)) \
           + '<Relationship Id="styleId" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'
    styles = '<?xml version="1.0" encoding="utf-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' \
             '<fonts count="4"><font><sz val="10"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="14"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="10"/></font><font><color rgb="FF173B32"/><sz val="10"/></font></fonts>' \
             '<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF075C49"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE4F2EA"/></patternFill></fill></fills>' \
             '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="hair"><color rgb="FFD4E3DB"/></left><right style="hair"><color rgb="FFD4E3DB"/></right><top style="hair"><color rgb="FFD4E3DB"/></top><bottom style="hair"><color rgb="FFD4E3DB"/></bottom><diagonal/></border></borders>' \
             '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="0" fontId="2" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="0" fontId="3" fillId="0" borderId="1" xfId="0"/><xf numFmtId="0" fontId="3" fillId="3" borderId="1" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    root_rels = '<?xml version="1.0" encoding="utf-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    bio = BytesIO()
    with ZipFile(bio, "w", ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', types)
        z.writestr('_rels/.rels', root_rels)
        z.writestr('xl/workbook.xml', workbook)
        z.writestr('xl/_rels/workbook.xml.rels', rels)
        z.writestr('xl/styles.xml', styles)
        for i, (_name, data, kind) in enumerate(sheets, 1):
            z.writestr(f'xl/worksheets/sheet{i}.xml', sheet_xml(data, kind))
    return bio.getvalue()
