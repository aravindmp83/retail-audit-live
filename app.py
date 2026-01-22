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

st.set_page_config(page_title="Trends Audit Debug", page_icon="🐞", layout="wide")

# ---------------------------------------------------------
# LOGIC: DEBUG AI (Prints Raw Errors)
# ---------------------------------------------------------
def analyze_image(image):
    # 1. OPTIMIZE IMAGE
    img_small = image.copy()
    img_small.thumbnail((800, 800))
    buffered = io.BytesIO()
    img_small.save(buffered, format="JPEG", quality=60, optimize=True)
    img_base64 = base64.b64encode(buffered.getvalue()).decode()
    
    # 2. PROMPT
    system_prompt = """
    You are a strict retail store auditor for 'Trends'. Analyze this image.
    STEP 1: CLASSIFY: 'Trial Room', 'Staff Grooming', 'Greeter', 'Merchandise Display'.
    STEP 2: AUDIT based on strict rules.
    STEP 3: OUTPUT FORMAT: Category: [Name] | Result: [PASS/FAIL] | Reason: [Short sentence]
    """

    # 3. MODEL LIST (Only Multimodal Models)
    # We added 'gemini-2.0-flash-exp' which is often free and empty!
    models = ["gemini-1.5-flash", "gemini-1.5-flash-8b", "gemini-2.0-flash-exp"]

    last_error = ""

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GOOGLE_API_KEY}"
        payload = {"contents": [{"parts": [{"text": system_prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": img_base64}}]}]}
        
        try:
            # We use a placeholder for status to show the user what's happening
            with st.status(f"Trying AI Model: {model}...", expanded=True) as status:
                response = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload, timeout=10)
                
                if response.status_code == 200:
                    status.update(label="Success!", state="complete", expanded=False)
                    return response.json()['candidates'][0]['content']['parts'][0]['text']
                
                else:
                    # CAPTURE THE RAW ERROR
                    error_msg = f"Model {model} failed: {response.text}"
                    st.write(error_msg) # Print error to screen for debugging
                    last_error = error_msg
                    status.update(label="Busy/Failed", state="error", expanded=False)
                    continue

        except Exception as e:
            last_error = str(e)
            continue

    # If we get here, ALL models failed. Return the RAW error to the user.
    return f"DEBUG_ERROR: {last_error}"

# ---------------------------------------------------------
# LOGIC: Cloud Storage
# ---------------------------------------------------------
def save_audit_to_cloud(store_code, mgr_name, result_text, image):
    try:
        category = "General"
        status = "FAIL" 
        reason = result_text

        # 1. PARSE RESULT
        # If the result starts with DEBUG_ERROR, we know it failed.
        if "DEBUG_ERROR" in result_text or "Connection Error" in result_text:
            status = "FAIL"
            # We save the raw error to the database reason so we can see it in logs too
            reason = result_text[:100] + "..." # Truncate if too long
        elif "|" in result_text:
            parts = result_text.split("|")
            for part in parts:
                if "Category:" in part: category = part.replace("Category:", "").strip()
                if "Result:" in part: status = part.replace("Result:", "").strip()
                if "Reason:" in part: reason = part.replace("Reason:", "").strip()
        else:
            status = "FAIL" if "FAIL" in result_text.upper() else "PASS"

        # 2. UPLOAD IMAGE
        image = image.copy()
        image.thumbnail((800, 800)) 
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=50, optimize=True)
        img_byte_arr = img_byte_arr.getvalue()
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{store_code}_{timestamp}.jpg"
        
        supabase.storage.from_("audit-photos").upload(
            filename, img_byte_arr, {"content-type": "image/jpeg"}
        )
        img_url = supabase.storage.from_("audit-photos").get_public_url(filename)

        # 3. INSERT DATA
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
# UI & HELPERS
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

def main():
    st.title("🐞 Trends Audit (Debug Mode)")

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
        
        img_input = st.camera_input("Take Photo")
        
        if img_input and st.button("Run Audit"):
            # We removed the spinner so the 'status' container inside analyze_image works
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
                    if "DEBUG_ERROR" in reason:
                        st.warning("Please screenshot the error above and share it.")
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
                    response = supabase.table("audit_logs").select("*").filter("created_at", "gte", f"{today}T00:00:00").order("created_at", desc=True).execute()
                    data = response.data
                    if data:
                        df_logs = pd.DataFrame(data)
                        df_logs = df_logs[df_logs['store_code'].isin(my_stores)]
                        if not df_logs.empty:
                            st.dataframe(df_logs[['store_code', 'audit_type', 'result', 'reason']])
                        else: st.info("No data.")
                    else: st.info("No data.")
                except Exception as e: st.error(f"DB Error: {e}")

if __name__ == "__main__":
    main()
