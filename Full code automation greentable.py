import os
import re
import json
import time
import random
import base64
import easyocr
import gspread
import requests
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from datetime import datetime, timedelta
from PIL import Image, ImageEnhance

from google.oauth2.service_account import Credentials
import google.generativeai as genai

# ==============================================================================
# 1. KONFIGURASI HALAMAN & VARIABEL GLOBAL
# ==============================================================================
st.set_page_config(page_title="GNS All-in-One Automation", page_icon="⚙️", layout="wide")

# Konfigurasi Greentable
GT_SPREADSHEET_ID = "1FvJBZvtjKQ3QiZEHCOic2lZtegbuRK0wmsE6jCGTooc"
GT_TAB_MINGGUAN = "Bonus Mingguan_This week"
GT_TAB_PUNCAK = "Bonus Tambang Puncak_This Week"
TAHUN_SEKARANG = datetime.now().year

# Konfigurasi Gems Extractor
GEMS_SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1W6t8fCOPB_dFlWnzHSpVJ66tAnOcHHnMn1jXtHfC2S4/edit?usp=sharing"
APPS_SCRIPT_WEBHOOK_URL = st.secrets.get("apps_script_webhook_url", "")

MUSIC_PATH = "assets/bgm.mp3"

# ==============================================================================
# 2. HELPER: AUTH, OCR, & GEMINI ENGINE (SHARED)
# ==============================================================================
@st.cache_resource
def get_gsheets_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=scopes
    )
    return gspread.authorize(creds)

@st.cache_resource(show_spinner="Memuat Engine OCR... (Mohon tunggu sebentar)")
def load_ocr_engine():
    return easyocr.Reader(['en'], gpu=False)

reader = load_ocr_engine()

MODEL_PRIORITY = [
    "gemini-3.1-flash-lite",   
    "gemini-2.5-flash-lite",   
    "gemini-3-flash",          
    "gemini-3.5-flash",        
    "gemini-2.5-flash", 
    "gemini-1.5-flash"
]

def get_model_fallback_list():
    available = [m.name for m in genai.list_models() if "generateContent" in m.supported_generation_methods]
    ordered = []
    for key in MODEL_PRIORITY:
        match = next((m for m in available if key in m), None)
        if match and match not in ordered:
            ordered.append(match)
    if not ordered:
        ordered = [m for m in available if "flash" in m]
    return ordered

def upload_to_gsheets(gc, df, sheet_id, tab_name):
    sh = gc.open_by_key(sheet_id)
    worksheet = sh.worksheet(tab_name)
    data_to_upload = [df.columns.values.tolist()] + df.fillna("").values.tolist()
    worksheet.clear()
    worksheet.update(data_to_upload, value_input_option="USER_ENTERED")

# ==============================================================================
# 3. SIDEBAR (GLOBAL)
# ==============================================================================
@st.cache_data
def get_audio_base64(file_path):
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode()

