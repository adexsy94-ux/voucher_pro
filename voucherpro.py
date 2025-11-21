# voucher_pro_app.py
# Streamlit finance manager with Company Settings (configurable PDF header/footer)
# Requirements: streamlit, pandas, reportlab, openpyxl
# Optional (for PDF page screenshots in exported PDF): pymupdf OR pdf2image
#   pip install pymupdf
#   # or
#   pip install pdf2image

import base64
import psycopg2
from psycopg2.extras import DictCursor
import hashlib
from contextlib import closing
from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


import pandas as pd
import streamlit as st


def rerun():
    """Safely rerun the Streamlit app."""
    try:
        st.experimental_rerun()
    except Exception:
        try:
            st.rerun()
        except Exception:
            pass

# -------------------------- # -------------------------- Simple Auth (Signup/Login) --------------------------
# 01_auth_pg.py
# Full auth block rewritten to use PostgreSQL (psycopg2) correctly.
# Replace your existing AUTH_TABLE_SQL, _hash_password, _init_auth, _create_user, _verify_user with this.

import hashlib
from contextlib import closing
from typing import Optional

import psycopg2

AUTH_TABLE_SQL = """CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
"""


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _init_auth():
    """Ensure the users table exists in the main voucher PostgreSQL DB."""
    try:
        conn = connect()  # uses the connect() function from 00_postgres_env_and_connect
    except NameError:
        # connect() not yet defined (because of script order); it will be called again later.
        return

    try:
        with closing(conn):
            with closing(conn.cursor()) as cur:
                cur.execute(AUTH_TABLE_SQL)
                conn.commit()
    except Exception:
        # Do not crash the app if auth init fails; login will just not work.
        pass


def _create_user(username: str, password: str) -> Optional[str]:
    username_norm = (username or "").strip().lower()
    if not username_norm or not password:
        return "Username and password are required."

    pw_hash = _hash_password(password)

    try:
        with closing(connect()) as conn, closing(conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                (username_norm, pw_hash),
            )
            conn.commit()
        return None
    except psycopg2.IntegrityError:
        return "A user with this username already exists."
    except Exception as e:
        return f"Error creating user: {e}"


def _verify_user(username: str, password: str) -> bool:
    username_norm = (username or "").strip().lower()
    if not username_norm or not password:
        return False

    pw_hash = _hash_password(password)

    try:
        with closing(connect()) as conn, closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT password_hash FROM users WHERE username = %s",
                (username_norm,),
            )
            row = cur.fetchone()
    except Exception:
        return False

    if not row:
        return False

    return row[0] == pw_hash


def _require_login():
    """Render login/signup UI and stop if no user is logged in."""
    if "user" not in st.session_state:
        st.session_state["user"] = None

    # try to initialize auth table (connect might now exist)
    try:
        _init_auth()
    except Exception:
        pass

    if st.session_state["user"] is not None:
        return  # already logged in

    st.markdown("<h1>Voucher & CRM Login</h1>", unsafe_allow_html=True)
    login_tab, signup_tab = st.tabs(["Login", "Sign up"])

    with login_tab:
        lu = st.text_input("Username", key="login_username")
        lp = st.text_input("Password", type="password", key="login_password")
        if st.button("Login", key="btn_login"):
            if not lu or not lp:
                st.error("Please enter both username and password.")
            elif _verify_user(lu, lp):
                st.session_state["user"] = lu.strip().lower()
                st.success("Login successful.")
                rerun()
            else:
                st.error("Invalid username or password.")

    with signup_tab:
        su = st.text_input("Choose a username", key="signup_username")
        sp1 = st.text_input("Choose a password", type="password", key="signup_password1")
        sp2 = st.text_input("Confirm password", type="password", key="signup_password2")
        if st.button("Create account", key="btn_signup"):
            if not su or not sp1 or not sp2:
                st.error("Please fill all fields.")
            elif sp1 != sp2:
                st.error("Passwords do not match.")
            else:
                err = _create_user(su, sp1)
                if err:
                    st.error(err)
                else:
                    st.success("Account created. You can now log in.")
    st.stop()

# ---------------- CRM lookup helpers for VoucherPro ----------------
CRM_DB_PATH = Path("crm.sqlite")

def _crm_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Read CRM tables from PostgreSQL using the main connect() function."""
    try:
        with closing(connect()) as conn:
            return pd.read_sql_query(sql, conn, params=params)
    except Exception:
        return pd.DataFrame()


def get_crm_vendor_options() -> List[str]:
    df = _crm_df("SELECT name FROM vendors ORDER BY name")
    opts = df["name"].dropna().tolist() if not df.empty else []
    return opts or ["-- Add vendors in CRM first --"]


def get_crm_requester_options() -> List[str]:
    df = _crm_df(
        """        SELECT first_name, last_name
        FROM staff
        WHERE status = 'Active'
        ORDER BY last_name, first_name
        """
    )
    if df.empty:
        return ["-- Add staff in CRM first --"]

    names: List[str] = []
    for _, row in df.iterrows():
        fn = row.get("first_name") or ""
        ln = row.get("last_name") or ""
        full = f"{fn} {ln}".strip()
        if full:
            names.append(full)
    return names or ["-- Add staff in CRM first --"]

def get_crm_payable_accounts() -> List[str]:
    df = _crm_df(
        """
        SELECT name, type
        FROM accounts
        ORDER BY name
        """
    )
    if df.empty:
        return ["-- Add accounts in CRM first --"]

    liab = df[df["type"].isin(["Liability", "Payable"])]
    if not liab.empty:
        opts = liab["name"].dropna().tolist()
    else:
        opts = df["name"].dropna().tolist()
    return opts or ["-- Add accounts in CRM first --"]


def get_crm_expense_or_asset_accounts() -> List[str]:
    df = _crm_df(
        """
        SELECT name, type
        FROM accounts
        ORDER BY name
        """
    )
    if df.empty:
        return ["-- Add accounts in CRM first --"]

    mask = df["type"].isin(["Expense", "Asset"])
    subset = df[mask] if mask.any() else df
    opts = subset["name"].dropna().tolist()
    return opts or ["-- Add accounts in CRM first --"]


from reportlab.platypus import Image  # already imported at top, repeated here just for clarity
# =========================== ReportLab (PDF) ===========================
try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Flowable, PageBreak, Image, KeepTogether
    )
    REPORTLAB_OK = True
except Exception:
    REPORTLAB_OK = False
# ======================================================================

# ====================== Optional screenshot engines ====================
# 1) PyMuPDF (preferred)
try:
    import fitz  # PyMuPDF
    PYMUPDF_OK = True
except Exception:
    PYMUPDF_OK = False

# 2) pdf2image fallback
try:
    from pdf2image import convert_from_bytes
    PDF2IMAGE_OK = True
except Exception:
    PDF2IMAGE_OK = False
# ======================================================================

# ============================ PAGE CONFIG ==============================
st.set_page_config(
    page_title="VoucherPro - Finance Manager",
    page_icon="💳",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# =============================== THEME ================================
st.markdown("""
<style>
@import url(''https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

:root{
  --bg: #f7f8fa;
  --surface: #ffffff;
  --text: #0f172a;
  --text-muted: #475569;
  --border: #eef0f3;
  --border-strong: #e5e7eb;

  --green-600: #16a34a;
  --green-700: #15803d;

  --radius: 12px;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.03);
}

