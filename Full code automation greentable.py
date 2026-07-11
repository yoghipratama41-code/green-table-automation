import os
import re
import json
import time
import random
import base64
import requests
from datetime import datetime, timedelta

import easyocr
import gspread
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageEnhance

from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.auth.transport.requests import Request as GoogleAuthRequest
import google.generativeai as genai

# Tambahan library untuk Google Slides & Drive API
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ==========================================
# 0. KONFIGURASI HALAMAN
# ==========================================
st.set_page_config(page_title="GNS Greentable & Gems Automator", page_icon="🟢", layout="wide")

SPREADSHEET_ID = "1FvJBZvtjKQ3QiZEHCOic2lZtegbuRK0wmsE6jCGTooc"
TAB_MINGGUAN = "Bonus Mingguan_This week"
TAB_PUNCAK = "Bonus Tambang Puncak_This Week"

# Gems Automator Configuration
GEMS_SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1W6t8fCOPB_dFlWnzHSpVJ66tAnOcHHnMn1jXtHfC2S4/edit?usp=sharing"
GEMS_SLIDES_ID = "1bKyyrS-w8JNzJzxkB-2Wkp5RmYk1nmEz"  # Target Slide ID sesuai instruksi
GEMS_MUSIC_PATH = "assets/bgm.mp3"
GEMS_APPS_SCRIPT_WEBHOOK_URL = st.secrets.get("apps_script_webhook_url", "")

TAHUN_SEKARANG = datetime.now().year

MODEL_PRIORITY = [
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-1.5-flash",
]

# Scope tambahan agar mendukung Google Slides dan Google Drive (untuk handling upload image placeholder)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/drive"
]

# ==========================================
# OAuth (akun user biasa) khusus untuk Slides & Drive
# ==========================================
# Service account TIDAK punya storage quota di Drive, sehingga upload gambar via
# service account akan gagal dengan error "storageQuotaExceeded". Karena itu,
# khusus untuk push_to_google_slides (upload gambar & update Slides), kita pakai
# OAuth delegation ke akun Google biasa (refresh token), persis seperti pada
# app__6_.py yang sudah terbukti berjalan.
OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/drive.file "
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/presentations"
)
OAUTH_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# ==========================================
# 1. KONEKSI GOOGLE SERVICES
# ==========================================
@st.cache_resource
def get_google_credentials():
    return Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=SCOPES
    )

@st.cache_resource(ttl=1800)  # cache 30 menit, supaya tidak refresh token tiap rerun/interaksi
def get_slides_drive_oauth_creds():
    """
    Bangun Credentials OAuth dari refresh token yang tersimpan di secrets, khusus
    dipakai untuk Google Slides & Drive (upload gambar). Ini menghindari error
    'storageQuotaExceeded' yang muncul kalau upload gambar dilakukan pakai
    service account (service account tidak punya storage quota di Drive).

    Perlu tiga secrets tambahan: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
    GOOGLE_REFRESH_TOKEN (refresh token milik akun Google biasa, bukan service
    account -- lihat cara generate-nya di app__6_.py / get_refresh_token.py).
    """
    creds = OAuthCredentials(
        token=None,
        refresh_token=st.secrets["GOOGLE_REFRESH_TOKEN"],
        token_uri=OAUTH_TOKEN_ENDPOINT,
        client_id=st.secrets["GOOGLE_CLIENT_ID"],
        client_secret=st.secrets["GOOGLE_CLIENT_SECRET"],
        scopes=OAUTH_SCOPES.split(),
    )
    creds.refresh(GoogleAuthRequest())
    return creds

@st.cache_resource
def get_spreadsheet():
    creds = get_google_credentials()
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)

def upload_to_gsheets(df, tab_name):
    sh = get_spreadsheet()
    try:
        worksheet = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        st.error(f"❌ Tab '{tab_name}' tidak ditemukan di spreadsheet. Upload dibatalkan.")
        return False

    data_to_upload = [df.columns.values.tolist()] + df.fillna("").values.tolist()
    worksheet.clear()
    worksheet.update(data_to_upload, value_input_option="USER_ENTERED")
    return True

@st.cache_resource
def get_gems_sheet():
    creds = get_google_credentials()
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(GEMS_SPREADSHEET_URL)
    return sh.worksheet("This week")

# ==========================================
# 2. CACHE ENGINE OCR & GEMINI SETUP
# ==========================================
@st.cache_resource(show_spinner="Memuat Engine OCR... (Mohon tunggu sebentar)")
def load_ocr_engine():
    return easyocr.Reader(["en"], gpu=False)

def get_model_fallback_list(api_key):
    genai.configure(api_key=api_key)
    available = [m.name for m in genai.list_models() if "generateContent" in m.supported_generation_methods]
    ordered = []
    for key in MODEL_PRIORITY:
        match = next((m for m in available if key in m), None)
        if match and match not in ordered:
            ordered.append(match)
    if not ordered:
        ordered = [m for m in available if "flash" in m]
    return ordered

# ==========================================
# 3. MODUL: BONUS MINGGUAN & HARIAN (LEAVE ALONE)
# ==========================================
def run_ocr(reader, image_path):
    results = reader.readtext(image_path)
    def sort_key(item):
        bbox, text, conf = item
        y = min(p[1] for p in bbox)
        x = min(p[0] for p in bbox)
        return (round(y / 10), x)
    return sorted(results, key=sort_key)

def crop_to_part1(ocr_results):
    start_y, end_y = None, None
    for bbox, text, conf in ocr_results:
        y = min(p[1] for p in bbox)
        t = text.lower()
        if "bonus mingguan" in t and start_y is None:
            start_y = y
        if "pick-up" in t or "jarak jauh" in t:
            end_y = y
            break
    if start_y is None:
        raise ValueError("Tidak ketemu section 'Bonus Mingguan' di hasil OCR.")
    if end_y is None:
        end_y = float("inf")
    return [r for r in ocr_results if start_y - 5 <= min(p[1] for p in r[0]) < end_y]

