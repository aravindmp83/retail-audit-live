import streamlit as st
import pandas as pd
import requests
import base64
import json
import io
import time
from PIL import Image
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

# ---------------------------------------------------------
# CONFIGURATION (Supports both flat and nested secrets)
# ---------------------------------------------------------
try:
    # Try flat structure first
    if "GOOGLE_API_KEY" in st.secrets:
        GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
        SUPABASE_URL = st.secrets["SUPABASE_URL"]
        SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
    # Fallback to nested structure from your other app
    else:
        GOOGLE_API_KEY = st.secrets["google"]["api_key"]
        SUPABASE_URL = st.secrets["supabase"]["url"]
        SUPABASE_KEY = st.secrets["supabase"]["key"]
except Exception as e:
    st.error(f"Secret Access Error: {e}. Please check your Streamlit Secrets structure.")
    st.stop()

# Initialize Cloud Backend
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

st.set_page_config(page_title="Trends Audit Live", page_icon="✅", layout="wide")

# ---------------------------------------------------------
# LOGIC: 1. THE "SCOUT" (Smarter model finding)
# ---------------------------------------------------------
@st.cache_data(ttl=3600)
def get_best_model_name():
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GOOGLE_API_KEY}"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            models = response.json().get('models', [])
            # Priority: 1.5-flash -> flash-latest -> 1.5-pro
            for preferred in ["gemini-1.5-flash", "flash-latest", "gemini-1.5-pro"]:
                for m in models:
                    name = m['name'].replace("models/", "")
                    if preferred in name:
                        return name
            if models: return models[0]['name'].replace("models/", "")
        return "gemini-1.5-flash"
    except:
        return "gemini-1.5-flash"