.main{ background:var(--bg); font-family:''Inter',sans-serif; color:var(--text); }

/* Headings */
h1{ text-align:center; font-size:1.9rem; font-weight:800; margin:.4rem 0 .6rem; letter-spacing:-0.01em; }
.subtitle{ text-align:center; color:var(--text-muted); font-size:.96rem; margin:-.2rem 0 1rem; }

/* Tabs */
.stTabs [role="tablist"]{ gap:6px !important; justify-content:center; margin-bottom:6px; }
.stTabs [role="tab"]{
  background:#fff; border:1px solid var(--border);
  border-radius:999px; padding:8px 14px; font-weight:700; font-size:.92rem; color:var(--text);
  box-shadow:var(--shadow-sm);
}
.stTabs [role="tab"][aria-selected="true"]{
  border-color:#dcfce7; color:#14532d; box-shadow:0 4px 14px rgba(20,83,45,.10);
}

/* Cards */
.card{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  box-shadow:var(--shadow-sm);
  padding:.9rem .95rem;
  margin:.4rem 0 .8rem;
}
.card-header{
  display:flex; align-items:center; gap:.6rem; margin-bottom:.4rem;
  font-weight:800; font-size:1.02rem; color:var(--text);
}
.card-sub{ color:var(--text-muted); font-size:.9rem; margin-top:-.15rem; }

/* Summary boxes */
.total-box{
  background:#fff; color:var(--text);
  border:1px solid var(--border); border-radius:10px;
  text-align:center; font-size:.98rem; font-weight:700; padding:.6rem;
  box-shadow:var(--shadow-sm);
}

/* Calc line */
.calc-line{
  background:#fbfcfd; padding:.55rem .7rem; border-radius:10px;
  border:1px solid var(--border);
  color:var(--text); font-weight:600; margin:.35rem 0;
}

/* Micro notes under tables */
.micro-note{ color:#64748b; font-size:.84rem; margin-top:.3rem; }

/* Alerts */
.info-box, .warning-box, .error-box{
  padding:.7rem; border-radius:10px; font-weight:600; border:1px solid var(--border);
  box-shadow:var(--shadow-sm);
}
.info-box    { background:#f1f6ff; color:#1e40af; }
.warning-box { background:#fff8ed; color:#92400e; }
.error-box   { background:#fff2f2; color:#991b1b; }

/* GREEN primary buttons */
.stButton>button{
  position:relative; overflow:hidden;
  background: linear-gradient(180deg, var(--green-600), var(--green-700));
  color:#ffffff; border:1px solid #14532d; border-radius:999px;
  padding:.58rem 1.1rem; font-weight:800; letter-spacing:.01em;
  box-shadow: 0 10px 22px rgba(21,128,61,.18), inset 0 -2px 0 rgba(255,255,255,.06);
  transition: transform .05s ease, box-shadow .15s ease, filter .15s ease;
}
.stButton>button:hover{ transform:translateY(-1px); filter:brightness(1.03); }
.stButton>button:active{ transform:translateY(0); }
.stButton>button:disabled{ background:#cbd5e1; border-color:#cbd5e1; box-shadow:none; }

/* Inputs */
.stTextInput input, .stNumberInput input, .stTextArea textarea,
.stDateInput input, .stTimeInput input{
  background:#fff; border:1px solid var(--border-strong); border-radius:10px;
  color:var(--text); box-shadow:none; padding:.48rem .6rem; font-size:.94rem;
}
.stTextInput input:focus, .stNumberInput input:focus, .stTextArea textarea:focus,
.stDateInput input:focus, .stTimeInput input:focus{
  outline:none; border-color:#a7f3d0; box-shadow:0 0 0 3px rgba(16,185,129,0.18);
}

/* Select wrapper */
div[data-baseweb="select"] > div{ border:1px solid var(--border-strong); border-radius:10px; box-shadow:none; }
div[data-baseweb="select"] > div:focus-within{
  border-color:#a7f3d0; box-shadow:0 0 0 3px rgba(16,185,129,0.18);
}

/* File uploader */
.stFileUploader > section[data-testid="stFileUploadDropzone"]{
  background:#fff; border:1px dashed var(--border-strong); border-radius:12px;
}

/* Tables */
.stDataFrame, .stTable{ border:1px solid var(--border); border-radius:var(--radius); box-shadow:var(--shadow-sm); overflow:hidden; }
.dataframe td, .dataframe th{ font-size:.9rem; padding:.48rem .6rem !important; border-color:var(--border); }
.dataframe thead th{ background:#f8fafc; color:#var(--text); font-weight:700; }

/* Workflow */
.stepper{ display:flex; flex-direction:column; gap:.65rem; }
.step{ display:flex; gap:.75rem; align-items:flex-start; background:#fff;
  border:1px solid var(--border); border-radius:12px; padding:.75rem; box-shadow:var(--shadow-sm);
}
.step-badge{
  min-width:28px; height:28px; border-radius:50%; background:#14532d; color:#fff;
  display:flex; align-items:center; justify-content:center; font-weight:800;
}
.step h4{ margin:0; font-size:1rem; }
.step p{ margin:.1rem 0 0 0; color:#64748b; font-size:.9rem; }

/* Mini tables */
.logic-table{
  width:100%; border-collapse:collapse; margin:.35rem 0 .1rem;
  border:1px solid var(--border); border-radius:8px; overflow:hidden;
}
.logic-table th, .logic-table td{
  border:1px solid var(--border); padding:.45rem .55rem; font-size:.9rem;
}
.logic-table thead th{ background:#f8fafc; font-weight:700; }
</style>
""", unsafe_allow_html=True)

# =========================== DB & AUDIT =============================
DB_FILE = Path("voucher_db.sqlite")  # legacy; not used by Postgres but kept for compatibility

# PostgreSQL connection settings (configure via environment variables)
PG_HOST = "pg-cb495ce-adexsy94-643a.i.aivencloud.com"
PG_PORT = 14073
PG_DB   = "defaultdb"
PG_USER = "avnadmin"
PG_PASS = "AVNS_HW9bgleEeofjFFF21iW"



def connect():
    """
    Open a new PostgreSQL connection.
    Uses DictCursor so rows can be accessed like dicts if needed.
    """
    return psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASS,
        cursor_factory=DictCursor,
    )

_init_auth()

def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def log_action(action: str, entity: str, ref: str, details: str, user: Optional[str] = None) -> None:
    """Write an action to the audit log using the logged-in user when available."""
    u = user if user is not None else get_current_user()
    with closing(connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "INSERT INTO audit_log (ts, username, action, entity, ref, details) VALUES (%s, %s, %s, %s, %s, %s)",
            (now_iso(), u, action, entity, ref, details),
        )
        conn.commit()


def _audit(action: str, entity: str, ref: str, details: str, user: Optional[str] = None) -> None:
    """Small wrapper around log_action that swallows any internal audit errors."""
    try:
        log_action(action, entity, ref, details, user=user)
    except Exception:
        # Never let audit logging break the main flow
        pass


def get_current_user() -> str:
    """Return the currently logged-in username for audit trail."""
    try:
        u = st.session_state.get("user")
    except Exception:
        u = None
    return str(u or "local")


def get_get_current_user() -> str:
    """Backward-compatible alias for get_current_user()."""
    return get_current_user()


def init_voucher_db() -> None:
    """
    Initialize / migrate PostgreSQL schema for vouchers, invoices, audit_log, company_settings,
    company_documents and voucher_documents.
    """
    ddl_statements = [
        # vouchers
        """
        CREATE TABLE IF NOT EXISTS vouchers (
            id              SERIAL PRIMARY KEY,
            parent_id       INTEGER,
            version         INTEGER DEFAULT 1,
            voucher_number  TEXT,
            vendor          TEXT,
            requester       TEXT,
            invoice         TEXT,
            file_name       TEXT,
            file_data       BYTEA,
            last_modified   TIMESTAMPTZ
        )
        """,
        # voucher_lines
        """
        CREATE TABLE IF NOT EXISTS voucher_lines (
            id              SERIAL PRIMARY KEY,
            voucher_id      INTEGER,
            description     TEXT,
            amount          NUMERIC,
            expense_account TEXT,
            vat_percent     NUMERIC,
            wht_percent     NUMERIC,
            vat_value       NUMERIC,
            wht_value       NUMERIC,
            total           NUMERIC
        )
        """,
        # invoices
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id                      SERIAL PRIMARY KEY,
            parent_id               INTEGER,
            version                 INTEGER DEFAULT 1,
            invoice_number          TEXT,
            vendor_invoice_number   TEXT,
            vendor                  TEXT,
            summary                 TEXT,
            vatable_amount          NUMERIC DEFAULT 0.0,
            vat_rate                NUMERIC DEFAULT 0.0,
            wht_rate                NUMERIC DEFAULT 0.0,
            vat_amount              NUMERIC DEFAULT 0.0,
            wht_amount              NUMERIC DEFAULT 0.0,
            non_vatable_amount      NUMERIC DEFAULT 0.0,
            subtotal                NUMERIC DEFAULT 0.0,
            total_amount            NUMERIC DEFAULT 0.0,
            terms                   TEXT,
            last_modified           TIMESTAMPTZ,
            payable_account         TEXT,
            expense_asset_account   TEXT,
            currency                TEXT DEFAULT 'NGN',
            file_name               TEXT,
            file_data               BYTEA
        )
        """,
        # audit_log
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id          SERIAL PRIMARY KEY,
            ts          TIMESTAMPTZ,
            username    TEXT,
            action      TEXT,
            entity      TEXT,
            ref         TEXT,
            details     TEXT
        )
        """,
        # company_settings
        """
        CREATE TABLE IF NOT EXISTS company_settings (
            id                 INTEGER PRIMARY KEY,
            name               TEXT,
            rc                 TEXT,
            tin                TEXT,
            addr               TEXT,
            title              TEXT,
            authorizer_label   TEXT,
            approval_label     TEXT,
            authorizer_name    TEXT,
            approver_name      TEXT,
            department_default TEXT,
            company_doc_name   TEXT,
            company_doc_data   BYTEA
        )
        """,
        # company_documents
        """
        CREATE TABLE IF NOT EXISTS company_documents (
            id          SERIAL PRIMARY KEY,
            company_id  INTEGER,
            doc_name    TEXT,
            doc_data    BYTEA,
            uploaded_at TIMESTAMPTZ
        )
        """,
        # voucher_documents
        """
        CREATE TABLE IF NOT EXISTS voucher_documents (
            id          SERIAL PRIMARY KEY,
            voucher_id  INTEGER,
            doc_name    TEXT,
            doc_data    BYTEA,
            uploaded_at TIMESTAMPTZ
        )
        """,
    ]

    with closing(connect()) as conn, closing(conn.cursor()) as cur:
        for stmt in ddl_statements:
            cur.execute(stmt)

        # Indexes
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_invoice_number ON invoices(invoice_number)"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_vouchers_voucher_number ON vouchers(voucher_number)"
        )

        # Seed default company row if needed
        cur.execute("SELECT COUNT(*) FROM company_settings")
        row = cur.fetchone()
        count = row[0] if row else 0
        if (count or 0) == 0:
            cur.execute(
                """
                INSERT INTO company_settings 
                    (id, name, rc, tin, addr, title, authorizer_label, approval_label,
                     authorizer_name, approver_name, department_default,
                     company_doc_name, company_doc_data)
                VALUES (
                    1,
                    'Your Company Name',
                    'RC123456',
                    '1234567890',
                    'Company Address',
                    'EFT/CHEQUE/CASH REQUISITION',
                    'Authorizer',
                    'Approval',
                    'Authoriser Name',
                    'Approver Name',
                    'ACCOUNT',
                    NULL,
                    NULL
                )
                """
            )

        conn.commit()
def get_company_settings() -> Dict[str, Any]:
    with closing(connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            SELECT name, rc, tin, addr, title, 
                   authorizer_label, approval_label,
                   authorizer_name, approver_name,
                   department_default,
                   company_doc_name, company_doc_data
            FROM company_settings
            WHERE id = 1
            """
        )
        row = cur.fetchone()
        if not row:
            return {}
        keys = [
            "name",
            "rc",
            "tin",
            "addr",
            "title",
            "authorizer_label",
            "approval_label",
            "authorizer_name",
            "approver_name",
            "department_default",
            "company_doc_name",
            "company_doc_data",
        ]
        return dict(zip(keys, row))



def save_company_settings(settings: Dict[str, Any]) -> None:
    """
    Persist company header/footer/settings to the company_settings table.
    Expects keys:
      name, rc, tin, addr, title,
      authorizer_label, approval_label,
      authorizer_name, approver_name,
      department_default,
      company_doc_name, company_doc_data
    """
    with closing(connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            UPDATE company_settings
            SET name=%s, rc=%s, tin=%s, addr=%s, title=%s,
                authorizer_label=%s, approval_label=%s,
                authorizer_name=%s, approver_name=%s, department_default=%s,
                company_doc_name=%s, company_doc_data=%s
            WHERE id = 1
            """,
            (
                (settings.get("name") or "").strip(),
                (settings.get("rc") or "").strip(),
                (settings.get("tin") or "").strip(),
                (settings.get("addr") or "").strip(),
                (settings.get("title") or "").strip(),
                (settings.get("authorizer_label") or "").strip(),
                (settings.get("approval_label") or "").strip(),
                (settings.get("authorizer_name") or "").strip(),
                (settings.get("approver_name") or "").strip(),
                (settings.get("department_default") or "").strip(),
                settings.get("company_doc_name"),
                settings.get("company_doc_data"),
            ),
        )
        conn.commit()

    log_action(
        "UPDATE",
        "SETTINGS",
        "company_settings",
        "Company settings updated",
        user=get_get_current_user(),
    )


def safe_index(options: List[str], value: Optional[str], fallback: int = 0) -> int:
    """
    Return the index of value in options or fallback if not present.
    Extremely defensive to avoid UI crashes when options change.
    """
    try:
        if value in options:
            return options.index(value)
        return fallback
    except Exception:
        return fallback


def embed_file(name: str, data: Optional[bytes]) -> None:
    """
    Show an uploaded file inline in Streamlit.

    PDF  -> iframe preview
    JPG/PNG -> st.image
    """
    if not data or not name:
        return

    b64 = base64.b64encode(data).decode()

    if name.lower().endswith(".pdf"):
        # Inline PDF preview
        html = f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="600"></iframe>'
        st.markdown(html, unsafe_allow_html=True)
    else:
        # Assume image
        try:
            st.image(BytesIO(data), use_column_width=True)
        except Exception:
            # Fallback: simple download link if image rendering fails
            href = f"data:application/octet-stream;base64,{b64}"
            st.markdown(f'<a href="{href}" download="{name}">Download file</a>', unsafe_allow_html=True)


def excel_download_link_multi(
    df_invoices: pd.DataFrame,
    df_vouchers: pd.DataFrame,
    df_lines: pd.DataFrame,
    df_journal: pd.DataFrame,
    df_audit: pd.DataFrame,
    filename: str = "VoucherPro_Report",
) -> str:
    """
    Build a single Excel file with multiple sheets and return an HTML download button.
    Also normalises any timezone-aware datetime columns because Excel doesn't support tz.
    """

    def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
        try:
            # Work on a copy so we don't mutate caller DataFrames unexpectedly
            df = df.copy()
            # For pandas >=1.x you can select datetimetz
            dt_tz_cols = df.select_dtypes(include=["datetimetz"]).columns
            for col in dt_tz_cols:
                df[col] = df[col].dt.tz_convert(None)
        except Exception:
            # If anything goes wrong, just return the df as-is
            pass
        return df

    df_invoices = _strip_tz(df_invoices)
    df_vouchers = _strip_tz(df_vouchers)
    df_lines = _strip_tz(df_lines)
    df_journal = _strip_tz(df_journal)
    df_audit = _strip_tz(df_audit)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_invoices.to_excel(writer, index=False, sheet_name="Invoices")
        df_vouchers.to_excel(writer, index=False, sheet_name="Vouchers")
        df_lines.to_excel(writer, index=False, sheet_name="Line_Items")
        df_journal.to_excel(writer, index=False, sheet_name="General_Journal")
        df_audit.to_excel(writer, index=False, sheet_name="Audit_Trail")
    output.seek(0)
    b64 = base64.b64encode(output.read()).decode()
    stamp = datetime.now().strftime("%Y%m%d")
    return (
        f'<a href="data:application/octet-stream;base64,{b64}" '
        f'download="{filename}_{stamp}.xlsx">'
        f'<button>Download Excel (Invoices/Vouchers/Lines/Journal/Audit)</button></a>'
    )


def money(n: float, currency: str = "NGN") -> str:
    """
    Format money with a currency symbol.
    Defaults to NGN for backward compatibility.
    """
    try:
        cur = (currency or "NGN").upper()
        symbol_map = {
            "NGN": "₦",
            "USD": "$",
            "GBP": "£",
            "EUR": "€",
        }
        symbol = symbol_map.get(cur)
        if symbol:
            return f"{symbol}{n:,.2f}"
        # Fallback: prefix with currency code if unknown
        return f"{cur} {n:,.2f}"
    except Exception:
        # Very defensive: fall back to plain number with currency code
        return f"{currency or 'NGN'} {n:,.2f}"


# ============================= CRUD ================================
def get_latest_invoices_df() -> pd.DataFrame:
    """Return only the latest version of each invoice (by parent_id)."""
    with closing(connect()) as conn:
        return pd.read_sql_query(
            """
            SELECT i.* FROM invoices i
            JOIN (SELECT parent_id, MAX(version) mv FROM invoices GROUP BY parent_id) m
              ON i.parent_id = m.parent_id AND i.version = m.mv
            ORDER BY i.id DESC
            """,
            conn,
        )


def get_latest_vouchers_df() -> pd.DataFrame:
    """Return only the latest version of each voucher (by parent_id)."""
    with closing(connect()) as conn:
        return pd.read_sql_query(
            """
            SELECT v.* FROM vouchers v
            JOIN (SELECT parent_id, MAX(version) mv FROM vouchers GROUP BY parent_id) m
              ON v.parent_id = m.parent_id AND v.version = m.mv
            ORDER BY v.id DESC
            """,
            conn,
        )


def get_lines_for_voucher(vid: int) -> pd.DataFrame:
    """Return all line items for a given voucher id."""
    with closing(connect()) as conn:
        return pd.read_sql_query(
            "SELECT * FROM voucher_lines WHERE voucher_id=%s ORDER BY id",
            conn,
            params=(vid,),
        )


def create_invoice_record(data: Dict[str, Any]) -> Optional[str]:
    """
    Insert a brand new invoice (version 1).
    Returns None on success or an error message string on failure.

    data must contain:
      inv_num, vendor_inv, vendor, summary,
      vatable, vat_rate, wht_rate, vat_amt, wht_amt,
      non_vatable, subtotal, total, terms,
      payable_account, expense_asset_account,
      currency (optional, default 'NGN'),
      file_name (optional), file_bytes (optional)
    """
    try:
        with closing(connect()) as conn, closing(conn.cursor()) as cur:
            ts = now_iso()
            cur.execute(
                """
                INSERT INTO invoices 
                    (parent_id, version, invoice_number, vendor_invoice_number, vendor, summary,
                     vatable_amount, vat_rate, wht_rate, vat_amount, wht_amount,
                     non_vatable_amount, subtotal, total_amount, terms, last_modified,
                     payable_account, expense_asset_account, currency, file_name, file_data)
                VALUES (NULL, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    data["inv_num"],
                    data["vendor_inv"],
                    data["vendor"],
                    data["summary"],
                    data["vatable"],
                    data["vat_rate"],
                    data["wht_rate"],
                    data["vat_amt"],
                    data["wht_amt"],
                    data["non_vatable"],
                    data["subtotal"],
                    data["total"],
                    data["terms"],
                    ts,
                    data.get("payable_account"),
                    data.get("expense_asset_account"),
                    data.get("currency", "NGN"),
                    data.get("file_name"),
                    data.get("file_bytes"),
                ),
            )

            # Set parent_id to self for the first version
            cur.execute("SELECT currval(pg_get_serial_sequence('invoices','id'))")
            new_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE invoices SET parent_id = %s WHERE id = %s",
                (new_id, new_id),
            )
            conn.commit()

            log_action(
                "CREATE",
                "INVOICE",
                data["inv_num"],
                f"New invoice for {data['vendor']} gross={data['total']}",
                user=get_get_current_user(),
            )
            return None
    except psycopg2.IntegrityError:
        return "Invoice number already exists. Please use a unique invoice number."
    except Exception as e:
        return f"Error creating invoice: {e}"


def create_invoice_new_version(parent_id: int, data: Dict[str, Any]) -> Optional[str]:
    """
    Create a new version of an invoice, with smart handling for edits.
    If the invoice number is unchanged from the latest version, the latest row is updated in-place.
    If the invoice number has changed, a new version row is inserted.
    Returns None on success or an error message string on failure.
    """
    try:
        with closing(connect()) as conn, closing(conn.cursor()) as cur:
            # Get latest version for this parent
            cur.execute(
                "SELECT id, invoice_number, version FROM invoices WHERE parent_id=%s ORDER BY version DESC LIMIT 1",
                (parent_id,),
            )
            row = cur.fetchone()
            latest_id = row[0] if row else None
            latest_inv_num = row[1] if row else None
            latest_ver = row[2] if row else 1

            ts = now_iso()

            if latest_id is not None and data["inv_num"] == latest_inv_num:
                # Same invoice number -> just update the latest row
                cur.execute(
                    """
                    UPDATE invoices
                    SET vendor_invoice_number=%s,
                        vendor=%s,
                        summary=%s,
                        vatable_amount=%s,
                        vat_rate=%s,
                        wht_rate=%s,
                        vat_amount=%s,
                        wht_amount=%s,
                        non_vatable_amount=%s,
                        subtotal=%s,
                        total_amount=%s,
                        terms=%s,
                        last_modified=%s,
                        payable_account=%s,
                        expense_asset_account=%s,
                        currency=%s,
                        file_name=%s,
                        file_data=%s
                    WHERE id=%s
                    """,
                    (
                        data["vendor_inv"],
                        data["vendor"],
                        data["summary"],
                        data["vatable"],
                        data["vat_rate"],
                        data["wht_rate"],
                        data["vat_amt"],
                        data["wht_amt"],
                        data["non_vatable"],
                        data["subtotal"],
                        data["total"],
                        data["terms"],
                        ts,
                        data.get("payable_account"),
                        data.get("expense_asset_account"),
                        data.get("currency", "NGN"),
                        data.get("file_name"),
                        data.get("file_bytes"),
                        latest_id,
                    ),
                )
                conn.commit()
                log_action(
                    "UPDATE",
                    "INVOICE",
                    data["inv_num"],
                    "Edited latest version",
                    user=get_get_current_user(),
                )
            else:
                # Different invoice number (or no previous row) -> insert a new version
                new_ver = (latest_ver or 1) + 1
                cur.execute(
                    """
                    INSERT INTO invoices 
                        (parent_id, version, invoice_number, vendor_invoice_number, vendor, summary,
                         vatable_amount, vat_rate, wht_rate, vat_amount, wht_amount,
                         non_vatable_amount, subtotal, total_amount, terms, last_modified,
                         payable_account, expense_asset_account, currency, file_name, file_data)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        parent_id,
                        new_ver,
                        data["inv_num"],
                        data["vendor_inv"],
                        data["vendor"],
                        data["summary"],
                        data["vatable"],
                        data["vat_rate"],
                        data["wht_rate"],
                        data["vat_amt"],
                        data["wht_amt"],
                        data["non_vatable"],
                        data["subtotal"],
                        data["total"],
                        data["terms"],
                        ts,
                        data.get("payable_account"),
                        data.get("expense_asset_account"),
                        data.get("currency", "NGN"),
                        data.get("file_name"),
                        data.get("file_bytes"),
                    ),
                )
                conn.commit()
                log_action(
                    "UPDATE",
                    "INVOICE",
                    data["inv_num"],
                    f"New version v{new_ver}",
                    user=get_get_current_user(),
                )

            return None
    except psycopg2.IntegrityError:
        return "Invoice number already exists. Please use a unique invoice number."
    except Exception as e:
        return f"Error creating invoice version: {e}"


def create_voucher_with_lines(
    voucher_number: str,
    vendor: str,
    requester: str,
    invoice: Optional[str],
    lines: List[Dict[str, Any]],
    file_name: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    attachments: Optional[List[Tuple[str, bytes]]] = None,
) -> Optional[str]:
    """
    Create a brand-new voucher (version 1) with its line items.
    Optionally stores a primary file on the vouchers table and
    any number of extra documents in voucher_documents.
    """
    try:
        with closing(connect()) as conn, closing(conn.cursor()) as cur:
            ts = now_iso()

            # Main voucher row
            cur.execute(
                """
                INSERT INTO vouchers 
                    (parent_id, version, voucher_number, vendor, requester, invoice, file_name, file_data, last_modified)
                VALUES (NULL, 1, %s, %s, %s, %s, %s, %s, %s)
                """,
                (voucher_number, vendor, requester, invoice, file_name, file_bytes, ts),
            )
            cur.execute("SELECT currval(pg_get_serial_sequence('vouchers','id'))")
            vid = cur.fetchone()[0]

            # Set parent_id = self for first version
            cur.execute("UPDATE vouchers SET parent_id=%s WHERE id=%s", (vid, vid))

            # Extra voucher documents (multi-file support)
            if attachments:
                for doc_name, doc_bytes in attachments:
                    if not doc_name or not doc_bytes:
                        continue
                    cur.execute(
                        """
                        INSERT INTO voucher_documents (voucher_id, doc_name, doc_data, uploaded_at)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (vid, str(doc_name), doc_bytes, ts),
                    )

            # Line items
            for ln in lines:
                cur.execute(
                    """
                    INSERT INTO voucher_lines
                        (voucher_id, description, amount, expense_account, 
                         vat_percent, wht_percent, vat_value, wht_value, total)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        vid,
                        ln["description"],
                        ln["amount"],
                        ln["expense_account"],
                        ln["vat_percent"],
                        ln["wht_percent"],
                        ln["vat_value"],
                        ln["wht_value"],
                        ln["total"],
                    ),
                )

            conn.commit()

            log_action(
                "CREATE",
                "VOUCHER",
                voucher_number,
                f"New voucher linked_to={invoice or 'None'} lines={len(lines)}",
                user=get_get_current_user(),
            )
            return None
    except psycopg2.IntegrityError:
        return "Voucher number already exists. Please use a unique voucher number."
    except Exception as e:
        return f"Error creating voucher: {e}"


def create_voucher_new_version(
    parent_id: int,
    vendor: str,
    requester: str,
    invoice: Optional[str],
    lines: List[Dict[str, Any]],
    file_name: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    voucher_number: Optional[str] = None,
    attachments: Optional[List[Tuple[str, bytes]]] = None,
) -> Optional[str]:
    """
    Update an existing voucher or create a new version, while keeping voucher_number unique.

    Behaviour:
      * If voucher_number is not supplied, the latest voucher_number for this parent is reused.
      * If the effective voucher_number is the SAME as the latest row for this parent,
        that latest row is UPDATED in-place (lines are replaced).
      * If the effective voucher_number is DIFFERENT, a brand new version row is inserted.
    """
    try:
        with closing(connect()) as conn, closing(conn.cursor()) as cur:
            ts = now_iso()

            # Get the latest voucher row for this parent
            cur.execute(
                """
                SELECT id, voucher_number, version
                FROM vouchers
                WHERE parent_id = %s
                ORDER BY version DESC
                LIMIT 1
                """,
                (parent_id,),
            )
            row = cur.fetchone()
            latest_id = row[0] if row else None
            latest_vnum = row[1] if row else None
            latest_ver = row[2] if row else 1

            # Decide which voucher number we are actually using
            eff_vnum = (voucher_number or latest_vnum or "").strip() or None

            # ----------------- CASE 1: update latest row in-place -----------------
            # Same voucher number -> UPDATE instead of INSERT
            if latest_id is not None and eff_vnum == latest_vnum:
                # Update main voucher row
                cur.execute(
                    """
                    UPDATE vouchers
                    SET voucher_number = %s,
                        vendor         = %s,
                        requester      = %s,
                        invoice        = %s,
                        file_name      = %s,
                        file_data      = %s,
                        last_modified  = %s
                    WHERE id = %s
                    """,
                    (
                        eff_vnum,
                        vendor,
                        requester,
                        invoice,
                        file_name,
                        file_bytes,
                        ts,
                        latest_id,
                    ),
                )

                # Replace all line items for this voucher id
                cur.execute("DELETE FROM voucher_lines WHERE voucher_id = %s", (latest_id,))
                for ln in lines:
                    cur.execute(
                        """
                        INSERT INTO voucher_lines
                            (voucher_id, description, amount, expense_account,
                             vat_percent, wht_percent, vat_value, wht_value, total)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            latest_id,
                            ln["description"],
                            ln["amount"],
                            ln["expense_account"],
                            ln["vat_percent"],
                            ln["wht_percent"],
                            ln["vat_value"],
                            ln["wht_value"],
                            ln["total"],
                        ),
                    )

                # Any new attachments belong to this same voucher id
                if attachments:
                    for doc_name, doc_bytes in attachments:
                        if not doc_name or not doc_bytes:
                            continue
                        cur.execute(
                            """
                            INSERT INTO voucher_documents (voucher_id, doc_name, doc_data, uploaded_at)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (latest_id, str(doc_name), doc_bytes, ts),
                        )

                conn.commit()
                log_action(
                    "UPDATE",
                    "VOUCHER",
                    eff_vnum or str(parent_id),
                    "Edited latest voucher version",
                    user=get_get_current_user(),
                )
                return None

            # ----------------- CASE 2: number changed -> new version row -----------------
            new_ver = (latest_ver or 1) + 1 if latest_id is not None else 1
            cur.execute(
                """
                INSERT INTO vouchers
                    (parent_id, version, voucher_number, vendor, requester, invoice, file_name, file_data, last_modified)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (parent_id, new_ver, eff_vnum, vendor, requester, invoice, file_name, file_bytes, ts),
            )
            new_vid = cur.fetchone()[0]

            # Extra voucher documents (multi-file support) for this new version
            if attachments:
                for doc_name, doc_bytes in attachments:
                    if not doc_name or not doc_bytes:
                        continue
                    cur.execute(
                        """
                        INSERT INTO voucher_documents (voucher_id, doc_name, doc_data, uploaded_at)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (new_vid, str(doc_name), doc_bytes, ts),
                    )

            # Line items
            for ln in lines:
                cur.execute(
                    """
                    INSERT INTO voucher_lines
                        (voucher_id, description, amount, expense_account,
                         vat_percent, wht_percent, vat_value, wht_value, total)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        new_vid,
                        ln["description"],
                        ln["amount"],
                        ln["expense_account"],
                        ln["vat_percent"],
                        ln["wht_percent"],
                        ln["vat_value"],
                        ln["wht_value"],
                        ln["total"],
                    ),
                )

            conn.commit()
            log_action(
                "UPDATE",
                "VOUCHER",
                eff_vnum or str(parent_id),
                f"New voucher version v{new_ver}",
                user=get_get_current_user(),
            )
            return None
    except psycopg2.IntegrityError:
        # This kicks in if someone tries to create another voucher with the same voucher_number
        return "Voucher number already exists. Please use a unique voucher number."
    except Exception as e:
        return f"Error creating voucher version: {e}"
def _render_pdf_pages_to_pngs(file_bytes: bytes, max_pages: int = 4, dpi: int = 150) -> List[bytes]:
    """
    Try PyMuPDF first; if unavailable, try pdf2image.
    Returns a list of PNG bytes for up to max_pages pages.
    """
    images: List[bytes] = []

    if PYMUPDF_OK:
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            count = min(max_pages, len(doc))
            for i in range(count):
                page = doc.load_page(i)
                zoom = dpi / 72.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                images.append(pix.tobytes("png"))
            doc.close()
            return images
        except Exception:
            pass

    if PDF2IMAGE_OK:
        try:
            pil_imgs = convert_from_bytes(file_bytes, dpi=dpi, fmt="png")
            for img in pil_imgs[:max_pages]:
                buf = BytesIO()
                img.save(buf, format="PNG")
                images.append(buf.getvalue())
            return images
        except Exception:
            pass

    return images  # empty if neither works


def _normalize_to_pages(file_name: Optional[str], file_bytes: Optional[bytes]) -> List[bytes]:
    """
    Returns up to 4 PNG bytes representing pages 1..4.
    For JPG/PNG uploads: returns one image (page 1); remaining slots omitted.
    For PDFs: returns rendered pages (requires PyMuPDF or pdf2image).
    """
    if not file_bytes or not file_name:
        return []

    lower = file_name.lower()
    if lower.endswith(".pdf"):
        return _render_pdf_pages_to_pngs(file_bytes, max_pages=4, dpi=160)

    # image types
    if lower.endswith((".jpg", ".jpeg", ".png")):
        return [file_bytes]  # single "page"
    return []


def _scaled_image_flowable(img_bytes: bytes, max_w: float, max_h: float) -> Image:
    """
    Scale an image to fit within max_w x max_h (points), preserving aspect ratio.
    Adds a small safety margin to avoid rare 0.1pt rounding overflows that cause LayoutError.
    """
    safety = 2.0  # points
    max_w_eff = max(1.0, max_w - safety)
    max_h_eff = max(1.0, max_h - safety)

    img = Image(BytesIO(img_bytes))
    iw = float(getattr(img, "imageWidth", 0) or 0)
    ih = float(getattr(img, "imageHeight", 0) or 0)

    # Fallback if image metadata is odd
    if iw <= 0 or ih <= 0:
        img.hAlign = "CENTER"
        return img

    scale = min(max_w_eff / iw, max_h_eff / ih)
    if not (0 < scale < 10000):
        scale = 1.0

    dw = iw * scale
    dh = ih * scale

    # Final hard cap (prevents fractional overflow)
    if dw > max_w_eff:
        dw = max_w_eff
    if dh > max_h_eff:
        dh = max_h_eff

    img.drawWidth = dw
    img.drawHeight = dh
    img.hAlign = "CENTER"
    return img


def build_voucher_pdf_bytes(
    settings: Dict[str, str],
    voucher_meta: Dict[str, Any],
    line_rows: List[Dict[str, Any]],
    attachment: Optional[Tuple[Optional[str], Optional[bytes]]] = None,
) -> bytes:
    """
    Page 1: Voucher info (incl. signatures).
    Pages 2–5: At most 4 pages from the attached document, one per page, scaled to fit.
    Any extra source pages are ignored. Images/PDF pages are auto-fitted to printable area.
    """
    # Always enrich voucher_meta with latest vendor bank details from CRM vendors table (CRM DB)
    try:
        payee = voucher_meta.get("payable_to")
        if payee:
            vend_row = _crm_df(
                "SELECT website, contact_person, bank_name, bank_account, notes FROM vendors WHERE name = %s",
                (str(payee),),
            )
            if not vend_row.empty:
                row0 = vend_row.iloc[0]
                voucher_meta["website"] = (row0.get("website") or "") or ""
                voucher_meta["contact_person"] = (row0.get("contact_person") or "") or ""
                voucher_meta["bank"] = (row0.get("bank_name") or "") or ""
                voucher_meta["acc_no"] = (row0.get("bank_account") or "") or ""
                voucher_meta["vendor_notes"] = (row0.get("notes") or "") or ""
    except Exception:
        # Fail silently; PDF will just omit bank details if lookup fails
        pass

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
        Flowable,
        PageBreak,
        Image,
        KeepTogether,
    )
    from reportlab.lib import colors

    # ---------- helpers ----------
    def scale_widths(widths: List[float], content_w: float) -> List[float]:
        s = sum(widths) or 1.0
        return [w * content_w / s for w in widths]

    def _clean_currency_text(text: Any) -> str:
        """
        Replace Naira symbol with 'NGN ' for PDFs so it doesn't render as a box
        on systems without that glyph.
        """
        try:
            s = str(text if text is not None else "")
        except Exception:
            s = ""
        return s.replace("₦", "NGN ")

    def register_unicode_font() -> Dict[str, str]:
        import os as _os

        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "C:\\Windows\\Fonts\\DejaVuSans.ttf",
            "C:\\Windows\\Fonts\\DejaVuSans-Bold.ttf",
            "/Library/Fonts/DejaVuSans.ttf",
            "/Library/Fonts/DejaVuSans-Bold.ttf",
        ]
        reg = bold = None
        for p in candidates:
            if _os.path.exists(p) and p.lower().endswith("dejavusans.ttf"):
                reg = p
            if _os.path.exists(p) and p.lower().endswith("dejavusans-bold.ttf"):
                bold = p
        try:
            if reg:
                pdfmetrics.registerFont(TTFont("DejaVuSans", reg))
                if bold:
                    pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", bold))
                    return {"regular": "DejaVuSans", "bold": "DejaVuSans-Bold"}
                return {"regular": "DejaVuSans", "bold": "DejaVuSans"}
        except Exception:
            pass
        return {"regular": "Helvetica", "bold": "Helvetica-Bold"}

    # ---------- number to words ----------
    ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
    TEENS = [
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
    ]
    TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
    THOUSANDS = ["", "thousand", "million", "billion", "trillion", "quadrillion"]

    def chunk_to_words(n: int) -> str:
        parts: List[str] = []
        h, rem = divmod(n, 100)
        if h:
            parts.append(ONES[h] + " hundred")
            if rem:
                parts.append("and")
        if rem >= 20:
            t, o = divmod(rem, 10)
            parts.append(TENS[t])
            if o:
                parts.append(ONES[o])
        elif rem >= 10:
            parts.append(TEENS[rem - 10])
        elif rem > 0:
            parts.append(ONES[rem])
        return " ".join(parts) if parts else "zero"

    def int_to_words(n: int) -> str:
        if n == 0:
            return "zero"
        words: List[str] = []
        i = 0
        while n > 0 and i < len(THOUSANDS):
            n, chunk = divmod(n, 1000)
            if chunk:
                label = THOUSANDS[i]
                chunk_words = chunk_to_words(chunk)
                if label:
                    words.append(f"{chunk_words} {label}")
                else:
                    words.append(chunk_words)
            i += 1
        return " ".join(reversed(words))

    def amount_to_words(amount: float, currency: str = "NGN") -> str:
        """
        Convert a numeric amount to words for the given currency.
        Supports NGN, USD, GBP, EUR. Falls back to generic wording with the
        currency code if an unknown code is provided.
        """
        try:
            amt = round(float(amount) + 1e-9, 2)
        except Exception:
            amt = 0.0
        major = int(amt)
        minor = int(round((amt - major) * 100))

        cur = (currency or "NGN").upper()
        if cur == "NGN":
            major_name, minor_name = "naira", "kobo"
        elif cur == "USD":
            major_name, minor_name = "dollars", "cents"
        elif cur == "GBP":
            major_name, minor_name = "pounds", "pence"
        elif cur == "EUR":
            major_name, minor_name = "euros", "cents"
        else:
            major_name, minor_name = cur.lower(), "cents"

        words = int_to_words(major)
        words = words[0].upper() + words[1:] if words else "Zero"

        if minor > 0:
            minor_words = int_to_words(minor)
            minor_words = minor_words[0].upper() + minor_words[1:]
            return f"{words} {major_name}, {minor_words} {minor_name} only."
        else:
            return f"{words} {major_name} only."

    def amount_to_words_naira(amount: float) -> str:
        """Backward-compatible wrapper for existing naira-only behaviour."""
        return amount_to_words(amount, "NGN")

    # ---------- fonts & layout ----------
    font_names = register_unicode_font()
    MM = 72.0 / 25.4
    left_margin = 10 * MM
    right_margin = 10 * MM
    top_margin = 4 * MM
    bottom_margin = 4 * MM

    page_size = landscape(A4)
    PAGE_W, PAGE_H = page_size
    content_w = PAGE_W - left_margin - right_margin
    content_h = PAGE_H - top_margin - bottom_margin

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        leftMargin=left_margin,
        rightMargin=right_margin,
        topMargin=top_margin,
        bottomMargin=bottom_margin,
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="HeadBold", fontName=font_names["bold"], fontSize=12, leading=14))
    styles.add(ParagraphStyle(name="HeadBig", fontName=font_names["bold"], fontSize=13, leading=16, alignment=1))
    styles.add(ParagraphStyle(name="Small", fontName=font_names["regular"], fontSize=9, leading=11))
    styles.add(ParagraphStyle(name="SmallBold", fontName=font_names["bold"], fontSize=9, leading=11))

    story: List[Flowable] = []

    # ---------- Header block ----------
    addr_lines = (settings.get("addr") or "").splitlines()
    right_block = "<br/>".join(filter(None, [settings.get("rc", ""), settings.get("tin", ""), *addr_lines]))
    t0_colw = scale_widths([0.58, 0.42], content_w)
    top_table = Table(
        [
            [
                Paragraph(f"<b>{settings.get('name','')}</b>", styles["HeadBig"]),
                Paragraph(right_block, styles["Small"]),
            ]
        ],
        colWidths=t0_colw,
    )
    top_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(top_table)
    story.append(Spacer(1, 4))

    # Title + Voucher No
    story.append(Paragraph(settings.get("title", "EFT/CHEQUE/CASH REQUISITION"), styles["HeadBold"]))
    voucher_no = voucher_meta.get("voucher_number", "")
    if voucher_no:
        story.append(Paragraph(f"VOUCHER NO: <b>{voucher_no}</b>", styles["Small"]))
    story.append(Spacer(1, 4))

    # ---------- Table 1: Date/Amount + Requested/Dept ----------
    t1_colw = scale_widths([90, 320, 100, 218], content_w)
    block1 = Table(
        [
            ["DATE", voucher_meta.get("date_str", ""), "AMOUNT", _clean_currency_text(voucher_meta.get("amount_str", ""))],
            ["REQUESTED. BY", voucher_meta.get("requested_by", ""), "DEPARTMENT", voucher_meta.get("department", "")],
        ],
        colWidths=t1_colw,
    )
    block1.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.25, colors.black),
                ("FONTNAME", (0, 0), (-1, -1), font_names["regular"]),
                ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke),
                ("ALIGN", (1, 0), (1, 0), "LEFT"),
                ("ALIGN", (3, 0), (3, 0), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    story.append(block1)
    story.append(Spacer(1, 4))

    # ---------- Table 2: Bank/payee strip ----------
    t2_colw = scale_widths([95, 220, 95, 200, 120, 98], content_w)
    block2 = Table(
        [
            [
                "PAYABLE TO",
                voucher_meta.get("payable_to", ""),
                "BANK NAME",
                voucher_meta.get("bank", ""),
                "ACCOUNT NUMBER",
                voucher_meta.get("acc_no", ""),
            ]
        ],
        colWidths=t2_colw,
    )
    block2.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.25, colors.black),
                ("FONTNAME", (0, 0), (-1, -1), font_names["regular"]),
                ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(block2)
    story.append(Spacer(1, 6))

    # ---------- Table 3: Line items ----------
    # Columns: INV NO. (wider), DETAILS (wrapped), AMOUNT, VAT AMT, WHT AMT, PAYABLE AMT
    t3_colw = scale_widths([140, 300, 70, 70, 70, 58], content_w)

    header = ["INV NO.", "DETAILS", "AMOUNT", "VAT AMT", "WHT AMT", "PAYABLE AMT"]
    rows: List[List[Any]] = [header]

    sum_amount = 0.0
    sum_vat = 0.0
    sum_wht = 0.0
    sum_payable = 0.0

    for r in line_rows:
        amt = float(r.get("_amount", 0.0) or 0.0)
        vat = float(r.get("_vat", 0.0) or 0.0)
        wht = float(r.get("_wht", 0.0) or 0.0)
        payable = (amt + vat) - wht

        sum_amount += amt
        sum_vat += vat
        sum_wht += wht
        sum_payable += payable

        inv_no_text = r.get("inv_no", "")
        inv_no_para = Paragraph(str(inv_no_text), styles["Small"])

        details_text = r.get("details", "")
        details_para = Paragraph(str(details_text), styles["Small"])

        rows.append(
            [
                inv_no_para,
                details_para,
                _clean_currency_text(r.get("amount_str", f"{amt:,.2f}")),
                _clean_currency_text(r.get("vat_str", f"{vat:,.2f}")),
                _clean_currency_text(r.get("wht_str", f"{wht:,.2f}" if wht else "-")),
                _clean_currency_text(r.get("payable_str", f"{payable:,.2f}")),
            ]
        )

    # Pad up to 10 rows max (excluding header), so we don't get too many blank lines.
    while len(rows) < 10:
        rows.append(["", "", "", "", "", ""])

    rows.append(
        [
            "",
            "TOTALS",
            _clean_currency_text(f"{sum_amount:,.2f}"),
            _clean_currency_text(f"{sum_vat:,.2f}"),
            _clean_currency_text(f"{sum_wht:,.2f}" if sum_wht else "-"),
            _clean_currency_text(f"{sum_payable:,.2f}"),
        ]
    )

    line_table = Table(rows, colWidths=t3_colw, repeatRows=1)
    line_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.25, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), font_names["bold"]),
                ("FONTNAME", (0, 1), (-1, -1), font_names["regular"]),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                ("ALIGN", (2, 1), (5, -1), "RIGHT"),
            ]
        )
    )
    story.append(line_table)
    story.append(Spacer(1, 4))

    # Amount in words (respect voucher currency)
    voucher_currency = (voucher_meta.get("currency") or "NGN") if isinstance(voucher_meta, dict) else "NGN"
    amt_words = amount_to_words(sum_payable, voucher_currency)
    story.append(Paragraph(f"<b>Payable amount (in words):</b> {amt_words}", styles["Small"]))
    story.append(Spacer(1, 6))

    # ---------- Signatures (page 1) ----------
    sig_headers = ["Activity", "Name", "Date", "Signature"]
    requested_by = voucher_meta.get("requested_by", "")
    authorised_name = voucher_meta.get("authorizer", settings.get("authorizer_name", ""))
    approved_name = voucher_meta.get("approver", settings.get("approver_name", ""))
    date_val = voucher_meta.get("date_str", "")

    t4_colw = scale_widths([120, 360, 120, 128], content_w)
    sig_rows = [
        sig_headers,
        ["Requested by", requested_by, date_val, ""],
        ["Authorised by", authorised_name, date_val, ""],
        ["Approved by", approved_name, date_val, ""],
    ]
    sig_table = Table(sig_rows, colWidths=t4_colw)
    sig_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.25, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), font_names["bold"]),
                ("FONTNAME", (0, 1), (-1, -1), font_names["regular"]),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                # Give plenty of padding in the signature column so there's room to sign
                ("TOPPADDING", (3, 1), (3, -1), 18),
                ("BOTTOMPADDING", (3, 1), (3, -1), 18),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, 0), "LEFT"),
            ]
        )
    )
    story.append(KeepTogether(sig_table))

    # ---------- Attachment pages: ONE image per page (max 4) ----------
    page_imgs: List[bytes] = []

    # Support either a single (name, bytes) tuple or a list of such tuples.
    if attachment:
        try:
            # If it's a list/tuple of many attachments
            if isinstance(attachment, (list, tuple)) and attachment and isinstance(attachment[0], (list, tuple)):
                for name_bytes in attachment:
                    if not name_bytes or len(name_bytes) < 2:
                        continue
                    att_name, att_bytes = name_bytes[0], name_bytes[1]
                    if not att_name or not att_bytes:
                        continue
                    # Extend pages but cap at 4 pages total across all attachments
                    new_pages = _normalize_to_pages(att_name, att_bytes)
                    for p in new_pages:
                        page_imgs.append(p)
                        if len(page_imgs) >= 4:
                            break
                    if len(page_imgs) >= 4:
                        break
            else:
                # Single (name, bytes) tuple
                att_name, att_bytes = attachment  # type: ignore[misc]
                if att_name and att_bytes:
                    page_imgs = _normalize_to_pages(att_name, att_bytes)[:4]
        except Exception:
            # Fail safe: ignore bad attachment structures
            page_imgs = []

    if page_imgs:
        avail_w = content_w
        # use a fixed safe headroom (points) to avoid rounding causing LayoutError
        avail_h = content_h - 12

        for img_bytes in page_imgs:
            story.append(PageBreak())
            # use the robust scaler to ensure the image fits the frame
            img = _scaled_image_flowable(img_bytes, max_w=avail_w, max_h=avail_h)
            story.append(img)

    doc.build(story)
    return buffer.getvalue()