TEMPOH_RE = re.compile(r"Tempoh\s*(\d+)\s*[:.]?\s*(.+)", re.IGNORECASE)
MONEY_RE = re.compile(r"RM\s?\d+", re.IGNORECASE)
NUM_RE = re.compile(r"^\d{1,4}$")

def group_rows_by_y(items, y_tol=12):
    rows = []
    for bbox, text, conf in items:
        y = min(p[1] for p in bbox)
        x = min(p[0] for p in bbox)
        placed = False
        for row in rows:
            if abs(row["y"] - y) <= y_tol:
                row["items"].append((x, text))
                placed = True
                break
        if not placed:
            rows.append({"y": y, "items": [(x, text)]})
    for row in rows:
        row["items"].sort(key=lambda t: t[0])
    rows.sort(key=lambda r: r["y"])
    return rows

def parse_mingguan_section(part1_items):
    rows = group_rows_by_y(part1_items)
    periods = {}
    current_period = None
    for row in rows:
        line_text = " ".join(t for _, t in row["items"])
        m = TEMPOH_RE.search(line_text)
        if m and "jul" in line_text.lower() or (m and "jun" in line_text.lower()):
            label = f"Tempoh {m.group(1)}"
            current_period = label
            periods[current_period] = {"date_range": m.group(2).strip(), "tiers": []}
            continue
        if "bonus harian" in line_text.lower():
            break
        if current_period and any(c.isdigit() for c in line_text):
            nums = [t for _, t in row["items"] if NUM_RE.match(t)]
            monies = [t for _, t in row["items"] if MONEY_RE.search(t)]
            if len(nums) == 1 and len(monies) == 2:
                periods[current_period]["tiers"].append({
                    "jumlah_pesanan": int(nums[0]),
                    "biasa": monies[0].replace(" ", ""),
                    "berganda": monies[1].replace(" ", ""),
                })
    return periods

def parse_harian_section(part1_items):
    rows = group_rows_by_y(part1_items)
    date_range, tier, in_harian = None, None, False
    for row in rows:
        line_text = " ".join(t for _, t in row["items"])
        if "bonus harian" in line_text.lower():
            in_harian = True
            continue
        if in_harian:
            m = TEMPOH_RE.search(line_text)
            if m:
                date_range = m.group(2).strip()
                continue
            nums = [t for _, t in row["items"] if NUM_RE.match(t)]
            monies = [t for _, t in row["items"] if MONEY_RE.search(t)]
            if len(nums) == 1 and len(monies) == 1:
                tier = {"jumlah_pesanan": int(nums[0]), "bonus": monies[0].replace(" ", "")}
                break
    return {"date_range": date_range, "tier": tier}

GEMINI_BM_PASS_1 = """
Kamu melihat gambar tabel insentif rider (Bahasa Indonesia/Melayu).
Ambil HANYA bagian "Bonus Mingguan" dan "Bonus Harian" (jangan ambil Tambang Puncak / bagian lain).
Balikin JSON PERSIS format ini, tanpa teks tambahan:
{
  "bonus_mingguan": [
    {
      "tempoh_label": "Tempoh 1",
      "date_range": "29 Jun - 2 Jul 2026",
      "tiers": [
        {"jumlah_pesanan": 30, "biasa": "RM20", "berganda": "RM35"}
      ]
    }
  ],
  "bonus_harian": {
    "date_range": "29 Jun - 5 Jul 2026",
    "jumlah_pesanan": 15,
    "bonus": "RM10"
  }
}
"""
GEMINI_BM_PASS_2 = """
Ini adalah gambar tabel yang sama, dan hasil JSON awalmu:
{JSON_SEBELUMNYA}
Tugas (Checking): Cek ulang gambar dengan sangat teliti.
Apakah ada "Tempoh" (periode) yang terlewat? Pastikan setiap Tempoh memiliki tepat 5 tingkatan (tiers).
Jika ada tier atau bonus harian yang terlewat, tambahkan. Pastikan akurasi angka & format JSON valid sempurna.
Kembalikan HANYA format JSON final yang direvisi, tanpa markdown.
"""

def _call_with_retry(model, contents, nama_model_pendek, step_label, log, max_retry=2):
    delay = 5
    last_err = None
    for attempt in range(max_retry):
        try:
            resp = model.generate_content(contents)
            return re.sub(r"^```json|```$", "", resp.text.strip(), flags=re.MULTILINE).strip()
        except Exception as e:
            err_msg = str(e)
            last_err = e
            if "429" in err_msg or "503" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                log(f"⏳ [{nama_model_pendek}] {step_label}: rate limited, retry dalam {delay:.0f}s...")
                time.sleep(delay + random.uniform(0, 2))
                delay *= 2
            else:
                raise
    raise last_err

