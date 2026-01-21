import streamlit as st
import pandas as pd
import requests
import base64
import json
import io
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
# LOGIC: AI Analysis (Direct API)
# ---------------------------------------------------------
def analyze_image(image, prompt):
    buffered = io.BytesIO()
    image.save(buffered, format="JPEG")
    img_base64 = base64.b64encode(buffered.getvalue()).decode()
    
    # Using auto-discovery of models to prevent 404s
    try:
        discovery_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GOOGLE_API_KEY}"
        models = requests.get(discovery_url).json().get('models', [])
        model_name = next((m['name'].replace("models/", "") for m in models if "flash" in m['name']), "gemini-1.5-pro")
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GOOGLE_API_KEY}"
        payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": img_base64}}]}]}
        
        response = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload)
        if response.status_code == 200:
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        return f"AI Error {response.status_code}"
    except Exception as e:
        return f"Connection Error: {e}"

# ---------------------------------------------------------
# OPTIMIZED LOGIC: Cloud Storage (With Compression)
# ---------------------------------------------------------
def save_audit_to_cloud(store_code, mgr_name, result_text, image):
    try:
        # 1. Determine PASS/FAIL
        status = "FAIL" if "FAIL" in result_text else "PASS"
        
        # 2. IMAGE COMPRESSION (The Magic Step)
        # Resize to max 800px width (plenty for AI)
        image = image.copy()
        image.thumbnail((800, 800)) 
        
        # Save as optimized JPEG
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=50, optimize=True)
        img_byte_arr = img_byte_arr.getvalue()
        
        # 3. Upload Optimized Image
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{store_code}_{timestamp}.jpg"
        
        supabase.storage.from_("audit-photos").upload(
            filename, 
            img_byte_arr, 
            {"content-type": "image/jpeg"}
        )
        
        # Get Public URL
        img_url = supabase.storage.from_("audit-photos").get_public_url(filename)

        # 4. Insert Data Row
        data = {
            "store_code": store_code,
            "manager_name": mgr_name,
            "audit_type": "Trial Room",
            "result": status,
            "reason": result_text,
            "image_url": img_url,
            "created_at": datetime.now().isoformat()
        }
        supabase.table("audit_logs").insert(data).execute()
        return True, status
    except Exception as e:
        return False, str(e)

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

    # Sidebar for navigation
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
        st.header("📸 Trial Room Audit")
        
        # Nudge Reminder
        st.warning("🔔 Remember to complete audits at 11:30 AM, 2:30 PM, 5:00 PM, and 7:00 PM daily.")

        img_input = st.camera_input("Take Photo")
        if img_input and st.button("Run AI Audit & Submit"):
            with st.spinner("Analyzing and Uploading to Cloud..."):
                image = Image.open(img_input)
                prompt = "You are a store auditor. Count clothing items on desk/floor. If >3 answer 'FAIL: Too messy'. If <=3 answer 'PASS: Tidy'."
                result_text = analyze_image(image, prompt)
                
                if "AI Error" not in result_text:
                    # Save to Cloud
                    success, status = save_audit_to_cloud(st.session_state['code'], st.session_state['mgr'], result_text, image)
                    if success:
                        if status == "FAIL":
                            st.error(f"Audit Submitted: {status}. Cluster Manager Notified.")
                        else:
                            st.success(f"Audit Submitted: {status}.")
                    else:
                        st.error(f"Cloud Upload Failed: {status}")
                else:
                    st.error(result_text)

        if st.button("Logout"):
            st.session_state['logged_in'] = False
            st.rerun()

def cluster_manager_interface():
    st.header("👀 Cluster Manager View")
    # Simple password protection for CM view for now
    pwd = st.text_input("Enter CM Password", type="password")
    if pwd == "admin123": # Change this!
        if st.button("Refresh Today's Data"):
            # Query Supabase for today's logs
            today = datetime.now().strftime("%Y-%m-%d")
            try:
                response = supabase.table("audit_logs").select("*") \
                    .filter("created_at", "gte", f"{today}T00:00:00") \
                    .order("created_at", desc=True).execute()
                
                data = response.data
                if data:
                    df_logs = pd.DataFrame(data)
                    
                    # Metrics
                    st.metric("Total Audits Today", len(df_logs))
                    fails = len(df_logs[df_logs['result'] == 'FAIL'])
                    st.metric("Failures", fails, delta=-fails, delta_color="inverse")

                    st.subheader("Detailed Logs")
                    # Display data with images
                    for index, row in df_logs.iterrows():
                        with st.expander(f"{row['created_at'][11:16]} - Store {row['store_code']} - {row['result']}"):
                            col1, col2 = st.columns([1, 2])
                            with col1:
                                if row['image_url']:
                                    st.image(row['image_url'], width=200)
                            with col2:
                                st.write(f"**Manager:** {row['manager_name']}")
                                st.write(f"**Reason:** {row['reason']}")
                                if row['result'] == 'FAIL':
                                    st.error("Action Required")
                else:
                    st.info("No audits conducted yet today.")
            except Exception as e:
                st.error(f"Database Error: {e}")

if __name__ == "__main__":
    main()