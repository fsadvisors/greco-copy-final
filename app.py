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
1. Upload your **GSTR-2B** and **Purchase Register** files (any header format, .xls or .xlsx).  
2. Tweak thresholds if needed.  
3. Click **Process Reconciliation**.  
4. View **Summary** & **Details**.  
5. Download your report.
""")
st.sidebar.markdown("---")
st.sidebar.markdown("Powered by **Felicity Strategic Advisors**")

# ── CONSTANTS ──
STANDARD_HEADERS = [
    "SupplierName","InvoiceNo","InvoiceDate","GSTIN",
    "InvoiceValue","TaxableValue","CGST","SGST","IGST","CESS"
]

HEADER_ALIASES = {
    "supplier name":  "SupplierName",
    "party name":     "SupplierName",
    "vendor name":    "SupplierName",
    "invoice no":     "InvoiceNo",
    "inv no":         "InvoiceNo",
    "voucher":        "InvoiceNo",
    "gstin/uin":      "GSTIN",
    "gst no":         "GSTIN",
    "invoice date":   "InvoiceDate",
    "bill date":      "InvoiceDate",
    "invoice value":  "InvoiceValue",
    "taxable value":  "TaxableValue",
    "cgst amount":    "CGST",
    "sgst amount":    "SGST",
    "igst amount":    "IGST",
    "cess amount":    "CESS",
}

def map_headers(raw_cols):
    mapping = {}
    used = set()
    raw_lower = [c.strip().lower() for c in raw_cols]

    # 1) exact alias
    for i, col in enumerate(raw_cols):
        key = raw_lower[i]
        if key in HEADER_ALIASES:
            mapping[col] = HEADER_ALIASES[key]
            used.add(col)

    # 2) keyword rules
    for i, col in enumerate(raw_cols):
        if col in used: continue
        key = raw_lower[i]
        if any(k in key for k in ["supplier","party","vendor"]):
            mapping[col] = "SupplierName"; used.add(col); continue
        if any(k in key for k in ["invoice no","inv no","voucher","bill no"]):
            mapping[col] = "InvoiceNo"; used.add(col); continue
        if "date" in key:
            mapping[col] = "InvoiceDate"; used.add(col); continue
        if "gstin" in key or "gst no" in key:
            mapping[col] = "GSTIN"; used.add(col); continue
        if "invoice value" in key or "total invoice" in key:
            mapping[col] = "InvoiceValue"; used.add(col); continue
        if "taxable" in key:
            mapping[col] = "TaxableValue"; used.add(col); continue
        if "cgst" in key:
            mapping[col] = "CGST"; used.add(col); continue
        if "sgst" in key:
            mapping[col] = "SGST"; used.add(col); continue
        if "igst" in key:
            mapping[col] = "IGST"; used.add(col); continue
        if "cess" in key:
            mapping[col] = "CESS"; used.add(col); continue

    # 3) fuzzy fallback
    for std in STANDARD_HEADERS:
        if std in mapping.values(): continue
        best, best_score = None, -1.0
        for i, col in enumerate(raw_cols):
            if col in used: continue
            score = SequenceMatcher(None, std.lower(), raw_lower[i]).ratio()
            if score > best_score:
                best_score, best = score, col
        mapping[best] = std
        used.add(best)

    return mapping

def read_with_header_detection(uploaded_file):
    ext = Path(uploaded_file.name).suffix.lower()
    engine = "xlrd" if ext == ".xls" else "openpyxl"

    peek = pd.read_excel(uploaded_file, header=None, engine=engine)
    candidates = [
        "supplier","party","vendor",
        "invoice","voucher","bill","date","gstin","gst no",
        "value","taxable","cgst","sgst","igst","cess"
    ]
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

def clean_and_standardize(df: pd.DataFrame, suffix: str) -> pd.DataFrame:
    df0 = df.copy()
    df0.columns = df0.columns.str.strip()
    mapping = map_headers(df0.columns.tolist())

    # build std→raw, picking first raw for each std
    std_to_raw = {}
    for raw, std in mapping.items():
        if std not in std_to_raw:
            std_to_raw[std] = raw

    # select exactly one raw per standard, in order
    selected = [std_to_raw[std] for std in STANDARD_HEADERS]
    df_sel = df0[selected].copy()
    df_sel.columns = STANDARD_HEADERS[:]
    df_sel.columns = [f"{c}{suffix}" for c in df_sel.columns]

    # drop dupes & blanks
    df_sel = (
        df_sel
        .drop_duplicates()
        .replace(r'^\s*$', np.nan, regex=True)
        .dropna(subset=[f"InvoiceNo{suffix}", f"GSTIN{suffix}"], how="any")
    )
    amt_cols = [f"{c}{suffix}" for c in ("InvoiceValue","TaxableValue","IGST","CGST","SGST")]
    df_sel = df_sel.dropna(subset=amt_cols, how="all").reset_index(drop=True)

    # parse date
    df_sel[f"InvoiceDate{suffix}"] = pd.to_datetime(
        df_sel[f"InvoiceDate{suffix}"],
        errors="coerce",
        infer_datetime_format=True,
        dayfirst=True
    ).dt.date

    # normalize InvoiceNo: remove trailing “.0” on floats, strip non-alphanumerics, uppercase
    inv = df_sel[f"InvoiceNo{suffix}"].astype(str).str.strip()
    inv = inv.str.replace(r"\.0$", "", regex=True)
    inv = inv.str.replace(r"\W+", "", regex=True).str.upper()
    df_sel[f"InvoiceNo{suffix}"] = inv

    # clean GSTIN & extract PAN
    gst = (
        df_sel[f"GSTIN{suffix}"]
        .astype(str)
        .str.replace(r"\W+", "", regex=True)
        .str.upper()
    )
    df_sel[f"GSTIN{suffix}"] = gst
    df_sel[f"PAN{suffix}"] = gst.str.slice(2, 12)

    return df_sel

def get_suffix(fn: str) -> str:
    lf = fn.lower()
    if "portal" in lf: return "_portal"
    if "books"  in lf: return "_books"
    raise ValueError("Filename must include 'portal' or 'books'")

def make_remark_logic(row, g_sfx, b_sfx, amt_tol, date_tol):
    def getval(r,c):
        v = r.get(c,""); return "" if pd.isna(v) or not str(v).strip() else v
    def norm_id(s):  return re.sub(r"[\W_]+","",str(s)).lower()
    def strip_ws(s): return re.sub(r"\s+","",str(s)).lower()
    def sim(a,b):    return SequenceMatcher(None,a,b).ratio()

    mismatches, trivial = [], False
    gst_cols   = [f"{c}{g_sfx}" for c in STANDARD_HEADERS]
    books_cols = [f"{c}{b_sfx}" for c in STANDARD_HEADERS]

    if all(getval(row,c)=="" for c in gst_cols):   return "❌ Not in 2B"
    if all(getval(row,c)=="" for c in books_cols): return "❌ Not in books"

    # date
    bd = pd.to_datetime(row.get(f"InvoiceDate{b_sfx}"), errors="coerce")
    gd = pd.to_datetime(row.get(f"InvoiceDate{g_sfx}"), errors="coerce")
    if pd.notna(bd) and pd.notna(gd):
        d = abs((bd - gd).days)
        if d==0: pass
        elif d<=date_tol: trivial=True
        else: mismatches.append("⚠️ Mismatch of InvoiceDate")

    # invoice no
    bno,gno = getval(row,f"InvoiceNo{b_sfx}"),getval(row,f"InvoiceNo{g_sfx}")
    if norm_id(bno)!=norm_id(gno): mismatches.append("⚠️ Mismatch of InvoiceNo")
    elif strip_ws(bno)!=strip_ws(gno): trivial=True

    # GSTIN
    bg,gg = str(getval(row,f"GSTIN{b_sfx}")).lower(),str(getval(row,f"GSTIN{g_sfx}")).lower()
    if bg and gg and bg!=gg: mismatches.append("⚠️ Mismatch of GSTIN")

    # amounts
    for fld in ["InvoiceValue","TaxableValue","IGST","CGST","SGST","CESS"]:
        bv = row.get(f"{fld}{b_sfx}",0) or 0
        gv = row.get(f"{fld}{g_sfx}",0) or 0
        try:
            diff = abs(float(bv)-float(gv))
            if diff>amt_tol: mismatches.append(f"⚠️ Mismatch of {fld}")
            elif diff>0:     trivial=True
        except: pass

    # supplier name
    bp = str(getval(row,f"SupplierName{b_sfx}"))
    gp = str(getval(row,f"SupplierName{g_sfx}"))
    sc = sim(re.sub(r"[^\w\s]","",bp).lower(),re.sub(r"[^\w\s]","",gp).lower())
    if sc<0.8:   mismatches.append("⚠️ Mismatch of SupplierName")
    elif sc<1.0: trivial=True

    if mismatches: return " & ".join(dict.fromkeys(mismatches))
    if trivial:    return "✅ Matched, trivial error"
    return "✅ Matched"

# ── UPLOAD & PROCESS ──
col1, col2 = st.columns(2)
with col1:
    gst_file   = st.file_uploader("GSTR-2B Excel",   type=["xls","xlsx"])
with col2:
    books_file = st.file_uploader("Purchase Register Excel", type=["xls","xlsx"])

with st.expander("⚙️ Threshold Settings", expanded=True):
    amt_threshold = st.selectbox("Amount diff threshold", [0.01,0.1,1,10,100], index=0)
    date_threshold= st.selectbox("Date diff threshold (days)", [1,2,3,4,5,6],  index=4)

if gst_file and books_file:
    raw_gst  = read_with_header_detection(gst_file)
    raw_books= read_with_header_detection(books_file)
    s1       = get_suffix(gst_file.name)
    s2       = get_suffix(books_file.name)

    df1 = clean_and_standardize(raw_gst,  s1)
    df2 = clean_and_standardize(raw_books,s2)

    df1["key"] = df1[f"InvoiceNo{s1}"].astype(str)+"_"+df1[f"GSTIN{s1}"]
    df2["key"] = df2[f"InvoiceNo{s2}"].astype(str)+"_"+df2[f"GSTIN{s2}"]
    merged = pd.merge(df1, df2, on="key", how="outer", suffixes=(s1,s2))
    merged["Remarks"] = merged.apply(
        lambda r: make_remark_logic(r,s1,s2,amt_threshold,date_threshold),
        axis=1
    )

    st.success("✅ Reconciliation Complete!")
    st.session_state.merged = merged

# ── SUMMARY & DOWNLOAD ──
if "merged" in st.session_state:
    df = st.session_state.merged
    st.subheader("📊 Summary")
    counts = {
        "matched":  int(df.Remarks.eq("✅ Matched").sum()),
        "trivial":  int(df.Remarks.str.contains("trivial").sum()),
        "mismatch": int(df.Remarks.str.contains("⚠️").sum()),
        "missing":  int(df.Remarks.str.contains("❌").sum()),
    }
    c1,c2,c3,c4 = st.columns(4)
    if c1.button(f"✅ Matched\n{counts['matched']}"):    st.session_state.filter="matched"
    if c2.button(f"✅ Trivial\n{counts['trivial']}"):   st.session_state.filter="trivial"
    if c3.button(f"⚠️ Mismatch\n{counts['mismatch']}"): st.session_state.filter="mismatch"
    if c4.button(f"❌ Missing\n{counts['missing']}"):   st.session_state.filter="missing"

    flt = st.session_state.get("filter",None)
    def filter_df(df,cat):
        if cat=="matched":   return df[df.Remarks=="✅ Matched"]
        if cat=="trivial":   return df[df.Remarks.str.contains("trivial")]
        if cat=="mismatch":  return df[df.Remarks.str.contains("⚠️")]
        if cat=="missing":   return df[df.Remarks.str.contains("❌")]
        return df

    sub = filter_df(df,flt).drop(columns=["key"],errors="ignore")
    if sub.empty:
        st.info("No records in this category.")
    else:
        page_size = 30
        total     = len(sub)
        pages     = (total-1)//page_size + 1
        page      = st.number_input("Page",1,pages,value=1)
        st.dataframe(sub.iloc[(page-1)*page_size : page*page_size], height=400)

        buf = io.BytesIO()
        sub.to_excel(buf,index=False)
        buf.seek(0)
        st.download_button(
            "Download Filtered Report",
            data=buf,
            file_name="filtered_report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