def run_gemini_mingguan_fallback(image_path, api_key, log=st.write):
    model_fallback_list = get_model_fallback_list(api_key)
    img = Image.open(image_path)
    last_err = None
    for model_name in model_fallback_list:
        model = genai.GenerativeModel(model_name)
        nama_model_pendek = model_name.split("/")[-1]
        try:
            log(f"⏳ [{nama_model_pendek}] Pass 1/2: Generate...")
            json_1 = _call_with_retry(model, [GEMINI_BM_PASS_1, img], nama_model_pendek, "Generate", log)
            log(f"🔍 [{nama_model_pendek}] Pass 2/2: Checking...")
            json_2 = _call_with_retry(
                model, [GEMINI_BM_PASS_2.replace("{JSON_SEBELUMNYA}", json_1), img],
                nama_model_pendek, "Checking", log,
            )
            result = json.loads(json_2)
            log(f"✅ Berhasil pakai model **{nama_model_pendek}**.")
            return result
        except Exception as e:
            last_err = e
            log(f"⚠️ [{nama_model_pendek}] gagal ({e}), coba model berikutnya...")
            continue
    raise Exception(f"Fallback Mingguan gagal di semua model: {last_err}")

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6,
    "jul": 7, "ogo": 8, "aug": 8, "sep": 9, "okt": 10, "oct": 10,
    "nov": 11, "dis": 12, "dec": 12,
}

def parse_date_range(date_range_str):
    m = re.search(r"(\d{1,2})\s*([A-Za-z]{3,})?\s*-\s*(\d{1,2})\s*([A-Za-z]{3,})\s*(\d{4})", date_range_str.strip())
    if not m:
        raise ValueError(f"Tidak bisa parse tanggal: {date_range_str}")
    start_day, start_month_str, end_day, end_month_str, year = m.groups()
    end_month = MONTH_MAP[end_month_str.strip().lower()[:3]]
    start_month = MONTH_MAP[start_month_str.strip().lower()[:3]] if start_month_str else end_month
    start_date = datetime(int(year), start_month, int(start_day)).date()
    end_date = datetime(int(year), end_month, int(end_day)).date()
    dates, d = [], start_date
    while d <= end_date:
        dates.append(d)
        d += timedelta(days=1)
    return dates

def to_number(val):
    if not val:
        return ""
    num_str = re.sub(r"[^\d]", "", str(val))
    return int(num_str) if num_str else ""

def build_sheet_rows_mingguan(bonus_mingguan, bonus_harian):
    rows = []
    harian_dates = parse_date_range(bonus_harian["date_range"]) if bonus_harian else []
    harian_lookup = set(harian_dates)
    all_dates = set(harian_dates)
    period_for_date = {}
    for period in bonus_mingguan:
        p_dates = parse_date_range(period["date_range"])
        all_dates |= set(p_dates)
        for d in p_dates:
            period_for_date[d] = period
    for d in sorted(all_dates):
        if d in harian_lookup:
            rows.append({
                "Date": d.strftime("%Y-%m-%d"),
                "Total Order": to_number(bonus_harian["jumlah_pesanan"]),
                "Regular Bonus Rate": to_number(bonus_harian["bonus"]),
                "Double Bonus Rate": "",
            })
        period = period_for_date.get(d)
        if period:
            for tier in period["tiers"]:
                rows.append({
                    "Date": d.strftime("%Y-%m-%d"),
                    "Total Order": to_number(tier["jumlah_pesanan"]),
                    "Regular Bonus Rate": to_number(tier["biasa"]),
                    "Double Bonus Rate": to_number(tier["berganda"]),
                })
    return pd.DataFrame(rows)

def validate_mingguan_extraction(bonus_mingguan, bonus_harian):
    if not bonus_mingguan:
        return False
    for p in bonus_mingguan:
        if len(p.get("tiers", [])) != 5:
            return False
    if not bonus_harian or not bonus_harian.get("tier"):
        return False
    return True

def extract_mingguan(reader, image_path, api_key, log=st.write):
    log("🔎 [MODUL MINGGUAN] Mencoba OCR (EasyOCR)...")
    try:
        ocr_results = run_ocr(reader, image_path)
        part1_items = crop_to_part1(ocr_results)
        mingguan_raw = parse_mingguan_section(part1_items)
        harian_raw = parse_harian_section(part1_items)
        bonus_mingguan = [
            {"tempoh_label": k, "date_range": v["date_range"], "tiers": v["tiers"]}
            for k, v in mingguan_raw.items()
        ]
        bonus_harian = {
            "date_range": harian_raw["date_range"],
            "jumlah_pesanan": harian_raw["tier"]["jumlah_pesanan"],
            "bonus": harian_raw["tier"]["bonus"],
        } if harian_raw["tier"] else None
        if validate_mingguan_extraction(bonus_mingguan, {"tier": bonus_harian} if bonus_harian else None):
            log("✅ OCR berhasil lengkap.")
            return bonus_mingguan, bonus_harian
        else:
            log("⚠️ OCR tidak lengkap, pindah ke Gemini...")
    except Exception as e:
        log(f"⚠️ OCR gagal ({e}), pindah ke Gemini...")
    if not api_key:
        raise ValueError("OCR gagal dan Gemini API Key belum diisi di sidebar.")
    result = run_gemini_mingguan_fallback(image_path, api_key, log=log)
    return result.get("bonus_mingguan", []), result.get("bonus_harian", {})

