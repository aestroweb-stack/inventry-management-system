"""
One place that turns an uploaded .xlsx / .xls / .csv into a DataFrame.

Everything is read as TEXT (dtype=str): SKUs like "3202" must stay "3202",
never become 3202.0, and marketplace order exports are often CSV, not xlsx.
Numbers (quantities, counts) are parsed explicitly with to_int() below.
"""
from io import BytesIO

import pandas as pd


def read_table(file_bytes: bytes, filename: str = "") -> pd.DataFrame:
    """.xlsx (openpyxl), legacy .xls (xlrd), or .csv - by extension, falling
    back to sniffing the bytes if the extension is missing or wrong (browsers
    and marketplace panels don't always send it correctly)."""
    name = (filename or "").lower()
    engine = "xlrd" if name.endswith(".xls") and not name.endswith(".xlsx") else "openpyxl"
    try:
        if name.endswith(".csv"):
            return pd.read_csv(BytesIO(file_bytes), dtype=str, keep_default_na=True)
        if name.endswith((".xlsx", ".xls", ".xlsm")):
            return pd.read_excel(BytesIO(file_bytes), dtype=str, engine=engine)
    except Exception:
        pass  # extension lied about the real format - sniff the bytes instead below

    head = file_bytes[:8]
    try:
        if head.startswith(b"PK"):        # .xlsx/.xlsm are zip archives
            return pd.read_excel(BytesIO(file_bytes), dtype=str, engine="openpyxl")
        if head.startswith(b"\xd0\xcf\x11\xe0"):  # legacy .xls binary signature
            return pd.read_excel(BytesIO(file_bytes), dtype=str, engine="xlrd")
        first_line = file_bytes[:2000].split(b"\n", 1)[0]
        if any(sep in first_line for sep in (b",", b"\t", b";")):
            # structurally looks like delimited text - worth trying as CSV
            df = pd.read_csv(BytesIO(file_bytes), dtype=str, keep_default_na=True)
            if df.shape[1] > 1:
                return df
        raise ValueError("not a recognisable Excel or CSV file")
    except Exception as exc:
        raise ValueError(
            f"Could not read this file as Excel or CSV ({type(exc).__name__}). "
            "Save it as .xlsx or .csv and try again."
        ) from exc


def to_int(value):
    """'5', '5.0', 5, 5.0 -> 5.   Blank / junk -> None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


def clean_code(value) -> str | None:
    """Normalise a code cell: strip, uppercase. Blank -> None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip().upper()
    return text or None