# ============================ APP UI =================================

_require_login()

# -------------------------- Sidebar Navigation --------------------------
if "app_section" not in st.session_state:
    st.session_state["app_section"] = "VoucherPro"

with st.sidebar:
    st.markdown("## Voucher & CRM")
    st.markdown("Manage vouchers and CRM records from one place.")
    app_section = st.radio(
        "Go to:",
        ["VoucherPro", "CRM / Ops Master"],
        index=0 if st.session_state["app_section"] == "VoucherPro" else 1,
        key="app_section"
    )
    st.markdown("---")
    # show logged in user and logout
    current_user = st.session_state.get("user", None)
    if current_user:
        st.markdown(f"**User:** {current_user}")
        if st.button("Log out", key="btn_logout"):
            for key in ["user", "app_section", "last_message_type", "last_message_text", "draft_lines"]:
                st.session_state.pop(key, None)
            rerun()
    else:
        st.caption("Not logged in")

    st.markdown("**Shortcuts**")
    st.caption("• VoucherPro: Create & approve vouchers")
    st.caption("• CRM: Jobs, Vendors, Customers, Staff, Accounts")
    st.markdown("---")

if app_section == "VoucherPro":
    init_voucher_db()
    st.markdown("<h1>VoucherPro</h1>", unsafe_allow_html=True)
    st.markdown("<p class=''subtitle'>Paid = Amount + VAT • Remaining = Gross – Paid • Full Journal & Audit</p>", unsafe_allow_html=True)
    
    if "draft_lines" not in st.session_state:
        st.session_state.draft_lines = []
    
    tabs = st.tabs([
        "🧾 Create Voucher",
        "📑 Vouchers",
        "📄 Invoices",
        "📊 Reports (Complete)",
        "📜 Audit",
        "⚙️ Company Settings",
        "🧭 Workflow"
    ])
    
    # Populate options dynamically from CRM
    vendors = get_crm_vendor_options()
    requesters = get_crm_requester_options()
    # Line accounts now come directly from CRM chart of accounts
    posting_payables = get_crm_payable_accounts()
    expense_or_asset_options = get_crm_expense_or_asset_accounts()
    line_accounts = expense_or_asset_options
    
    vdf = get_latest_vouchers_df()
    idf = get_latest_invoices_df()
    company_settings = get_company_settings()
    
    # ============================ TAB: CREATE VOUCHER ====================
    with tabs[0]:
        st.markdown("<div class=''card'>", unsafe_allow_html=True)
        st.markdown("<div class=''card-header'>🧾 Create New Voucher</div>", unsafe_allow_html=True)
        top1, top2, top3, top4 = st.columns([2,2,3,3])
    
        voucher_number = top1.text_input("Voucher Number (must be unique)", placeholder="VCH-2025-0001", key="voucher_number_input")
        vendor = top2.selectbox("Vendor", vendors, key="nv")
        requester = top3.selectbox("Requester", requesters, key="nr")
    
        # Filter invoices by selected vendor for the "Link Invoice" dropdown
        if not idf.empty and vendor and vendor != "-- Add vendors in CRM first --":
            vendor_invoices_df = idf[idf["vendor"] == vendor]
        else:
            vendor_invoices_df = idf
    
        invoice_numbers_for_vendor = (
            vendor_invoices_df["invoice_number"].tolist()
            if not vendor_invoices_df.empty
            else []
        )
    
        invoice_opts = ["-- Create New Invoice --"] + invoice_numbers_for_vendor
        sel_inv = top4.selectbox("Link Invoice", invoice_opts, key="si")
    
        gross_invoice = 0.0
        paid_so_far = 0.0
        remaining_balance = 0.0
    
        invoice_linked = sel_inv != "-- Create New Invoice --"
    
        if invoice_linked:
            selected_invoice_row = idf[idf["invoice_number"] == sel_inv].iloc[0]
            inv_currency = (selected_invoice_row.get("currency") or "NGN") if hasattr(selected_invoice_row, "get") else str(selected_invoice_row["currency"]) if "currency" in selected_invoice_row.index else "NGN"
            gross_invoice = float(selected_invoice_row["total_amount"])
            linked = vdf[vdf["invoice"] == sel_inv]
            for _, r in linked.iterrows():
                lines_df = get_lines_for_voucher(int(r["id"]))
                paid_so_far += float(
                    (lines_df["amount"] + lines_df["vat_value"] + lines_df["wht_value"]).sum()
                )
            remaining_balance = max(0.0, gross_invoice - paid_so_far)
            st.markdown(
                f"""
                <div class=''info-box'>
                <strong>Gross Invoice:</strong> {money(gross_invoice, inv_currency)}<br>
                <strong>Paid So Far:</strong> {money(paid_so_far, inv_currency)}<br>
                <strong>Remaining Balance:</strong> {money(remaining_balance, inv_currency)}
                </div>
                """,
                unsafe_allow_html=True,
            )
            if remaining_balance <= 0:
                st.markdown("<div class=''error-box'>INVOICE FULLY PAID. No more vouchers allowed.</div>", unsafe_allow_html=True)
                st.markdown("</div>", unsafe_allow_html=True)
    

        # ----- Create Invoice form (only when not linking) -----
        if not invoice_linked:
            st.markdown("<div class=''card'>", unsafe_allow_html=True)
            st.markdown("<div class=''card-header'>🧮 Create Professional Invoice</div>", unsafe_allow_html=True)
            with st.container():
                c1, c2, c3 = st.columns([2, 2, 2])
                inv_num_default = f"{vendor}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                inv_num = c1.text_input(
                    "Invoice Number (must be unique)",
                    value=inv_num_default,
                    key="inv_num_input",
                    help="You can overwrite this.",
                )
                vendor_inv = c2.text_input(
                    "Vendor Invoice Number",
                    placeholder="SUP-INV-0001",
                    key="vendor_inv_input",
                )
                summary = c3.text_input(
                    "Invoice Summary",
                    placeholder="Office Supplies / Services",
                    key="summary_input",
                )

                v1, v2, v3 = st.columns(3)
                vatable = v1.number_input(
                    "Vatable Amount",
                    min_value=0.00,
                    value=0.00,
                    format="%.2f",
                    key="vatable",
                )
                vat_rate = v2.number_input(
                    "VAT Rate %",
                    value=7.5,
                    key="vat_rate",
                )
                wht_rate = v3.number_input(
                    "WHT Rate %",
                    value=5.0,
                    key="wht_rate",
                    help="Used for withholding tax posting",
                )

                v4, v5 = st.columns(2)
                non_vatable = v4.number_input(
                    "Non-vatable Amount",
                    min_value=0.00,
                    value=0.00,
                    format="%.2f",
                    key="non_vatable",
                )

                # Currency
                currency_options = ["NGN", "USD", "GBP", "EUR"]
                currency = v5.selectbox(
                    "Currency",
                    currency_options,
                    index=0,
                    key="currency_input",
                )

                vat_amt = vatable * vat_rate / 100.0
                wht_amt = vatable * wht_rate / 100.0
                subtotal = vatable + non_vatable + vat_amt - wht_amt
                total = vatable + non_vatable + vat_amt

                st.markdown(
                    f"<div class=''calc-line'>VAT: {money(vat_amt, currency)} • WHT: {money(wht_amt, currency)} • PAYABLE: {money(subtotal, currency)} • GROSS: {money(total, currency)}</div>",
                    unsafe_allow_html=True,
                )

                # Accounts from CRM CoA
                payable_opts = posting_payables
                exp_asset_opts = expense_or_asset_options

                a1, a2 = st.columns(2)
                payable_account = a1.selectbox(
                    "Payable / Liability Account (CR)",
                    payable_opts,
                    index=safe_index(payable_opts, "Accounts Payable"),
                )
                expense_asset_account = a2.selectbox(
                    "Expense or Asset Account (DR)",
                    exp_asset_opts,
                )

                inv_upload = st.file_uploader(
                    "Attach Invoice Document (PDF/JPG/PNG)",
                    type=["pdf", "jpg", "jpeg", "png"],
                    key="inv_upload_new",
                )
                inv_file_name: Optional[str] = None
                inv_file_bytes: Optional[bytes] = None
                if inv_upload is not None:
                    inv_file_bytes = inv_upload.read()
                    inv_file_name = inv_upload.name
                    embed_file(inv_file_name, inv_file_bytes)
                    inv_upload.seek(0)

                terms = st.text_area("Terms", value="", placeholder="Payment due in 30 days", key="terms_input")

                if st.button("Create Invoice"):
                    if not inv_num:
                        st.error("Invoice number is required.")
                    else:
                        data = {
                            "inv_num": inv_num,
                            "vendor_inv": vendor_inv,
                            "vendor": vendor,
                            "summary": summary,
                            "vatable": float(vatable),
                            "vat_rate": float(vat_rate),
                            "wht_rate": float(wht_rate),
                            "vat_amt": float(vat_amt),
                            "wht_amt": float(wht_amt),
                            "non_vatable": float(non_vatable),
                            "subtotal": float(subtotal),
                            "total": float(total),
                            "terms": terms,
                            "payable_account": payable_account,
                            "expense_asset_account": expense_asset_account,
                            "currency": currency,
                            "file_name": inv_file_name,
                            "file_bytes": inv_file_bytes,
                        }
                        err = create_invoice_record(data)
                        if err:
                            st.error(err)
                        else:
                            st.success(f"Invoice {inv_num} created!")
                            st.balloons()
                            # Reset invoice input widgets so the form returns to a clean default state
                            for k in [
                                "inv_num_input", "vendor_inv_input", "summary_input", "terms_input",
                                "vatable", "vat_rate", "wht_rate", "non_vatable",
                                "currency_input",
                                "inv_upload_new", "inv_upload", "nv",
                            ]:
                                if k in st.session_state:
                                    st.session_state.pop(k, None)
                            rerun()

        # ----- Line Items -----
        st.markdown("<div class=''card'>", unsafe_allow_html=True)
        st.markdown(
            "<div class=''card-header'>🧰 Line Items</div>",
            unsafe_allow_html=True,
        )

        total_payable_lines = 0.0
        total_gross_this_voucher = 0.0

        line_account_options = line_accounts

        for i, line in enumerate(st.session_state.draft_lines):
            with st.expander(
                f"Line {i+1} - {line.get('description', '') or 'New'}",
                expanded=True,
            ):
                if invoice_linked:
                    c1, c2, c3, c4 = st.columns([4, 2, 2, 2])
                    desc = c1.text_input(
                        "Description",
                        line.get("description", ""),
                        key=f"d{i}",
                    )
                    amt = c2.number_input(
                        "Amount",
                        value=float(line.get("amount", 0.00)),
                        min_value=0.00,
                        format="%.2f",
                        key=f"a{i}",
                    )
                    vat_p = c3.number_input(
                        "VAT %",
                        value=float(line.get("vat_percent", 7.5)),
                        key=f"v{i}",
                    )
                    wht_p = c4.number_input(
                        "WHT %",
                        value=float(line.get("wht_percent", 0.0)),
                        key=f"w{i}",
                    )
                    # Account comes from linked invoice; keep as stored value
                    acct_value = line.get("expense_account", "Expense")
                else:
                    c1, c2, c3, c4, c5 = st.columns([3, 2, 3, 2, 2])
                    desc = c1.text_input(
                        "Description",
                        line.get("description", ""),
                        key=f"d{i}",
                    )
                    amt = c2.number_input(
                        "Amount",
                        value=float(line.get("amount", 0.00)),
                        min_value=0.00,
                        format="%.2f",
                        key=f"a{i}",
                    )
                    acct_value = c3.selectbox(
                        "Expense or Asset Account",
                        line_account_options,
                        index=safe_index(
                            line_account_options,
                            line.get("expense_account", None),
                        ),
                        key=f"c{i}",
                    )
                    vat_p = c4.number_input(
                        "VAT %",
                        value=float(line.get("vat_percent", 7.5)),
                        key=f"v{i}",
                    )
                    wht_p = c5.number_input(
                        "WHT %",
                        value=float(line.get("wht_percent", 0.0)),
                        key=f"w{i}",
                    )

                vat_v = amt * vat_p / 100
                wht_v = amt * wht_p / 100
                line_payable = amt + vat_v - wht_v
                line_gross = amt + vat_v + wht_v

                total_payable_lines += float(line_payable)
                total_gross_this_voucher += float(line_gross)

                st.markdown(
                    f"<div class=''calc-line'>VAT: {money(vat_v, inv_currency if invoice_linked else currency)} • WHT: {money(wht_v, inv_currency if invoice_linked else currency)} • PAYABLE: {money(line_payable, inv_currency if invoice_linked else currency)}</div>",
                    unsafe_allow_html=True,
                )

                st.session_state.draft_lines[i].update(
                    {
                        "description": desc,
                        "amount": float(amt),
                        "expense_account": acct_value,
                        "vat_percent": float(vat_p),
                        "wht_percent": float(wht_p),
                        "vat_value": float(vat_v),
                        "wht_value": float(wht_v),
                        "total": float(line_payable),
                    }
                )

                if st.button("Delete Line", key=f"del{i}"):
                    st.session_state.draft_lines.pop(i)
                    rerun()

        if invoice_linked:
            st.markdown(
                f"<div class=''micro-note'>GROSS THIS VOUCHER: {money(total_gross_this_voucher, inv_currency if invoice_linked else currency)}</div>",
                unsafe_allow_html=True,
            )
        st.markdown(
            f"<div class=''micro-note'>VOUCHER PAYABLE: {money(total_payable_lines, inv_currency if invoice_linked else currency)}</div>",
            unsafe_allow_html=True,
        )
        st.markdown("</div>", unsafe_allow_html=True)

        # ----- Validation + Attach + Save -----
        can_save = True
        if invoice_linked and remaining_balance > 0:
            if paid_so_far + total_gross_this_voucher > gross_invoice:
                excess = (paid_so_far + total_gross_this_voucher) - gross_invoice
                st.markdown(f"<div class=''error-box'>EXCEEDS BALANCE BY {money(excess)}! Reduce lines.</div>", unsafe_allow_html=True)
                can_save = False
    
        up1, up2 = st.columns([2,3])
        # Multiple document upload for vouchers
        multi_files = up1.file_uploader(
            "Attach Document(s) (PDF/JPG/PNG) — you can select multiple files",
            type=["pdf", "jpg", "jpeg", "png"],
            accept_multiple_files=True,
        )

        primary_name: Optional[str] = None
        primary_bytes: Optional[bytes] = None
        attachments: List[Tuple[str, bytes]] = []

        if multi_files:
            for idx, f in enumerate(multi_files):
                try:
                    fb = f.read()
                except Exception:
                    fb = None
                if not fb:
                    continue

                # First file becomes the main file on vouchers table
                if primary_name is None and primary_bytes is None:
                    primary_name = f.name
                    primary_bytes = fb

                attachments.append((f.name, fb))

                # Preview only the first file to avoid clutter
                if idx == 0:
                    embed_file(f.name, fb)

                try:
                    f.seek(0)
                except Exception:
                    pass

        act1, act2 = st.columns([1,4])
        if act1.button("Add Line Item"):
            st.session_state.draft_lines.append({
                "description": "", "amount": 0.00, "expense_account": "Expense",
                "vat_percent": 7.5, "wht_percent": 0.0, "vat_value": 0.0, "wht_value": 0.0, "total": 0.00
            })
            rerun()

        if act2.button("Save Voucher", disabled=not can_save):
            if not voucher_number.strip():
                st.error("Voucher Number is required.")
            else:
                inv_link = sel_inv if invoice_linked else None
                err = create_voucher_with_lines(
                    voucher_number.strip(),
                    vendor,
                    requester,
                    inv_link,
                    st.session_state.draft_lines,
                    primary_name if primary_bytes else None,
                    primary_bytes if primary_bytes else None,
                    attachments=attachments if attachments else None,
                )
                if err:
                    st.error(err)
                else:
                    st.success(f"Voucher {voucher_number} saved successfully!")
                    # Reset voucher input widgets so the form returns to a clean default state
                    st.session_state.draft_lines = []
                    for k in [
                        "nv", "nr", "invoice_linked", "sel_inv",
                        "voucher_number_input", "uploaded_bytes", "uploaded_name",
                    ]:
                        if k in st.session_state:
                            st.session_state.pop(k, None)
                    rerun()