# ==========================================
# 4. MODUL: TAMBANG PUNCAK (LEAVE ALONE)
# ==========================================
def get_peak_prompts(year):
    pass_1 = f"""
Kamu adalah data analyst. Lihat gambar tabel insentif rider ini.
Tugasmu adalah fokus HANYA pada bagian "Tambang Puncak" (Peak Fare).
Ubah data matriks tersebut menjadi format list of objects (unpivoted) dengan aturan:
1. HANYA ekstrak data untuk 7 HARI PERTAMA (Senin hingga Minggu). Jika ada kolom hari Senin berikutnya, ABAIKAN.
2. HANYA ambil sel jam yang ADA isinya (misal: 15%, 20%). Jika sel kosong, JANGAN dimasukkan.
3. Hitung 'Date' (YYYY-MM-DD) berdasarkan tanggal di header kolom. Asumsikan tahun {year}.
4. Tentukan 'Day' dalam bahasa Inggris (Monday, Tuesday, dst).
5. Tuliskan 'Hour' secara eksplisit dalam format AM/PM (contoh: "12 AM", "1 PM").
6. 'Bonus' adalah string persentase.
Balikin JSON PERSIS format ini, tanpa teks tambahan atau markdown:
[
  {{"Date": "{year}-06-29", "Day": "Monday", "Hour": "12 AM", "Bonus": "15%"}}
]
"""
    pass_2 = """
Ini JSON awalmu:
{JSON_SEBELUMNYA}
Tugas (Checking):
Beberapa sel sering TERLEWAT, contohnya di hari Jumat jam 5 PM (17:00) atau jam pergantian shift.
SCAN ULANG gambar teliti. Jika ada kotak berisi persen yang belum masuk di JSON, TAMBAHKAN.
Sebaliknya, pastikan juga tidak ada "jam siluman" (sel kosong di gambar tapi dimasukkan JSON).
Kembalikan HANYA format JSON final revisi, tanpa markdown.
"""
    return pass_1, pass_2

def extract_peak_fare(image_path, api_key, year=TAHUN_SEKARANG, log=st.write):
    model_fallback_list = get_model_fallback_list(api_key)
    img = Image.open(image_path)
    pass_1, pass_2 = get_peak_prompts(year)
    last_err = None
    log(f"🔎 [MODUL TAMBANG PUNCAK] Memulai ekstraksi (Tahun: {year})")
    for model_name in model_fallback_list:
        model = genai.GenerativeModel(model_name)
        nama_model_pendek = model_name.split("/")[-1]
        try:
            log(f"⏳ [{nama_model_pendek}] Pass 1/2: Generate...")
            json_1 = _call_with_retry(model, [pass_1, img], nama_model_pendek, "Generate", log)
            log(f"🔍 [{nama_model_pendek}] Pass 2/2: Checking...")
            json_2 = _call_with_retry(
                model, [pass_2.replace("{JSON_SEBELUMNYA}", json_1), img],
                nama_model_pendek, "Checking", log,
            )
            result = json.loads(json_2)
            log(f"✅ Berhasil pakai model **{nama_model_pendek}**.")
            return result
        except Exception as e:
            last_err = e
            log(f"⚠️ [{nama_model_pendek}] gagal ({e}), coba model berikutnya...")
            continue
    raise Exception(f"Ekstraksi Puncak gagal di semua model: {last_err}")

def to_percentage(val):
    if not val:
        return ""
    num_str = re.sub(r"[^\d.]", "", str(val))
    if num_str:
        return float(num_str) / 100.0
    return ""

def process_raw_data_puncak(raw_data):
    df = pd.DataFrame(raw_data)
    df["Bonus"] = df["Bonus"].apply(to_percentage)
    df["Hour"] = df["Hour"].astype(str).str.replace(r"^0\s*AM$", "12 AM", regex=True, flags=re.IGNORECASE)
    df["Hour"] = pd.to_datetime(df["Hour"], format="%I %p").dt.hour
    df["Date_Obj"] = pd.to_datetime(df["Date"])
    min_date = df["Date_Obj"].min()
    df = df[df["Date_Obj"] < min_date + pd.Timedelta(days=7)]
    df = df.sort_values(by=["Date_Obj", "Hour"]).drop(columns=["Date_Obj"])
    return df.reset_index(drop=True)


# ==========================================
# 5. MODUL: GEMS AUTOMATOR & SLIDES INTEGRATION
# ==========================================
@st.cache_data
def get_audio_base64(file_path):
    with open(file_path, "rb") as f:
        data = f.read()
    return base64.b64encode(data).decode()

def render_music_player(file_path):
    try:
        audio_base64 = get_audio_base64(file_path)
    except FileNotFoundError:
        st.sidebar.warning(f"⚠️ File musik tidak ditemukan di: {file_path}")
        return
    player_html = f"""
    <div style="display:flex; align-items:center; gap:10px; font-family:sans-serif;">
        <button id="musicBtn" onclick="toggleMusic()" style="
            padding:8px 16px; border-radius:8px; border:none;
            background:#4A90D9; color:white; cursor:pointer; font-size:14px;">
            🔊 Play Musik
        </button>
    </div>
    <audio id="bgmAudio" loop>
        <source src="data:audio/mp3;base64,{audio_base64}" type="audio/mp3">
    </audio>
    <script>
        const audio = document.getElementById("bgmAudio");
        const btn = document.getElementById("musicBtn");
        function toggleMusic() {{
            if (audio.paused) {{
                audio.play();
                btn.innerText = "⏸️ Pause Musik";
            }} else {{
                audio.pause();
                btn.innerText = "🔊 Play Musik";
            }}
        }}
    </script>
    """
    components.html(player_html, height=60)

def enhance_image_for_ocr(path):
    img = Image.open(path).convert("L")
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = ImageEnhance.Sharpness(img).enhance(2.0)
    enhanced_path = "enhanced_" + os.path.basename(path)
    img.save(enhanced_path)
    return enhanced_path

def extract_gems_rewards(ocr_result_list):
    start_idx, end_idx = -1, len(ocr_result_list)
    for idx, text in enumerate(ocr_result_list):
        if "target" in text.lower():
            start_idx = idx
        if "qualification" in text.lower() or "criteria" in text.lower():
            end_idx = idx
            break
    target_section_text = ocr_result_list[start_idx + 1: end_idx]
    gems_found, rewards_found = [], []
    for text in target_section_text:
        if "%" in text:
            continue
        matches = re.findall(r"\d+(?:\.\d+)?", text)
        for m in matches:
            val = float(m)
            if val <= 0 or val > 200:
                continue
            if "." in text or "$" in text or "s$" in text.lower():
                rewards_found.append(val)
            elif val.is_integer() and val < 150:
                gems_found.append(int(val))
    return sorted(list(set(gems_found))), sorted(list(set(rewards_found))), target_section_text

