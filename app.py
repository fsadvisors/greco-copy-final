import streamlit as st
import pandas as pd
import gdown
import requests
from bs4 import BeautifulSoup
import os

st.set_page_config(page_title="Greco.AI Reconciliation", page_icon="🧾", layout="wide")

st.sidebar.title("Greco.AI")
st.sidebar.markdown("Automated reconciliation via public Google Drive folder.")

# ---- USER INPUT ----
FOLDER_ID = "1bY4lSwcbn2evjRcpXIpF5N-mdWJHGV3m"
default_folder_url = f"https://drive.google.com/drive/folders/{FOLDER_ID}"
folder_url = st.sidebar.text_input("Google Drive Public Folder URL", value=default_folder_url)

# ---- SCRAPE GOOGLE DRIVE FOLDER ----
def get_gdrive_file_links(folder_url):
    # Get the folder page and scrape file links
    res = requests.get(folder_url)
    soup = BeautifulSoup(res.text, "html.parser")
    links = []
    for tag in soup.find_all("a"):
        href = tag.get("href")
        if href and "/file/d/" in href:
            # Find file ID and name from nearby tag
            file_id = href.split("/file/d/")[1].split("/")[0]
            # File title is not directly available, so we use the Google Drive download link and fuzzy match name from the text.
            links.append({
                "id": file_id,
                "gdown_url": f"https://drive.google.com/uc?id={file_id}",
                "page_url": "https://drive.google.com" + href,
                "label": tag.text.strip()
            })
    # Remove duplicates
    uniq = {x["id"]: x for x in links}
    return list(uniq.values())

def find_latest_file(files, keyword):
    # Returns the first file (likely latest) matching keyword
    filtered = [f for f in files if keyword.lower() in f["label"].lower()]
    # Optionally, sort by label or something else if you use timestamps in file names
    return filtered[0] if filtered else None

def download_file(url, output_path):
    # Use gdown for reliable Drive download
    gdown.download(url, output_path, quiet=True, fuzzy=True)

# ---- MAIN ----
st.header("📥 Automated File Fetch from Google Drive Folder")

files = []
if folder_url:
    files = get_gdrive_file_links(folder_url)
    if not files:
        st.warning("No files found in this folder. Make sure the folder is public!")
    else:
        st.success(f"Found {len(files)} file(s) in Google Drive folder.")
        with st.expander("Show all detected files"):
            for f in files:
                st.markdown(f"- {f['label']}")

# ---- Find GST and Books Files ----
gst_file_info = find_latest_file(files, "_gst")
books_file_info = find_latest_file(files, "_books")

# ---- Download and Load Data ----
def load_any_file(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".csv":
        return pd.read_csv(filepath)
    elif ext in [".xls", ".xlsx"]:
        return pd.read_excel(filepath)
    else:
        return None

gst_path = books_path = None
df_gst = df_books = None

if gst_file_info:
    gst_path = "/tmp/gst_data" + (".csv" if gst_file_info["label"].lower().endswith(".csv") else ".xlsx")
    download_file(gst_file_info["gdown_url"], gst_path)
    df_gst = load_any_file(gst_path)
    st.success(f"GST Data: {gst_file_info['label']} loaded!")

if books_file_info:
    books_path = "/tmp/books_data" + (".csv" if books_file_info["label"].lower().endswith(".csv") else ".xlsx")
    download_file(books_file_info["gdown_url"], books_path)
    df_books = load_any_file(books_path)
    st.success(f"Books Data: {books_file_info['label']} loaded!")

if not gst_file_info or not books_file_info:
    st.warning("Did not find both GST and Books files (_gst, _books) in the folder.")

# ---- Display Preview ----
if df_gst is not None:
    st.subheader("GST Data (Preview)")
    st.dataframe(df_gst.head())

if df_books is not None:
    st.subheader("Books Data (Preview)")
    st.dataframe(df_books.head())

if df_gst is not None and df_books is not None:
    st.success("Both files loaded! You can proceed with your reconciliation logic here.")
    # ...Insert your reconciliation logic below...