# ---------------------------------------------------------
# LOGIC: 2. THE AUDITOR (With Raw Error Reporting)
# ---------------------------------------------------------
def analyze_image(image):
    img_small = image.copy()
    img_small.thumbnail((800, 800))
    buffered = io.BytesIO()
    img_small.save(buffered, format="JPEG", quality=60, optimize=True)
    img_base64 = base64.b64encode(buffered.getvalue()).decode()
    
    model_name = get_best_model_name()
    
    system_prompt = """
    Analyze this Trends retail store image. 
    Classify as: 'Trial Room', 'Staff Grooming', 'Greeter', or 'Merchandise Display'.
    Audit strictly based on cleanliness and compliance.
    Output: Category: [Name] | Result: [PASS/FAIL] | Reason: [Short sentence]
    """

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GOOGLE_API_KEY}"
    payload = {"contents": [{"parts": [{"text": system_prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": img_base64}}]}]}
    
    try:
        response = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload, timeout=20)
        
        # Simple Retry for busy server
        if response.status_code in [429, 503]:
            time.sleep(5) 
            response = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload, timeout=20)

        if response.status_code == 200:
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        else:
            # SHOW RAW ERROR FOR DEBUGGING
            return f"AI_DEBUG_ERROR {response.status_code}: {response.text}"

    except Exception as e:
        return f"CONNECTION_DEBUG_ERROR: {str(e)}"

# ---------------------------------------------------------
# LOGIC: Cloud Storage & Parsing
# ---------------------------------------------------------
def save_audit_to_cloud(store_code, mgr_name, result_text, image):
    try:
        category = "General"
        status = "FAIL" 
        reason = result_text

        # 1. PARSE RESULT (Now shows raw errors)
        if "DEBUG_ERROR" in result_text:
            status = "FAIL"
            # We no longer hide the error behind "System Busy"
            reason = result_text 
        elif "|" in result_text:
            parts = result_text.split("|")
            for part in parts:
                if "Category:" in part: category = part.replace("Category:", "").strip()
                if "Result:" in part: status = part.replace("Result:", "").strip()
                if "Reason:" in part: reason = part.replace("Reason:", "").strip()
        else:
            status = "FAIL" if "FAIL" in result_text.upper() else "PASS"

        # 2. COMPRESS & UPLOAD
        image.thumbnail((800, 800)) 
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=50, optimize=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{store_code}_{timestamp}.jpg"
        
        supabase.storage.from_("audit-photos").upload(filename, img_byte_arr.getvalue(), {"content-type": "image/jpeg"})
        img_url = supabase.storage.from_("audit-photos").get_public_url(filename)

        # 4. INSERT LOG
        data = {
            "store_code": store_code,
            "manager_name": mgr_name,
            "audit_type": category,
            "result": status,
            "reason": reason,
            "image_url": img_url,
            "created_at": datetime.now().isoformat()
        }
        supabase.table("audit_logs").insert(data).execute()
        return True, status, category, reason
    except Exception as e:
        return False, str(e), "Error", "Error"

# --- REMAINDER OF YOUR UI CODE ---
@st.cache_data
def load_store_data():
    try:
        df = pd.read_csv("stores.csv")
        df.columns = df.columns.str.strip()
        df['Store Code'] = df['Store Code'].astype(str).str.strip()
        return df
    except: return None

def main():
    st.title("✅ Trends Audit Live")
    role = st.sidebar.radio("Select Role", ["Store Manager", "Cluster Manager"])
    if role == "Store Manager": store_manager_interface()
    else: cluster_manager_interface()

def store_manager_interface():
    if 'logged_in' not in st.session_state: st.session_state['logged_in'] = False
    if not st.session_state['logged_in']:
        st.subheader("Store Login")
        df = load_store_data()
        if df is not None:
            code = st.text_input("Enter Store Code").strip()
            if st.button("Login"):
                row = df[df['Store Code'] == code]
                if not row.empty:
                    st.session_state['logged_in'] = True
                    st.session_state['code'] = code
                    st.session_state['mgr'] = row.iloc[0].get('SM Name - USER', 'Manager')
                    st.rerun()
                else: st.error("Invalid Code")
    else:
        st.info(f"Store: {st.session_state['code']} | Manager: {st.session_state['mgr']}")
        img_input = st.camera_input("Take Photo")
        if img_input and st.button("Run Audit"):
            with st.spinner("Connecting..."):
                image = Image.open(img_input)
                result_text = analyze_image(image)
                success, status, category, reason = save_audit_to_cloud(st.session_state['code'], st.session_state['mgr'], result_text, image)
                if success:
                    st.divider()
                    st.subheader(f"Detected: {category}")
                    if status == "PASS": st.success("✅ PASS")
                    else: st.error(f"❌ {status}")
                    st.write(f"**Reason:** {reason}")
                else: st.error(f"Upload Failed: {status}")
        if st.button("Logout"):
            st.session_state['logged_in'] = False
            st.rerun()

def cluster_manager_interface():
    st.header("👀 Cluster Manager View")
    df_stores = load_store_data()
    if df_stores is not None:
        cm_col = next((col for col in df_stores.columns if "Cluster" in col or "CM" in col), "Cluster Manager")
        if cm_col in df_stores.columns:
            cms = df_stores[cm_col].dropna().unique().tolist()
            cms.sort()
            selected_cm = st.selectbox("Select Your Name", cms)
            if st.button("Load Data"):
                my_stores = df_stores[df_stores[cm_col] == selected_cm]['Store Code'].astype(str).tolist()
                today = datetime.now().strftime("%Y-%m-%d")
                try:
                    response = supabase.table("audit_logs").select("*").filter("created_at", "gte", f"{today}T00:00:00").order("created_at", desc=True).execute()
                    if response.data:
                        df_logs = pd.DataFrame(response.data)
                        df_logs = df_logs[df_logs['store_code'].isin(my_stores)]
                        if not df_logs.empty:
                            st.metric("Total Audits", len(df_logs))
                            for index, row in df_logs.iterrows():
                                with st.expander(f"{row['store_code']} - {row['result']}"):
                                    st.image(row['image_url'], width=200)
                                    st.write(f"**Reason:** {row['reason']}")
                        else: st.info("No data.")
                    else: st.info("No audits.")
                except Exception as e: st.error(f"DB Error: {e}")
        else: st.error("Column 'Cluster Manager' not found.")

if __name__ == "__main__":
    main()
