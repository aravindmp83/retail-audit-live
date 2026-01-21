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
# CONFIGURATION (SECURE FOR CLOUD)
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

st.set_page_config(page_title="Trends Audit Live", page_icon="✅", layout="wide")

# ---------------------------------------------------------
# LOGIC: AI Analysis (Robust Multi-Model Retry)
# ---------------------------------------------------------
def analyze_image(image):
    buffered = io.BytesIO()
    image.save(buffered, format="JPEG")
    img_base64 = base64.b64encode(buffered.getvalue()).decode()
    
    # THE "SMART" PROMPT (Classify & Audit)
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

    # PRIORITY LIST: Try these models in order until one works
    # This fixes 404 (Not Found) and 429 (Busy) errors
    models_to_try = [
        "gemini-1.5-flash",
        "gemini-1.5-flash-latest",
        "gemini-1.5-pro", 
        "gemini-pro" # Legacy backup
    ]
    
    for model_name in models_to_try:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GOOGLE_API_KEY}"
            payload = {"contents": [{"parts": [{"text": system_prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": img_base64}}]}]}
            
            # Timeout prevents it from hanging forever
            response = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload, timeout=10)
            
            if response.status_code == 200:
                # SUCCESS!
                return response.json()['candidates'][0]['content']['parts'][0]['text']
            
            elif response.status_code in [429, 503]:
                # BUSY? Wait 2 seconds and try next model
                time.sleep(2)
                continue
            
            elif response.status_code == 404:
                # MODEL NOT FOUND? Try next model immediately
                continue
            
            else:
                # Other errors (400, 403)
                return f"AI Error {response.status_code}"
                
        except Exception as e:
            time.sleep(1)
            continue

    return "System Busy. Please wait 1 minute and try again."

# ---------------------------------------------------------
# LOGIC: Cloud Storage & Parsing
# ---------------------------------------------------------
def save_audit_to_cloud(store_code, mgr_name, result_text, image):
    try:
        # 1. Parse AI Response
        category = "General"
        status = "FAIL" # Default to FAIL for safety
        reason = result_text

        # CRITICAL FIX: If AI failed, force FAIL status so it doesn't look compliant
        if "AI Error" in result_text or "System Busy" in result_text:
            status = "FAIL"
            reason = "System Error - Please Retry"
        elif "|" in result_text:
            parts = result_text.split("|")
            for part in parts:
                if "Category:" in part:
                    category = part.replace("Category:", "").strip()
                if "Result:" in part:
                    status = part.replace("Result:", "").strip()
                if "Reason:" in part:
                    reason = part.replace("Reason:", "").strip()
        else:
            # Fallback for simple responses
            status = "FAIL" if "FAIL" in result_text.upper() else "PASS"

        # 2. Compress Image (Quality 50)
        image = image.copy()
        image.thumbnail((800, 800)) 
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=50, optimize=True)
        img_byte_arr = img_byte_arr.getvalue()
        
        # 3. Upload
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{store_code}_{timestamp}.jpg"
        
        supabase.storage.from_("audit-photos").upload(
            filename, 
            img_byte_arr, 
            {"content-type": "image/jpeg"}
        )
        img_url = supabase.storage.from_("audit-photos").get_public_url(filename)

        # 4. Insert Data
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
# MAIN APP UI
# ---------------------------------------------------------
def main():
    st.title("✅ Trends Store Audit")

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
        st.header("📸 Run Audit")
        
        img_input = st.camera_input("Take Photo")
        
        if img_input and st.button("Run Smart Audit"):
            with st.spinner("AI is checking rules..."):
                image = Image.open(img_input)
                result_text = analyze_image(image)
                
                # Save & Parse
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
                        st.error(f"❌ FAIL / ACTION REQUIRED")
                        st.write(f"**Reason:** {reason}")
                        if "System Error" in reason:
                            st.warning("⚠️ The AI system is busy. Please wait 1 minute and try again.")
                        else:
                            st.info("Action: Please fix the issue and re-audit.")
                else:
                    st.error(f"Upload Failed: {status}")

        if st.button("Logout"):
            st.session_state['logged_in'] = False
            st.rerun()

def cluster_manager_interface():
    st.header("👀 Cluster Manager View")
    
    df_stores = load_store_data()
    if df_stores is not None:
        # Find CM Column
        cm_col = next((col for col in df_stores.columns if "Cluster" in col or "CM" in col), "Cluster Manager")
        
        if cm_col in df_stores.columns:
            # Dropdown for CMs
            cms = df_stores[cm_col].dropna().unique().tolist()
            cms.sort()
            selected_cm = st.selectbox("Select Your Name", cms)
            
            if st.button("Load My Data"):
                my_stores = df_stores[df_stores[cm_col] == selected_cm]['Store Code'].astype(str).tolist()
                today = datetime.now().strftime("%Y-%m-%d")
                
                try:
                    with st.spinner("Fetching data..."):
                        response = supabase.table("audit_logs").select("*") \
                            .filter("created_at", "gte", f"{today}T00:00:00") \
                            .order("created_at", desc=True).execute()
                    
                    data = response.data
                    if data:
                        df_logs = pd.DataFrame(data)
                        # Filter for selected CM
                        df_logs = df_logs[df_logs['store_code'].isin(my_stores)]
                        
                        if not df_logs.empty:
                            st.metric("Total Audits", len(df_logs))
                            fails = len(df_logs[df_logs['result'] == 'FAIL'])
                            st.metric("Issues Found", fails, delta=-fails, delta_color="inverse")
                            
                            st.divider()
                            for index, row in df_logs.iterrows():
                                # IST Time Conversion
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
                            st.info("No data found for your stores today.")
                    else:
                        st.info("No audits found today.")
                except Exception as e:
                    st.error(f"Database Error: {e}")
        else:
            st.error("Column 'Cluster Manager' not found in CSV.")

if __name__ == "__main__":
    main()