def analisis_dengan_ultimate_retry(model, prompt, gambar_list, max_retry=5):
    delay = 10
    for i in range(max_retry):
        try:
            time.sleep(2)
            respon = model.generate_content([prompt] + gambar_list)
            return respon.text
        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg or "503" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                wait_time = delay + random.uniform(0, 5)
                st.warning(f"⚠️ Server sibuk. Menunggu {wait_time:.1f} detik... (percobaan {i+1}/{max_retry})")
                time.sleep(wait_time)
                delay *= 2
            else:
                raise e
    raise Exception("Gagal total setelah percobaan maksimal.")

def extract_via_gemini_gems(image_path, api_key):
    if not api_key:
        return [], []
    try:
        genai.configure(api_key=api_key)
        models = [m.name for m in genai.list_models() if "generateContent" in m.supported_generation_methods]
        model_name = next((m for m in models if "1.5-flash" in m), next((m for m in models if "flash" in m), models[0]))
        gemini_model = genai.GenerativeModel(model_name)
    except Exception as e:
        st.error(f"⚠️ Gagal setup Gemini: {e}")
        return [], []
    prompt = """Look at this screenshot of a delivery rider mission app.
Find the "TARGET & REWARD" section. It contains exactly 3 tiers, each with:
- a "gems" target number (a small whole number)
- a corresponding "S$" reward amount
Return ONLY a raw JSON object: {"gems": [g1, g2, g3], "rewards": [r1, r2, r3]}"""
    try:
        img = Image.open(image_path)
        teks_raw = analisis_dengan_ultimate_retry(gemini_model, prompt, [img], max_retry=3)
        clean_text = re.sub(r"^```json\s*|\s*```$", "", teks_raw.strip())
        data = json.loads(clean_text)
        gems = sorted([int(g) for g in data.get("gems", [])])
        rewards = sorted([float(r) for r in data.get("rewards", [])])
        if len(gems) == 3 and len(rewards) == 3:
            return gems, rewards
        return [], []
    except Exception:
        return [], []

def get_target_cells(tier, vehicle):
    tier_l = tier.lower()
    vehicle_l = vehicle.lower()
    if vehicle_l in ["e-bike", "bicycle"]:
        same_tier_group = ["E-bike", "Bicycle"]
    else:
        same_tier_group = ["Walker"] if vehicle_l == "walker" else [vehicle.capitalize()]
    targets = [(tier, v) for v in same_tier_group]
    if tier_l in ["diamond", "sapphire"] and vehicle_l != "walker":
        other_tier = "Sapphire" if tier_l == "diamond" else "Diamond"
        for v in same_tier_group:
            targets.append((other_tier, v))
    seen = set()
    unique_targets = []
    for t in targets:
        key = (t[0].lower(), t[1].lower())
        if key not in seen:
            seen.add(key)
            unique_targets.append(t)
    return unique_targets

def trigger_color_compare(row_numbers):
    if not GEMS_APPS_SCRIPT_WEBHOOK_URL:
        st.warning("⚠️ `apps_script_webhook_url` belum di-set di secrets, pewarnaan cell dilewati.")
        return
    if not row_numbers:
        return
    try:
        resp = requests.post(
            GEMS_APPS_SCRIPT_WEBHOOK_URL,
            json={"rows": sorted(set(row_numbers))},
            timeout=30,
        )
        resp.raise_for_status()
        st.info(f"🎨 Pewarnaan cell diproses oleh Apps Script untuk {len(set(row_numbers))} baris.")
    except Exception as e:
        st.warning(f"⚠️ Gagal memanggil Apps Script untuk pewarnaan cell: {e}")

# --- PEMBENTUKAN STRING TANGGAL SESUAI DENGAN TEMPLATE SLIDE 1-2 ---
def get_slide_date_replacements(any_date):
    """Mencari tanggal awal minggu (Senin) lalu menghitung format text persis slide contoh."""
    # Find Monday of that week
    monday = any_date - timedelta(days=any_date.weekday())
    tuesday = monday + timedelta(days=1)
    wednesday = monday + timedelta(days=2)
    thursday = monday + timedelta(days=3)
    friday = monday + timedelta(days=4)
    saturday = monday + timedelta(days=5)
    sunday = monday + timedelta(days=6)
    
    months = ["", "July", "August", "September", "October", "November", "December", 
              "January", "February", "March", "April", "May", "June"]
    
    def fmt_full(d):
        return f"{d.day} {d.strftime('%B')}"

    # Mengikuti visual: Monday - Wednesday 6 July - 8 July
    date_monday_str = fmt_full(monday)
    date_wednesday_str = fmt_full(wednesday)

    # Mengikuti visual: Thursday and Friday 9 & 10 July (jika beda bulan: 31 August & 1 September)
    if thursday.month == friday.month:
        date_thursday_str = str(thursday.day)
        date_friday_str = fmt_full(friday)
    else:
        date_thursday_str = fmt_full(thursday)
        date_friday_str = fmt_full(friday)

    # Mengikuti visual: Saturday and Sunday 11 & 12 July
    if saturday.month == sunday.month:
        date_saturday_str = str(saturday.day)
        date_sunday_str = fmt_full(sunday)
    else:
        date_saturday_str = fmt_full(saturday)
        date_sunday_str = fmt_full(sunday)

    return {
        "{date_monday}": date_monday_str,
        "{date_wednesday}": date_wednesday_str,
        "{date_thursday}": date_thursday_str,
        "{date_friday}": date_friday_str,
        "{date_saturday}": date_saturday_str,
        "{date_sunday}": date_sunday_str
    }

