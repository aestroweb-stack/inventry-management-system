"""MYRAA packing-only picklist (v3): dynamic SKU parsing, audited grouping, printable Excel.

No inventory writes, no order dispatch. All positive source quantities must reconcile.
Do not assume seller from source SKU prefix when a marketplace provides only summary totals.
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
SIZE_ORDER = ("XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL", "6XL")
SIZE_CHOICES = set(SIZE_ORDER) | {"2XL", "OS", "ONE SIZE", "FREE SIZE", "FREESIZE"}
PARTY_SEQUENCE = ("NO PARTY", "V", "NK", "O", "LP", "NEEL")
# These are common party indicators, not a required brand/party vocabulary.
# Unknown repeated party codes are also learned from the uploaded file.
COMMON_PARTY = {"V", "NK", "O", "LP", "NEEL", "DEV", "UD"}
# Fallback only when input has no useful Color column. New colors in source are learned.
BASIC_COLORS = set("""BLACK WHITE BLUE RED GREEN PINK PURPLE YELLOW ORANGE BEIGE GREY GRAY
 NAVY RUST PEACH LEMON MUSTARD WINE MAROON CREAM BROWN TEAL OLIVE RAMA PISTA
 GOLD GOLDEN CHIKU SKY LIGHT DARK OFF RANI LAVENDER MINT KHAKI TAN MAGENTA
 EMERALD IVORY AQUA MULTI MAUVE COFFEE COFFE SILVER LILAC NEON MEHNDI SAGE
 NUDE CHERRY PARROT SEA ROSE ONION PURPLEE FIROZI FIROJI FUCHSIA CHOCOLATE""".split())
SOURCE_COLUMNS = ("master sku", "master_sku", "seller sku", "seller sku code", "sku code", "sku", "style code", "variant code", "product sku")
QTY_COLUMNS = ("qty", "quantity", "order qty", "ordered qty", "order quantity", "units", "pieces", "count")
SIZE_COLUMNS = ("size", "size name", "variant size")


class PicklistError(ValueError):
    pass


def _pick_column(keys, candidates):
    found = {str(k).strip().lower(): k for k in keys}
    return next((found[x] for x in candidates if x in found), None)


def _canon(v):
    # Preserve real design numbers: PINKPARI-2, NK-851 and BS-22141.
    # Remove only *parenthesized* short variant markers (A), (B), (1), (2).
    v = re.sub(r"\(\s*(?:[A-Z]|\d{1,2})\s*\)", "", str(v or "").upper())
    return re.sub(r"[_\s-]+", "_", v.strip()).strip("_")


def _parts(v):
    v = re.sub(r"\(\s*(?:[A-Z]|\d{1,2})\s*\)", "", str(v or "").upper())
    return [t.strip() for t in re.split(r"[_-]+", v) if t.strip()]


def _norm_size(v):
    s = str(v or "").strip().upper().replace(" ", "")
    return {"2XL": "XXL", "FREE": "FREE SIZE", "FREESIZE": "FREE SIZE", "ONESIZE": "ONE SIZE"}.get(s, s)


def _valid_size(v):
    return _norm_size(v) in SIZE_CHOICES or bool(re.fullmatch(r"\d{1,3}Y?", _norm_size(v)))


def _color_codes(v):
    text = _canon(v).replace("GRAY", "GREY").replace("COFFEE", "COFFE")
    if not text:
        return set()
    toks = text.split("_")
    return {text, toks[0], toks[-1]}


def _selected_date(source_filename, date_value):
    if date_value:
        try:
            return datetime.strptime(date_value, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError as exc:
            raise PicklistError("Date must be YYYY-MM-DD") from exc
    found = re.search(r"(20\d\d)[_-](\d\d)[_-](\d\d)", source_filename or "")
    if found:
        try:
            return datetime(*map(int, found.groups())).strftime("%d/%m/%Y")
        except ValueError:
            pass
    return datetime.now().strftime("%d/%m/%Y")


def _learn_tokens(source_rows):
    """Infer *recurring* brand prefixes and party codes without a fixed brand list.

    Ambiguous single examples are deliberately NOT auto-stripped from design.
    """
    first_to_second = defaultdict(set)
    party_evidence = Counter()
    color_tokens = set(BASIC_COLORS)
    for r in source_rows:
        color_tokens.update(_color_codes(r["color"]))
    for r in source_rows:
        p = r["tokens"]
        if len(p) >= 3:
            second = _canon(p[1])
            if second not in color_tokens and not _valid_size(second):
                first_to_second[p[0]].add(second)
        if len(p) >= 4 and _canon(p[1]) in COMMON_PARTY:
            party_evidence[p[0]] += 1
    brands = {k for k, seconds in first_to_second.items()
              if len(seconds) >= 2 or party_evidence[k] >= 1}
    # Single-character manufacturer prefix, e.g. M-PINKPARI-RANI-L.
    # 'EVIL_EYE_BLUE_S' remains a design, never a brand.
    brands.update(r["tokens"][0] for r in source_rows
                  if len(r["tokens"]) >= 4 and len(r["tokens"][0]) == 1
                  and r["tokens"][0] not in COMMON_PARTY)
    # Compact brand + numerical model code; do not strip SW297 itself.
    brands.update(r["tokens"][0] for r in source_rows
                  if len(r["tokens"]) >= 4 and len(r["tokens"][0]) <= 2
                  and re.fullmatch(r"\d+", r["tokens"][1]))
    learned_party = set(COMMON_PARTY)
    for brand in brands:
        candidates = defaultdict(set)
        for r in source_rows:
            p = r["tokens"]
            if len(p) >= 5 and p[0] == brand:
                possible = _canon(p[1])
                design_token = _canon(p[2])
                if possible not in color_tokens and design_token not in color_tokens and not _valid_size(design_token):
                    candidates[possible].add(design_token)
        learned_party.update(code for code, names in candidates.items() if len(names) >= 2)
    return brands, learned_party, color_tokens


def _get_seller_from_summary(records, total):
    """Only infer marketplace seller when exactly one seller summary equals total.

    Multiple marketplace summary counts do not give a row-level seller mapping.
    """
    cols = list(dict.fromkeys(k for r in records[:200] for k in r.keys()))
    market_col = _pick_column(cols, ("marketplace", "market place", "seller account"))
    count_col = _pick_column(cols, ("count", "marketplace count"))
    if not market_col or not count_col:
        return None
    sellers = {}
    for r in records:
        marketplace = str(r.get(market_col) or "")
        n = to_int(r.get(count_col))
        if n is None or not marketplace or marketplace.lower().strip() in {"total", "courier", "marketplace"}:
            continue
        # Marketplace summary examples: Myntra PPMP 2(MY_Twinslady),
        # Myntra PPMP(MY_MFB-Moda). Never match the "My" in Myntra.
        found = re.search(r"\(\s*MY[_ -]([^)]*)\)", marketplace, re.I)
        if not found:
            continue
        name = found.group(1).strip()
        name = re.sub(r"(?i)^MFB[-_ ]*", "", name)
        if name.upper() in {"TWINSLADY", "TWINS LADY"}:
            name = "TWINS"
        sellers[_canon(name)] = n
    if len(sellers) == 1:
        name, count = next(iter(sellers.items()))
        if count == total:
            return name
    return None


def _strip_color(body, row_color, known_colors):
    # Row's own color column is primary, learned color tokens secondary.
    # Only strip suffix, never a design number, and never the sole product token.
    original = list(body)
    choices = _color_codes(row_color) | known_colors
    while len(body) > 1 and _canon(body[-1]).replace("GRAY", "GREY").replace("COFFEE", "COFFE") in choices:
        body = body[:-1]
    return body, "_".join(original[len(body):])


def _size_order(v):
    s = _norm_size(v)
    try:
        return (0, SIZE_ORDER.index(s), "")
    except ValueError:
        return (1, int(s), "") if s.isdigit() else (2, 0, s)


def party_rank(party):
    return (PARTY_SEQUENCE.index(party), "") if party in PARTY_SEQUENCE else (len(PARTY_SEQUENCE), party)


def _sorted_items(items):
    return sorted(items, key=lambda x: (party_rank(x["party"]), x["design"], _size_order(x["size"]), x["pick_sku"]))


def transform(records, *, brand_override="AUTO", picklist_date=None, filename=""):
    if not records:
        raise PicklistError("Picklist empty")
    keys = list(dict.fromkeys(str(k) for r in records[:200] for k in r.keys()))
    sku_col = _pick_column(keys, SOURCE_COLUMNS)
    qty_col = _pick_column(keys, QTY_COLUMNS)
    size_col = _pick_column(keys, SIZE_COLUMNS)
    color_col = _pick_column(keys, ("color", "colour", "variant color"))
    brand_col = _pick_column(keys, ("seller brand", "seller_brand", "brand", "supplier brand"))
    party_col = _pick_column(keys, ("party", "party code", "party name"))
    if not sku_col:
        raise PicklistError("Master SKU / Seller SKU / SKU column missing")
    raw = []
    ignored = 0
    totals = []
    invalid = []
    for rownum, r in enumerate(records, 2):
        sku = str(r.get(sku_col) or "").strip()
        if sku.upper() in {"TOTAL", "GRAND TOTAL"}:
            val = to_int(r.get(qty_col)) if qty_col else None
            if val is not None:
                totals.append(val)
            ignored += 1
            continue
        if not sku or sku.upper() in {"SUBTOTAL", "SKU"}:
            ignored += 1
            continue
        amount = r.get(qty_col) if qty_col else 1
        qty = to_int(amount)
        if qty is None or qty <= 0 or (not isinstance(amount, (int, float)) and str(amount).strip() not in {str(qty), f"{qty}.0"}):
            invalid.append(rownum)
            continue
        p = _parts(sku)
        raw.append({"row":rownum, "original_sku":sku, "tokens":p, "qty":qty,
                    "supplied_size":r.get(size_col) if size_col else None,
                    "color":r.get(color_col) if color_col else None,
                    "given_brand":str(r.get(brand_col) or "").strip().upper() if brand_col else "",
                    "given_party":str(r.get(party_col) or "").strip().upper() if party_col else ""})
    if invalid:
        raise PicklistError("Invalid Qty; Excel rows: " + ", ".join(map(str, invalid[:20])))
    if not raw or len(raw) > MAX_SOURCE_ROWS:
        raise PicklistError("No valid SKU or too many rows")
    expected = sum(row["qty"] for row in raw)
    if totals and any(x != expected for x in totals):
        raise PicklistError(f"Source total mismatch: {totals} vs parsed {expected}")
    brands, party_codes, colors = _learn_tokens(raw)
    source_seller = _get_seller_from_summary(records, expected)
    # Even a mixed summary provides a grounded *candidate* seller name.
    # It does NOT identify which unlabelled SKU belongs to which seller.
    cols = list(dict.fromkeys(k for r in records[:200] for k in r.keys()))
    marketplace_col = _pick_column(cols, ("marketplace", "market place", "seller account"))
    if marketplace_col:
        for rec in records:
            raw_market = str(rec.get(marketplace_col) or "")
            found = re.search(r"\(\s*MY[_ -]([^)]*)\)", raw_market, re.I)
            if not found:
                continue
            name = re.sub(r"(?i)^MFB[-_ ]*", "", found.group(1).strip())
            if name.upper() in {"TWINSLADY", "TWINS LADY"}:
                name = "TWINS"
            normalized_name = _canon(name)
            if normalized_name:
                brands.add(normalized_name)
    # Keep override compatible with the old API; HTML no longer asks for brand.
    brand_override = str(brand_override or "AUTO").strip().upper()
    lines = []
    review = []
    for r in raw:
        toks = list(r["tokens"])
        notes = []
        if not toks:
            notes.append("SKU has no tokens")
            toks = [_canon(r["original_sku"])]
        party = "NO PARTY"
        brand = ""
        # Support PARTY_BRAND_PRODUCT and BRAND_PARTY_PRODUCT; never assume
        # every first token is a brand (EVIL_EYE is a valid full product name).
        if toks and _canon(toks[0]) in party_codes and len(toks) > 1:
            party = _canon(toks.pop(0))
            if toks and (toks[0] in brands or _canon(toks[0]) == source_seller):
                brand = _canon(toks.pop(0))
        elif toks and (toks[0] in brands or _canon(toks[0]) == source_seller or (len(toks)>2 and "&" in toks[0])):
            brand = _canon(toks.pop(0))
            if toks and _canon(toks[0]) in party_codes and len(toks) > 1:
                party = _canon(toks.pop(0))
        if r["given_party"]:
            if party != "NO PARTY" and _canon(r["given_party"]) != party:
                notes.append("SKU/column Party mismatch")
            party = _canon(r["given_party"])
        actual_brand = (brand_override if brand_override != "AUTO" else r["given_brand"] or source_seller or brand or "UNSPECIFIED")
        if actual_brand == "UNSPECIFIED":
            notes.append("Brand not explicit; product prefix preserved")
        suffix = _norm_size(toks[-1]) if toks else ""
        given_size = _norm_size(r["supplied_size"])
        if given_size and not _valid_size(given_size):
            notes.append("Invalid Size column: " + given_size)
            given_size = ""
        if given_size:
            size = given_size
            if suffix and _valid_size(suffix):
                if suffix != given_size:
                    notes.append(f"Size conflict SKU={suffix} column={given_size}; column used")
                toks.pop()
        elif suffix and _valid_size(suffix):
            size = suffix
            toks.pop()
        else:
            size = "UNKNOWN"
            notes.append("Size missing or unrecognised")
        original_design = _canon("_".join(toks))
        toks, removed = _strip_color(toks, r["color"], colors)
        design = _canon("_".join(toks)) or original_design
        if not design:
            design = _canon(r["original_sku"])
            notes.append("Design missing")
        prefix = "" if party in ("NO PARTY", "V") else party + "_"
        pick_sku = prefix + design + "_" + size
        line = {"row":r["row"], "original_sku":r["original_sku"], "brand":actual_brand,
                "sku_brand":brand or "", "party":party, "explicit_party":party if party != "NO PARTY" else None,
                "original_design":original_design, "design":design, "size":size,
                "qty":r["qty"], "color_removed":removed, "pick_sku":pick_sku,
                "note":"; ".join(notes)}
        lines.append(line)
        if notes:
            review.append({"row":r["row"], "sku":r["original_sku"], "qty":r["qty"], "party":party,
                           "issue":"; ".join(notes)})
    # Crucial: packing teams must distinguish real colors. Merge BLUE with a
    # no-color product only when that no-color SKU actually exists for the
    # SAME brand, party and size. Otherwise keep PINK vs PURPLE separate.
    # Example: EVIL_EYE_S + EVIL_EYE_BLUE_S => EVIL_EYE_S=2,
    # but HELLY_PINK_S + HELLY_PURPLE_S stays two physical pick lines.
    uncolored = {(line["brand"], line["party"], line["design"], line["size"])
                 for line in lines if not line["color_removed"]}
    for line in lines:
        if line["color_removed"] and (line["brand"],line["party"],line["design"],line["size"]) not in uncolored:
            line["design"] = line["original_design"]
            line["color_removed"] = ""
        prefix = "" if line["party"] in ("NO PARTY","V") else line["party"] + "_"
        line["pick_sku"] = prefix + line["design"] + "_" + line["size"]
    aggreg = defaultdict(lambda:{"qty":0,"source_rows":0})
    groups = defaultdict(lambda:{"qty":0,"source_rows":0, "brands":set()})
    for line in lines:
        identity = (line["brand"], line["party"], line["design"], line["size"])
        aggreg[identity]["qty"] += line["qty"]
        aggreg[identity]["source_rows"] += 1
        k = (line["party"], line["design"], line["size"])
        groups[k]["qty"] += line["qty"]
        groups[k]["source_rows"] += 1
        groups[k]["brands"].add(line["brand"])
    brand_items = []
    for (brand,party,design,size), v in aggreg.items():
        prefix = "" if party in ("NO PARTY", "V") else party + "_"
        brand_items.append({"brand":brand,"party":party,"design":design,"size":size,"qty":v["qty"],
                            "pick_sku":prefix+design+"_"+size,"source_rows":v["source_rows"]})
    items = []
    for (party,design,size), v in groups.items():
        prefix = "" if party in ("NO PARTY", "V") else party + "_"
        items.append({"party":party,"design":design,"size":size,"qty":v["qty"],
                      "pick_sku":prefix+design+"_"+size,"source_rows":v["source_rows"],
                      "brands":", ".join(sorted(v["brands"]))})
    if sum(x["qty"] for x in items) != expected or sum(x["qty"] for x in brand_items) != expected:
        raise PicklistError("Quantity reconciliation failed")
    party_totals = Counter()
    brand_totals = Counter()
    for row in lines:
        party_totals[row["party"]] += row["qty"]
        brand_totals[row["brand"]] += row["qty"]
    return {"total_qty":expected,"source_rows":len(lines),"merged_rows":len(brand_items),
            "picklist_date":_selected_date(filename,picklist_date),"brand_selection":brand_override,
            "skipped_non_item_rows":ignored,"review_count":len(review),
            "party_totals":dict(party_totals),"brand_totals":dict(brand_totals),
            "items":items,"brand_items":brand_items,"audit":lines,"review":review}




def apply_registered_master_skus(result: dict, db) -> dict:
    """Read-only catalog resolution for picklist EXPORT and PREVIEW.

    Use ONLY registered exact SKU codes (or their unambiguous legacy separators):
      * active stockable Variant codes and variant marketplace aliases;
      * product-wide aliases and Master SKUs with an explicit or suffix size.
    Never fuzzy-match a design, assume a size, or edit catalog/order stock.
    Unmapped and ambiguous rows retain the legacy printable SKU unchanged.
    """
    from collections import defaultdict
    from sqlalchemy.orm import joinedload
    from models import Product, Variant
    from business_models import MarketplaceSkuAlias, ProductMarketplaceAlias

    active = db.query(Variant).join(Product, Variant.product_id == Product.id).options(
        joinedload(Variant.product)
    ).filter(Variant.is_deleted.is_(False), Product.is_deleted.is_(False)).all()
    variants = {v.id: v for v in active}
    by_product_size = {(v.product_id, _norm_size(v.size)): v for v in active}
    code_to_variant = defaultdict(set)
    code_to_product = defaultdict(set)
    product_id_by_sku = defaultdict(set)
    for v in active:
        for code in (v.variant_code, v.seller_sku_code, v.marketplace_sku_code):
            if code:
                code_to_variant[str(code).strip().upper()].add(v.id)
        product_id_by_sku[str(v.product.sku).strip().upper()].add(v.product_id)
    for a in db.query(MarketplaceSkuAlias).all():
        if a.variant_id in variants:
            code_to_variant[str(a.normalized_code).strip().upper()].add(a.variant_id)
    active_product_ids = {v.product_id for v in active}
    for a in db.query(ProductMarketplaceAlias).all():
        if a.product_id in active_product_ids:
            code_to_product[str(a.normalized_code).strip().upper()].add(a.product_id)
    for code, ids in product_id_by_sku.items():
        code_to_product[code].update(ids)

    def candidate_keys(original: str):
        raw = str(original).strip().upper()
        # Preserve explicit matching first; never strip arbitrary alphanumeric
        # product suffixes. Legacy `SKU-S` vs `SKU_S` is checked separately.
        keys = [raw]
        normal = _canon(raw)
        if normal != raw:
            keys.append(normal)
        if '-' in raw or '_' in raw:
            for sep in ('-', '_'):
                before, found, after = raw.rpartition(sep)
                if before and found and _valid_size(after):
                    alternate = before + ('_' if sep == '-' else '-') + after
                    if alternate not in keys:
                        keys.append(alternate)
        return keys

    def one_variant(ids):
        return variants[next(iter(ids))] if len(ids) == 1 else None

    def find_variant(line):
        keys = candidate_keys(line['original_sku'])
        size = _norm_size(line['size'])
        # Conflicting exact codes are ambiguous and MUST NOT fall back to a
        # product-wide match. Avoid accidental SKU remapping.
        for key in keys:
            hits = code_to_variant.get(key, set())
            if hits:
                if len(hits) != 1:
                    return None, 'Ambiguous catalog SKU (multiple sizes/products)'
                variant = one_variant(hits)
                if variant and size not in ('', 'UNKNOWN') and _norm_size(variant.size) != size:
                    return None, f'Catalog size {_norm_size(variant.size)} conflicts with uploaded size {size}'
                return variant, None
        # Product-wide matching requires the size from sheet or SKU suffix.
        # Exact alias/product code is prioritized over removing any suffix.
        choices = []
        for key in keys:
            if key in code_to_product:
                choices.append((key, size))
            for sep in ('_', '-'):
                base, delimiter, ending = key.rpartition(sep)
                if delimiter and _valid_size(ending):
                    suffix_size = _norm_size(ending)
                    if size not in ('', 'UNKNOWN') and size != suffix_size:
                        continue
                    choices.append((base, suffix_size))
        matches = set()
        for key, chosen_size in choices:
            if chosen_size in ('', 'UNKNOWN') or not _valid_size(chosen_size):
                continue
            for product_id in code_to_product.get(key, ()):
                variant = by_product_size.get((product_id, chosen_size))
                if variant:
                    matches.add(variant.id)
        if len(matches) > 1:
            return None, 'Ambiguous product-wide catalog SKU'
        return one_variant(matches), None

    # Build a separate copy, so old importer logic remains unchanged.
    from copy import deepcopy
    output = deepcopy(result)
    mappings = 0
    mapped_qty = 0
    for line in output['audit']:
        variant, error = find_variant(line)
        if error:
            line['note'] = '; '.join(x for x in (line.get('note'), error) if x)
            output['review'].append({'row': line['row'], 'sku': line['original_sku'],
                'qty': line['qty'], 'party': line['party'], 'issue': error})
        if variant is None:
            continue
        master_sku = variant.product.sku.strip().upper()
        master_size = _norm_size(variant.size)
        master_print = f'{master_sku}_{master_size}'
        line['master_sku'] = master_sku
        line['master_variant_code'] = variant.variant_code
        line['pick_sku'] = master_print
        line['design'] = master_sku
        line['size'] = master_size
        mappings += 1
        mapped_qty += line['qty']

    # Some marketplaces omit the party on one of several registered aliases
    # for the SAME physical Master SKU and size.  When exactly ONE named party
    # exists for the brand/master/size, merge those NO PARTY rows into it.
    # Do not infer party for an unregistered design, or when known parties
    # conflict (e.g. V and NK), because that can misroute packing work.
    known_parties = defaultdict(set)
    for line in output['audit']:
        if line.get('master_sku') and line['party'] != 'NO PARTY':
            known_parties[(line['brand'], line['master_sku'], line['size'])].add(line['party'])
    for line in output['audit']:
        if not line.get('master_sku') or line['party'] != 'NO PARTY':
            continue
        choices = known_parties[(line['brand'], line['master_sku'], line['size'])]
        if len(choices) == 1:
            line['party'] = next(iter(choices))
            line['note'] = '; '.join(filter(None, (line.get('note'), 'Party grouped by exact registered Master SKU')))

    brand_groups = defaultdict(lambda: {'qty': 0, 'source_rows': 0})
    print_groups = defaultdict(lambda: {'qty': 0, 'source_rows': 0, 'brands': set()})
    for line in output['audit']:
        # Match IDs, rather than displayed text, distinguish mapped and
        # unknown rows even when their printed SKUs happen to be identical.
        identity = ('master', line['master_sku'], line['size']) if line.get('master_sku') else (
            'unmapped', line['design'], line['size'])
        brand_key = (line['brand'], line['party'], identity)
        global_key = (line['party'], identity)
        brand_groups[brand_key]['qty'] += line['qty']
        brand_groups[brand_key]['source_rows'] += 1
        brand_groups[brand_key]['print_sku'] = line['pick_sku']
        brand_groups[brand_key]['design'] = line['design']
        brand_groups[brand_key]['size'] = line['size']
        print_groups[global_key]['qty'] += line['qty']
        print_groups[global_key]['source_rows'] += 1
        print_groups[global_key]['brands'].add(line['brand'])
        print_groups[global_key]['print_sku'] = line['pick_sku']
        print_groups[global_key]['design'] = line['design']
        print_groups[global_key]['size'] = line['size']
    output['brand_items'] = [
        {'brand': brand, 'party': party, 'design': v['design'], 'size': v['size'],
         'qty': v['qty'], 'pick_sku': v['print_sku'], 'source_rows': v['source_rows']}
        for (brand, party, _identity), v in brand_groups.items()
    ]
    output['items'] = [
        {'party': party, 'design': v['design'], 'size': v['size'], 'qty': v['qty'],
         'pick_sku': v['print_sku'], 'source_rows': v['source_rows'],
         'brands': ', '.join(sorted(v['brands']))}
        for (party, _identity), v in print_groups.items()
    ]
    if sum(x['qty'] for x in output['brand_items']) != output['total_qty'] or sum(
            x['qty'] for x in output['items']) != output['total_qty']:
        raise PicklistError('Master SKU mapping broke quantity reconciliation')
    output['merged_rows'] = len(output['brand_items'])
    output['party_totals'] = dict(Counter({party: sum(line['qty'] for line in output['audit'] if line['party'] == party)
                                            for party in {line['party'] for line in output['audit']}}))
    output['brand_totals'] = dict(Counter({brand: sum(line['qty'] for line in output['audit'] if line['brand'] == brand)
                                            for brand in {line['brand'] for line in output['audit']}}))
    output['review_count'] = len(output['review'])
    output['master_sku_mapped_rows'] = mappings
    output['master_sku_mapped_qty'] = mapped_qty
    return output


def parse_upload(payload, filename, *, brand_override="AUTO", picklist_date=None):
    if not payload or len(payload) > MAX_UPLOAD_BYTES:
        raise PicklistError("Excel empty or over 12 MB")
    if not filename.lower().endswith((".xlsx", ".xls", ".csv")):
        raise PicklistError("Upload .xlsx/.xls/.csv")
    try:
        frame = read_table(payload, filename)
    except Exception as exc:
        raise PicklistError("Excel could not be read: " + str(exc)[:200]) from exc
    if len(frame) > MAX_SOURCE_ROWS + 1:
        raise PicklistError("Too many rows")
    return transform(frame.where(frame.notna(), None).to_dict(orient="records"),
                     brand_override=brand_override,picklist_date=picklist_date,filename=filename)


def _pages(items, *, row_limit=39, max_lanes=6):
    """Use 1..6 SKU/Qty blocks and real row breaks, not merged cells.

    Attempt extra blocks before creating a nearly empty overflow page. Families
    stay together whenever possible, and no printed page exceeds 40 rows.
    """
    sorted_items = _sorted_items(items)
    needed = max(1, ceil(len(sorted_items) / row_limit))
    families = defaultdict(list)
    for item in sorted_items:
        families[(item["party"], item["design"])].append(item)

    def layout(lane_count):
        pages = []
        page = [[] for _ in range(lane_count)]
        lane = 0
        for family in families.values():
            waiting = list(family)
            while waiting:
                remaining = row_limit - len(page[lane])
                if remaining == 0 or (len(waiting) <= row_limit and len(waiting) > remaining and page[lane]):
                    lane += 1
                    if lane == lane_count:
                        pages.append(page)
                        page = [[] for _ in range(lane_count)]
                        lane = 0
                    remaining = row_limit - len(page[lane])
                batch, waiting = waiting[:remaining], waiting[remaining:]
                page[lane].extend(batch)
                if waiting:
                    lane += 1
                    if lane == lane_count:
                        pages.append(page)
                        page = [[] for _ in range(lane_count)]
                        lane = 0
        if any(page):
            pages.append(page)
        return pages

    for lanes in range(min(max_lanes, needed), max_lanes+1):
        attempt = layout(lanes)
        if len(attempt) <= 1:
            return attempt
    return layout(max_lanes)


def _sheet_name(name: str, used: set[str]) -> str:
    stem = re.sub(r"[\\/*?:\[\]]", "-", name).strip()[:31] or "Sheet"
    candidate, suffix = stem, 2
    while candidate.lower() in used:
        candidate = f"{stem[:27]}_{suffix}"
        suffix += 1
    used.add(candidate.lower())
    return candidate


def create_sheet_data(result: dict, *, include_brand_pages=True, include_audit=False):
    """One sheet per brand/day; 1..6 blocks, multiple PRINT pages vertically.

    Header+39 data rows per printed page. No merged cells and no TOTAL row.
    For each brand, all pages are part of the *same Excel sheet*.
    """
    date = result["picklist_date"]
    brands = defaultdict(list)
    for r in result["brand_items"]:
        brands[r["brand"]].append(r)
    output = []
    for brand, items in sorted(brands.items()):
        pages = _pages(items)
        nlanes = len(pages[0])
        cols = nlanes * 3 - 1
        rows = []
        for page in pages:
            header = [None]*cols
            header[0] = brand + "-" + date
            rows.append(header)
            for i in range(39):
                line = [None]*cols
                for laneidx, lane in enumerate(page):
                    if i < len(lane):
                        line[laneidx * 3] = lane[i]["pick_sku"]
                        line[laneidx * 3 + 1] = lane[i]["qty"]
                rows.append(line)
        # Remove blank trailing rows on LAST page; previous pages stay exactly 40.
        while rows and all(x is None for x in rows[-1]):
            rows.pop()
        output.append((f"{brand}-{date.replace('/', '-')}", rows, "packing"))
    if not output:
        raise PicklistError("Nothing to print")
    if include_audit:
        checks=[["Source Row","SKU","Qty","Issue","Party"]]
        checks += [[x["row"],x["sku"],x["qty"],x["issue"],x["party"]] for x in result["review"]]
        output.append(("Review",checks,"review"))
        audits=[["Row","Original SKU","Brand","Party","Design Before","Design After","Size","Qty","Printed SKU","Note"]]
        audits += [[x["row"],x["original_sku"],x["brand"],x["party"],x["original_design"],x["design"],x["size"],x["qty"],x["pick_sku"],x["note"]] for x in result["audit"]]
        output.append(("Source Audit",audits,"audit"))
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
                       '<cols>' + ''.join(f'<col min="{i+1}" max="{i+1}" width="{42 if i%3==0 else 9 if i%3==1 else 3}" customWidth="1"/>' for i in range(len(rows[0]))) + '</cols>']
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
            # Manual horizontal page breaks: row 41, 81, etc. (0-based ids 40,80).
            count_pages = (len(rows)+39)//40
            if count_pages > 1:
                breaks = "".join(f'<brk id="{i*40}" min="0" max="16383" man="1"/>' for i in range(1,count_pages))
                output.append(f'<rowBreaks count="{count_pages-1}" manualBreakCount="{count_pages-1}">{breaks}</rowBreaks>')
            output.extend(['<printOptions horizontalCentered="1"/>',
                '<pageMargins left="0.1" right="0.1" top="0.15" bottom="0.15" header="0" footer="0"/>',
                '<pageSetup paperSize="8" orientation="landscape" fitToWidth="1" fitToHeight="0"/>'])
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
        '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFFFFF"/><bgColor indexed="64"/></patternFill></fill></fills>' \
        '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"><color rgb="FF000000"/></left><right style="thin"><color rgb="FF000000"/></right><top style="thin"><color rgb="FF000000"/></top><bottom style="thin"><color rgb="FF000000"/></bottom><diagonal/></border></borders>' \
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' \
        '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="3" fillId="0" borderId="1" xfId="0"/><xf numFmtId="0" fontId="3" fillId="0" borderId="1" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
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
