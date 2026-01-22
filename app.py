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
# CONFIGURATION
# ---------------------------------------------------------
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
    SUPABASE_URL = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
except FileNotFoundError:
    st.error("Secrets not found. Please setup secrets.toml or Streamlit Cloud Secrets.")
    st.stop()

# Initialize Cloud Backend
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

st.set_page_config(page_title="Trends Audit Pilot", page_icon="🎯", layout="wide")

# ---------------------------------------------------------
# LOGIC: Traffic Dodge AI (Tries 3 distinct models)
# ---------------------------------------------------------
def analyze_image(image):
    # 1. OPTIMIZE IMAGE (Aggressive resize for speed)
    img_small = image.copy()
    img_small.thumbnail((800, 800))
    
    buffered = io.BytesIO()
    img_small.save(buffered, format="JPEG", quality=60, optimize=True)
    img_base64 = base64.b64encode(buffered.getvalue()).decode()
    
    # 2. THE PROMPT
    system_prompt = """
    You are a strict retail store auditor for 'Trends'. Analyze this image.
    
    STEP 1: CLASSIFY the image into exactly one of these categories:
    - 'Trial Room' (Look for desks, mirrors, cubicles)
    - 'Staff Grooming' (Look for a person, uniform, ID card)
    - 'Greeter' (Look for store entrance, security guard)
    - 'Merchandise Display' (Look for shelves, clothes stacks)

    STEP 2: AUDIT based on these STRICT criteria:
    
    [Trial Room]
    - FAIL if: >3 items on desk/floor, trash visible, dirty mirror.
    - PASS only if: Clean, empty desk, organized.

    [Staff Grooming]
    - FAIL if: No ID Card, untucked shirt, casual shoes.
    - PASS only if: Sharp uniform, ID card visible.

    [Greeter]
    - FAIL if: Entrance empty (no staff), trash at door.
    - PASS only if: Staff present, clean entrance.

    [Merchandise Display]
    - FAIL if: Gaps on shelves, messy stacks, fallen items.
    - PASS only if: Fully stocked, aligned.

    STEP 3: OUTPUT FORMAT (Strictly):
    Category: [Name] | Result: [PASS/FAIL] | Reason: [Short sentence]
    """

    # 3. THE MODEL LIST (The "Lanes")
    # Lane 1: The new "Lightweight" model (Less crowded)
    # Lane 2: The standard model
    # Lane 3: The old reliable model (Legacy)
    models = ["gemini-1.5-flash-8b", "gemini-1.5-flash", "gemini-1.0-pro"]

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GOOGLE_API_KEY}"
        payload = {"contents": [{"parts": [{"text": system_prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": img_base64}}]}]}
        
        try:
            # Short timeout to fail fast and try next model
            response = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload, timeout=8)
            
            if response.status_code == 200:
                return response.json()['candidates'][0]['content']['parts'][0]['text']
            
            # If busy, just loop to the next model immediately
            continue

        except Exception:
            continue

    return "System Error: All AI lanes are busy. Please try again in 1 minute."

# ---------------------------------------------------------
# LOGIC: Cloud Storage & Parsing
# ---------------------------------------------------------
def save_audit_to_cloud(store_code, mgr_name, result_text, image):
    try:
        # Defaults
        category = "General"
        status = "FAIL" 
        reason = result_text

        # 1. PARSE AI RESULT
        if "System Error" in result_text or "Connection Error" in result_text:
            status = "FAIL"
            reason = "AI Service Busy - Please Retry"
        elif "|" in result_text:
            parts = result_text.split("|")
            for part in parts:
                if "Category:" in part: category = part.replace("Category:", "").strip()
                if "Result:" in part: status = part.replace("Result:", "").strip()
                if "Reason:" in part: reason = part.replace("Reason:", "").strip()
        else:
            status = "FAIL" if "FAIL" in result_text.upper() else "PASS"

        # 2. COMPRESS IMAGE (For Cloud Storage)
        image = image.copy()
        image.thumbnail((800, 800)) 
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=50, optimize=True)
        img_byte_arr = img_byte_arr.getvalue()
        
        # 3. UPLOAD TO SUPABASE
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{store_code}_{timestamp}.jpg"
        
        supabase.storage.from_("audit-photos").upload(
            filename, 
            img_byte_arr, 
            {"content-type": "image/jpeg"}
        )
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

# ---------------------------------------------------------
# DATA LOADER
# ---------------------------------------------------------
@st.cache_data
def load_store_data():
    try:
        df = pd.read_csv("stores.csv")
        df.columns = df.columns.str.strip()
        df['Store Code'] = df['Store Code'].astype(str).str.strip()
        return df
    except:
        return None

# ---------------------------------------------------------
# UI
# ---------------------------------------------------------
def main():
    st.title("🎯 Trends Audit Pilot")

    role = st.sidebar.radio("Select Role", ["Store Manager", "Cluster Manager"])

    if role == "Store Manager":
        store_manager_interface()
    else:
        cluster_manager_interface()

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
                else:
                    st.error("Invalid Code")
    else:
        st.info(f"Store: {st.session_state['code']} | Manager: {st.session_state['mgr']}")
        st.header("📸 Smart Audit")
        
        img_input = st.camera_input("Take Photo")
        
        if img_input and st.button("Run Audit"):
            with st.spinner("Analyzing..."):
                image = Image.open(img_input)
                result_text = analyze_image(image)
                
                success, status, category, reason = save_audit_to_cloud(
                    st.session_state['code'], 
                    st.session_state['mgr'], 
                    result_text, 
                    image
                )
                
                if success:
                    st.divider()
                    st.subheader(f"Detected: {category}")
                    
                    if status == "PASS":
                        st.success(f"✅ PASS")
                        st.write(f"**Reason:** {reason}")
                    else:
                        st.error(f"❌ {status}")
                        st.write(f"**Reason:** {reason}")
                else:
                    st.error(f"Upload Failed: {status}")

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
                    with st.spinner("Fetching..."):
                        response = supabase.table("audit_logs").select("*") \
                            .filter("created_at", "gte", f"{today}T00:00:00") \
                            .order("created_at", desc=True).execute()
                    
                    data = response.data
                    if data:
                        df_logs = pd.DataFrame(data)
                        df_logs = df_logs[df_logs['store_code'].isin(my_stores)]
                        
                        if not df_logs.empty:
                            st.metric("Total Audits", len(df_logs))
                            fails = len(df_logs[df_logs['result'] == 'FAIL'])
                            st.metric("Issues", fails, delta=-fails, delta_color="inverse")
                            
                            st.divider()
                            for index, row in df_logs.iterrows():
                                try:
                                    utc_time = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
                                    ist_offset = timezone(timedelta(hours=5, minutes=30))
                                    ist_time = utc_time.astimezone(ist_offset)
                                    fmt_time = ist_time.strftime("%I:%M %p") 
                                except:
                                    fmt_time = "Time Error"

                                label = f"{row['store_code']} - {row['audit_type']} - {row['result']}"
                                with st.expander(f"{fmt_time} | {label}"):
                                    col1, col2 = st.columns([1, 2])
                                    with col1:
                                        if row['image_url']: st.image(row['image_url'], width=200)
                                    with col2:
                                        st.write(f"**Manager:** {row['manager_name']}")
                                        st.write(f"**Reason:** {row['reason']}")
                                        if row['result'] == 'FAIL': st.error("Action Required")
                        else:
                            st.info("No data found.")
                    else:
                        st.info("No audits found.")
                except Exception as e:
                    st.error(f"Database Error: {e}")
        else:
            st.error("Column 'Cluster Manager' not found.")

if __name__ == "__main__":
    main()