def push_to_google_slides(slide_id, tier, vehicle, processed_images_list, log_box):
    """Menangani upload image ke Google Drive sebagai penampung publik sementara, kemudian update template Google Slides."""
    creds = get_slides_drive_oauth_creds()
    slides_service = build('slides', 'v1', credentials=creds)
    drive_service = build('drive', 'v3', credentials=creds)
    
    requests_body = []
    
    # 1. Mengganti Text Placeholder Utama
    requests_body.append({
        'replaceAllText': {
            'containsText': {'text': '{Tier}', 'matchCase': True},
            'replaceText': tier
        }
    })
    requests_body.append({
        'replaceAllText': {
            'containsText': {'text': '{Vehicle}', 'matchCase': True},
            'replaceText': vehicle
        }
    })
    
    # 2. Mengganti Date Placeholders otomatis dari entitas batch pertama
    if processed_images_list:
        any_date = processed_images_list[0]['date_obj']
        date_replacements = get_slide_date_replacements(any_date)
        for placeholder, replacement_value in date_replacements.items():
            requests_body.append({
                'replaceAllText': {
                    'containsText': {'text': placeholder, 'matchCase': True},
                    'replaceText': replacement_value
                }
            })
            
    # 3. Mengganti Image Shape Placeholders secara Eksklusif (Hanya apa yang di-upload)
    day_to_placeholder_map = {
        'monday': '{IMG_mon}',
        'tuesday': '{IMG_tues}',
        'wednesday': '{IMG_wed}',
        'thursday': '{IMG_thur}',
        'friday': '{IMG_fri}',
        'saturday': '{IMG_sat}',
        'sunday': '{IMG_sun}'
    }
    
    uploaded_drive_file_ids = []
    
    for item in processed_images_list:
        day_str = item['day'].lower()
        placeholder_shape_text = day_to_placeholder_map.get(day_str)
        
        if not placeholder_shape_text:
            continue
            
        log_box.write(f"📤 Menyiapkan & Mengunggah tangkapan layar hari **{item['day']}** ke server cloud...")
        
        # Upload ke drive agar mendapatkan link publik sementara untuk di-inject ke Slides API
        file_metadata = {'name': f"temp_slide_{day_str}.jpg", 'mimeType': 'image/jpeg'}
        media = MediaFileUpload(item['path'], mimetype='image/jpeg')
        uploaded_file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        file_id = uploaded_file.get('id')
        uploaded_drive_file_ids.append(file_id)
        
        # Mengubah permission berkas drive ke public viewable link
        drive_service.permissions().create(
            fileId=file_id,
            body={'type': 'anyone', 'role': 'reader'}
        ).execute()
        
        # Sediakan URL download mentah berkas gambar tersebut
        direct_image_url = f"https://docs.google.com/uc?export=download&id={file_id}"
        
        # Append request injeksi gambar menggantikan bentuk koordinat box placeholder text
        requests_body.append({
            'replaceAllShapesWithImage': {
                'imageUrl': direct_image_url,
                'imageReplaceMethod': 'CENTER_INSIDE',
                'containsText': {'text': placeholder_shape_text, 'matchCase': True}
            }
        })
        
    # Eksekusi seluruh antrean transaksi batch ke Google Slides API
    if requests_body:
        log_box.write("⚙️ Memperbarui data teks dan menyisipkan tangkapan layar ke Google Slides secara langsung...")
        slides_service.presentations().batchUpdate(
            presentationId=slide_id,
            body={'requests': requests_body}
        ).execute()
        
    return uploaded_drive_file_ids

# ==========================================
# 6. UI UTAMA (STREAMLIT APPS)
# ==========================================
st.title("🟢 GNS Greentable & Gems Automator")
st.caption("Aplikasi Otomasi Ekstraksi Insentif Driver & Sinkronisasi Google Sheets / Google Slides.")

try:
    sh = get_spreadsheet()
    st.success(f"✅ Terhubung ke spreadsheet utama: **{sh.title}**")
except Exception as e:
    st.error(f"❌ Gagal konek ke spreadsheet. Cek secrets `gcp_service_account`. Error: {e}")
    st.stop()

reader = load_ocr_engine()

with st.sidebar:
    st.markdown("### 🎵 Musik Latar")
    render_music_player(GEMS_MUSIC_PATH)
    st.divider()

st.sidebar.header("⚙️ Pengaturan API")
gemini_api_key = st.sidebar.text_input("Gemini API Key (Untuk fallback & Tambang Puncak & Gems)", type="password")
st.sidebar.caption(f"Target tab greentable:\n- `{TAB_MINGGUAN}`\n- `{TAB_PUNCAK}`")

st.divider()

uploaded_image = st.file_uploader(
    "Unggah gambar insentif mingguan Greentable (full image)",
    type=["jpg", "jpeg", "png"],
)

image_path = None
if uploaded_image is not None:
    os.makedirs("temp_uploads", exist_ok=True)
    image_path = os.path.join("temp_uploads", uploaded_image.name)
    with open(image_path, "wb") as f:
        f.write(uploaded_image.getbuffer())
    st.image(image_path, caption="Preview Gambar Greentable", width=350)

st.divider()

tab_mingguan, tab_puncak, tab_gems = st.tabs(
    ["📅 Bonus Mingguan & Harian", "⛰️ Tambang Puncak", "💎 Gems Automator"]
)