def render_music_player(file_path):
    try:
        audio_base64 = get_audio_base64(file_path)
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
                if (audio.paused) {{ audio.play(); btn.innerText = "⏸️ Pause Musik"; }} 
                else {{ audio.pause(); btn.innerText = "🔊 Play Musik"; }}
            }}
        </script>
        """
        components.html(player_html, height=60)
    except FileNotFoundError:
        st.warning("⚠️ File musik tidak ditemukan.")

with st.sidebar:
    st.markdown("### 🎵 Musik Latar")
    render_music_player(MUSIC_PATH)
    st.divider()
    st.header("⚙️ Konfigurasi Utama")
    gemini_api_key = st.text_input("Gemini API Key (Wajib untuk Fallback)", type="password")


# ==============================================================================
# 4. LOGIKA APLIKASI 1: GREENTABLE (MINGGUAN & PUNCAK)
# ==============================================================================
# Regex Mingguan
TEMPOH_RE = re.compile(r"Tempoh\s*(\d+)\s*[:.]?\s*(.+)", re.IGNORECASE)
MONEY_RE = re.compile(r"RM\s?\d+", re.IGNORECASE)
NUM_RE = re.compile(r"^\d{1,4}$")
MONTH_MAP = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6, "jul": 7, "ogo": 8, "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "dis": 12, "dec": 12}

def gt_parse_date_range(date_range_str):
    m = re.search(r"(\d{1,2})\s*([A-Za-z]{3,})?\s*-\s*(\d{1,2})\s*([A-Za-z]{3,})\s*(\d{4})", date_range_str.strip())
    if not m: raise ValueError(f"Gagal parse tanggal: {date_range_str}")
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

def gt_to_number(val):
    if not val: return ""
    num_str = re.sub(r"[^\d]", "", str(val))
    return int(num_str) if num_str else ""

def gt_build_sheet_rows_mingguan(bonus_mingguan, bonus_harian):
    rows = []
    harian_dates = gt_parse_date_range(bonus_harian["date_range"]) if bonus_harian else []
    harian_lookup = set(harian_dates)
    all_dates = set(harian_dates)
    period_for_date = {}
    for period in bonus_mingguan:
        p_dates = gt_parse_date_range(period["date_range"])
        all_dates |= set(p_dates)
        for d in p_dates: period_for_date[d] = period

    for d in sorted(all_dates):
        if d in harian_lookup:
            rows.append({"Date": d.strftime("%Y-%m-%d"), "Total Order": gt_to_number(bonus_harian["jumlah_pesanan"]), "Regular Bonus Rate": gt_to_number(bonus_harian["bonus"]), "Double Bonus Rate": ""})
        period = period_for_date.get(d)
        if period:
            for tier in period["tiers"]:
                rows.append({"Date": d.strftime("%Y-%m-%d"), "Total Order": gt_to_number(tier["jumlah_pesanan"]), "Regular Bonus Rate": gt_to_number(tier["biasa"]), "Double Bonus Rate": gt_to_number(tier["berganda"])})
    return pd.DataFrame(rows)

def gt_extract_mingguan_ocr(image_path):
    results = reader.readtext(image_path)
    def sort_key(item):
        bbox = item[0]
        return (round(min(p[1] for p in bbox) / 10), min(p[0] for p in bbox))
    ocr_res = sorted(results, key=sort_key)
    start_y, end_y = None, float("inf")
    for bbox, text, conf in ocr_res:
        y = min(p[1] for p in bbox)
        t = text.lower()
        if "bonus mingguan" in t and start_y is None: start_y = y
        if "pick-up" in t or "jarak jauh" in t: 
            end_y = y
            break
    if start_y is None: raise ValueError("Section tidak ketemu.")
    part1_items = [r for r in ocr_res if start_y - 5 <= min(p[1] for p in r[0]) < end_y]
    
    rows = []
    for bbox, text, conf in part1_items:
        y, x = min(p[1] for p in bbox), min(p[0] for p in bbox)
        placed = False
        for row in rows:
            if abs(row["y"] - y) <= 12:
                row["items"].append((x, text))
                placed = True
                break
        if not placed: rows.append({"y": y, "items": [(x, text)]})
    for r in rows: r["items"].sort(key=lambda t: t[0])
    rows.sort(key=lambda r: r["y"])

    periods, current_period = {}, None
    date_range_harian, tier_harian, in_harian = None, None, False

    for row in rows:
        line_text = " ".join(t for _, t in row["items"])
        m = TEMPOH_RE.search(line_text)
        if not in_harian:
            if m and ("jul" in line_text.lower() or "jun" in line_text.lower()):
                current_period = f"Tempoh {m.group(1)}"
                periods[current_period] = {"date_range": m.group(2).strip(), "tiers": []}
                continue
            if "bonus harian" in line_text.lower():
                in_harian = True
                continue
            if current_period and any(c.isdigit() for c in line_text):
                nums = [t for _, t in row["items"] if NUM_RE.match(t)]
                monies = [t for _, t in row["items"] if MONEY_RE.search(t)]
                if len(nums) == 1 and len(monies) == 2:
                    periods[current_period]["tiers"].append({"jumlah_pesanan": int(nums[0]), "biasa": monies[0].replace(" ", ""), "berganda": monies[1].replace(" ", "")})
        else:
            if m:
                date_range_harian = m.group(2).strip()
                continue
            nums = [t for _, t in row["items"] if NUM_RE.match(t)]
            monies = [t for _, t in row["items"] if MONEY_RE.search(t)]
            if len(nums) == 1 and len(monies) == 1:
                tier_harian = {"jumlah_pesanan": int(nums[0]), "bonus": monies[0].replace(" ", "")}
                break

    b_mingguan = [{"tempoh_label": k, "date_range": v["date_range"], "tiers": v["tiers"]} for k, v in periods.items()]
    b_harian = {"date_range": date_range_harian, "jumlah_pesanan": tier_harian["jumlah_pesanan"], "bonus": tier_harian["bonus"]} if tier_harian else None
    if not b_mingguan or any(len(p["tiers"]) != 5 for p in b_mingguan) or not b_harian: raise ValueError("Data OCR tidak lengkap.")
    return b_mingguan, b_harian

def gt_process_raw_data_puncak(raw_data):
    def to_percentage(val):
        if not val: return ""
        num_str = re.sub(r"[^\d.]", "", str(val))
        return float(num_str) / 100.0 if num_str else ""
    df = pd.DataFrame(raw_data)
    df["Bonus"] = df["Bonus"].apply(to_percentage)
    df["Hour"] = df["Hour"].astype(str).str.replace(r"^0\s*AM$", "12 AM", regex=True, flags=re.IGNORECASE)
    df["Hour"] = pd.to_datetime(df["Hour"], format="%I %p").dt.hour
    df["Date_Obj"] = pd.to_datetime(df["Date"])
    min_date = df["Date_Obj"].min()
    df = df[df["Date_Obj"] < min_date + pd.Timedelta(days=7)]
    df = df.sort_values(by=["Date_Obj", "Hour"]).drop(columns=["Date_Obj"])
    return df

# Greentable Prompts
GEMINI_BM_PASS_1 = '''Ambil HANYA bagian "Bonus Mingguan" dan "Bonus Harian" dari gambar insentif rider. Balikin JSON PERSIS format ini: {"bonus_mingguan": [{"tempoh_label": "Tempoh 1", "date_range": "29 Jun - 2 Jul 2026", "tiers": [{"jumlah_pesanan": 30, "biasa": "RM20", "berganda": "RM35"}]}], "bonus_harian": {"date_range": "29 Jun - 5 Jul 2026", "jumlah_pesanan": 15, "bonus": "RM10"}}'''
GEMINI_BM_PASS_2 = '''JSON awal: {JSON_SEBELUMNYA}\nCek ulang gambar. Apakah ada "Tempoh" yang terlewat? Pastikan setiap Tempoh memiliki tepat 5 tier. Kembalikan JSON final.'''
GEMINI_BM_PASS_3 = '''JSON revisi: {JSON_SEBELUMNYA}\nFinal check. Pastikan akurasi angka dan format valid. Kembalikan array JSON sempurna.'''
GEMINI_TP_PASS_1 = f'''Fokus HANYA "Tambang Puncak". Unpivot data dengan aturan:\n1. 7 HARI PERTAMA saja.\n2. Hanya sel ber-isi.\n3. Date YYYY-MM-DD (Tahun {TAHUN_SEKARANG}).\n4. Day bahasa Inggris.\n5. Hour AM/PM eksplisit.\n6. Bonus persentase.\nBalikin JSON PERSIS format: [{{"Date": "{TAHUN_SEKARANG}-06-29", "Day": "Monday", "Hour": "12 AM", "Bonus": "15%"}}]'''
GEMINI_TP_PASS_2 = '''JSON awal: {JSON_SEBELUMNYA}\nSCAN ULANG. Sel sering TERLEWAT di Jumat 5 PM. Jika ada yang terlewat, TAMBAHKAN. Kembalikan JSON revisi.'''
GEMINI_TP_PASS_3 = '''JSON revisi: {JSON_SEBELUMNYA}\nFinal check: Tidak ada tertinggal di 7 hari, tidak ada jam siluman. Kembalikan JSON sempurna.'''

def run_gemini_3_pass(img, prompt1, prompt2, prompt3, module_name, status_box):
    for model_name in get_model_fallback_list():
        model = genai.GenerativeModel(model_name)
        delay = 5
        for attempt in range(3):
            try:
                status_box.info(f"⏳ [{module_name}] Pass 1 ({model_name.split('/')[-1]})...")
                r1 = re.sub(r"^```json|```$", "", model.generate_content([prompt1, img]).text.strip(), flags=re.MULTILINE).strip()
                status_box.info(f"🔍 [{module_name}] Pass 2 ({model_name.split('/')[-1]})...")
                r2 = re.sub(r"^```json|```$", "", model.generate_content([prompt2.replace("{JSON_SEBELUMNYA}", r1), img]).text.strip(), flags=re.MULTILINE).strip()
                status_box.info(f"🛡️ [{module_name}] Pass 3 ({model_name.split('/')[-1]})...")
                r3 = re.sub(r"^```json|```$", "", model.generate_content([prompt3.replace("{JSON_SEBELUMNYA}", r2), img]).text.strip(), flags=re.MULTILINE).strip()
                status_box.success(f"✅ [{module_name}] 3-Pass Selesai!")
                return json.loads(r3)
            except Exception as e:
                if "429" in str(e) or "503" in str(e):
                    time.sleep(delay)
                    delay *= 2
                else: break
    raise Exception(f"{module_name} Gagal via Gemini.")


# ==============================================================================
# 5. LOGIKA APLIKASI 2: GEMS EXTRACTOR
# ==============================================================================
def ge_enhance_image_for_ocr(path):
    img = Image.open(path).convert('L')
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = ImageEnhance.Sharpness(img).enhance(2.0)
    enhanced_path = "enhanced_" + os.path.basename(path)
    img.save(enhanced_path)
    return enhanced_path

def ge_extract_gems_rewards(ocr_result_list):
    start_idx, end_idx = -1, len(ocr_result_list)
    for idx, text in enumerate(ocr_result_list):
        if 'target' in text.lower(): start_idx = idx
        if 'qualification' in text.lower() or 'criteria' in text.lower():
            end_idx = idx
            break
    target_section_text = ocr_result_list[start_idx + 1: end_idx]
    gems_found, rewards_found = [], []
    for text in target_section_text:
        if '%' in text: continue
        matches = re.findall(r'\d+(?:\.\d+)?', text)
        for m in matches:
            val = float(m)
            if val <= 0 or val > 200: continue
            if '.' in text or '$' in text or 's$' in text.lower(): rewards_found.append(val)
            elif val.is_integer() and val < 150: gems_found.append(int(val))
    return sorted(list(set(gems_found))), sorted(list(set(rewards_found))), target_section_text

def ge_extract_via_gemini(image_path, api_key):
    if not api_key: return [], []
    try:
        models = [m.name for m in genai.list_models() if "generateContent" in m.supported_generation_methods]
        gemini_model = genai.GenerativeModel(next((m for m in models if "1.5-flash" in m), models[0]))
        prompt = """Look at this screenshot of a delivery rider mission app. Find the "TARGET & REWARD" section. It contains exactly 3 tiers, each with a gems target number and S$ reward amount. Return ONLY a raw JSON object: {"gems": [g1, g2, g3], "rewards": [r1, r2, r3]}"""
        img = Image.open(image_path)
        teks_raw = gemini_model.generate_content([prompt, img]).text
        data = json.loads(re.sub(r'^```json\s*|\s*```$', '', teks_raw.strip()))
        gems = sorted([int(g) for g in data.get("gems", [])])
        rewards = sorted([float(r) for r in data.get("rewards", [])])
        if len(gems) == 3 and len(rewards) == 3: return gems, rewards
    except: pass
    return [], []

def ge_get_target_cells(tier, vehicle):
    t, v = tier.lower(), vehicle.lower()
    same_tier_group = ['E-bike', 'Bicycle'] if v in ['e-bike', 'bicycle'] else (['Walker'] if v == 'walker' else [vehicle.capitalize()])
    targets = [(tier, vg) for vg in same_tier_group]
    if t in ['diamond', 'sapphire'] and v != 'walker':
        for vg in same_tier_group: targets.append(('Sapphire' if t == 'diamond' else 'Diamond', vg))
    unique = []
    for tg in targets:
        if tg not in unique: unique.append(tg)
    return unique

def ge_trigger_color_compare(row_numbers):
    if not APPS_SCRIPT_WEBHOOK_URL or not row_numbers: return
    try:
        requests.post(APPS_SCRIPT_WEBHOOK_URL, json={"rows": sorted(set(row_numbers))}, timeout=30)
        st.info(f"🎨 Pewarnaan cell (Apps Script) dijalankan untuk {len(set(row_numbers))} baris.")
    except Exception as e:
        st.warning(f"⚠️ Gagal trigger pewarnaan Apps Script: {e}")


# ==============================================================================
# 6. MEMBANGUN UI DENGAN TABS
# ==============================================================================
try:
    gc = get_gsheets_client()
    st.sidebar.success("✅ Google Sheets Connected!")
except Exception as e:
    st.error(f"❌ Gagal koneksi Service Account: {e}")
    st.stop()

tab_gt, tab_gems = st.tabs(["📊 Greentable (Mingguan & Puncak)", "💎 Gems Extractor"])

# ---------------------------------------------------------
# TAB 1: GREENTABLE
# ---------------------------------------------------------
with tab_gt:
    st.header("📊 GNS Greentable Automation")
    st.caption("Upload jadwal insentif (Malaysia) -> Masuk ke tab Mingguan & Puncak otomatis.")
    
    gt_files = st.file_uploader("Unggah Gambar Greentable (Batch)", type=["jpg", "png"], accept_multiple_files=True, key="up_gt")
    
    if gt_files and st.button("🚀 Ekstrak Greentable", type="primary", key="btn_gt"):
        if not gemini_api_key: st.error("⚠️ API Key Gemini di Sidebar belum diisi!"); st.stop()
        genai.configure(api_key=gemini_api_key)
        
        all_mingguan, all_puncak = [], []
        prog_gt = st.progress(0)
        
        for idx, f in enumerate(gt_files):
            with st.expander(f"⚙️ Memproses: {f.name}", expanded=True):
                box_gt = st.empty()
                tmp_gt = f"tmp_gt_{f.name}"
                with open(tmp_gt, "wb") as file: file.write(f.getbuffer())
                img_gt = Image.open(tmp_gt)
                
                # Mingguan
                try:
                    box_gt.info("OCR Mingguan...")
                    bm, bh = gt_extract_mingguan_ocr(tmp_gt)
                except Exception:
                    box_gt.warning("OCR gagal, beralih ke Gemini 3-Pass...")
                    res_m = run_gemini_3_pass(img_gt, GEMINI_BM_PASS_1, GEMINI_BM_PASS_2, GEMINI_BM_PASS_3, "Mingguan", box_gt)
                    bm, bh = res_m.get("bonus_mingguan", []), res_m.get("bonus_harian", {})
                
                all_mingguan.append(gt_build_sheet_rows_mingguan(bm, bh))
                
                # Puncak
                box_gt.info("Ekstraksi Tambang Puncak (Gemini 3-Pass)...")
                raw_p = run_gemini_3_pass(img_gt, GEMINI_TP_PASS_1, GEMINI_TP_PASS_2, GEMINI_TP_PASS_3, "Tambang Puncak", box_gt)
                all_puncak.append(gt_process_raw_data_puncak(raw_p))
                
                os.remove(tmp_gt)
            prog_gt.progress((idx + 1) / len(gt_files))
            
        with st.spinner("Mengunggah ke Sheets..."):
            if all_mingguan:
                upload_to_gsheets(gc, pd.concat(all_mingguan, ignore_index=True), GT_SPREADSHEET_ID, GT_TAB_MINGGUAN)
            if all_puncak:
                upload_to_gsheets(gc, pd.concat(all_puncak, ignore_index=True), GT_SPREADSHEET_ID, GT_TAB_PUNCAK)
        st.balloons(); st.success("🎉 Greentable berhasil diperbarui!")

# ---------------------------------------------------------
# TAB 2: GEMS EXTRACTOR
# ---------------------------------------------------------
with tab_gems:
    st.header("💎 Gems Extractor Automation")
    st.caption("Upload *screenshot* misi -> diekstrak -> masuk ke spreadsheet SG.")
    
    col1, col2 = st.columns(2)
    sel_tier = col1.selectbox("Pilih Tier", ['Diamond', 'Sapphire', 'Ruby', 'Emerald'])
    sel_veh = col2.selectbox("Pilih Vehicle", ['Walker', 'Motorcycle', 'E-bike', 'Bicycle'])
    
    st.info(f"🔄 **Auto-fill ke kembaran:** {', '.join([f'{t}-{v}' for t, v in ge_get_target_cells(sel_tier, sel_veh)])}")
    
    ge_files = st.file_uploader("Unggah Gambar Misi (Batch)", type=["jpg", "png"], accept_multiple_files=True, key="up_ge")
    
    if ge_files and st.button("🚀 Ekstrak Gems", type="primary", key="btn_ge"):
        try:
            sh_gems = gc.open_by_url(GEMS_SPREADSHEET_URL).worksheet("This week")
            all_rows = sh_gems.get_all_values()
        except Exception as e:
            st.error(f"Gagal buka Sheet Gems: {e}"); st.stop()
            
        prog_ge = st.progress(0)
        touched_rows = set()
        
        for idx, f in enumerate(ge_files):
            with st.expander(f"⚙️ Memproses: {f.name}", expanded=True):
                tmp_ge = f"tmp_ge_{f.name}"
                with open(tmp_ge, "wb") as file: file.write(f.getbuffer())
                img_ge = Image.open(tmp_ge)
                if img_ge.width > img_ge.height:
                    img_ge = img_ge.rotate(90, expand=True)
                    img_ge.save(tmp_ge)

                result = reader.readtext(tmp_ge, detail=0)
                full_text = " ".join(result)

                # Cari Tanggal
                f_date, e_day = None, None
                match = re.search(r'(\d{1,2})[\s_]*([A-Za-z]{3,9})', os.path.splitext(f.name)[0])
                if match:
                    try: 
                        dt = datetime.strptime(f"{match.group(1)} {match.group(2)[:3].capitalize()} 2026", "%d %b %Y")
                        e_day, f_date = dt.strftime("%A"), dt.strftime("%d/%m/%Y")
                    except: pass
                if not f_date:
                    match = re.search(r'(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4})?', full_text)
                    if match:
                        try:
                            dt = datetime.strptime(f"{match.group(1)} {match.group(2)[:3].capitalize()} {match.group(3) or '2026'}", "%d %b %Y")
                            e_day, f_date = dt.strftime("%A"), dt.strftime("%d/%m/%Y")
                        except: pass
                
                if not f_date:
                    st.error("⚠️ Tanggal tidak terdeteksi."); continue
                st.write(f"📅 Waktu: **{f_date} ({e_day})**")

                # Ekstrak Angka
                gems, rwds, _ = ge_extract_gems_rewards(result)
                if len(gems) != 3 or len(rwds) != 3:
                    gems, rwds, _ = ge_extract_gems_rewards(reader.readtext(ge_enhance_image_for_ocr(tmp_ge), detail=0, text_threshold=0.4, low_text=0.3))
                    if len(gems) != 3 or len(rwds) != 3:
                        gems, rwds = ge_extract_via_gemini(tmp_ge, gemini_api_key)
                
                if len(gems) == 3 and len(rwds) == 3:
                    pairs = list(zip(gems, rwds))
                    t_day = e_day.lower()
                    for c_tier, c_veh in ge_get_target_cells(sel_tier, sel_veh):
                        matched = []
                        for r_idx, row in enumerate(all_rows):
                            if r_idx == 0: continue
                            r_t = row[0].strip().lower() if len(row) > 0 else ""
                            r_v = row[1].strip().lower() if len(row) > 1 else ""
                            r_d = row[2].strip().lower() if len(row) > 2 else ""
                            if r_d == "wedesday": r_d = "wednesday"
                            if r_t == c_tier.lower() and r_v == c_veh.lower() and r_d == t_day:
                                matched.append(r_idx + 1)

                        if len(matched) >= 3:
                            payload = [[f_date, pairs[i][0], round(pairs[i][1], 2), round((pairs[i][1]/pairs[i][0]) if pairs[i][0]>0 else 0, 2)] for i in range(3)]
                            s, e = matched[0], matched[2]
                            sh_gems.update(values=payload, range_name=f"D{s}:G{e}", value_input_option="USER_ENTERED")
                            touched_rows.update(matched)
                            st.success(f"✨ Data {c_tier}-{c_veh} masuk baris {s}-{e}.")
                        else:
                            st.error(f"❌ Baris {c_tier}-{c_veh} hari {e_day} tidak ditemukan.")
                else:
                    st.error("❌ Gagal baca angka Gems/Reward.")
                os.remove(tmp_ge)
            prog_ge.progress((idx + 1) / len(ge_files))
            
        ge_trigger_color_compare(touched_rows)
        st.balloons(); st.success("🎉 Selesai memproses misi Gems!")