# ============================ TAB: VOUCHERS (EDIT) ====================
    with tabs[1]:
        st.markdown("<div class=''card'><div class=''card-header'>📑 All Vouchers</div>", unsafe_allow_html=True)
        current_vdf = get_latest_vouchers_df()
        current_idf = get_latest_invoices_df()
        invoice_numbers = ["-- None --"] + current_idf["invoice_number"].tolist()
    
        for _, v in current_vdf.iterrows():
            lines_df = get_lines_for_voucher(int(v["id"]))
            voucher_currency = "NGN"
            try:
                if isinstance(v.get("invoice"), str) and v["invoice"] in current_idf["invoice_number"].values:
                    linked_inv_row = current_idf[current_idf["invoice_number"] == v["invoice"]].iloc[0]
                    voucher_currency = linked_inv_row.get("currency", "NGN")
            except Exception:
                voucher_currency = "NGN"
            total_payable_v = float((lines_df["amount"] + lines_df["vat_value"] - lines_df["wht_value"]).sum())
            # Resolve a display voucher number: prefer current row, else root parent row
            display_vnum = v.get("voucher_number")
            if not display_vnum:
                try:
                    with closing(connect()) as conn, closing(conn.cursor()) as cur2:
                        cur2.execute("SELECT voucher_number FROM vouchers WHERE id=%s", (int(v["parent_id"]),))
                        row_vn = cur2.fetchone()
                        if row_vn and row_vn[0]:
                            display_vnum = row_vn[0]
                except Exception:
                    display_vnum = None
            if not display_vnum:
                display_vnum = str(v["id"])

            with st.expander(f"VOUCHER {display_vnum} • {v['vendor']} • PAYABLE {money(total_payable_v, voucher_currency)}", expanded=False):
                col1, col2, col3, col4 = st.columns([2,2,2,3])
                vnum = col1.text_input("Voucher Number", value=v.get("voucher_number") or "", key=f"vnum_{v['id']}", disabled=True)
                new_vendor = col2.selectbox("Vendor", vendors, index=safe_index(vendors, v["vendor"]), key=f"ev_{v['id']}")
                new_requester = col3.selectbox("Requester", requesters, index=safe_index(requesters, v["requester"]), key=f"er_{v['id']}")
                current_inv = v["invoice"] if v["invoice"] in invoice_numbers else "-- None --"
                new_invoice = col4.selectbox("Invoice", invoice_numbers, index=safe_index(invoice_numbers, current_inv), key=f"ei_{v['id']}")
    
                invoice_linked_edit = new_invoice != "-- None --"
    
                st.markdown("### Line Items")
                updated_lines: List[Dict[str, Any]] = []
                edit_total = 0.0
    
                for idx, ln in lines_df.iterrows():
                    if invoice_linked_edit:
                        lc = st.columns([4, 2, 2, 2])
                        desc = lc[0].text_input("Desc", ln["description"], key=f"ldesc_{v['id']}_{idx}")
                        amt = lc[1].number_input("Amt", value=float(ln["amount"]), min_value=0.00, format="%.2f", key=f"lamt_{v['id']}_{idx}")
                        vat = lc[2].number_input("VAT%", value=float(ln["vat_percent"]), key=f"lvat_{v['id']}_{idx}")
                        wht = lc[3].number_input("WHT%", value=float(ln["wht_percent"]), key=f"lwht_{v['id']}_{idx}")
                        acct_val = ln["expense_account"]
                    else:
                        lc = st.columns([3, 2, 3, 2, 2])
                        desc = lc[0].text_input("Desc", ln["description"], key=f"ldesc_{v['id']}_{idx}")
                        amt = lc[1].number_input("Amt", value=float(ln["amount"]), min_value=0.00, format="%.2f", key=f"lamt_{v['id']}_{idx}")
                        acct_val = lc[2].selectbox("Expense or Asset Account", line_accounts, index=safe_index(line_accounts, ln["expense_account"]), key=f"lacct_{v['id']}_{idx}")
                        vat = lc[3].number_input("VAT%", value=float(ln["vat_percent"]), key=f"lvat_{v['id']}_{idx}")
                        wht = lc[4].number_input("WHT%", value=float(ln["wht_percent"]), key=f"lwht_{v['id']}_{idx}")
    
                    vat_val = amt * vat / 100
                    wht_val = amt * wht / 100
                    total_val = amt + vat_val - wht_val
                    edit_total += float(total_val)
    
                    st.markdown(f"<div class=''calc-line'>VAT: {money(vat_val, voucher_currency)} • WHT: {money(wht_val, voucher_currency)} • PAYABLE: {money(total_val, voucher_currency)}</div>", unsafe_allow_html=True)
    
                    updated_lines.append({
                        "description": desc, "amount": float(amt), "expense_account": acct_val,
                        "vat_percent": float(vat), "wht_percent": float(wht),
                        "vat_value": float(vat_val), "wht_value": float(wht_val), "total": float(total_val)
                    })
    
                st.markdown(f"<div class=''micro-note'>EDITED PAYABLE: {money(edit_total, voucher_currency)}</div>", unsafe_allow_html=True)

                # Show main attached file (legacy single-file storage)
                if v["file_data"]:
                    main_name = v.get("file_name") or f"voucher_main_{v['id']}"
                    embed_file(str(main_name), v["file_data"])

                # Show any extra voucher documents stored in voucher_documents
                extra_vdocs = []
                try:
                    with closing(connect()) as conn, closing(conn.cursor()) as cur2:
                        cur2.execute(
                            """
                            SELECT id, doc_name, uploaded_at
                            FROM voucher_documents
                            WHERE voucher_id IN (
                                SELECT id FROM vouchers WHERE parent_id = %s
                            )
                            ORDER BY id DESC
                            """,
                            (int(v["parent_id"]),),
                        )
                        extra_vdocs = cur2.fetchall()
                except Exception:
                    extra_vdocs = []

                if extra_vdocs:
                    options_docs = ["-- Select attached document to preview --"] + [
                        f"{row[0]} - {row[1]} ({row[2] or ''})" for row in extra_vdocs
                    ]
                    sel_doc = st.selectbox(
                        "Existing Attached Documents",
                        options_docs,
                        key=f"voucher_doc_select_{v['id']}",
                    )
                    if sel_doc != "-- Select attached document to preview --":
                        try:
                            sel_id = int(sel_doc.split(" - ", 1)[0])
                        except Exception:
                            sel_id = None
                        if sel_id is not None:
                            with closing(connect()) as conn, closing(conn.cursor()) as cur2:
                                cur2.execute(
                                    "SELECT doc_name, doc_data FROM voucher_documents WHERE id=%s",
                                    (sel_id,),
                                )
                                rowd = cur2.fetchone()
                            if rowd and rowd[0] and rowd[1]:
                                embed_file(rowd[0], rowd[1])

                            # Allow deletion of the selected attachment
                            if st.button("Delete Selected Attachment", key=f"del_voucher_doc_{v['id']}"):
                                try:
                                    with closing(connect()) as conn, closing(conn.cursor()) as cur_del:
                                        cur_del.execute("DELETE FROM voucher_documents WHERE id = %s", (sel_id,))
                                        conn.commit()
                                    # Clear the selectbox state so the deleted id is not re-selected after rerun
                                    try:
                                        select_key = f"voucher_doc_select_{v['id']}"
                                        if select_key in st.session_state:
                                            st.session_state.pop(select_key, None)
                                    except Exception:
                                        pass
                                    st.success("Attachment deleted.")
                                    rerun()
                                except Exception as e:
                                    st.error(f"Failed to delete attachment: {e}")
                else:
                    st.caption("No additional attached documents for this voucher yet.")

                # Multi-file uploader for this voucher version
                new_files = st.file_uploader(
                    "Attach / Replace Voucher Documents (PDF/JPG/PNG) — accepts multiple files",
                    type=["pdf", "jpg", "jpeg", "png"],
                    key=f"files_{v['id']}",
                    accept_multiple_files=True,
                )

                # Default primary file from existing data
                primary_name = v["file_name"]
                primary_bytes = v["file_data"]
                new_attachments: List[Tuple[str, bytes]] = []

                if new_files:
                    for idx_file, f in enumerate(new_files):
                        try:
                            fb = f.read()
                        except Exception:
                            fb = None
                        if not fb:
                            continue

                        # First uploaded file becomes the main one on vouchers table
                        if idx_file == 0:
                            primary_name = f.name
                            primary_bytes = fb

                        new_attachments.append((f.name, fb))

                        try:
                            f.seek(0)
                        except Exception:
                            pass

                # Main file variables used both for DB storage and PDF export
                new_file_name = primary_name
                new_file_bytes = primary_bytes

                act_left, act_right = st.columns([1, 1])

                if act_left.button("Save Changes", key=f"save_{v['id']}"):
                    create_voucher_new_version(
                        int(v["parent_id"]),
                        str(new_vendor),
                        str(new_requester),
                        (new_invoice if new_invoice != "-- None --" else None),
                        updated_lines,
                        new_file_name,
                        new_file_bytes,
                        attachments=new_attachments if new_attachments else None,
                    )
                    st.success("Voucher updated!")
                    rerun()

                
# ---------- Download PDF ----------
                if REPORTLAB_OK:
                    inv_no_for_lines = new_invoice if new_invoice != "-- None --" else ""
                    pdf_line_rows = []
                    sum_payable = 0.0
    
                    for ln in updated_lines:
                        amt = float(ln["amount"])
                        vat_val = float(ln["vat_value"])
                        wht_val = float(ln["wht_value"])
                        payable = (amt + vat_val) - wht_val
                        sum_payable += payable
                        pdf_line_rows.append({
                            "inv_no": inv_no_for_lines,
                            "acct_code": ("" if inv_no_for_lines else ln["expense_account"]),
                            "details": ln["description"],
                            # format line amounts with voucher currency
                            "amount_str": money(amt, voucher_currency),
                            "vat_str":    money(vat_val, voucher_currency),
                            "wht_str":    (money(wht_val, voucher_currency) if wht_val else "-"),
                            "payable_str": money(payable, voucher_currency),
                            "_amount": amt, "_vat": vat_val, "_wht": wht_val,
                        })
    
                    date_str = datetime.now().strftime("%d-%b-%y")
                    amount_str = money(sum_payable, voucher_currency)
    
                    vnum_display = v.get("voucher_number")
                    if not vnum_display:
                        try:
                            with closing(connect()) as conn, closing(conn.cursor()) as cur3:
                                cur3.execute("SELECT voucher_number FROM vouchers WHERE id=%s", (int(v["parent_id"]),))
                                row_vn2 = cur3.fetchone()
                                if row_vn2 and row_vn2[0]:
                                    vnum_display = row_vn2[0]
                        except Exception:
                            vnum_display = None
                    if not vnum_display:
                        vnum_display = f"V{v['id']}"
                    voucher_meta = {
                        "date_str": date_str,
                        "amount_str": amount_str,
                        "voucher_number": vnum_display,
                        "requested_by": str(new_requester),
                        "department": company_settings.get("department_default","ACCOUNT"),
                        "payable_to": str(new_vendor),
                        "bank": "",
                        "acc_no": "",
                        "inv_no": inv_no_for_lines,
                        "authorizer": company_settings.get("authorizer_name",""),
                        "approver": company_settings.get("approver_name",""),
                        "currency": voucher_currency,
                    }   
                    # Collect all attachments for this voucher (all versions under the same parent)
                    attachments_for_pdf: List[Tuple[str, bytes]] = []
                    seen_keys = set()
                    
                    def _add_attachment(name: Optional[str], data: Optional[bytes]) -> None:
                        """Add a single attachment only once (dedupe by name + size)."""
                        if not data:
                            return
                        key = (str(name or ""), len(data))
                        if key in seen_keys:
                            return
                        seen_keys.add(key)
                        attachments_for_pdf.append((str(name or ""), data))
                    
                    # 1) Main file for this version (UI primary file)
                    if new_file_bytes:
                        _add_attachment(new_file_name or "", new_file_bytes)
                    elif v["file_data"]:
                        _add_attachment(v["file_name"] or "", v["file_data"])
                    
                    # 2) Additional documents from voucher_documents for any version of this voucher (by parent_id)
                    try:
                        with closing(connect()) as conn, closing(conn.cursor()) as cur_pdf:
                            cur_pdf.execute(
                                """
                                SELECT doc_name, doc_data
                                FROM voucher_documents
                                WHERE voucher_id IN (
                                    SELECT id FROM vouchers WHERE parent_id = %s
                                )
                                ORDER BY id
                                """,
                                (int(v["parent_id"]),),
                            )
                            rows_pdf = cur_pdf.fetchall()
                    
                        for name, data in rows_pdf:
                            _add_attachment(name, data)
                    except Exception:
                        # If anything goes wrong, just fall back to whatever we already collected
                        pass
                    
                    # Pass the complete list of unique attachments to the PDF builder
                    pdf_bytes = build_voucher_pdf_bytes(
                        company_settings,
                        voucher_meta,
                        pdf_line_rows,
                        attachment=attachments_for_pdf if attachments_for_pdf else None,
                    )
                    # Use a safe filename that still contains the full voucher number
                    safe_vnum = "".join(
                        ch if ch.isalnum() or ch in ("-", "_") else "_" 
                        for ch in str(vnum_display)
                    )
                    if not safe_vnum:
                        safe_vnum = "voucher"
                    
                    act_right.download_button(
                        label="Download PDF",
                        data=pdf_bytes,
                        file_name=f"{safe_vnum}.pdf",
                        mime="application/pdf",
                        key=f"pdf_{v['id']}",
                        help="Export voucher to PDF: page 1 (voucher) + up to 4 attachment pages (one per page). Extra pages ignored.",
                    )
                else:
                    act_right.info("Install reportlab to enable PDF download:  pip install reportlab")
        st.markdown("</div>", unsafe_allow_html=True)
    
    # ============================ TAB: INVOICES (EDIT) ====================
    with tabs[2]:
        st.markdown("<div class=''card'><div class=''card-header'>📄 All Invoices</div>", unsafe_allow_html=True)
        for _, inv in get_latest_invoices_df().iterrows():
            with st.expander(
                f"{inv['invoice_number']} • {inv['vendor']} • GROSS {money(inv['total_amount'], inv.get('currency') or 'NGN')}",
                expanded=False,
            ):
                c1, c2, c3 = st.columns([2,2,2])
                inv_num = c1.text_input("Invoice Number", value=inv["invoice_number"], key=f"invnum_{inv['id']}", disabled=True)
                vendor_inv = c2.text_input("Vendor Invoice No", value=inv["vendor_invoice_number"], key=f"vi_{inv['id']}")
                summary = c3.text_input("Summary", value=inv["summary"], key=f"s_{inv['id']}")
    
                v1, v2, v3 = st.columns(3)
                vatable = v1.number_input("Vatable", value=float(inv["vatable_amount"]), min_value=0.00, format="%.2f", key=f"va_{inv['id']}")
                vat_rate = v2.number_input("VAT %", value=float(inv["vat_rate"]), key=f"vr_{inv['id']}")
                wht_rate = v3.number_input("WHT % (ignored in GL)", value=float(inv["wht_rate"]), key=f"wr_{inv['id']}")
    
                p1, p2 = st.columns(2)
                payable_account = p1.selectbox(
                    "Payable Account",
                    posting_payables,
                    index=safe_index(posting_payables, (inv.get("payable_account") or "Accounts Payable")),
                    key=f"payacc_{inv['id']}"
                )
                expense_asset_account = p2.selectbox(
                    "Expense or Asset Account (posting DR)",
                    expense_or_asset_options,
                    index=safe_index(expense_or_asset_options, (inv.get("expense_asset_account") or "Expense")),
                    key=f"eaacc_{inv['id']}"
                )

                # Invoice currency (multi-currency support)
                currency_options = ["NGN", "USD", "GBP", "EUR"]
                current_currency = inv.get("currency") or "NGN"
                currency = st.selectbox(
                    "Invoice Currency",
                    currency_options,
                    index=safe_index(currency_options, current_currency),
                    key=f"cur_{inv['id']}",
                )

                vat_amt = vatable * vat_rate / 100
                wht_amt = vatable * wht_rate / 100
                subtotal = vatable + vat_amt - wht_amt
                non_vatable = st.number_input("Non-Vatable", value=float(inv["non_vatable_amount"]), min_value=0.00, format="%.2f", key=f"nv_{inv['id']}")
                total_payable = subtotal + non_vatable
                gross = vatable + vat_amt + non_vatable
    
                st.markdown(f"<div class=''calc-line'>VAT: {money(vat_amt)} • WHT: {money(wht_amt)} • PAYABLE: {money(total_payable)}</div>", unsafe_allow_html=True)
                c1b, c2b = st.columns(2)
                c1b.markdown(f"<div class=''total-box'>PAYABLE<br>{money(total_payable)}</div>", unsafe_allow_html=True)
                c2b.markdown(f"<div class=''total-box'>GROSS<br>{money(gross)}</div>", unsafe_allow_html=True)
    
                # Existing invoice attachment
                existing_file_name = inv.get("file_name")
                existing_file_data = inv.get("file_data")

                if existing_file_name and existing_file_data:
                    st.markdown("**Current Invoice Document:**")
                    embed_file(existing_file_name, existing_file_data)

                inv_upload_edit = st.file_uploader(
                    "Replace Invoice Document (PDF/JPG/PNG)",
                    type=["pdf", "jpg", "jpeg", "png"],
                    key=f"inv_upload_edit_{inv['id']}",
                )

                new_file_name = existing_file_name
                new_file_bytes = existing_file_data
                if inv_upload_edit is not None:
                    try:
                        new_file_bytes = inv_upload_edit.read()
                    except Exception:
                        new_file_bytes = None
                    new_file_name = inv_upload_edit.name if new_file_bytes else existing_file_name
                    if new_file_bytes:
                        embed_file(new_file_name, new_file_bytes)
                        inv_upload_edit.seek(0)

                terms = st.text_area("Terms", value=inv["terms"], key=f"t_{inv['id']}")
    
                if st.button("Save Invoice", key=f"is_{inv['id']}"):
                    data = {
                        "inv_num": inv["invoice_number"], "vendor_inv": vendor_inv, "vendor": inv["vendor"], "summary": summary,
                        "vatable": float(vatable), "vat_rate": float(vat_rate), "wht_rate": float(wht_rate),
                        "vat_amt": float(vat_amt), "wht_amt": float(wht_amt), "non_vatable": float(non_vatable),
                        "subtotal": float(total_payable), "total": float(gross), "terms": terms,
                        "payable_account": payable_account, "expense_asset_account": expense_asset_account, "currency": currency, "file_name": new_file_name, "file_bytes": new_file_bytes
                    }
                    err = create_invoice_new_version(int(inv["parent_id"]), data)
                    if err:
                        st.error(err)
                    else:
                        st.success("Invoice updated!")
                        rerun()
        st.markdown("</div>", unsafe_allow_html=True)
    
    # ============================ TAB: REPORTS ============================
    with tabs[3]:
        st.markdown("<div class=''card'><div class=''card-header'>📊 Complete Financial Reports</div>", unsafe_allow_html=True)
        vdf_latest = get_latest_vouchers_df()
        idf_latest = get_latest_invoices_df()
    
        # Invoice summary
        invoice_rows: List[Dict[str, Any]] = []
        for _, inv in idf_latest.iterrows():
            inv_num = inv["invoice_number"]
            gross = float(inv["total_amount"])
            vatable = float(inv["vatable_amount"])
            vat_amt = float(inv["vat_amount"])
            wht_amt = float(inv["wht_amount"])
            non_vatable = float(inv["non_vatable_amount"])
            payable = float(inv["subtotal"])
            linked_vouchers = vdf_latest[vdf_latest["invoice"] == inv_num]
    
            paid_gross = paid_payable = 0.0
            v_count = 0
            for _, v in linked_vouchers.iterrows():
                v_count += 1
                lines = get_lines_for_voucher(int(v["id"]))
                v_gross = float((lines["amount"] + lines["vat_value"] + lines["wht_value"]).sum())
                v_payable_sum = float((lines["amount"] + lines["vat_value"] - lines["wht_value"]).sum())
                paid_gross += v_gross
                paid_payable += v_payable_sum
    
            remaining_gross = max(0.0, gross - paid_gross)
            remaining_payable = max(0.0, payable - paid_payable)
            status = "Fully Paid" if remaining_gross <= 1e-6 else ("Partially Paid" if paid_gross > 0 else "Unpaid")
    
            invoice_rows.append({
                "Invoice No": inv_num,
                "Vendor": inv["vendor"],
                "Currency": inv.get("currency", "NGN"),
                "Vendor Inv No": inv["vendor_invoice_number"],
                "Summary": inv["summary"],
                "VAT Rate %": float(inv["vat_rate"]),
                "WHT Rate %": float(inv["wht_rate"]),
                "Vatable": vatable, "VAT": vat_amt, "WHT": wht_amt, "Non-Vatable": non_vatable,
                "Total Payable (Invoice)": payable, "Gross Invoice": gross,
                "Paid (Gross)": paid_gross, "Paid (Payable)": paid_payable,
                "Remaining (Gross)": remaining_gross, "Remaining (Payable)": remaining_payable,
                "Voucher Count": v_count,
                "Payable Account": inv.get("payable_account","Accounts Payable"),
                "Expense/Asset Account": inv.get("expense_asset_account","Expense"),
                "Terms": inv["terms"], "Last Modified": inv["last_modified"], "Status": status,
            })
        df_invoices = pd.DataFrame(invoice_rows)
    
        # Ensure Currency column always exists (backward compatibility)
        if "Currency" not in df_invoices.columns:
            df_invoices["Currency"] = "NGN"
    
        # Split NGN vs Multi-currency for better understanding
        df_invoices_ngn = df_invoices[df_invoices["Currency"].fillna("NGN").str.upper() == "NGN"]
        df_invoices_fx = df_invoices[df_invoices["Currency"].fillna("NGN").str.upper() != "NGN"]
    
        
        # Voucher summary
        voucher_rows: List[Dict[str, Any]] = []
        voucher_currency_cache: Dict[int, str] = {}
        for _, v in vdf_latest.iterrows():
            vid = int(v["id"])
            # Derive voucher currency from linked invoice when available
            v_currency = "NGN"
            try:
                if isinstance(v.get("invoice"), str) and not idf_latest.empty:
                    inv_match = idf_latest[idf_latest["invoice_number"] == v["invoice"]]
                    if not inv_match.empty:
                        inv_row = inv_match.iloc[0]
                        v_currency = (inv_row.get("currency") or "NGN")
            except Exception:
                v_currency = "NGN"
            voucher_currency_cache[vid] = v_currency

            lines = get_lines_for_voucher(vid)
            v_payable = float((lines["amount"] + lines["vat_value"] - lines["wht_value"]).sum())
            v_gross = float((lines["amount"] + lines["vat_value"] + lines["wht_value"]).sum())
            voucher_rows.append({
                "Voucher No": v.get("voucher_number") or f"V{v['id']}",
                "Voucher Id": vid, "Parent Id": int(v["parent_id"]), "Version": int(v["version"]),
                "Vendor": v["vendor"], "Requester": v["requester"],
                "Linked Invoice": v["invoice"] if v["invoice"] else "",
                "Voucher Status": v.get("status", ""),
                "Currency": v_currency,
                "Payable (Voucher)": v_payable, "Gross (Voucher)": v_gross,
                "File Name": v["file_name"] if v["file_name"] else "", "Last Modified": v["last_modified"],
            })
        df_vouchers = pd.DataFrame(voucher_rows)
    
        # Line items
        line_rows: List[Dict[str, Any]] = []
        for _, v in vdf_latest.iterrows():
            vid = int(v["id"])
            lines = get_lines_for_voucher(vid)
            v_currency = voucher_currency_cache.get(vid, "NGN")
            for _, ln in lines.iterrows():
                line_rows.append({
                    "Voucher No": v.get("voucher_number") or f"V{v['id']}",
                    "Linked Invoice": v["invoice"] if v["invoice"] else "",
                    "Currency": v_currency,
                    "Description": ln["description"],
                    "Amount": float(ln["amount"]),
                    "Expense/Asset Account": ln["expense_account"],
                    "VAT %": float(ln["vat_percent"]), "WHT %": float(ln["wht_percent"]),
                    "VAT Value": float(ln["vat_value"]), "WHT Value": float(ln["wht_value"]),
                    "Line Total (Payable)": float(ln["total"]),
                })
        df_lines = pd.DataFrame(line_rows)

        # General Journal
        journal_rows: List[Dict[str, Any]] = []