# ------------------------------------------
# TAB 1 — BONUS MINGGUAN & HARIAN (LEAVE ALONE)
# ------------------------------------------
with tab_mingguan:
    st.subheader("Bonus Mingguan & Bonus Harian")
    if image_path is None:
        st.info("Upload gambar dulu di atas untuk mulai ekstraksi.")
    else:
        if st.button("🚀 Jalankan Ekstraksi", key="run_mingguan"):
            log_box = st.container()
            try:
                bonus_mingguan, bonus_harian = extract_mingguan(
                    reader, image_path, gemini_api_key, log=log_box.write
                )
                df_mingguan = build_sheet_rows_mingguan(bonus_mingguan, bonus_harian)
                st.session_state["df_mingguan"] = df_mingguan
                st.success(f"Ekstraksi selesai — {len(df_mingguan)} baris siap diupload.")
            except Exception as e:
                st.error(f"❌ Ekstraksi gagal: {e}")

        if "df_mingguan" in st.session_state:
            st.dataframe(st.session_state["df_mingguan"], use_container_width=True)
            if st.button("📤 Upload ke Google Sheets", key="upload_mingguan"):
                ok = upload_to_gsheets(st.session_state["df_mingguan"], TAB_MINGGUAN)
                if ok:
                    st.success(f"🎉 {len(st.session_state['df_mingguan'])} baris terkirim ke '{TAB_MINGGUAN}'.")

# ------------------------------------------
# TAB 2 — TAMBANG PUNCAK (LEAVE ALONE)
# ------------------------------------------
with tab_puncak:
    st.subheader("Bonus Tambang Puncak")
    if image_path is None:
        st.info("Upload gambar dulu di atas untuk mulai ekstraksi.")
    elif not gemini_api_key:
        st.warning("⚠️ Modul ini pakai Gemini Vision (2-pass) — isi Gemini API Key di sidebar dulu.")
    else:
        if st.button("🚀 Jalankan Ekstraksi", key="run_puncak"):
            log_box = st.container()
            try:
                raw_puncak = extract_peak_fare(image_path, gemini_api_key, log=log_box.write)
                df_puncak = process_raw_data_puncak(raw_puncak)
                st.session_state["df_puncak"] = df_puncak
                st.success(f"Ekstraksi selesai — {len(df_puncak)} baris siap diupload.")
            except Exception as e:
                st.error(f"❌ Ekstraksi gagal: {e}")

        if "df_puncak" in st.session_state:
            st.dataframe(st.session_state["df_puncak"], use_container_width=True)
            if st.button("📤 Upload ke Google Sheets", key="upload_puncak"):
                ok = upload_to_gsheets(st.session_state["df_puncak"], TAB_PUNCAK)
                if ok:
                    st.success(f"🎉 {len(st.session_state['df_puncak'])} baris terkirim ke '{TAB_PUNCAK}'.")

