import warnings
import streamlit as st
import pandas as pd
import numpy as np
import io
import re
from pathlib import Path
from difflib import SequenceMatcher

# ── SILENCE DATE-PARSING WARNINGS ──
warnings.filterwarnings(
    "ignore",
    message="Could not infer format, so each element will be parsed individually"
)

# ── PAGE CONFIG ──
st.set_page_config(page_title="GReco.AI", page_icon="🧾", layout="wide")

# ── SIDEBAR ──
st.sidebar.title("GReco.AI")
st.sidebar.markdown("AI-powered GST ITC reconciliation for CAs.")
with st.sidebar.expander("🆘 Help & Usage", expanded=True):
    st.markdown("""
1. Upload your **GSTR-2B** and **Purchase Register** files (.xls or .xlsx).  
2. Tweak **Threshold Settings** if needed.  
3. Click **Process Reconciliation**.  
4. Explore **Summary** & **Details**.  
5. Download your report.
""")
st.sidebar.markdown("---")
st.sidebar.markdown("Powered by **Felicity Strategic Advisors**")

# ── CONSTANTS ──
STANDARD_HEADERS = [
    "SupplierName","InvoiceNo","InvoiceDate","GSTIN",
    "InvoiceValue","TaxableValue","CGST","SGST","IGST","CESS"
]

# ── CONTENT-AWARE HEADER MAPPER ──
GSTIN_RE = re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$')

def map_headers(df: pd.DataFrame) -> dict:
    """
    Map each raw column to exactly one STANDARD_HEADERS
    by combining header-name similarity with content cues.
    """
    raw_cols = df.columns.tolist()
    n = len(df)

    def norm(s): 
        return re.sub(r'\W+', ' ', s).strip().lower()

    raw_norm = [norm(c) for c in raw_cols]
    std_norm = [norm(s) for s in STANDARD_HEADERS]

    # header similarity matrix
    header_sim = {
        (i, j): SequenceMatcher(None, raw_norm[i], std_norm[j]).ratio()
        for i in range(len(raw_cols))
        for j in range(len(STANDARD_HEADERS))
    }

    # content metrics per raw column
    metrics = {}
    for col in raw_cols:
        ser = df[col].dropna().astype(str)
        cnt = len(ser)
        uniq   = ser.nunique() / n if n else 0
        num    = ser.str.match(r'^-?\d+(\.\d+)?$').sum() / cnt if cnt else 0
        date   = ser.apply(lambda v: bool(pd.to_datetime(v, errors='coerce'))).sum() / cnt if cnt else 0
        gstin  = ser.str.match(GSTIN_RE).sum() / cnt if cnt else 0
        alnum  = ser.apply(lambda v: bool(re.search(r'[A-Za-z]', v) and re.search(r'\d', v))).sum() / cnt if cnt else 0
        metrics[col] = {
            'unique': uniq,
            'numeric': num,
            'date': date,
            'gstin': gstin,
            'alnum': alnum,
        }

    # assignment order: (standard, header_weight, content_key, content_weight)
    order = [
        ("GSTIN",        0.3, 'gstin',   0.7),
        ("InvoiceDate",  0.3, 'date',    0.7),
        ("InvoiceValue", 0.3, 'numeric', 0.7),
        ("TaxableValue", 0.3, 'numeric', 0.7),
        ("CGST",         0.3, 'numeric', 0.7),
        ("SGST",         0.3, 'numeric', 0.7),
        ("IGST",         0.3, 'numeric', 0.7),
        ("CESS",         0.3, 'numeric', 0.7),
        ("InvoiceNo",    0.3, 'alnum',   0.7),
        ("SupplierName", 0.3, 'unique',  0.7),
    ]

    used_raw = set()
    mapping = {}

    for std, wh, ck, wc in order:
        best_col, best_score = None, -1.0
        std_idx = STANDARD_HEADERS.index(std)
        for i, col in enumerate(raw_cols):
            if col in used_raw:
                continue
            h = header_sim[(i, std_idx)]
            c = metrics[col][ck]
            score = wh * h + wc * c
            if score > best_score:
                best_score, best_col = score, col
        if best_col is None:
            raise KeyError(f"Could not map any column to '{std}'")
        mapping[best_col] = std
        used_raw.add(best_col)

    return mapping

# ── DYNAMIC HEADER DETECTION ──
def read_with_header_detection(uploaded_file):
    ext = Path(uploaded_file.name).suffix.lower()
    engine = "xlrd" if ext == ".xls" else "openpyxl"

    peek = pd.read_excel(uploaded_file, header=None, engine=engine)
    candidates = ["supplier","party","vendor","invoice","voucher","bill","date",
                  "gstin","value","taxable","cgst","sgst","igst","cess"]
    best_idx, best_score = 0, -1
    for i, row in peek.head(10).iterrows():
        score = sum(
            1 for cell in row
            if isinstance(cell, str) and any(tok in cell.lower() for tok in candidates)
        )
        if score > best_score:
            best_score, best_idx = score, i

    uploaded_file.seek(0)
    return pd.read_excel(uploaded_file, header=best_idx, engine=engine)

# ── CLEAN & STANDARDIZE DATA ──
def clean_and_standardize(raw: pd.DataFrame, suffix: str) -> pd.DataFrame:
    df = raw.copy()
    df.columns = df.columns.str.strip()

    # 1) map headers
    mapping = map_headers(df)

    # 2) pick exactly one raw col for each standard
    std_to_raw = {std: raw_col for raw_col, std in mapping.items()}
    selected = [std_to_raw[s] for s in STANDARD_HEADERS]

    df_sel = df[selected].copy()
    df_sel.columns = STANDARD_HEADERS[:]
    df_sel.columns = [f"{c}{suffix}" for c in STANDARD_HEADERS]

    # 3) drop dupes, blanks
    df_sel = (
        df_sel.drop_duplicates()
              .replace(r'^\s*$', np.nan, regex=True)
              .dropna(subset=[f"InvoiceNo{suffix}", f"GSTIN{suffix}"], how="any")
    )
    amt_cols = [f"{c}{suffix}" for c in ("InvoiceValue","TaxableValue","IGST","CGST","SGST")]
    df_sel = df_sel.dropna(subset=amt_cols, how="all").reset_index(drop=True)

    # 4) parse dates
    df_sel[f"InvoiceDate{suffix}"] = (
        pd.to_datetime(df_sel[f"InvoiceDate{suffix}"],
                       errors="coerce",
                       infer_datetime_format=True,
                       dayfirst=True)
          .dt.date
    )

    # 5) normalize InvoiceNo
    inv = df_sel[f"InvoiceNo{suffix}"].astype(str).str.strip()
    inv = inv.str.replace(r"\.0$",    "", regex=True)
    inv = inv.str.replace(r"\W+",      "", regex=True).str.upper()
    df_sel[f"InvoiceNo{suffix}"] = inv

    # 6) clean GSTIN & extract PAN
    gst = (
        df_sel[f"GSTIN{suffix}"]
        .astype(str)
        .str.replace(r"\W+", "", regex=True)
        .str.upper()
    )
    df_sel[f"GSTIN{suffix}"] = gst
    df_sel[f"PAN{suffix}"]   = gst.str.slice(2, 12)

    return df_sel

def get_suffix(fn: str) -> str:
    lf = fn.lower()
    if "portal" in lf: return "_portal"
    if "books"  in lf: return "_books"
    raise ValueError("Filename must include 'portal' or 'books'")

# … the rest of your reconcile + remark + UI code stays exactly the same …
# i.e. read files, clean_and_standardize, build key, merge, make_remark_logic, show tables, etc.