# Invoices DR/CR
        for _, inv in idf_latest.iterrows():
            inv_num = inv["invoice_number"]
            summary = inv["summary"]
            inv_currency = (inv.get("currency") or "NGN") if hasattr(inv, "get") else str(inv["currency"]) if "currency" in inv.index else "NGN"
            vatable = float(inv["vatable_amount"])
            non_vatable = float(inv["non_vatable_amount"])
            vat_amt = float(inv["vat_amount"])
            pay_acc = inv.get("payable_account") or "Accounts Payable"
            exp_asset_acc = inv.get("expense_asset_account") or "Expense"
            invoice_drcr_total = vatable + non_vatable + vat_amt
            if invoice_drcr_total > 0:
                journal_rows.append({
                    "Date": inv["last_modified"], "Type": "INVOICE", "Invoice No": inv_num, "Voucher No": "",
                    "Description": f"{summary} (DR exp/asset actual+VAT+non-vatable)",
                    "Currency": inv_currency,
                    "DR Account": exp_asset_acc, "DR Amount": invoice_drcr_total,
                    "CR Account": "", "CR Amount": 0.0
                })
                journal_rows.append({
                    "Date": inv["last_modified"], "Type": "INVOICE", "Invoice No": inv_num, "Voucher No": "",
                    "Description": f"{summary} (CR accounts payable)",
                    "Currency": inv_currency,
                    "DR Account": "", "DR Amount": 0.0,
                    "CR Account": pay_acc, "CR Amount": invoice_drcr_total
                })
    
        # Vouchers postings
        for _, v in vdf_latest.iterrows():
            vnum = v.get("voucher_number") or f"V{v['id']}"
            lines = get_lines_for_voucher(int(v["id"]))
            linked_inv = None
            if v["invoice"]:
                inv_match = idf_latest[idf_latest["invoice_number"] == v["invoice"]]
                if not inv_match.empty:
                    linked_inv = inv_match.iloc[0]
            if linked_inv is not None:
                pay_acc = linked_inv.get("payable_account") or "Accounts Payable"
                inv_num = linked_inv["invoice_number"]
                v_currency = (linked_inv.get("currency") or "NGN") if hasattr(linked_inv, "get") else str(linked_inv["currency"]) if "currency" in linked_inv.index else "NGN"
                for _, ln in lines.iterrows():
                    amt = float(ln["amount"])
                    vat_val = float(ln["vat_value"])
                    wht_val = float(ln["wht_value"])
                    total_no_wht = amt + vat_val
                    if total_no_wht > 0:
                        journal_rows.append({
                            "Date": v["last_modified"], "Type": "VOUCHER", "Invoice No": inv_num, "Voucher No": vnum,
                            "Description": f"{ln['description']} (DR payable, amt+vat)",
                            "DR Account": pay_acc, "DR Amount": total_no_wht,
                            "CR Account": "", "CR Amount": 0.0
                        })
                        journal_rows.append({
                            "Date": v["last_modified"], "Type": "VOUCHER", "Invoice No": inv_num, "Voucher No": vnum,
                            "Description": f"{ln['description']} (CR suspense, amt+vat)",
                            "Currency": v_currency,
                            "DR Account": "", "DR Amount": 0.0,
                            "CR Account": "Suspense Account", "CR Amount": total_no_wht
                        })
                    if wht_val > 0:
                        journal_rows.append({
                            "Date": v["last_modified"], "Type": "VOUCHER", "Invoice No": inv_num, "Voucher No": vnum,
                            "Description": f"{ln['description']} (DR payable WHT)",
                            "Currency": v_currency,
                            "DR Account": pay_acc, "DR Amount": wht_val,
                            "CR Account": "", "CR Amount": 0.0
                        })
                        journal_rows.append({
                            "Date": v["last_modified"], "Type": "VOUCHER", "Invoice No": inv_num, "Voucher No": vnum,
                            "Description": f"{ln['description']} (CR WHT payable)",
                            "Currency": v_currency,
                            "DR Account": "", "DR Amount": 0.0,
                            "CR Account": "WHT Payable", "CR Amount": wht_val
                        })
            else:
                v_currency = "NGN"
                for _, ln in lines.iterrows():
                    amt = float(ln["amount"])
                    vat_val = float(ln["vat_value"])
                    wht_val = float(ln["wht_value"])
                    line_payable = amt + vat_val - wht_val
                    if line_payable <= 0:
                        continue
                    journal_rows.append({
                        "Date": v["last_modified"], "Type": "VOUCHER", "Invoice No": "", "Voucher No": vnum,
                        "Description": f"{ln['description']} (DR exp/asset)",
                        "Currency": v_currency,
                        "DR Account": ln["expense_account"], "DR Amount": line_payable,
                        "CR Account": "", "CR Amount": 0.0
                    })
                    journal_rows.append({
                        "Date": v["last_modified"], "Type": "VOUCHER", "Invoice No": "", "Voucher No": vnum,
                        "Description": f"{ln['description']} (CR suspense)",
                        "Currency": v_currency,
                        "DR Account": "", "DR Amount": 0.0,
                        "CR Account": "Suspense Account", "CR Amount": line_payable
                    })
    
        df_journal = pd.DataFrame(journal_rows)


        st.markdown("<div class=''card'><div class=''card-header'>🧾 Invoice Summary</div>", unsafe_allow_html=True)
        if not df_invoices.empty:
            # NGN-only report
            if not df_invoices_ngn.empty:
                st.markdown("**Invoices (NGN Only)**")
                st.dataframe(
                    df_invoices_ngn.style.format({
                        "Vatable": "{:,.2f}", "VAT": "{:,.2f}", "WHT": "{:,.2f}",
                        "Non-Vatable": "{:,.2f}", "Total Payable (Invoice)": "{:,.2f}",
                        "Gross Invoice": "{:,.2f}", "Paid (Gross)": "{:,.2f}",
                        "Paid (Payable)": "{:,.2f}", "Remaining (Gross)": "{:,.2f}",
                        "Remaining (Payable)": "{:,.2f}",
                    }),
                    use_container_width=True,
                )
            else:
                st.info("No NGN invoices yet.")

            # Multi-currency report (non-NGN)
            if not df_invoices_fx.empty:
                st.markdown("**Invoices (Multi-Currency – Non-NGN)**")
                st.dataframe(
                    df_invoices_fx.style.format({
                        "Vatable": "{:,.2f}", "VAT": "{:,.2f}", "WHT": "{:,.2f}",
                        "Non-Vatable": "{:,.2f}", "Total Payable (Invoice)": "{:,.2f}",
                        "Gross Invoice": "{:,.2f}", "Paid (Gross)": "{:,.2f}",
                        "Paid (Payable)": "{:,.2f}", "Remaining (Gross)": "{:,.2f}",
                        "Remaining (Payable)": "{:,.2f}",
                    }),
                    use_container_width=True,
                )
            else:
                st.info("No non-NGN (multi-currency) invoices yet.")

            # Currency-level summary (all currencies)
            if "Currency" in df_invoices.columns:
                currency_summary = (
                    df_invoices.groupby("Currency")[
                        [
                            "Vatable",
                            "VAT",
                            "WHT",
                            "Non-Vatable",
                            "Total Payable (Invoice)",
                            "Gross Invoice",
                            "Paid (Gross)",
                            "Paid (Payable)",
                            "Remaining (Gross)",
                            "Remaining (Payable)",
                        ]
                    ]
                    .sum()
                    .reset_index()
                )
                st.markdown("**Totals by Currency (All Invoices)**")
                st.dataframe(currency_summary, use_container_width=True)
        else:
            st.info("No invoices yet.")
        st.markdown("</div>", unsafe_allow_html=True)

    
        st.markdown("<div class=''card'><div class=''card-header'>📑 Voucher Summary</div>", unsafe_allow_html=True)
        if not df_vouchers.empty:
            st.dataframe(
                df_vouchers.style.format({
                    "Payable (Voucher)": "{:,.2f}", "Gross (Voucher)": "{:,.2f}",
                }),
                use_container_width=True,
            )
        else:
            st.info("No vouchers yet.")
        st.markdown("</div>", unsafe_allow_html=True)
    
        st.markdown("<div class=''card'><div class=''card-header'>🧰 Line Items (All)</div>", unsafe_allow_html=True)
        if not df_lines.empty:
            st.dataframe(
                df_lines.style.format({
                    "Amount": "{:,.2f}", "VAT Value": "{:,.2f}",
                    "WHT Value": "{:,.2f}", "Line Total (Payable)": "{:,.2f}",
                }),
                use_container_width=True,
            )
        else:
            st.info("No line items yet.")
        st.markdown("</div>", unsafe_allow_html=True)
    
        st.markdown("<div class=''card'><div class=''card-header'>📚 General Journal (Linked DR / CR)</div>", unsafe_allow_html=True)
        if not df_journal.empty:
            st.dataframe(
                df_journal.style.format({
                    "DR Amount": "{:,.2f}", "CR Amount": "{:,.2f}"
                }),
                use_container_width=True,
            )
        else:
            st.info("No journal entries yet.")
        st.markdown("</div>", unsafe_allow_html=True)
    
        with closing(connect()) as conn:
            df_audit = pd.read_sql_query("SELECT * FROM audit_log ORDER BY id DESC", conn)
    
        st.markdown(excel_download_link_multi(df_invoices, df_vouchers, df_lines, df_journal, df_audit), unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)
    
    # ============================ TAB: AUDIT ==============================
    with tabs[4]:
        st.markdown("<div class=''card'><div class=''card-header'>📜 Master Audit Trail</div>", unsafe_allow_html=True)
        with closing(connect()) as conn:
            df_audit = pd.read_sql_query(
                "SELECT ts AS Timestamp, user AS User, action AS Action, entity AS Entity, ref AS Reference, details AS Details FROM audit_log ORDER BY id DESC",
                conn
            )
        if df_audit.empty:
            st.info("No activities logged yet.")
        else:
            st.dataframe(df_audit, use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)
    

    # ======================= TAB: COMPANY SETTINGS ========================
    with tabs[5]:
        st.markdown(
            "<div class=''card'><div class=''card-header'>⚙️ Company Settings</div>",
            unsafe_allow_html=True,
        )
        company_settings = get_company_settings()

        c1, c2 = st.columns(2)
        name = c1.text_input("Company Name", value=company_settings.get("name", ""))
        rc = c2.text_input("RC / Registration No.", value=company_settings.get("rc", ""))

        tin = c1.text_input("Tax Identification Number", value=company_settings.get("tin", ""))
        title = c2.text_input(
            "Voucher Title",
            value=company_settings.get("title", "EFT/CHEQUE/CASH REQUISITION"),
        )
        addr = st.text_area(
            "Address (one line per row)",
            value=company_settings.get("addr", ""),
            height=100,
        )

        c3, c4 = st.columns(2)
        authorizer_label = c3.text_input(
            "Authorizer Label",
            value=company_settings.get("authorizer_label", "Authorizer"),
        )
        approval_label = c4.text_input(
            "Approval Label",
            value=company_settings.get("approval_label", "Approval"),
        )

        c5, c6 = st.columns(2)
        authorizer_name = c5.text_input(
            "Default Authorizer Name",
            value=company_settings.get("authorizer_name", ""),
        )
        approver_name = c6.text_input(
            "Default Approver Name",
            value=company_settings.get("approver_name", ""),
        )

        dept_default = st.text_input(
            "Default Department",
            value=company_settings.get("department_default", "ACCOUNT"),
        )

        st.markdown("---")


        # Company document upload (primary + multiple)
        existing_doc_name = company_settings.get("company_doc_name")
        existing_doc_data = company_settings.get("company_doc_data")

        st.markdown("**Primary Company Document (Header / Letterhead)**")
        if existing_doc_name and existing_doc_data:
            if (
                existing_doc_name.lower().endswith(".pdf")
                or existing_doc_name.lower().endswith(".png")
                or existing_doc_name.lower().endswith(".jpg")
                or existing_doc_name.lower().endswith(".jpeg")
            ):
                embed_file(existing_doc_name, existing_doc_data)
            else:
                st.info(existing_doc_name)
        else:
            st.info("No primary company document set yet.")

        new_doc = st.file_uploader(
            "Upload / Replace Primary Company Document (PDF/JPG/PNG)",
            type=["pdf", "jpg", "jpeg", "png"],
            key="company_doc_upload",
        )
        new_doc_name = existing_doc_name
        new_doc_bytes = existing_doc_data
        if new_doc is not None:
            new_doc_bytes = new_doc.read()
            new_doc_name = new_doc.name
            embed_file(new_doc_name, new_doc_bytes)
            new_doc.seek(0)

        st.markdown("---")
        st.markdown("**Additional Company Documents (Multiple)**")

        # List existing extra documents
        extra_docs_rows = []
        try:
            with closing(connect()) as conn, closing(conn.cursor()) as cur:
                cur.execute(
                    """
                    SELECT id, doc_name, uploaded_at
                    FROM company_documents
                    WHERE company_id = 1
                    ORDER BY id DESC
                    """
                )
                extra_docs_rows = cur.fetchall()
        except Exception:
            extra_docs_rows = []

        if extra_docs_rows:
            options = ["-- Select document to preview --"] + [
                f"{row[0]} - {row[1]} ({row[2] or ''})" for row in extra_docs_rows
            ]
            sel_label = st.selectbox(
                "Existing Additional Documents",
                options,
                key="company_doc_select",
            )
            if sel_label != "-- Select document to preview --":
                try:
                    sel_id = int(sel_label.split(" - ", 1)[0])
                except Exception:
                    sel_id = None
                if sel_id is not None:
                    with closing(connect()) as conn, closing(conn.cursor()) as cur:
                        cur.execute(
                            "SELECT doc_name, doc_data FROM company_documents WHERE id=%s",
                            (sel_id,),
                        )
                        row = cur.fetchone()
                    if row and row[0] and row[1]:
                        embed_file(row[0], row[1])
        else:
            st.info("No additional company documents saved yet.")

        multi_docs = st.file_uploader(
            "Upload Additional Company Documents (PDF/JPG/PNG) — accepts multiple files",
            type=["pdf", "jpg", "jpeg", "png"],
            key="company_doc_multi_upload",
            accept_multiple_files=True,
        )

        if st.button("Save Settings"):
            new_settings = {
                "name": name,
                "rc": rc,
                "tin": tin,
                "addr": addr,
                "title": title,
                "authorizer_label": authorizer_label,
                "approval_label": approval_label,
                "authorizer_name": authorizer_name,
                "approver_name": approver_name,
                "department_default": dept_default,
                "company_doc_name": new_doc_name,
                "company_doc_data": new_doc_bytes,
            }
            save_company_settings(new_settings)

            # Persist any newly uploaded additional documents
            if multi_docs:
                with closing(connect()) as conn, closing(conn.cursor()) as cur:
                    for f in multi_docs:
                        try:
                            fb = f.read()
                        except Exception:
                            fb = None
                        if not fb:
                            continue
                        cur.execute(
                            """
                            INSERT INTO company_documents (company_id, doc_name, doc_data, uploaded_at)
                            VALUES (1, %s, %s, %s)
                            """,
                            (f.name, fb, now_iso()),
                        )
                    conn.commit()

            st.success("Company settings saved.")
            rerun()

            st.markdown("</div>", unsafe_allow_html=True)

    # ============================ TAB: WORKFLOW ===========================
    with tabs[6]:
        st.markdown("<div class=''card'><div class=''card-header'>🧭 VoucherPro Workflow — Full Logic Map</div>", unsafe_allow_html=True)
        st.markdown("""
    <div class="stepper">
    
      <div class="step"><div class="step-badge">1</div><div>
        <h4>Data Model</h4>
        <table class="logic-table">
          <thead><tr><th>Table</th><th>Key Fields</th><th>Notes</th></tr></thead>
          <tbody>
            <tr><td><b>invoices</b></td><td>invoice_number (unique), parent_id, version, vendor, summary, vatable_amount, vat_rate, wht_rate, non_vatable_amount, totals, payable_account, expense_asset_account</td><td>Versioned; latest = MAX(version) per parent_id.</td></tr>
            <tr><td><b>vouchers</b></td><td>voucher_number (unique), parent_id, version, vendor, requester, invoice (link to invoice_number)</td><td>Versioned; file attachment per version.</td></tr>
            <tr><td><b>voucher_lines</b></td><td>voucher_id → description, amount, expense_account, vat_percent, wht_percent, vat_value, wht_value, total</td><td>Line-by-line calculation and postings.</td></tr>
            <tr><td><b>audit_log</b></td><td>ts, user, action, entity, ref, details</td><td>Tracks CREATE/UPDATE for audit.</td></tr>
            <tr><td><b>company_settings</b></td><td>name, rc, tin, addr, title, authorizer/approval labels+names, department_default</td><td>Drives PDF header/footer content.</td></tr>
          </tbody>
        </table>
      </div></div>
    
      <div class="step"><div class="step-badge">2</div><div>
        <h4>Uniqueness & Versioning</h4>
        <ul>
          <li>Invoice Number: <b>unique</b> (cannot duplicate). Update = new version (parent_id same, version+1).</li>
          <li>Voucher Number: <b>unique</b>. Update = new version.</li>
        </ul>
      </div></div>
    
      <div class="step"><div class="step-badge">3</div><div>
        <h4>Invoice Calculations</h4>
        <table class="logic-table">
          <thead><tr><th>Field</th><th>Formula</th><th>Meaning</th></tr></thead>
          <tbody>
            <tr><td>VAT Amount</td><td>vatable_amount × vat_rate%</td><td>Tax on vatable portion.</td></tr>
            <tr><td>WHT Amount</td><td>vatable_amount × wht_rate%</td><td>Withholding (used in invoice math only).</td></tr>
            <tr><td>Subtotal</td><td>vatable_amount + VAT − WHT</td><td>Net after WHT.</td></tr>
            <tr><td>Total Payable</td><td>Subtotal + non_vatable_amount</td><td>Net due per invoice math.</td></tr>
            <tr><td>Gross</td><td>vatable_amount + VAT + non_vatable_amount</td><td>Base + VAT + non-vatable (no WHT deduction).</td></tr>
          </tbody>
        </table>
        <p><b>GL Posting (Invoice):</b> <i>Ignore WHT.</i> Post <b>DR Expense/Asset (vatable + VAT + non_vatable)</b> and <b>CR Accounts Payable</b> for the same amount.</p>
      </div></div>
    
      <div class="step"><div class="step-badge">4</div><div>
        <h4>Voucher Line Inputs & Calculations</h4>
        <ul>
          <li>Per line: Description, Amount, (Account if not linked), VAT%, WHT%.</li>
          <li>Computed:
            <ul>
              <li>VAT Value = Amount × VAT%</li>
              <li>WHT Value = Amount × WHT%</li>
              <li>Line Payable = Amount + VAT − WHT</li>
              <li>Line Gross = Amount + VAT + WHT</li>
            </ul>
          </li>
          <li>If an invoice is selected: account selector hidden; WHT% remains visible and effective.</li>
        </ul>
      </div></div>
    
      <div class="step"><div class="step-badge">5</div><div>
        <h4>Voucher Posting Rules</h4>
        <table class="logic-table">
          <thead><tr><th>Scenario</th><th>Per-Line Posting</th><th>Explanation</th></tr></thead>
          <tbody>
            <tr>
              <td>Linked to Invoice</td>
              <td>
                1) <b>DR Accounts Payable</b> (Amt+VAT) / <b>CR Suspense</b> (Amt+VAT)<br>
                2) If WHT&gt;0: <b>DR Accounts Payable</b> (WHT) / <b>CR WHT Payable</b> (WHT)
              </td>
              <td>Clears A/P for billed items; recognizes WHT liability separately.</td>
            </tr>
            <tr>
              <td>Not Linked</td>
              <td><b>DR Selected Expense/Asset</b> (Amt+VAT−WHT) / <b>CR Suspense</b> (same)</td>
              <td>Direct recognition when no invoice reference exists.</td>
            </tr>
          </tbody>
        </table>
      </div></div>
    
      <div class="step"><div class="step-badge">6</div><div>
        <h4>Controls</h4>
        <ul>
          <li><b>Overpayment Check (Linked):</b> Σ voucher <i>Gross</i> (Amt+VAT+WHT) ≤ Invoice <b>Gross</b>.</li>
          <li>Default numeric inputs start at <b>0.00</b>.</li>
          <li>Attachments kept per voucher version.</li>
          <li><b>Multi-currency:</b> Invoice and voucher amounts keep their own currency; general journal carries a <code>Currency</code> column, and reports separate NGN vs non-NGN invoices for clarity.</li>
        </ul>
      </div></div>
    
      <div class="step"><div class="step-badge">7</div><div>
        <h4>Reports & Audit</h4>
        <table class="logic-table">
          <thead><tr><th>Report</th><th>Contents</th></tr></thead>
          <tbody>
            <tr><td>Invoice Summary</td><td>Totals, rates, paid vs remaining (gross & payable), linked vouchers, accounts used, status.</td></tr>
            <tr><td>Voucher Summary</td><td>Voucher totals, requester, linked invoice (if any), attachments.</td></tr>
            <tr><td>Line Items</td><td>All lines with amounts, VAT/WHT %, VAT/WHT values, line payable.</td></tr>
            <tr><td><b>General Journal</b></td><td>All DR/CR with date, type, invoice/voucher references, description, accounts, amounts.</td></tr>
            <tr><td>Audit Trail</td><td>All create/update actions with timestamps & details.</td></tr>
          </tbody>
        </table>
      </div></div>
    
      <div class="step"><div class="step-badge">8</div><div>
        <h4>PDF Output</h4>
        <ul>
          <li>Very small top/bottom margins; landscape layout.</li>
          <li><b>Table 2</b> is a single-row strip: PAYABLE TO | BANK NAME | ACCOUNT NUMBER.</li>
          <li><b>Page 1</b> contains all voucher information and signatures.</li>
          <li><b>Pages 2–5</b>: up to <b>four</b> pages from the attached document, <b>one per page</b>, each auto-scaled to fit the printable area while preserving aspect ratio. Any extra source pages are ignored.</li>
        </ul>
      </div></div>
    
    </div>
    """, unsafe_allow_html=True)
        st.success("Workflow updated: PDF now fits one attachment per page (max 4) → total pages up to 5.")
        st.markdown("</div>", unsafe_allow_html=True)
    
    
    
    
    
    
    
    # crm.py