# ------------------------------------------
# TAB 3 — GEMS AUTOMATOR (FOKUS UTAMA & SLIDES UPDATE)
# ------------------------------------------
with tab_gems:
    st.subheader("💎 Automasi Ekstraksi Gems & Sinkronisasi Google Slides")
    st.caption("Upload screenshot mission → Ekstraksi angka ke GSheets → Auto-generate Presentasi Google Slides.")

    try:
        sheet_this_week = get_gems_sheet()
        st.success("✅ Terhubung dengan Spreadsheet Gems!")
    except Exception as e:
        st.error(f"❌ Gagal membuka spreadsheet Gems. Error: {e}")
        st.stop()

    col_a, col_b = st.columns(2)
    tier_options = ["Diamond", "Sapphire", "Ruby", "Emerald"]
    vehicle_options = ["Walker", "Motorcycle", "E-bike", "Bicycle"]
    selected_tier = col_a.selectbox("Pilih Tier", tier_options, key="gems_tier")
    selected_vehicle = col_b.selectbox("Pilih Vehicle", vehicle_options, key="gems_vehicle")

    target_cells = get_target_cells(selected_tier, selected_vehicle)
    target_display = [f"{t}-{v}" for t, v in target_cells]
    st.info(f"📌 **Target Utama GSheets:** {selected_tier} - {selected_vehicle}\n\n"
            f"🔄 **Auto-fill GSheets ke:** {', '.join(target_display)}\n\n"
            f"📊 **Target Sinkronisasi Slides:** Sesuai dengan opsi yang di-upload saja (Eksklusif).")

    gems_files = st.file_uploader(
        "Unggah Gambar Mission (Bisa batch / banyak sekaligus)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        key="gems_uploader",
    )

    if gems_files and st.button("🚀 Jalankan Ekstraksi & Update Seluruh System", type="primary", key="run_gems"):
        temp_dir = "temp_uploads_gems"
        os.makedirs(temp_dir, exist_ok=True)

        all_rows_this_week = sheet_this_week.get_all_values()
        progress_bar = st.progress(0)
        touched_rows = set()
        
        # Penampung daftar metadata gambar yang sukses diekstrak untuk dilempar ke modul Google Slides
        slides_payload_list = []

        for idx, uploaded_file in enumerate(gems_files):
            filename = uploaded_file.name

            with st.expander(f"⚙️ Memproses: {filename}", expanded=True):
                temp_path = os.path.join(temp_dir, filename)
                with open(temp_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())

                img = Image.open(temp_path)
                if img.width > img.height:
                    st.write("🔄 Rotasi gambar landscape agar tegak...")
                    img_rotated = img.rotate(90, expand=True)
                    temp_path = os.path.join(temp_dir, "prepared_" + filename)
                    img_rotated.save(temp_path)

                result = reader.readtext(temp_path, detail=0)
                full_text = " ".join(result)

                formatted_date, extracted_day, current_dt_obj = None, None, None
                fname_match = re.search(r"(\d{1,2})[\s_]*([A-Za-z]{3,9})", os.path.splitext(filename)[0])

                if fname_match:
                    try:
                        current_dt_obj = datetime.strptime(f"{fname_match.group(1)} {fname_match.group(2)[:3].capitalize()} 2026", "%d %b %Y")
                        extracted_day = current_dt_obj.strftime("%A")
                        formatted_date = current_dt_obj.strftime("%d/%m/%Y")
                    except Exception:
                        pass

                if not formatted_date:
                    date_match = re.search(r"(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4})?", full_text)
                    if date_match:
                        try:
                            tahun_angka = date_match.group(3) if date_match.group(3) else "2026"
                            current_dt_obj = datetime.strptime(f"{date_match.group(1)} {date_match.group(2)[:3].capitalize()} {tahun_angka}", "%d %b %Y")
                            extracted_day = current_dt_obj.strftime("%A")
                            formatted_date = current_dt_obj.strftime("%d/%m/%Y")
                        except Exception:
                            pass

                if not formatted_date:
                    st.error("⚠️ Tanggal tidak terdeteksi dari OCR teks maupun nama berkas. File dilewati.")
                    continue
                else:
                    st.write(f"📅 Hari & Waktu Terdeteksi: **{formatted_date} ({extracted_day})**")

                gems_found, rewards_found, _ = extract_gems_rewards(result)

                if len(gems_found) != 3 or len(rewards_found) != 3:
                    st.warning("⚠️ Pembacaan normal gagal. Mencoba Image Enhancement...")
                    enhanced_path = enhance_image_for_ocr(temp_path)
                    res_v2 = reader.readtext(enhanced_path, detail=0, text_threshold=0.4, low_text=0.3)
                    gems_found, rewards_found, _ = extract_gems_rewards(res_v2)

                    if len(gems_found) != 3 or len(rewards_found) != 3:
                        st.warning("⚠️ Masih gagal. Mencoba Fallback ke Gemini Vision API...")
                        gems_found, rewards_found = extract_via_gemini_gems(temp_path, gemini_api_key)

                if len(gems_found) == 3 and len(rewards_found) == 3:
                    pairs = list(zip(gems_found, rewards_found))
                    # Masukkan ke dalam payload slide jika ekstraksi sukses
                    slides_payload_list.append({
                        'day': extracted_day,
                        'path': temp_path,
                        'date_obj': current_dt_obj
                    })
                else:
                    st.error("❌ Ekstraksi angka gagal total pada berkas ini. Silakan periksa manual.")
                    continue

                # --- 1. PROSES UPDATE GOOGLE SHEETS (LOGIKA LAMA DITEPATI & TIDAK BERUBAH) ---
                target_day = extracted_day.lower()
                current_target_cells = get_target_cells(selected_tier, selected_vehicle)

                for cell_tier, cell_vehicle in current_target_cells:
                    matched_row_indices = []
                    for r_idx, row in enumerate(all_rows_this_week):
                        if r_idx == 0:
                            continue
                        r_tier = row[0].strip().lower() if len(row) > 0 else ""
                        r_veh = row[1].strip().lower() if len(row) > 1 else ""
                        r_day = row[2].strip().lower() if len(row) > 2 else ""
                        if r_day == "wedesday":
                            r_day = "wednesday"

                        if r_tier == cell_tier.lower() and r_veh == cell_vehicle.lower() and r_day == target_day:
                            matched_row_indices.append(r_idx + 1)

                    if len(matched_row_indices) >= 3:
                        update_payload = []
                        for i in range(3):
                            g_val, r_val = pairs[i][0], pairs[i][1]
                            inc = round(r_val / g_val, 2) if g_val > 0 else 0
                            update_payload.append([formatted_date, g_val, round(r_val, 2), round(inc, 2)])

                        start_row, end_row = matched_row_indices[0], matched_row_indices[2]
                        sheet_this_week.update(
                            values=update_payload,
                            range_name=f"D{start_row}:G{end_row}",
                            value_input_option="USER_ENTERED",
                        )
                        touched_rows.update(matched_row_indices)

                        if cell_tier.lower() == selected_tier.lower() and cell_vehicle.lower() == selected_vehicle.lower():
                            st.success(f"✨ Data **{cell_tier}-{cell_vehicle}** berhasil masuk ke Spreadsheet baris {start_row}-{end_row}.")
                        else:
                            st.info(f"➡️ Data kembaran **{cell_tier}-{cell_vehicle}** otomatis diisi di baris {start_row}-{end_row}.")
                    else:
                        st.error(f"❌ Baris kosong untuk {cell_tier}-{cell_vehicle} hari {extracted_day} tidak ditemukan.")

            progress_bar.progress((idx + 1) / len(gems_files))

        trigger_color_compare(touched_rows)

        # --- 2. PROSES UPDATE GOOGLE SLIDES (LOGIKA BARU - SELEPAS SHEET PROCESSED) ---
        if slides_payload_list:
            st.divider()
            st.subheader("📊 Memulai Sinkronisasi Presentasi Google Slides...")
            log_slides = st.container()
            try:
                uploaded_ids = push_to_google_slides(
                    slide_id=GEMS_SLIDES_ID,
                    tier=selected_tier,
                    vehicle=selected_vehicle,
                    processed_images_list=slides_payload_list,
                    log_box=log_slides
                )
                st.success("🎉 Google Slides Master Template berhasil diperbarui dengan sempurna!")
                st.info(f"🔗 Tautan Template Presentasi: [Buka Google Slides](https://docs.google.com/presentation/d/{GEMS_SLIDES_ID}/edit)")
            except Exception as slide_err:
                st.error(f"⚠️ Gagal memperbarui Google Slides: {slide_err}")
        
        st.balloons()
        st.success("🎉 Seluruh rangkaian otomasi data Sheets dan visualisasi Slides selesai diproses!")
