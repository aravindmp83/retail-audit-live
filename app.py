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
# ---------------------------------------------------------
# LOGIC: AI Analysis (Smart Auto-Classification)
# ---------------------------------------------------------
def analyze_image(image, prompt_override=None):
    buffered = io.BytesIO()
    image.save(buffered, format="JPEG")
    img_base64 = base64.b64encode(buffered.getvalue()).decode()
    
    # THE "SMART" PROMPT
    # We give the AI the rulebook here.
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

    try:
        discovery_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GOOGLE_API_KEY}"
        models = requests.get(discovery_url).json().get('models', [])
        # Prefer "Flash" for speed, fall back to Pro
        model_name = next((m['name'].replace("models/", "") for m in models if "flash" in m['name']), "gemini-1.5-pro")
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GOOGLE_API_KEY}"
        payload = {"contents": [{"parts": [{"text": system_prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": img_base64}}]}]}
        
        response = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload)
        if response.status_code == 200:
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        return f"AI Error {response.status_code}"
    except Exception as e:
        return f"Connection Error: {e}"

# ---------------------------------------------------------
# OPTIMIZED LOGIC: Cloud Storage (With Auto-Category Parsing)
# ---------------------------------------------------------
def save_audit_to_cloud(store_code, mgr_name, result_text, image):
    try:
        # 1. Parse the AI Response (Format: "Category: X | Result: Y | Reason: Z")
        # Set defaults in case AI output is messy
        category = "General"
        status = "FAIL"
        reason = result_text

        if "|" in result_text:
            parts = result_text.split("|")
            for part in parts:
                if "Category:" in part:
                    category = part.replace("Category:", "").strip()
                if "Result:" in part:
                    status = part.replace("Result:", "").strip()
                if "Reason:" in part:
                    reason = part.replace("Reason:", "").strip()
        else:
            # Fallback for simple "FAIL" responses
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

        # 4. Insert Data (With the new Category!)
        data = {
            "store_code": store_code,
            "manager_name": mgr_name,
            "audit_type": category,   # <--- Saving the auto-detected category
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
        st.header("📸 Smart Audit")
        
        # Nudge Reminder
        st.warning("🔔 Remember to complete audits at 11:30 AM, 2:30 PM, 5:00 PM, and 7:00 PM daily.")

        img_input = st.camera_input("Take Photo")
        
        # --- FIXED LOGIC STARTS HERE ---
        if img_input and st.button("Run Smart Audit"):
            with st.spinner("AI is classifying and auditing..."):
                image = Image.open(img_input)
                
                # 1. Run AI
                result_text = analyze_image(image)
                
                if "AI Error" not in result_text:
                    # 2. Save & Parse (Returns 4 values)
                    success, status, category, reason = save_audit_to_cloud(
                        st.session_state['code'], 
                        st.session_state['mgr'], 
                        result_text, 
                        image
                    )
                    
                    if success:
                        # 3. Show Result Card
                        st.divider()
                        st.subheader(f"Detected: {category}")
                        
                        if status == "PASS":
                            st.success(f"✅ PASS")
                            st.write(f"**Reason:** {reason}")
                        else:
                            st.error(f"❌ FAIL")
                            st.write(f"**Reason:** {reason}")
                            st.info("Action: Please fix the issue and re-audit.")
                    else:
                        st.error(f"Cloud Upload Failed: {status}")
                else:
                    st.error(result_text)

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