# Streamlit CRM/Ops master (fixed edit dropdowns):
# Jobs, Vendors, Customers, Inventory, Accounts, Staff (+ Reference, Guarantor, Attachments),
# Reports & Workflow. Tables moved to Reports tab only. Each entity tab has a stable "Edit" dropdown.

import os
from contextlib import closing
from datetime import datetime
from io import BytesIO
from typing import List, Optional, Tuple

import pandas as pd
import streamlit as st


# ---------------------------- Styles ----------------------------
st.markdown("""
<style>
:root{--bg:#f7f8fa; --surface:#fff; --text:#0f172a; --muted:#64748b; --border:#e5e7eb;}
.main{background:var(--bg); color:var(--text); font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial}
.card{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:14px; margin:8px 0; }
.card-header{ font-weight:800; display:flex; align-items:center; gap:.5rem; margin-bottom:.35rem; }
.stButton>button { border-radius:999px; font-weight:700; padding:.45rem 1rem; }
.table-note{ color:var(--muted); font-size:.85rem; margin-top:.25rem; }
.doc-pill {display:inline-block; padding:.25rem .5rem; border:1px solid var(--border); border-radius:999px; margin:.15rem .25rem; background:#fafafa;}
.fixed-row { position: sticky; top: 64px; z-index: 5; background: var(--bg); padding: .5rem 0; border-bottom: 1px dashed var(--border);}
</style>
""", unsafe_allow_html=True)

# ---------------------------- DB core (PostgreSQL) ----------------------------

# These env vars let you host CRM in the same DB or a different DB.
CRM_PG_HOST = "pg-cb495ce-adexsy94-643a.i.aivencloud.com"
CRM_PG_PORT = 14073                # IMPORTANT: same port you saw in SQL Shell
CRM_PG_DB   = "defaultdb"
CRM_PG_USER = "avnadmin"
CRM_PG_PASS = "AVNS_HW9bgleEeofjFFF21iW"  # same password you typed in SQL Shell (psql)

STAFF_DOCS_DIR = os.path.join("storage", "staff_docs")
os.makedirs(STAFF_DOCS_DIR, exist_ok=True)


def _connect() -> psycopg2.extensions.connection:
    """Open a PostgreSQL connection for the CRM / Ops master part of the app."""
    conn = psycopg2.connect(
        host=CRM_PG_HOST,
        port=CRM_PG_PORT,
        dbname=CRM_PG_DB,
        user=CRM_PG_USER,
        password=CRM_PG_PASS,
    )
    conn.autocommit = False
    return conn


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def init_crm_db():
    """Create/migrate CRM tables in PostgreSQL (idempotent)."""
    with closing(_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """        CREATE TABLE IF NOT EXISTS jobs(
            id SERIAL PRIMARY KEY,
            code TEXT UNIQUE,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'Active',
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS vendors(
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            email TEXT, phone TEXT,
            address TEXT, city TEXT, state TEXT, country TEXT,
            tax_id TEXT, website TEXT, contact_person TEXT,
            bank_name TEXT, bank_account TEXT,
            notes TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS customers(
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            email TEXT, phone TEXT,
            address TEXT, city TEXT, state TEXT, country TEXT,
            tax_id TEXT, website TEXT, contact_person TEXT,
            notes TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS inventory(
            id SERIAL PRIMARY KEY,
            sku TEXT UNIQUE,
            name TEXT NOT NULL,
            description TEXT,
            unit_cost REAL DEFAULT 0,
            sell_price REAL DEFAULT 0,
            qty_on_hand REAL DEFAULT 0,
            notes TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS accounts(
            id SERIAL PRIMARY KEY,
            code TEXT UNIQUE,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            notes TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS staff(
            id SERIAL PRIMARY KEY,
            staff_code TEXT UNIQUE,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            email TEXT, phone TEXT,
            address TEXT, city TEXT, state TEXT, country TEXT,
            role TEXT, department TEXT,
            employment_type TEXT,
            status TEXT DEFAULT 'Active',
            bank_name TEXT, bank_account TEXT,
            notes TEXT,
            created_at TEXT, updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS vendor_jobs(
            vendor_id INTEGER, job_id INTEGER,
            PRIMARY KEY(vendor_id, job_id)
        );
        CREATE TABLE IF NOT EXISTS customer_jobs(
            customer_id INTEGER, job_id INTEGER,
            PRIMARY KEY(customer_id, job_id)
        );
        CREATE TABLE IF NOT EXISTS inventory_jobs(
            inventory_id INTEGER, job_id INTEGER,
            PRIMARY KEY(inventory_id, job_id)
        );

        
        CREATE TABLE IF NOT EXISTS staff_jobs(
            staff_id INTEGER, job_id INTEGER,
            PRIMARY KEY(staff_id, job_id)
        );

        CREATE TABLE IF NOT EXISTS staff_documents(
            id SERIAL PRIMARY KEY,
            staff_id INTEGER NOT NULL,
            doc_type TEXT,
            original_name TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            uploaded_at TEXT NOT NULL
        );
            """
        )

        # Idempotent extra columns on staff
        extra_staff_cols = [
            ("ref_name", "TEXT"), ("ref_relationship", "TEXT"),
            ("ref_phone", "TEXT"), ("ref_email", "TEXT"),
            ("ref_address", "TEXT"),
            ("guar_name", "TEXT"), ("guar_relationship", "TEXT"),
            ("guar_phone", "TEXT"), ("guar_email", "TEXT"),
            ("guar_address", "TEXT"),
        ]
        for col, typ in extra_staff_cols:
            try:
                cur.execute(f"ALTER TABLE staff ADD COLUMN {col} {typ}")
            except Exception:
                pass

        conn.commit()




# Initialise CRM database schema on startup
init_crm_db()

def _df(sql: str, params: Tuple = ()) -> pd.DataFrame:
    """Helper to run a SELECT and get a DataFrame."""
    with closing(_connect()) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def _exec(sql: str, params: Tuple = ()) -> Optional[str]:
    """Execute a single write statement with friendly error messages."""
    with closing(_connect()) as conn, closing(conn.cursor()) as cur:
        try:
            cur.execute("BEGIN")
            cur.execute(sql, params)
            conn.commit()
            return None
        except IntegrityError as e:
            if conn.closed == 0:
                conn.rollback()
            msg = str(e)

            # You can adjust these constraint name checks once you see the actual names in Postgres.
            if "jobs_code_key" in msg:
                return "This job information is already saved. Please use a different Job Code or leave it blank."
            if "vendors_name_key" in msg:
                return "This vendor information is already saved. Please use a different Vendor Name."
            if "customers_name_key" in msg:
                return "This customer information is already saved. Please use a different Customer Name."
            if "inventory_sku_key" in msg:
                return "An item with this SKU is already saved. Please use a different SKU or leave it blank."
            if "accounts_code_key" in msg:
                return "An account with this code already exists. Please use a different Account Code or leave it blank."
            if "staff_staff_code_key" in msg:
                return "A staff member with this Staff Code already exists. Please use a different Staff Code or leave it blank."

            if "duplicate key value violates unique constraint" in msg:
                return "This information is already saved or conflicts with an existing record (duplicate value). Please review and try again."

            return f"Integrity error: {e}"
        except Exception as e:
            if conn.closed == 0:
                conn.rollback()
            return f"Error: {e}"


def _exec_many(sql: str, rows: List[Tuple]) -> Optional[str]:
    """Execute many INSERT/UPDATE statements in a batch."""
    with closing(_connect()) as conn, closing(conn.cursor()) as cur:
        try:
            cur.execute("BEGIN")
            cur.executemany(sql, rows)
            conn.commit()
            return None
        except Exception as e:
            if conn.closed == 0 and conn.closed is not None:
                conn.rollback()
            return f"Error: {e}"

def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

def _save_with_feedback(fn, *args, success_msg: str = "Saved!", **kwargs):
    """Run a DB helper and show success/error messages in Streamlit."""
    with st.spinner("Saving..."):
        err = None
        try:
            err = fn(*args, **kwargs)
        except psycopg2.OperationalError as e:
            err = f"Database connection error. Please retry. Details: {e}"
        except Exception as e:
            err = f"Unexpected error: {e}"

    if err:
        st.session_state["last_message_type"] = "error"
        st.session_state["last_message_text"] = err
    else:
        st.session_state["last_message_type"] = "success"
        st.session_state["last_message_text"] = success_msg

    _safe_rerun()

# --------------- helpers for job multiselect + prefill ---------------
def _get_linked_job_ids(link_table:str, id_col:str, row_id:int) -> List[int]:
    df = _df(f"SELECT job_id FROM {link_table} WHERE {id_col}=%s", (row_id,))
    return [] if df.empty else list(df["job_id"].astype(int))

def _multiselect_job_ids(prefix_key:str, selected_ids:List[int]) -> List[int]:
    jobs = _df("SELECT id, title, COALESCE(code,'') AS code FROM jobs ORDER BY title")
    options = [f"{int(r.id)} — {r.title}" + (f" ({r.code})" if r.code else "") for _, r in jobs.iterrows()]
    id_map = {opt:int(str(opt).split(" — ",1)[0]) for opt in options}
    default_opts = [opt for opt in options if id_map[opt] in (selected_ids or [])]
    chosen = st.multiselect("Jobs (cost centre)", options, default=default_opts, key=prefix_key)
    return [id_map[o] for o in chosen]

# ------------------------- upsert/delete ops -------------------------
def upsert_job(job_id:Optional[int], code:str, title:str, description:str, status:str) -> Optional[str]:
    if not title.strip(): return "Job Title is required."
    ts = _now()
    if job_id:
        err = _exec("""UPDATE jobs SET code=%s, title=%s, description=%s, status=%s, updated_at=%s WHERE id=%s""",
                    (code.strip() or None, title.strip(), description.strip(), status.strip(), ts, job_id))
        if not err: _audit("UPDATE","JOB",str(job_id),f"title={title}")
        return err
    else:
        err = _exec("""INSERT INTO jobs (code,title,description,status,created_at,updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (code.strip() or None, title.strip(), description.strip(), status.strip(), ts, ts))
        if not err: _audit("CREATE","JOB",title.strip(),f"code={code}")
        return err

def delete_job(job_id:int) -> Optional[str]:
    err = _exec("DELETE FROM jobs WHERE id=%s", (job_id,))
    if not err: _audit("DELETE","JOB",str(job_id),"Deleted")
    return err

def _link_reset(table:str, id_col:str, row_id:int, job_ids:List[int]) -> Optional[str]:
    with closing(_connect()) as conn, closing(conn.cursor()) as cur:
        try:
            cur.execute("BEGIN")
            cur.execute(f"DELETE FROM {table} WHERE {id_col}=%s", (row_id,))
            if job_ids:
                cur.executemany(f"INSERT INTO {table} ({id_col}, job_id) VALUES (%s, %s)",
                                [(row_id,j) for j in job_ids])
            conn.commit()
            return None
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return f"Error linking jobs: {e}"

def upsert_vendor(vendor_id:Optional[int], name:str, email:str, phone:str,
                  address:str, city:str, state:str, country:str,
                  tax_id:str, website:str, contact_person:str, bank_name:str, bank_account:str,
                  notes:str, job_ids:List[int]) -> Optional[str]:
    if not name.strip(): return "Vendor Name is required."
    ts = _now()
    if vendor_id:
        err = _exec("""UPDATE vendors SET name=%s,email=%s,phone=%s,address=%s,city=%s,state=%s,country=%s,
                       tax_id=%s,website=%s,contact_person=%s,bank_name=%s,bank_account=%s,notes=%s,updated_at=%s WHERE id=%s""",
                    (name.strip(),email.strip(),phone.strip(),address.strip(),city.strip(),state.strip(),country.strip(),
                     tax_id.strip(),website.strip(),contact_person.strip(),bank_name.strip(),bank_account.strip(),notes.strip(),ts,vendor_id))
        if err: return err
        link_err = _link_reset("vendor_jobs","vendor_id",vendor_id,job_ids)
        if link_err: return link_err
        _audit("UPDATE","VENDOR",str(vendor_id),f"name={name}")
        return None
    else:
        with closing(_connect()) as conn, closing(conn.cursor()) as cur:
            try:
                cur.execute("BEGIN")
                cur.execute("""INSERT INTO vendors(name,email,phone,address,city,state,country,tax_id,website,contact_person,notes,created_at,updated_at)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (name.strip(),email.strip(),phone.strip(),address.strip(),city.strip(),state.strip(),country.strip(),
                             tax_id.strip(),website.strip(),contact_person.strip(),notes.strip(),ts,ts))
                vid = cur.lastrowid
                if job_ids:
                    cur.executemany("INSERT INTO vendor_jobs(vendor_id,job_id) VALUES(%s,%s)",
                                    [(vid,j) for j in job_ids])
                conn.commit()
                _audit("CREATE","VENDOR",name.strip(),f"id={vid}")
                return None
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return f"Error: {e}"

def delete_vendor(vendor_id:int) -> Optional[str]:
    err = _exec("DELETE FROM vendors WHERE id=%s", (vendor_id,))
    if not err: _audit("DELETE","VENDOR",str(vendor_id),"Deleted")
    return err

