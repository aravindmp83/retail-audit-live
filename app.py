import streamlit as st
import pandas as pd
import requests
import base64
import json
import io
import time
from PIL import Image
from datetime import datetime
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

st.set_page_config(page_title="Trends Audit V2", page_icon="✅", layout="wide")

# ---------------------------------------------------------
# HELPER: Check Available Models (Diagnostic)
# ---------------------------------------------------------
def get_available_models():
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GOOGLE_API_KEY}"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            return [m['name'].replace("models/", "") for m in response.json().get('models', [])]
        return []
    except:
        return []

# ---------------------------------------------------------
# LOGIC: AI Analysis (With Auto-Failover & Diagnostics)
# ---------------------------------------------------------
def analyze_image(image, prompt_override=None):
    buffered = io.BytesIO()
    image.save(buffered, format="JPEG")
    img_base64 = base64.b64encode(buffered.getvalue()).decode()
    
    # THE "SMART" PROMPT
    system_prompt = """
    You are a strict retail store auditor for 'Trends'. Analyze this image.
    
    STEP 1: CLASSIFY the image into exactly one of these 4 categories:
    - 'Trial Room' (Look for desks, mirrors, cubicles)
    - 'Staff Grooming' (Look for a person, uniform, ID card)
    - 'Greeter' (Look for store entrance, security guard, welcome mat)
    - 'Merchandise Display' (Look for shelves, folded clothes, mannequins)

    STEP 2: AUDIT based on these STRICT criteria:
    
    [Trial Room Rules]
    - FAIL if: More than 3 clothing items on desk/floor.
    - FAIL if: Floor is dirty, dusty, or has trash.
    - FAIL if: Mirror is dirty.
    - PASS only if: Clean, empty desk, organized.

    [Staff Grooming Rules]
    - FAIL if: No ID Card visible.
    - FAIL if: Shirt is untucked or wrinkled.
    - FAIL if: Wearing casual shoes/slippers (must be formal).
    - PASS only if: Sharp uniform, ID card present, formal look.

    [Greeter Rules]
    - FAIL if: Entrance area is empty (no staff).
    - FAIL if: Debris or trash at entrance.
    - PASS only if: Staff present at door, clean entrance.

    [Merchandise Display Rules]
    - FAIL if: Visual gaps/empty spaces on shelves.
    - FAIL if: Clothes are folded messily/uneven stacks.
    - FAIL if: Fallen items on floor.
    - PASS only if: Fully stocked, perfectly aligned folds.

    STEP 3: OUTPUT FORMAT
    You must output exactly this format (no bolding, no markdown):
    Category: [Name] | Result: [PASS/FAIL] | Reason: [One short sentence]
    """

    # PRIORITY LIST: Flash -> Pro -> Legacy (gemini-pro)
    # We try widely different versions to ensure ONE works.
    models_to_try = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]
    
    for model_name in models_to_try:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GOOGLE_API_KEY}"
            payload = {"contents": [{"parts": [{"text": system_prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": img_base64}}]}]}
            
            response = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload)
            
            if response.status_code == 200:
                return response.json()['candidates'][0]['content']['parts'][0]['text']
            
            # If 404 (Not Found) or 429/503 (Busy), keep trying
            elif response.status_code in [404, 429, 503]:
                time.sleep(1)
                continue
            
            else:
                # If it's a hard error (e.g. 400), return it so we know
                return f"AI Error {response.status_code}: {response.text}"
                
        except Exception as e:
            time.sleep(1)
            continue

    return "System Busy. Please wait 30 seconds and try again."

# ---------------------------------------------------------
# OPTIMIZED LOGIC: Cloud Storage & Parsing
# ---------------------------------------------------------
def save_audit_to_cloud(store_code, mgr_name, result_text, image):
    try:
        # 1. Parse the AI Response
        category = "General"
        status = "FAIL" # Default to FAIL for safety
        reason = result_text

        # FIXED LOGIC: If AI reports error/busy, force FAIL
        if "Error" in result_text or "System Busy" in result_text:
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
            # Fallback
            status = "FAIL" if "FAIL" in result_text.upper() else "PASS"

        # 2. Compress Image
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
# LOGIC: Load Store Data
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
# UI: Main App
# ---------------------------------------------------------
def main():
    st.title("✅ Trends Store Audit V2")

    # --- SIDEBAR DIAGNOSTIC (Hidden Helper) ---
    with st.sidebar:
        st.write("🔧 **System Check**")
        if st.button("Check Connectivity"):
            models = get_available_models()
            if models:
                st.success(f"Access OK. Found {len(models)} models.")
                with st.expander("View Models"):
                    st.write(models)
            else:
                st.error("No models found. Check API Key.")

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
        st.warning("🔔 Remember to complete audits at 11:30 AM, 2:30 PM, 5:00 PM, and 7:00 PM daily.")

        img_input = st.camera_input("Take Photo")
        
        if img_input and st.button("Run Smart Audit"):
            with st.spinner("AI is classifying and auditing..."):
                image = Image.open(img_input)
                result_text = analyze_image(image)
                
                # We SAVE regardless of error to track issues
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
                        st.error(f"❌ FAIL / ERROR")
                        st.write(f"**Reason:** {reason}")
                        st.info("Action: Please fix the issue (or wait if System Error) and re-audit.")
                else:
                    st.error(f"Cloud Upload Failed: {status}")

        if st.button("Logout"):
            st.session_state['logged_in'] = False
            st.rerun()

def cluster_manager_interface():
    st.header("👀 Cluster Manager View")
    df_stores = load_store_data()
    if df_stores is not None:
        cm_col = next((col for col in df_stores.columns if "Cluster" in col or "CM" in col), "Cluster Manager")
        if cm_col not in df_stores.columns:
            st.error(f"Error: Could not find '{cm_col}' column.")
            return

        cms = df_stores[cm_col].dropna().unique().tolist()
        cms.sort()
        selected_cm = st.selectbox("Select Your Name", cms)
        
        if st.button("Load My Stores"):
            my_stores = df_stores[df_stores[cm_col] == selected_cm]['Store Code'].astype(str).tolist()
            today = datetime.now().strftime("%Y-%m-%d")
            try:
                with st.spinner(f"Fetching audits for {selected_cm}..."):
                    response = supabase.table("audit_logs").select("*") \
                        .filter("created_at", "gte", f"{today}T00:00:00") \
                        .order("created_at", desc=True).execute()
                
                data = response.data
                if data:
                    df_logs = pd.DataFrame(data)
                    df_logs = df_logs[df_logs['store_code'].isin(my_stores)]
                    
                    if not df_logs.empty:
                        st.metric("My Stores Audited", len(df_logs))
                        fails = len(df_logs[df_logs['result'] == 'FAIL'])
                        st.metric("Action Required", fails, delta=-fails, delta_color="inverse")

                        st.divider()
                        st.subheader(f"Detailed Logs ({len(df_logs)})")
                        
                        for index, row in df_logs.iterrows():
                            try:
                                utc_time = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
                                from datetime import timedelta, timezone
                                ist_offset = timezone(timedelta(hours=5, minutes=30))
                                ist_time = utc_time.astimezone(ist_offset)
                                fmt_time = ist_time.strftime("%I:%M %p") 
                            except:
                                fmt_time = row['created_at']

                            label = f"{row['store_code']} - {row['audit_type']} - {row['result']}"
                            
                            with st.expander(f"{fmt_time} | {label}"):
                                col1, col2 = st.columns([1, 2])
                                with col1:
                                    if row['image_url']:
                                        st.image(row['image_url'], width=200)
                                with col2:
                                    st.write(f"**Manager:** {row['manager_name']}")
                                    st.write(f"**Reason:** {row['reason']}")
                                    if row['result'] == 'FAIL':
                                        st.error("❌ Action Required")
                                    else:
                                        st.success("✅ Compliant")
                    else:
                        st.info(f"No audits submitted for {selected_cm}'s stores today yet.")
                else:
                    st.info("No audits found in the system today.")
            except Exception as e:
                st.error(f"Database Error: {e}")

if __name__ == "__main__":
    main()