def upsert_customer(customer_id:Optional[int], name:str, email:str, phone:str,
                    address:str, city:str, state:str, country:str,
                    tax_id:str, website:str, contact_person:str,
                    notes:str, job_ids:List[int]) -> Optional[str]:
    if not name.strip(): return "Customer Name is required."
    ts = _now()
    if customer_id:
        err = _exec("""UPDATE customers SET name=%s,email=%s,phone=%s,address=%s,city=%s,state=%s,country=%s,
                       tax_id=%s,website=%s,contact_person=%s,notes=%s,updated_at=%s WHERE id=%s""",
                    (name.strip(),email.strip(),phone.strip(),address.strip(),city.strip(),state.strip(),country.strip(),
                     tax_id.strip(),website.strip(),contact_person.strip(),notes.strip(),ts,customer_id))
        if err: return err
        link_err = _link_reset("customer_jobs","customer_id",customer_id,job_ids)
        if link_err: return link_err
        _audit("UPDATE","CUSTOMER",str(customer_id),f"name={name}")
        return None
    else:
        with closing(_connect()) as conn, closing(conn.cursor()) as cur:
            try:
                cur.execute("BEGIN")
                cur.execute("""INSERT INTO customers(name,email,phone,address,city,state,country,tax_id,website,contact_person,notes,created_at,updated_at)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (name.strip(),email.strip(),phone.strip(),address.strip(),city.strip(),state.strip(),country.strip(),
                             tax_id.strip(),website.strip(),contact_person.strip(),notes.strip(),ts,ts))
                cid = cur.lastrowid
                if job_ids:
                    cur.executemany("INSERT INTO customer_jobs(customer_id,job_id) VALUES(%s,%s)",
                                    [(cid,j) for j in job_ids])
                conn.commit()
                _audit("CREATE","CUSTOMER",name.strip(),f"id={cid}")
                return None
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return f"Error: {e}"

def delete_customer(customer_id:int) -> Optional[str]:
    err = _exec("DELETE FROM customers WHERE id=%s", (customer_id,))
    if not err: _audit("DELETE","CUSTOMER",str(customer_id),"Deleted")
    return err

def upsert_inventory(inv_id:Optional[int], sku:str, name:str, description:str, uom:str,
                     unit_cost:float, sell_price:float, qty_on_hand:float,
                     notes:str, job_ids:List[int]) -> Optional[str]:
    if not name.strip(): return "Inventory Name is required."
    ts = _now()
    if inv_id:
        err = _exec("""UPDATE inventory SET sku=%s,name=%s,description=%s,uom=%s,unit_cost=%s,sell_price=%s,qty_on_hand=%s,notes=%s,updated_at=%s WHERE id=%s""",
                    (sku.strip() or None, name.strip(), description.strip(), uom.strip(),
                     float(unit_cost or 0), float(sell_price or 0), float(qty_on_hand or 0),
                     notes.strip(), ts, inv_id))
        if err: return err
        link_err = _link_reset("inventory_jobs","inventory_id",inv_id,job_ids)
        if link_err: return link_err
        _audit("UPDATE","INVENTORY",str(inv_id),f"name={name}")
        return None
    else:
        with closing(_connect()) as conn, closing(conn.cursor()) as cur:
            try:
                cur.execute("BEGIN")
                cur.execute("""INSERT INTO inventory(sku,name,description,uom,unit_cost,sell_price,qty_on_hand,notes,created_at,updated_at)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (sku.strip() or None, name.strip(), description.strip(), uom.strip(),
                             float(unit_cost or 0), float(sell_price or 0), float(qty_on_hand or 0),
                             notes.strip(), ts, ts))
                iid = cur.lastrowid
                if job_ids:
                    cur.executemany("INSERT INTO inventory_jobs(inventory_id,job_id) VALUES(%s,%s)",
                                    [(iid,j) for j in job_ids])
                conn.commit()
                _audit("CREATE","INVENTORY",name.strip(),f"id={iid}")
                return None
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return f"Error: {e}"

def delete_inventory(inv_id:int) -> Optional[str]:
    err = _exec("DELETE FROM inventory WHERE id=%s", (inv_id,))
    if not err: _audit("DELETE","INVENTORY",str(inv_id),"Deleted")
    return err

def upsert_account(acc_id:Optional[int], code:str, name:str, acc_type:str, notes:str) -> Optional[str]:
    if not name.strip(): return "Account Name is required."
    if not acc_type.strip(): return "Account Type is required."
    ts = _now()
    if acc_id:
        err = _exec("""UPDATE accounts SET code=%s,name=%s,type=%s,notes=%s,updated_at=%s WHERE id=%s""",
                    (code.strip() or None, name.strip(), acc_type.strip(), notes.strip(), ts, acc_id))
        if not err: _audit("UPDATE","ACCOUNT",str(acc_id),f"name={name}")
        return err
    else:
        err = _exec("""INSERT INTO accounts(code,name,type,notes,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s)""",
                    (code.strip() or None, name.strip(), acc_type.strip(), notes.strip(), ts, ts))
        if not err: _audit("CREATE","ACCOUNT",name.strip(),f"code={code}")
        return err

def delete_account(acc_id:int) -> Optional[str]:
    err = _exec("DELETE FROM accounts WHERE id=%s", (acc_id,))
    if not err: _audit("DELETE","ACCOUNT",str(acc_id),"Deleted")
    return err

def upsert_staff(staff_id:Optional[int], staff_code:str, first_name:str, last_name:str,
                 email:str, phone:str, address:str, city:str, state:str, country:str,
                 role:str, department:str, employment_type:str, status:str,
                 bank_name:str, bank_account:str,
                 ref_name:str, ref_relationship:str, ref_phone:str, ref_email:str, ref_address:str,
                 guar_name:str, guar_relationship:str, guar_phone:str, guar_email:str, guar_address:str,
                 notes:str, job_ids:List[int]) -> Optional[str]:
    if not first_name.strip() or not last_name.strip():
        return "First name and Last name are required."
    ts = _now()
    if staff_id:
        err = _exec("""UPDATE staff SET
                        staff_code=%s,first_name=%s,last_name=%s,email=%s,phone=%s,address=%s,city=%s,state=%s,country=%s,
                        role=%s,department=%s,employment_type=%s,status=%s,
                        bank_name=%s,bank_account=%s,
                        ref_name=%s,ref_relationship=%s,ref_phone=%s,ref_email=%s,ref_address=%s,
                        guar_name=%s,guar_relationship=%s,guar_phone=%s,guar_email=%s,guar_address=%s,
                        notes=%s,updated_at=%s WHERE id=%s""",
                    (staff_code.strip() or None, first_name.strip(), last_name.strip(),
                     email.strip(), phone.strip(), address.strip(), city.strip(), state.strip(), country.strip(),
                     role.strip(), department.strip(), employment_type.strip(), status.strip(),
                     bank_name.strip(), bank_account.strip(),
                     ref_name.strip(), ref_relationship.strip(), ref_phone.strip(), ref_email.strip(), ref_address.strip(),
                     guar_name.strip(), guar_relationship.strip(), guar_phone.strip(), guar_email.strip(), guar_address.strip(),
                     notes.strip(), ts, staff_id))
        if err: return err
        link_err = _link_reset("staff_jobs","staff_id",staff_id,job_ids)
        if link_err: return link_err
        _audit("UPDATE","STAFF",str(staff_id),f"{first_name} {last_name}")
        return None
    else:
        with closing(_connect()) as conn, closing(conn.cursor()) as cur:
            try:
                cur.execute("BEGIN")
                cur.execute("""INSERT INTO staff(
                        staff_code,first_name,last_name,email,phone,address,city,state,country,
                        role,department,employment_type,status,
                        ref_name,ref_relationship,ref_phone,ref_email,ref_address,
                        guar_name,guar_relationship,guar_phone,guar_email,guar_address,
                        notes,created_at,updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s)""",
                    (staff_code.strip() or None, first_name.strip(), last_name.strip(),
                     email.strip(), phone.strip(), address.strip(), city.strip(), state.strip(), country.strip(),
                     role.strip(), department.strip(), employment_type.strip(), status.strip(),
                     ref_name.strip(), ref_relationship.strip(), ref_phone.strip(), ref_email.strip(), ref_address.strip(),
                     guar_name.strip(), guar_relationship.strip(), guar_phone.strip(), guar_email.strip(), guar_address.strip(),
                     notes.strip(), ts, ts))
                sid = cur.lastrowid
                if job_ids:
                    cur.executemany("INSERT INTO staff_jobs(staff_id,job_id) VALUES(%s,%s)",
                                    [(sid,j) for j in job_ids])
                conn.commit()
                _audit("CREATE","STAFF",f"{first_name} {last_name}",f"id={sid}")
                return None
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return f"Error: {e}"

def delete_staff(staff_id:int) -> Optional[str]:
    err = _exec("DELETE FROM staff WHERE id=%s", (staff_id,))
    if not err: _audit("DELETE","STAFF",str(staff_id),"Deleted")
    return err

# ---------- staff documents ops ----------
def add_staff_document(staff_id:int, doc_type:str, original_name:str, data:bytes) -> Optional[str]:
    if not staff_id:
        return "Save staff first before uploading documents."
    safe_name = original_name.replace("/", "_").replace("\\", "_")
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    file_name = f"{staff_id}_{stamp}_{safe_name}"
    folder = os.path.join(STAFF_DOCS_DIR, str(staff_id))
    os.makedirs(folder, exist_ok=True)
    stored_path = os.path.join(folder, file_name)
    try:
        with open(stored_path, "wb") as f:
            f.write(data)
    except Exception as e:
        return f"Failed saving file: {e}"
    return _exec("""INSERT INTO staff_documents(staff_id,doc_type,original_name,stored_path,uploaded_at)
                    VALUES(%s,%s,%s,%s,%s)""",
                 (staff_id, doc_type.strip() or None, original_name, stored_path, _now()))

def delete_staff_document(doc_id:int) -> Optional[str]:
    row = _df("SELECT stored_path FROM staff_documents WHERE id=%s", (doc_id,))
    path = None if row.empty else row.iloc[0]["stored_path"]
    err = _exec("DELETE FROM staff_documents WHERE id=%s", (doc_id,))
    if err: return err
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass
    return None

# ---------------------- UI helpers (search/list) ----------------------
def _list_with_search(df:pd.DataFrame, cols:List[str], key_prefix:str) -> pd.DataFrame:
    q = st.text_input("Search", key=f"{key_prefix}_q")
    if q:
        mask = pd.Series(False, index=df.index)
        ql = q.lower()
        for c in cols:
            mask = mask | df[c].astype(str).str.lower().str.contains(ql, na=False)
        df = df[mask]
    st.dataframe(df.reset_index(drop=True), use_container_width=True)
    return df

# --------------------------- Init ------------------------------------
if app_section == "CRM / Ops Master":
    init_crm_db()
    st.markdown("<h1>CRM / Ops Master</h1>", unsafe_allow_html=True)
    # Show last saved/error message (if any) after rerun
    msg_type = st.session_state.pop("last_message_type", None)
    msg_text = st.session_state.pop("last_message_text", None)
    if msg_type == "error" and msg_text:
        st.error(msg_text)
    elif msg_type == "success" and msg_text:
        st.success(msg_text)
        # After successful saves/deletes, reset related forms back to default "new" state
        if msg_text in ("Job saved", "Job deleted"):
            st.session_state["sel_job_label"] = "➕ New"
            for k in list(st.session_state.keys()):
                if k.startswith("job_code_") or k.startswith("job_title_") or k.startswith("job_status_") or k.startswith("job_desc_"):
                    st.session_state.pop(k, None)
        elif msg_text in ("Vendor saved", "Vendor deleted"):
            st.session_state["sel_vendor_label"] = "➕ New"
            for k in list(st.session_state.keys()):
                if k.startswith("v_name_") or k.startswith("v_email_") or k.startswith("v_phone_")                    or k.startswith("v_addr_") or k.startswith("v_city_") or k.startswith("v_state_")                    or k.startswith("v_country_") or k.startswith("v_tax_") or k.startswith("v_site_")                    or k.startswith("v_contact_") or k.startswith("v_bank_name_") or k.startswith("v_bank_acct_")                    or k.startswith("v_notes_") or k.startswith("v_jobs_"):
                    st.session_state.pop(k, None)
        elif msg_text in ("Customer saved", "Customer deleted"):
            st.session_state["sel_customer_label"] = "➕ New"
            for k in list(st.session_state.keys()):
                if k.startswith("c_name_") or k.startswith("c_email_") or k.startswith("c_phone_")                    or k.startswith("c_addr_") or k.startswith("c_city_") or k.startswith("c_state_")                    or k.startswith("c_country_") or k.startswith("c_tax_") or k.startswith("c_notes_")                    or k.startswith("c_jobs_"):
                    st.session_state.pop(k, None)
        elif msg_text in ("Inventory item saved", "Item deleted"):
            st.session_state["sel_inventory_label"] = "➕ New"
            for k in list(st.session_state.keys()):
                if k.startswith("i_sku_") or k.startswith("i_name_") or k.startswith("i_uom_")                    or k.startswith("i_uc_") or k.startswith("i_sp_") or k.startswith("i_qoh_")                    or k.startswith("i_notes_") or k.startswith("i_jobs_"):
                    st.session_state.pop(k, None)
        elif msg_text in ("Account saved", "Account deleted"):
            st.session_state["sel_account_label"] = "➕ New"
            for k in list(st.session_state.keys()):
                if k.startswith("acc_code_") or k.startswith("acc_name_") or k.startswith("acc_type_")                    or k.startswith("acc_notes_"):
                    st.session_state.pop(k, None)
        elif msg_text in ("Staff saved", "Staff deleted"):
            st.session_state["sel_staff_label"] = "➕ New"
            for k in list(st.session_state.keys()):
                if k.startswith("staff_"):
                    st.session_state.pop(k, None)


    tabs = st.tabs([
        "🏷️ Jobs","🏢 Vendors","👥 Customers","📦 Inventory",
        "📚 Chart of Accounts","🧑‍💼 Staff","📊 Reports","🧭 Workflow"
    ])
    
    # --------------------- Shared: edit selector ---------------------
    def _entity_selector(label:str, sql:str, fmt_fn, key:str, new_label="➕ New"):
        rows = _df(sql)
        options = [(0, new_label)]
        options += [(int(r["id"]), fmt_fn(r)) for _, r in rows.iterrows()]
        id_to_label = {i:l for i,l in options}
        labels = [l for _, l in options]
        default_label = st.session_state.get(key+"_label", new_label)
        sel_label = st.selectbox(label, labels, index=labels.index(default_label) if default_label in labels else 0, key=key)
        sel_id = next((i for i,l in id_to_label.items() if l==sel_label), 0)
        st.session_state[key+"_label"] = sel_label
        return sel_id
    
    # ============================== JOBS ==============================
    with tabs[0]:
        st.markdown("<div class=''card'><div class=''card-header'>🏷️ Jobs (Cost Centres)</div>", unsafe_allow_html=True)
    
        job_id = _entity_selector(
            "Edit Job",
            "SELECT id, code, title, status FROM jobs ORDER BY title",
            lambda r: f"[{int(r['id'])}] {r['title']} ({r['status']})",
            key="sel_job"
        )
    
        # Prefill
        if job_id:
            row = _df("SELECT * FROM jobs WHERE id=%s", (job_id,))
            j_code_val = row.iloc[0]["code"] or ""
            j_title_val = row.iloc[0]["title"] or ""
            j_desc_val = row.iloc[0]["description"] or ""
            j_status_val = row.iloc[0]["status"] or "Active"
        else:
            j_code_val = j_title_val = j_desc_val = ""
            j_status_val = "Active"
    
        with st.form("job_form"):
            c1,c2,c3 = st.columns(3)
            j_code  = c1.text_input("Job Code (optional)", value=j_code_val, key=f"job_code_{job_id or 'new'}")
            j_title = c2.text_input("Job Title *", value=j_title_val, key=f"job_title_{job_id or 'new'}")
            j_status = c3.selectbox("Status", ["Active","Inactive","Closed"],
                                    index=["Active","Inactive","Closed"].index(j_status_val) if j_status_val in ["Active","Inactive","Closed"] else 0,
                                    key=f"job_status_{job_id or 'new'}")
            j_desc = st.text_area("Description", value=j_desc_val, key=f"job_desc_{job_id or 'new'}")
    
            colS, colD, colC = st.columns([1,1,6])
            if colS.form_submit_button("Save"):
                _save_with_feedback(upsert_job, job_id, j_code, j_title, j_desc, j_status, success_msg="Job saved")
            if job_id and colD.form_submit_button("Delete"):
                _save_with_feedback(delete_job, job_id, success_msg="Job deleted")
                st.session_state["sel_job_label"] = "➕ New"
            if colC.form_submit_button("Cancel"):
                st.session_state["sel_job_label"] = "➕ New"
                _safe_rerun()
    
    # ============================ VENDORS =============================
    with tabs[1]:
        st.markdown("<div class=''card'><div class=''card-header'>🏢 Vendors</div>", unsafe_allow_html=True)
    
        vendor_id = _entity_selector(
            "Edit Vendor",
            "SELECT id, name FROM vendors ORDER BY name",
            lambda r: f"[{int(r['id'])}] {r['name']}",
            key="sel_vendor"
        )
    
        v_defaults = dict(name="",email="",phone="",address="",city="",state="",country="Nigeria",
                          tax_id="",website="",contact_person="",bank_name="",bank_account="",notes="", job_ids=[])
        if vendor_id:
            row = _df("SELECT * FROM vendors WHERE id=%s", (vendor_id,))
            if not row.empty:
                for k in ["name","email","phone","address","city","state","country","tax_id","website","contact_person","bank_name","bank_account","notes"]:
                    if k in row.columns:
                        v_defaults[k] = row.iloc[0][k] or ""
                v_defaults["job_ids"] = _get_linked_job_ids("vendor_jobs","vendor_id", vendor_id)
    
        with st.form("vendor_form"):
            vid = vendor_id
            a,b,c = st.columns(3)
            v_name  = a.text_input("Vendor Name *", value=v_defaults["name"], key=f"v_name_{vid or 'new'}")
            v_email = b.text_input("Email", value=v_defaults["email"], key=f"v_email_{vid or 'new'}")
            v_phone = c.text_input("Phone", value=v_defaults["phone"], key=f"v_phone_{vid or 'new'}")
    
            d,e,f,g = st.columns(4)
            v_addr = d.text_input("Address", value=v_defaults["address"], key=f"v_addr_{vid or 'new'}")
            v_city = e.text_input("City", value=v_defaults["city"], key=f"v_city_{vid or 'new'}")
            v_state = f.text_input("State", value=v_defaults["state"], key=f"v_state_{vid or 'new'}")
            v_country = g.text_input("Country", value=v_defaults["country"], key=f"v_country_{vid or 'new'}")
    
            h,i,j = st.columns(3)
            v_tax = h.text_input("Tax ID", value=v_defaults["tax_id"], key=f"v_tax_{vid or 'new'}")
            v_site = i.text_input("Website", value=v_defaults["website"], key=f"v_site_{vid or 'new'}")
            v_contact = j.text_input("Contact Person", value=v_defaults["contact_person"], key=f"v_contact_{vid or 'new'}")

            k,l = st.columns(2)
            v_bank_name = k.text_input("Bank Name", value=v_defaults.get("bank_name",""), key=f"v_bank_name_{vid or 'new'}")
            v_bank_acct = l.text_input("Bank Account Number", value=v_defaults.get("bank_account",""), key=f"v_bank_acct_{vid or 'new'}")
    
            v_notes = st.text_area("Notes", value=v_defaults["notes"], key=f"v_notes_{vid or 'new'}")
            v_jobs = _multiselect_job_ids(f"v_jobs_{vid or 'new'}", v_defaults["job_ids"])
    
            cS,cD,cC = st.columns([1,1,6])
            if cS.form_submit_button("Save"):
                _save_with_feedback(upsert_vendor, vid, v_name, v_email, v_phone, v_addr, v_city, v_state, v_country,
                                    v_tax, v_site, v_contact, v_bank_name, v_bank_acct, v_notes, v_jobs, success_msg="Vendor saved")
            if vid and cD.form_submit_button("Delete"):
                _save_with_feedback(delete_vendor, vid, success_msg="Vendor deleted")
                st.session_state["sel_vendor_label"] = "➕ New"
            if cC.form_submit_button("Cancel"):
                st.session_state["sel_vendor_label"] = "➕ New"
                _safe_rerun()
    
    # ============================ CUSTOMERS ============================
    with tabs[2]:
        st.markdown("<div class=''card'><div class=''card-header'>👥 Customers</div>", unsafe_allow_html=True)
    
        customer_id = _entity_selector(
            "Edit Customer",
            "SELECT id, name FROM customers ORDER BY name",
            lambda r: f"[{int(r['id'])}] {r['name']}",
            key="sel_customer"
        )
    
        c_defaults = dict(name="",email="",phone="",address="",city="",state="",country="Nigeria",
                          tax_id="",website="",contact_person="",notes="", job_ids=[])
        if customer_id:
            row = _df("SELECT * FROM customers WHERE id=%s", (customer_id,))
            if not row.empty:
                for k in ["name","email","phone","address","city","state","country","tax_id","website","contact_person","notes"]:
                    c_defaults[k] = row.iloc[0][k] or ""
                c_defaults["job_ids"] = _get_linked_job_ids("customer_jobs","customer_id", customer_id)
    
        with st.form("customer_form"):
            cid = customer_id
            a,b,c = st.columns(3)
            c_name  = a.text_input("Customer Name *", value=c_defaults["name"], key=f"c_name_{cid or 'new'}")
            c_email = b.text_input("Email", value=c_defaults["email"], key=f"c_email_{cid or 'new'}")
            c_phone = c.text_input("Phone", value=c_defaults["phone"], key=f"c_phone_{cid or 'new'}")
    
            d,e,f,g = st.columns(4)
            c_addr = d.text_input("Address", value=c_defaults["address"], key=f"c_addr_{cid or 'new'}")
            c_city = e.text_input("City", value=c_defaults["city"], key=f"c_city_{cid or 'new'}")
            c_state = f.text_input("State", value=c_defaults["state"], key=f"c_state_{cid or 'new'}")
            c_country = g.text_input("Country", value=c_defaults["country"], key=f"c_country_{cid or 'new'}")
    
            h,i,j = st.columns(3)
            c_tax = h.text_input("Tax ID", value=c_defaults["tax_id"], key=f"c_tax_{cid or 'new'}")
            c_site = i.text_input("Website", value=c_defaults["website"], key=f"c_site_{cid or 'new'}")
            c_contact = j.text_input("Contact Person", value=c_defaults["contact_person"], key=f"c_contact_{cid or 'new'}")
    
            c_notes = st.text_area("Notes", value=c_defaults["notes"], key=f"c_notes_{cid or 'new'}")
            c_jobs = _multiselect_job_ids(f"c_jobs_{cid or 'new'}", c_defaults["job_ids"])
    
            cS,cD,cC = st.columns([1,1,6])
            if cS.form_submit_button("Save"):
                _save_with_feedback(upsert_customer, cid, c_name, c_email, c_phone, c_addr, c_city, c_state, c_country,
                                    c_tax, c_site, c_contact, c_notes, c_jobs, success_msg="Customer saved")
            if cid and cD.form_submit_button("Delete"):
                _save_with_feedback(delete_customer, cid, success_msg="Customer deleted")
                st.session_state["sel_customer_label"] = "➕ New"
            if cC.form_submit_button("Cancel"):
                st.session_state["sel_customer_label"] = "➕ New"
                _safe_rerun()
    
    # ============================ INVENTORY ============================
    with tabs[3]:
        st.markdown("<div class=''card'><div class=''card-header'>📦 Inventory</div>", unsafe_allow_html=True)
    
        inventory_id = _entity_selector(
            "Edit Item",
            "SELECT id, COALESCE(sku,'') AS sku, name FROM inventory ORDER BY name",
            lambda r: f"[{int(r['id'])}] {r['name']}" + (f" (SKU {r['sku']})" if r['sku'] else ""),
            key="sel_inventory"
        )
    
        i_defaults = dict(sku="",name="",description="",uom="",unit_cost=0.0,sell_price=0.0,qty_on_hand=0.0,notes="", job_ids=[])
        if inventory_id:
            row = _df("SELECT * FROM inventory WHERE id=%s", (inventory_id,))
            if not row.empty:
                for k in ["sku","name","description","uom","notes"]:
                    i_defaults[k] = row.iloc[0][k] or ""
                for k in ["unit_cost","sell_price","qty_on_hand"]:
                    i_defaults[k] = float(row.iloc[0][k] or 0.0)
                i_defaults["job_ids"] = _get_linked_job_ids("inventory_jobs","inventory_id", inventory_id)
    
        with st.form("inventory_form"):
            iid = inventory_id
            a,b,c = st.columns(3)
            i_sku = a.text_input("SKU (optional)", value=i_defaults["sku"], key=f"i_sku_{iid or 'new'}")
            i_name = b.text_input("Item Name *", value=i_defaults["name"], key=f"i_name_{iid or 'new'}")
            i_uom  = c.text_input("UoM (e.g., pcs, box)", value=i_defaults["uom"], key=f"i_uom_{iid or 'new'}")
    
            d,e,f = st.columns(3)
            i_unit_cost = d.number_input("Unit Cost", min_value=0.0, value=i_defaults["unit_cost"], step=0.01, key=f"i_uc_{iid or 'new'}")
            i_sell_price = e.number_input("Sell Price", min_value=0.0, value=i_defaults["sell_price"], step=0.01, key=f"i_sp_{iid or 'new'}")
            i_qty = f.number_input("Qty on Hand", min_value=0.0, value=i_defaults["qty_on_hand"], step=0.01, key=f"i_qty_{iid or 'new'}")
    
            i_desc  = st.text_area("Description", value=i_defaults["description"], key=f"i_desc_{iid or 'new'}")
            i_notes = st.text_area("Notes", value=i_defaults["notes"], key=f"i_notes_{iid or 'new'}")
            i_jobs  = _multiselect_job_ids(f"i_jobs_{iid or 'new'}", i_defaults["job_ids"])
    
            cS,cD,cC = st.columns([1,1,6])
            if cS.form_submit_button("Save"):
                _save_with_feedback(upsert_inventory, iid, i_sku, i_name, i_desc, i_uom,
                                    i_unit_cost, i_sell_price, i_qty, i_notes, i_jobs,
                                    success_msg="Inventory item saved")
            if iid and cD.form_submit_button("Delete"):
                _save_with_feedback(delete_inventory, iid, success_msg="Item deleted")
                st.session_state["sel_inventory_label"] = "➕ New"
            if cC.form_submit_button("Cancel"):
                st.session_state["sel_inventory_label"] = "➕ New"
                _safe_rerun()
    
    # ====================== CHART OF ACCOUNTS =========================
    with tabs[4]:
        st.markdown("<div class=''card'><div class=''card-header'>📚 Chart of Accounts</div>", unsafe_allow_html=True)
    
        account_id = _entity_selector(
            "Edit Account",
            "SELECT id, COALESCE(code,'') AS code, name, type FROM accounts ORDER BY name",
            lambda r: f"[{int(r['id'])}] {r['name']} — {r['type']}" + (f" ({r['code']})" if r['code'] else ""),
            key="sel_account"
        )
    
        a_defaults = dict(code="",name="",type="Asset",notes="")
        if account_id:
            row = _df("SELECT * FROM accounts WHERE id=%s", (account_id,))
            if not row.empty:
                a_defaults["code"] = row.iloc[0]["code"] or ""
                a_defaults["name"] = row.iloc[0]["name"] or ""
                a_defaults["type"] = row.iloc[0]["type"] or "Asset"
                a_defaults["notes"] = row.iloc[0]["notes"] or ""
    
        with st.form("accounts_form"):
            aid = account_id
            a,b,c = st.columns(3)
            acc_code = a.text_input("Account Code (optional)", value=a_defaults["code"], key=f"acc_code_{aid or 'new'}")
            acc_name = b.text_input("Account Name *", value=a_defaults["name"], key=f"acc_name_{aid or 'new'}")
            types = ["Asset","Liability","Equity","Revenue","Expense"]
            acc_type = c.selectbox("Type *", types,
                                   index=types.index(a_defaults["type"]) if a_defaults["type"] in types else 0,
                                   key=f"acc_type_{aid or 'new'}")
            acc_notes = st.text_area("Notes", value=a_defaults["notes"], key=f"acc_notes_{aid or 'new'}")
    
            cS,cD,cC = st.columns([1,1,6])
            if cS.form_submit_button("Save"):
                _save_with_feedback(upsert_account, aid, acc_code, acc_name, acc_type, acc_notes, success_msg="Account saved")
            if aid and cD.form_submit_button("Delete"):
                _save_with_feedback(delete_account, aid, success_msg="Account deleted")
                st.session_state["sel_account_label"] = "➕ New"
            if cC.form_submit_button("Cancel"):
                st.session_state["sel_account_label"] = "➕ New"
                _safe_rerun()
    
    # =============================== STAFF =============================
    with tabs[5]:
        st.markdown("<div class=''card'><div class=''card-header'>🧑‍💼 Staff</div>", unsafe_allow_html=True)
    
        staff_id = _entity_selector(
            "Edit Staff",
            "SELECT id, COALESCE(staff_code,'') AS staff_code, first_name, last_name, status FROM staff ORDER BY last_name, first_name",
            lambda r: f"[{int(r['id'])}] {r['first_name']} {r['last_name']} ({r['status']})" + (f" · {r['staff_code']}" if r['staff_code'] else ""),
            key="sel_staff"
        )
    
        s_defaults = dict(
            staff_code="", first_name="", last_name="", email="", phone="",
            address="", city="", state="", country="Nigeria", role="",
            department="", employment_type="Full-time", status="Active",
            bank_name="", bank_account="",
            ref_name="", ref_relationship="", ref_phone="", ref_email="", ref_address="",
            guar_name="", guar_relationship="", guar_phone="", guar_email="", guar_address="",
            notes="", job_ids=[]
        )
        if staff_id:
            row = _df("SELECT * FROM staff WHERE id=%s", (staff_id,))
            if not row.empty:
                for k in s_defaults.keys():
                    if k == "job_ids": continue
                    s_defaults[k] = row.iloc[0].get(k, s_defaults[k]) or s_defaults[k]
                s_defaults["job_ids"] = _get_linked_job_ids("staff_jobs","staff_id", staff_id)
    
        with st.form("staff_form"):
            sid = staff_id
            a,b,c = st.columns(3)
            s_code  = a.text_input("Staff Code (optional)", value=s_defaults["staff_code"], key=f"s_code_{sid or 'new'}")
            s_first = b.text_input("First Name *", value=s_defaults["first_name"], key=f"s_first_{sid or 'new'}")
            s_last  = c.text_input("Last Name *", value=s_defaults["last_name"], key=f"s_last_{sid or 'new'}")
    
            d,e,f = st.columns(3)
            s_email = d.text_input("Email", value=s_defaults["email"], key=f"s_email_{sid or 'new'}")
            s_phone = e.text_input("Phone", value=s_defaults["phone"], key=f"s_phone_{sid or 'new'}")
            s_role  = f.text_input("Role", value=s_defaults["role"], key=f"s_role_{sid or 'new'}")
    
            g,h,i = st.columns(3)
            s_dept = g.text_input("Department", value=s_defaults["department"], key=f"s_dept_{sid or 'new'}")
            emp_types = ["Full-time","Part-time","Contract"]
            s_emp_type = h.selectbox("Employment Type", emp_types,
                                     index=emp_types.index(s_defaults["employment_type"]) if s_defaults["employment_type"] in emp_types else 0,
                                     key=f"s_emp_{sid or 'new'}")
            statuses = ["Active","Inactive","On Leave","Terminated"]
            s_status = i.selectbox("Status", statuses,
                                   index=statuses.index(s_defaults["status"]) if s_defaults["status"] in statuses else 0,
                                   key=f"s_status_{sid or 'new'}")
    
            j,k,l,m = st.columns(4)
            s_addr = j.text_input("Address", value=s_defaults["address"], key=f"s_addr_{sid or 'new'}")
            s_city = k.text_input("City", value=s_defaults["city"], key=f"s_city_{sid or 'new'}")
            s_state = l.text_input("State", value=s_defaults["state"], key=f"s_state_{sid or 'new'}")
            s_country = m.text_input("Country", value=s_defaults["country"], key=f"s_country_{sid or 'new'}")
    
            # Staff bank details
            bn, ba = st.columns(2)
            s_bank_name = bn.text_input("Bank Name", value=s_defaults["bank_name"], key=f"s_bank_name_{sid or 'new'}")
            s_bank_acct = ba.text_input("Bank Account Number", value=s_defaults["bank_account"], key=f"s_bank_acct_{sid or 'new'}")
    
            st.markdown("**Reference**")
            r1,r2,r3 = st.columns(3)
            ref_name = r1.text_input("Ref. Name", value=s_defaults["ref_name"], key=f"ref_name_{sid or 'new'}")
            ref_rel  = r2.text_input("Relationship", value=s_defaults["ref_relationship"], key=f"ref_rel_{sid or 'new'}")
            ref_phone= r3.text_input("Ref. Phone", value=s_defaults["ref_phone"], key=f"ref_phone_{sid or 'new'}")
            r4,r5 = st.columns(2)
            ref_email   = r4.text_input("Ref. Email", value=s_defaults["ref_email"], key=f"ref_email_{sid or 'new'}")
            ref_address = r5.text_input("Ref. Address", value=s_defaults["ref_address"], key=f"ref_addr_{sid or 'new'}")
    
            st.markdown("**Guarantor**")
            g1,g2,g3 = st.columns(3)
            guar_name = g1.text_input("Guarantor Name", value=s_defaults["guar_name"], key=f"guar_name_{sid or 'new'}")
            guar_rel  = g2.text_input("Relationship", value=s_defaults["guar_relationship"], key=f"guar_rel_{sid or 'new'}")
            guar_phone= g3.text_input("Guarantor Phone", value=s_defaults["guar_phone"], key=f"guar_phone_{sid or 'new'}")
            g4,g5 = st.columns(2)
            guar_email   = g4.text_input("Guarantor Email", value=s_defaults["guar_email"], key=f"guar_email_{sid or 'new'}")
            guar_address = g5.text_input("Guarantor Address", value=s_defaults["guar_address"], key=f"guar_addr_{sid or 'new'}")
    
            s_notes = st.text_area("Notes", value=s_defaults["notes"], key=f"s_notes_{sid or 'new'}")
            s_jobs = _multiselect_job_ids(f"s_jobs_{sid or 'new'}", s_defaults["job_ids"])
    
            cS,cD,cC = st.columns([1,1,6])
            if cS.form_submit_button("Save"):
                _save_with_feedback(
                    upsert_staff, sid, s_code, s_first, s_last, s_email, s_phone,
                    s_addr, s_city, s_state, s_country, s_role, s_dept, s_emp_type, s_status,
                    s_bank_name, s_bank_acct,
                    ref_name, ref_rel, ref_phone, ref_email, ref_address,
                    guar_name, guar_rel, guar_phone, guar_email, guar_address,
                    s_notes, s_jobs, success_msg="Staff saved"
                )
            if sid and cD.form_submit_button("Delete"):
                _save_with_feedback(delete_staff, sid, success_msg="Staff deleted")
                st.session_state["sel_staff_label"] = "➕ New"
            if cC.form_submit_button("Cancel"):
                st.session_state["sel_staff_label"] = "➕ New"
                _safe_rerun()
    
        # Staff attachments area (only when editing an existing staff)
        if staff_id:
            st.markdown("---")
            st.markdown("**Attachments**")
            up_col1, up_col2 = st.columns([3,2])
            doc_type = up_col1.selectbox("Document Type", ["CV","ID","Offer Letter","Contract","Reference Letter","Other"],
                                         key=f"doc_type_{int(staff_id)}")
            uploads = up_col2.file_uploader("Upload files", type=None, accept_multiple_files=True,
                                            key=f"uploader_{int(staff_id)}")
            if st.button("Save Uploads", key=f"save_uploads_{int(staff_id)}"):
                if uploads:
                    errs = []
                    for f in uploads:
                        data = f.read()
                        err = add_staff_document(int(staff_id), doc_type, f.name, data)
                        if err: errs.append(f"{f.name}: {err}")
                    if errs:
                        st.error("Some files failed:\n- " + "\n- ".join(errs))
                    else:
                        st.success("Files uploaded")
                        _safe_rerun()
                else:
                    st.info("No files selected.")
            docs = _df("SELECT id, doc_type, original_name, stored_path, uploaded_at FROM staff_documents WHERE staff_id=%s ORDER BY id DESC",
                       (int(staff_id),))
            if docs.empty:
                st.caption("No documents uploaded.")
            else:
                for _, drow in docs.iterrows():
                    with st.container():
                        pill = f"<span class=''doc-pill'>{drow['doc_type'] if drow['doc_type'] else 'Document'}</span>"
                        st.markdown(f"{pill} **{drow['original_name']}**  ·  _{drow['uploaded_at']}_", unsafe_allow_html=True)
                        colA, colB = st.columns([1,1])
                        try:
                            file_bytes = None
                            if os.path.exists(drow["stored_path"]):
                                with open(drow["stored_path"], "rb") as f:
                                    file_bytes = f.read()
                            if file_bytes:
                                colA.download_button(
                                    "Download",
                                    data=file_bytes,
                                    file_name=os.path.basename(drow["stored_path"]),
                                    key=f"dl_{int(drow['id'])}"
                                )
                            else:
                                colA.caption("File missing on disk")
                        except Exception as ex:
                            colA.caption(f"Cannot read file: {ex}")
                        if colB.button("Delete", key=f"del_doc_{int(drow['id'])}"):
                            _save_with_feedback(delete_staff_document, int(drow["id"]), success_msg="Document deleted")
    
    # =============================== REPORTS ==========================
    with tabs[6]:
        st.markdown("<div class='card'><div class='card-header'>📊 Reports (All Tables)</div>", unsafe_allow_html=True)
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Jobs", int(_df("SELECT COUNT(*) c FROM jobs")["c"].iloc[0]))
        c2.metric("Vendors", int(_df("SELECT COUNT(*) c FROM vendors")["c"].iloc[0]))
        c3.metric("Customers", int(_df("SELECT COUNT(*) c FROM customers")["c"].iloc[0]))
        c4.metric("Inventory", int(_df("SELECT COUNT(*) c FROM inventory")["c"].iloc[0]))
        c5.metric("Accounts", int(_df("SELECT COUNT(*) c FROM accounts")["c"].iloc[0]))
        c6.metric("Staff", int(_df("SELECT COUNT(*) c FROM staff")["c"].iloc[0]))

        st.divider()
        st.markdown("### Export all to Excel")
        if st.button("Generate Excel Export", key="export_btn"):
            out = BytesIO()
            with pd.ExcelWriter(out, engine="openpyxl") as writer:

                def _safe_write(df: pd.DataFrame, name: str):
                    if df.empty:
                        pd.DataFrame(columns=["Empty"]).to_excel(writer, sheet_name=name, index=False)
                    else:
                        # Excel does not support timezone-aware datetimes, strip tz info if present
                        try:
                            dt_tz_cols = df.select_dtypes(include=["datetimetz"]).columns
                            for col in dt_tz_cols:
                                df[col] = df[col].dt.tz_convert(None)
                        except Exception:
                            pass
                        df.to_excel(writer, sheet_name=name, index=False)

                _safe_write(_df("SELECT * FROM jobs ORDER BY id"), "Jobs")

                _safe_write(
                    _df(
                        """
                        SELECT v.*,
                               (
                                 SELECT string_agg(j.title, ', ')
                                 FROM vendor_jobs vj
                                 JOIN jobs j ON j.id = vj.job_id
                                 WHERE vj.vendor_id = v.id
                               ) AS jobs
                        FROM vendors v
                        ORDER BY v.id
                        """
                    ),
                    "Vendors",
                )

                _safe_write(
                    _df(
                        """
                        SELECT c.*,
                               (
                                 SELECT string_agg(j.title, ', ')
                                 FROM customer_jobs cj
                                 JOIN jobs j ON j.id = cj.job_id
                                 WHERE cj.customer_id = c.id
                               ) AS jobs
                        FROM customers c
                        ORDER BY c.id
                        """
                    ),
                    "Customers",
                )

                _safe_write(
                    _df(
                        """
                        SELECT i.*,
                               (
                                 SELECT string_agg(j.title, ', ')
                                 FROM inventory_jobs ij
                                 JOIN jobs j ON j.id = ij.job_id
                                 WHERE ij.inventory_id = i.id
                               ) AS jobs
                        FROM inventory i
                        ORDER BY i.id
                        """
                    ),
                    "Inventory",
                )

                _safe_write(_df("SELECT * FROM accounts ORDER BY id"), "Accounts")

                _safe_write(
                    _df(
                        """
                        SELECT s.*,
                               (SELECT COUNT(*) FROM staff_documents d WHERE d.staff_id = s.id) AS doc_count,
                               (
                                 SELECT string_agg(j.title, ', ')
                                 FROM staff_jobs sj
                                 JOIN jobs j ON j.id = sj.job_id
                                 WHERE sj.staff_id = s.id
                               ) AS jobs
                        FROM staff s
                        ORDER BY s.id
                        """
                    ),
                    "Staff",
                )

                _safe_write(_df("SELECT * FROM staff_documents ORDER BY id"), "StaffDocs")
                _safe_write(_df("SELECT * FROM audit_log ORDER BY id DESC"), "Audit")

            st.download_button(
                "Download Excel",
                data=out.getvalue(),
                file_name=f"crm_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        st.divider()
        st.markdown("### Jobs")
        dfj = _df(
            "SELECT id, code, title, status, COALESCE(description, '') AS description, updated_at "
            "FROM jobs ORDER BY id DESC"
        )
        _list_with_search(dfj, ["code", "title", "status", "description"], key_prefix="jobs_list")

        st.markdown("### Vendors")
        vdf = _df(
            """
            SELECT v.*,
                   (
                     SELECT string_agg(j.title, ', ')
                     FROM vendor_jobs vj
                     JOIN jobs j ON j.id = vj.job_id
                     WHERE vj.vendor_id = v.id
                   ) AS jobs
            FROM vendors v
            ORDER BY v.id DESC
            """
        )
        _list_with_search(
            vdf[["id", "name", "email", "phone", "city", "state", "country", "jobs", "updated_at"]],
            ["name", "email", "phone", "city", "state", "country", "jobs"],
            key_prefix="vdf",
        )

        st.markdown("### Customers")
        cdf = _df(
            """
            SELECT c.*,
                   (
                     SELECT string_agg(j.title, ', ')
                     FROM customer_jobs cj
                     JOIN jobs j ON j.id = cj.job_id
                     WHERE cj.customer_id = c.id
                   ) AS jobs
            FROM customers c
            ORDER BY c.id DESC
            """
        )
        _list_with_search(
            cdf[["id", "name", "email", "phone", "city", "state", "country", "jobs", "updated_at"]],
            ["name", "email", "phone", "city", "state", "country", "jobs"],
            key_prefix="cdf",
        )

        st.markdown("### Inventory")
        idf = _df(
            """
            SELECT i.*,
                   (
                     SELECT string_agg(j.title, ', ')
                     FROM inventory_jobs ij
                     JOIN jobs j ON j.id = ij.job_id
                     WHERE ij.inventory_id = i.id
                   ) AS jobs
            FROM inventory i
            ORDER BY i.id DESC
            """
        )
        _list_with_search(
            idf[["id", "sku", "name", "unit_cost", "sell_price", "qty_on_hand", "jobs", "updated_at"]],
            ["sku", "name", "jobs"],
            key_prefix="idf",
        )

        st.markdown("### Accounts")
        adf = _df(
            "SELECT id, code, name, type, COALESCE(notes, '') AS notes, updated_at "
            "FROM accounts ORDER BY id DESC"
        )
        _list_with_search(adf, ["code", "name", "type", "notes"], key_prefix="adf")

        st.markdown("### Staff")
        _exec("""CREATE TABLE IF NOT EXISTS staff_jobs( staff_id INTEGER, job_id INTEGER, PRIMARY KEY(staff_id, job_id));""")
        sdf = _df(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM staff_documents d WHERE d.staff_id = s.id) AS doc_count,
                   (
                     SELECT string_agg(j.title, ', ')
                     FROM staff_jobs sj
                     JOIN jobs j ON j.id = sj.job_id
                     WHERE sj.staff_id = s.id
                   ) AS jobs
            FROM staff s
            ORDER BY s.id DESC
            """
        )
        _list_with_search(
            sdf[
                [
                    "id",
                    "staff_code",
                    "first_name",
                    "last_name",
                    "email",
                    "phone",
                    "department",
                    "employment_type",
                    "status",
                    "jobs",
                    "doc_count",
                    "updated_at",
                ]
            ],
            ["staff_code", "first_name", "last_name", "email", "phone", "department", "employment_type", "status", "jobs"],
            key_prefix="sdf",
        )

        st.divider()
        st.markdown("### By Job — Linked Counts")
        df_job_rollup = _df(
            """
            WITH v AS (SELECT job_id, COUNT(*) n FROM vendor_jobs GROUP BY job_id),
                 c AS (SELECT job_id, COUNT(*) n FROM customer_jobs GROUP BY job_id),
                 i AS (SELECT job_id, COUNT(*) n FROM inventory_jobs GROUP BY job_id),
                 s AS (SELECT job_id, COUNT(*) n FROM staff_jobs GROUP BY job_id)
            SELECT j.id, j.code, j.title,
                   COALESCE(v.n, 0) AS vendors,
                   COALESCE(c.n, 0) AS customers,
                   COALESCE(i.n, 0) AS items,
                   COALESCE(s.n, 0) AS staff
            FROM jobs j
            LEFT JOIN v ON v.job_id = j.id
            LEFT JOIN c ON c.job_id = j.id
            LEFT JOIN i ON i.job_id = j.id
            LEFT JOIN s ON s.job_id = j.id
            ORDER BY j.title
            """
        )
        st.dataframe(df_job_rollup, use_container_width=True)
