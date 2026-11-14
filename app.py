from abc import ABC
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import random
import secrets
import sqlite3
import os
import shutil
import tempfile
import re
import json
import html
from datetime import datetime as _DT, timedelta as _TD, timezone as _TZ

app = Flask(__name__)
# NOTE: Production-e environment variable diye set korben (SECRET_KEY, ADMIN_IDENTIFIER, ADMIN_PASSWORD)
app.secret_key = os.environ.get('SECRET_KEY', 'opsnexus_secret_key')

ADMIN_IDENTIFIER = os.environ.get('ADMIN_IDENTIFIER', "tanvir.hassan7242@gmail.com")
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', "T@nv1r#2022")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(APP_DIR, 'fu_infotech.db')
if os.environ.get('VERCEL') == '1':
    DB_NAME = os.path.join(tempfile.gettempdir(), 'fu_infotech.db')
    if not os.path.exists(DB_NAME):
        shutil.copy2(os.path.join(APP_DIR, 'fu_infotech.db'), DB_NAME)
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


class execute:
    """Simple command executor with validation and standard result handling."""

    def __init__(self, on_error=None):
        self.on_error = on_error or self._default_error_handler

    def validate(self, payload):
        if payload is None:
            raise ValueError('Payload is required.')
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, (list, tuple)):
            return {'items': list(payload)}
        return {'value': payload}

    def run(self, payload):
        prepared = self.validate(payload)
        if not isinstance(prepared, dict):
            raise TypeError('Execution payload must be a dictionary-like object.')

        action = prepared.get('action') or prepared.get('command') or prepared.get('name')
        if not action:
            raise ValueError('No action/command name was provided for execution.')

        params = {key: value for key, value in prepared.items() if key not in {'action', 'command', 'name'}}
        return {'success': True, 'action': action, 'params': params}

    def execute(self, payload):
        try:
            normalized = self.validate(payload)
            result = self.run(normalized)
            if isinstance(result, dict):
                result.setdefault('success', True)
            return result
        except Exception as exc:
            return self.on_error(exc, payload)

    def _default_error_handler(self, exc, payload):
        return {'success': False, 'error': str(exc), 'payload': payload}


# ---------------------------------------------------------------------------
# Helper functions (XSS protection / validation)
# ---------------------------------------------------------------------------
def esc(value):
    """HTML escape for text and attribute values."""
    return html.escape('' if value is None else str(value), quote=True)


def js_attr(value):
    """Safe JS literal to be used inside an HTML attribute (onclick="...")."""
    return html.escape(json.dumps(value), quote=True)


def js_str(value):
    """Safe JS string literal to be used inside a <script> block."""
    return json.dumps(value).replace('</', '<\\/')


def safe_date(value):
    value = (value or '').strip()
    return value if DATE_RE.match(value) else ''


def fmt_msg(msg):
    """Escape message but keep the harmless <b></b> tags used by the app."""
    m = html.escape(msg or '')
    return m.replace('&lt;b&gt;', '<b>').replace('&lt;/b&gt;', '</b>')


def popup_info(msg):
    """Return (icon, title, text) for the centered popup, matched to the action."""
    m = (msg or '').strip()
    low = m.lower()
    if low.startswith('error') or low.startswith('invalid') or low == 'requisition not found!':
        text = m.split(':', 1)[1].strip() if low.startswith('error:') else m
        return 'error', 'Action Failed!', text
    if low.startswith('no ') and 'selected' in low:
        return 'warning', 'Nothing Selected!', m
    if 'added successfully' in low:
        return 'success', 'Member Added!', m
    if low.startswith('balance updated'):
        return 'success', 'Balance Updated!', m
    if low.startswith('profile settings'):
        return 'success', 'Profile Updated!', m
    if 'submitted successfully' in low:
        return 'success', 'Requisition Submitted!', m
    if low.startswith('requisition') and 'rejected' in low:
        return 'error', 'Requisition Rejected!', m
    if low.startswith('requisition') and 'approved' in low:
        return 'success', 'Requisition Approved!', m
    if low.startswith('requisition') and 'pending' in low:
        return 'info', 'Status Changed!', m
    if low.startswith('requisition') and 'updated' in low:
        return 'success', 'Requisition Updated!', m
    return 'success', 'Success!', m


def to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def is_owner():
    """The fixed owner account: logs in as HR post but has full (admin-level) access."""
    return bool(session.get('logged_in') and session.get('owner')
                and (session.get('user') or '').lower() == ADMIN_IDENTIFIER.lower())


def is_full():
    """Full access = real Admin role OR the fixed owner (HR post)."""
    return bool(session.get('logged_in') and (session.get('role') == 'Admin' or is_owner()))


REPORT_META = [
    ('mobile_bill', '📱', 'Monthly Mobile Bill Report', 'Member-wise mobile balance, usage and bill updates for a month'),
    ('requisition_stats', '📊', 'Monthly Requisition Statistics', 'Approved / pending / rejected counts and type-wise breakdown'),
    ('user_report', '👤', 'User Management Report', 'All system users, roles, status and new accounts'),
    ('balance_report', '💰', 'Operator & Balance Report', 'Operator-wise members, balance, usage and low-balance list'),
    ('activity_report', '🕒', 'Activity Log Report', 'Everything that happened in the system during a month'),
]

def column_exists(cursor, table, column):
    return any(row[1] == column for row in cursor.execute(f"PRAGMA table_info({table})").fetchall())


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            designation TEXT NOT NULL,
            operator TEXT NOT NULL,
            balance INTEGER DEFAULT 0,
            used INTEGER DEFAULT 0,
            created_date DATE DEFAULT CURRENT_DATE
        )
    ''')
    # SQLite does not allow ADD COLUMN with a non-constant default, so add plain column then fill it
    if not column_exists(cursor, 'employees', 'created_date'):
        cursor.execute("ALTER TABLE employees ADD COLUMN created_date DATE")
        cursor.execute("UPDATE employees SET created_date = date('now') WHERE created_date IS NULL")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS requisitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            emp_name TEXT NOT NULL,
            type TEXT NOT NULL,
            details TEXT NOT NULL,
            status TEXT DEFAULT 'Pending',
            created_date DATE DEFAULT CURRENT_DATE,
            remark TEXT
        )
    ''')
    if not column_exists(cursor, 'requisitions', 'created_date'):
        cursor.execute("ALTER TABLE requisitions ADD COLUMN created_date DATE")
        cursor.execute("UPDATE requisitions SET created_date = date('now') WHERE created_date IS NULL")
    # Reject remark column (auto-migrates old databases without losing data)
    if not column_exists(cursor, 'requisitions', 'remark'):
        cursor.execute("ALTER TABLE requisitions ADD COLUMN remark TEXT")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS app_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            user_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
            email TEXT UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('Admin','HR','Member')),
            status TEXT NOT NULL DEFAULT 'Active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            approved_by TEXT,
            approved_at DATETIME
        )
    ''')
    schema_row = cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='app_users'").fetchone()
    if schema_row and "'HR'" not in (schema_row[0] or ""):
        cursor.execute('''
            CREATE TABLE app_users_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                user_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('Admin','HR','Member')),
                status TEXT NOT NULL DEFAULT 'Active',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                approved_by TEXT,
                approved_at DATETIME
            )
        ''')
        cursor.execute('''INSERT INTO app_users_new
            (id,full_name,user_id,email,password_hash,role,status,created_at)
            SELECT id,full_name,user_id,email,password_hash,role,status,created_at FROM app_users''')
        cursor.execute('DROP TABLE app_users')
        cursor.execute('ALTER TABLE app_users_new RENAME TO app_users')
    if not column_exists(cursor, 'app_users', 'approved_by'):
        cursor.execute("ALTER TABLE app_users ADD COLUMN approved_by TEXT")
    if not column_exists(cursor, 'app_users', 'approved_at'):
        cursor.execute("ALTER TABLE app_users ADD COLUMN approved_at DATETIME")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # --- Forgot password / self sign-up: OTP is delivered to HR via notification bell ---
    if not column_exists(cursor, 'app_users', 'phone'):
        cursor.execute("ALTER TABLE app_users ADD COLUMN phone TEXT")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS auth_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            purpose TEXT NOT NULL,
            full_name TEXT,
            email TEXT,from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import random
import secrets
import sqlite3
import os
import shutil
import tempfile
import re
import json
import html
from datetime import datetime as _DT, timedelta as _TD, timezone as _TZ

app = Flask(__name__)
# NOTE: Production-e environment variable diye set korben (SECRET_KEY, ADMIN_IDENTIFIER, ADMIN_PASSWORD)
app.secret_key = os.environ.get('SECRET_KEY', 'opsnexus_secret_key')

ADMIN_IDENTIFIER = os.environ.get('ADMIN_IDENTIFIER', "tanvir.hassan7242@gmail.com")
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', "T@nv1r#2022")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(APP_DIR, 'fu_infotech.db')
if os.environ.get('VERCEL') == '1':
    DB_NAME = os.path.join(tempfile.gettempdir(), 'fu_infotech.db')
    if not os.path.exists(DB_NAME):
        shutil.copy2(os.path.join(APP_DIR, 'fu_infotech.db'), DB_NAME)
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


# ---------------------------------------------------------------------------
# Helper functions (XSS protection / validation)
# ---------------------------------------------------------------------------
def esc(value):
    """HTML escape for text and attribute values."""
    return html.escape('' if value is None else str(value), quote=True)


def js_attr(value):
    """Safe JS literal to be used inside an HTML attribute (onclick="...")."""
    return html.escape(json.dumps(value), quote=True)


def js_str(value):
    """Safe JS string literal to be used inside a <script> block."""
    return json.dumps(value).replace('</', '<\\/')


def safe_date(value):
    value = (value or '').strip()
    return value if DATE_RE.match(value) else ''


def fmt_msg(msg):
    """Escape message but keep the harmless <b></b> tags used by the app."""
    m = html.escape(msg or '')
    return m.replace('&lt;b&gt;', '<b>').replace('&lt;/b&gt;', '</b>')


def popup_info(msg):
    """Return (icon, title, text) for the centered popup, matched to the action."""
    m = (msg or '').strip()
    low = m.lower()
    if low.startswith('error') or low.startswith('invalid') or low == 'requisition not found!':
        text = m.split(':', 1)[1].strip() if low.startswith('error:') else m
        return 'error', 'Action Failed!', text
    if low.startswith('no ') and 'selected' in low:
        return 'warning', 'Nothing Selected!', m
    if 'added successfully' in low:
        return 'success', 'Member Added!', m
    if low.startswith('balance updated'):
        return 'success', 'Balance Updated!', m
    if low.startswith('profile settings'):
        return 'success', 'Profile Updated!', m
    if 'submitted successfully' in low:
        return 'success', 'Requisition Submitted!', m
    if low.startswith('requisition') and 'rejected' in low:
        return 'error', 'Requisition Rejected!', m
    if low.startswith('requisition') and 'approved' in low:
        return 'success', 'Requisition Approved!', m
    if low.startswith('requisition') and 'pending' in low:
        return 'info', 'Status Changed!', m
    if low.startswith('requisition') and 'updated' in low:
        return 'success', 'Requisition Updated!', m
    return 'success', 'Success!', m


def to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def is_owner():
    """The fixed owner account: logs in as HR post but has full (admin-level) access."""
    return bool(session.get('logged_in') and session.get('owner')
                and (session.get('user') or '').lower() == ADMIN_IDENTIFIER.lower())


def is_full():
    """Full access = real Admin role OR the fixed owner (HR post)."""
    return bool(session.get('logged_in') and (session.get('role') == 'Admin' or is_owner()))


REPORT_META = [
    ('mobile_bill', '📱', 'Monthly Mobile Bill Report', 'Member-wise mobile balance, usage and bill updates for a month'),
    ('requisition_stats', '📊', 'Monthly Requisition Statistics', 'Approved / pending / rejected counts and type-wise breakdown'),
    ('user_report', '👤', 'User Management Report', 'All system users, roles, status and new accounts'),
    ('balance_report', '💰', 'Operator & Balance Report', 'Operator-wise members, balance, usage and low-balance list'),
    ('activity_report', '🕒', 'Activity Log Report', 'Everything that happened in the system during a month'),
]


# ---------------------------------------------------------------------------
# RECYCLE BIN (HR only): every deleted user / member / requisition is kept here
# with full details and who deleted it.
# ---------------------------------------------------------------------------
def recycle_ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS recycle_bin (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_type TEXT NOT NULL,
        item_ref INTEGER,
        item_title TEXT,
        item_data TEXT,
        deleted_by_name TEXT,
        deleted_by_role TEXT,
        deleted_by_uid TEXT,
        deleted_by_email TEXT,
        deleted_at TEXT
    )""")

def recycle_add(conn, item_type, item_ref, title, data):
    """Store a snapshot of a deleted record. `data` is a dict (never include password hashes)."""
    recycle_ensure(conn)
    now = _DT.now(_TZ(_TD(hours=6))).strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        "INSERT INTO recycle_bin (item_type,item_ref,item_title,item_data,deleted_by_name,deleted_by_role,deleted_by_uid,deleted_by_email,deleted_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (item_type, item_ref, title, json.dumps(data, ensure_ascii=False, default=str),
         session.get('name') or '', session.get('role') or '', session.get('user_id') or '',
         session.get('user') or '', now))

def column_exists(cursor, table, column):
    return any(row[1] == column for row in cursor.execute(f"PRAGMA table_info({table})").fetchall())


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            designation TEXT NOT NULL,
            operator TEXT NOT NULL,
            balance INTEGER DEFAULT 0,
            used INTEGER DEFAULT 0,
            created_date DATE DEFAULT CURRENT_DATE
        )
    ''')
    # SQLite does not allow ADD COLUMN with a non-constant default, so add plain column then fill it
    if not column_exists(cursor, 'employees', 'created_date'):
        cursor.execute("ALTER TABLE employees ADD COLUMN created_date DATE")
        cursor.execute("UPDATE employees SET created_date = date('now') WHERE created_date IS NULL")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS requisitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            emp_name TEXT NOT NULL,
            type TEXT NOT NULL,
            details TEXT NOT NULL,
            status TEXT DEFAULT 'Pending',
            created_date DATE DEFAULT CURRENT_DATE,
            remark TEXT
        )
    ''')
    if not column_exists(cursor, 'requisitions', 'created_date'):
        cursor.execute("ALTER TABLE requisitions ADD COLUMN created_date DATE")
        cursor.execute("UPDATE requisitions SET created_date = date('now') WHERE created_date IS NULL")
    # Reject remark column (auto-migrates old databases without losing data)
    if not column_exists(cursor, 'requisitions', 'remark'):
        cursor.execute("ALTER TABLE requisitions ADD COLUMN remark TEXT")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS app_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            user_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
            email TEXT UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('Admin','HR','Member')),
            status TEXT NOT NULL DEFAULT 'Active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            approved_by TEXT,
            approved_at DATETIME
        )
    ''')
    schema_row = cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='app_users'").fetchone()
    if schema_row and "'HR'" not in (schema_row[0] or ""):
        cursor.execute('''
            CREATE TABLE app_users_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                user_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('Admin','HR','Member')),
                status TEXT NOT NULL DEFAULT 'Active',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                approved_by TEXT,
                approved_at DATETIME
            )
        ''')
        cursor.execute('''INSERT INTO app_users_new
            (id,full_name,user_id,email,password_hash,role,status,created_at)
            SELECT id,full_name,user_id,email,password_hash,role,status,created_at FROM app_users''')
        cursor.execute('DROP TABLE app_users')
        cursor.execute('ALTER TABLE app_users_new RENAME TO app_users')
    if not column_exists(cursor, 'app_users', 'approved_by'):
        cursor.execute("ALTER TABLE app_users ADD COLUMN approved_by TEXT")
    if not column_exists(cursor, 'app_users', 'approved_at'):
        cursor.execute("ALTER TABLE app_users ADD COLUMN approved_at DATETIME")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # --- Forgot password / self sign-up: OTP is delivered to HR via notification bell ---
    if not column_exists(cursor, 'app_users', 'phone'):
        cursor.execute("ALTER TABLE app_users ADD COLUMN phone TEXT")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS auth_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            purpose TEXT NOT NULL,
            full_name TEXT,
            email TEXT,
            phone TEXT,
            password_hash TEXT,
            target_user_id INTEGER,
            otp TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hr_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            request_id INTEGER,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Owner account is permanently on the HR post (no data is deleted, only the role value is set)
    cursor.execute("UPDATE app_users SET role='HR' WHERE (email=? COLLATE NOCASE OR user_id=? COLLATE NOCASE) AND role!='HR'",
                   (ADMIN_IDENTIFIER, ADMIN_IDENTIFIER))
    conn.commit()
    conn.close()

init_db()
DEFAULT_SETTINGS={'company_name':'OpsNexus','currency_symbol':'৳','date_format':'DD-MM-YYYY','rows_per_page':'10','active_operators':['GP','Robi','Banglalink','Airtel','Teletalk'],'operator_prefixes':{'GP':'017,013','Robi':'018','Banglalink':'019,014','Airtel':'016','Teletalk':'015'},'default_balance':'0','min_balance':'0','max_balance':'100000','low_balance_limit':'100','designations':'Executive, Manager, Officer, Staff','allow_duplicate_phone':False,'default_member_status':'active','requisition_types':'Phone Bill Increase, Accessories, Others','reject_remark_required':True,'max_requisition_amount':'50000','approval_roles':['Admin','HR'],'otp_expiry':'5','session_timeout':'30','wrong_login_limit':'5','otp_login_enabled':True,'notifications_enabled':True,'requisition_notifications':True,'low_balance_notifications':True,'log_retention_days':'90','print_header':'Official Company Report','print_footer':'System Generated Report','show_prepared_by':True,'show_approved_by':True,'theme_mode':'light','admin_password':''}
def load_settings():
 c=sqlite3.connect(DB_NAME); c.execute('CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
 rows=c.execute('SELECT key,value FROM app_settings').fetchall(); c.close(); d=dict(DEFAULT_SETTINGS)
 for k,v in rows:
  try:d[k]=json.loads(v)
  except (ValueError,TypeError):d[k]=v
 return d
def save_settings(d):
 c=sqlite3.connect(DB_NAME); c.execute('CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
 for k,v in d.items(): c.execute('INSERT INTO app_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,json.dumps(v,ensure_ascii=False)))
 c.commit(); c.close()
otp_storage = {}

# ======================= PER-USER THEME SYSTEM =======================
# Each user (Admin / HR / Member) picks their own theme. Stored in a NEW table
# (user_themes) so no existing data is touched.
def _t(name, desc, dark, bg, surface, surface2, border, inborder, text, text2, muted, accent, hover, soft, head, onaccent='#ffffff', inp=None):
    return dict(name=name, desc=desc, dark=dark, bg=bg, surface=surface, surface2=surface2, border=border,
                inborder=inborder, text=text, text2=text2, muted=muted, accent=accent, hover=hover, soft=soft,
                head=head, onaccent=onaccent, inp=inp or (surface2 if dark else surface))

THEMES = {
    'default':  _t('Classic Blue',   'Original clean look',          False, '#f4f6f9', '#ffffff', '#f8fafc', '#e2e8f0', '#cbd5e1', '#1e293b', '#475569', '#64748b', '#2563eb', '#1d4ed8', '#eff6ff', '#1e293b'),
    'ocean':    _t('Ocean Breeze',   'Fresh cyan & aqua tones',      False, '#ecfeff', '#ffffff', '#f0fdfa', '#cffafe', '#a5d8e6', '#0f172a', '#334155', '#64748b', '#0891b2', '#0e7490', '#cffafe', '#164e63'),
    'emerald':  _t('Emerald Fresh',  'Calm natural green',           False, '#f0fdf4', '#ffffff', '#f7fef9', '#d1fae5', '#a7dfc3', '#0f172a', '#334155', '#64748b', '#059669', '#047857', '#d1fae5', '#064e3b'),
    'violet':   _t('Royal Violet',   'Elegant purple accents',       False, '#f5f3ff', '#ffffff', '#faf5ff', '#ddd6fe', '#c4b5fd', '#1e1b4b', '#3f3a6b', '#6b6896', '#7c3aed', '#6d28d9', '#ede9fe', '#3b0764'),
    'sunset':   _t('Sunset Orange',  'Warm & energetic orange',      False, '#fff7ed', '#ffffff', '#fffaf5', '#fed7aa', '#fdba74', '#1c1917', '#44403c', '#78716c', '#ea580c', '#c2410c', '#ffedd5', '#7c2d12'),
    'rose':     _t('Rose Blossom',   'Soft pink & crimson',          False, '#fff1f2', '#ffffff', '#fff5f6', '#fecdd3', '#fda4af', '#1f1315', '#4c2a30', '#7a5a60', '#e11d48', '#be123c', '#ffe4e6', '#881337'),
    'midnight': _t('Midnight Dark',  'Deep navy dark mode',          True,  '#0b1220', '#111a2e', '#16213a', '#25324d', '#334463', '#e5edf9', '#cbd5e1', '#8b9bb8', '#3b82f6', '#2563eb', '#1b2a4d', '#0a1020'),
    'charcoal': _t('Charcoal Amber', 'Neutral black with amber',     True,  '#121212', '#1c1c1e', '#252528', '#333336', '#46464a', '#f4f4f5', '#d4d4d8', '#a1a1aa', '#f59e0b', '#d97706', '#3a2e12', '#0d0d0e', onaccent='#111111'),
    'forest':   _t('Deep Forest',    'Dark mode with forest green',  True,  '#0c1511', '#12201a', '#182a22', '#234034', '#2f5543', '#e6f4ec', '#c4dccf', '#86a898', '#22c55e', '#16a34a', '#14352a', '#08110d', onaccent='#052e16'),
    'aurora':   _t('Aurora Night',   'Dark mode with purple glow',   True,  '#120d1f', '#1a1330', '#221a3d', '#34285a', '#473a75', '#f0eafc', '#d6cdf0', '#a699c9', '#a855f7', '#9333ea', '#2d1f52', '#0e0a1a'),
    'indigo':   _t('Indigo Classic',  'Professional indigo & white',    False, '#f5f6ff', '#ffffff', '#f8f9ff', '#e0e3fa', '#c7cbf0', '#1e2142', '#3f4373', '#6b6f99', '#4f46e5', '#4338ca', '#e0e7ff', '#312e81'),
    'teal':     _t('Teal Corporate',  'Calm business teal',             False, '#f2f8f8', '#ffffff', '#f6fbfb', '#d5e7e8', '#b6d3d5', '#0f2a2e', '#35565a', '#668084', '#0f766e', '#115e59', '#ccfbf1', '#134e4a'),
    'sand':     _t('Desert Sand',     'Warm beige with copper accent',  False, '#faf6ef', '#fffdf9', '#fbf7f0', '#eadfcb', '#d9c8a8', '#2b2418', '#5a4d38', '#8a7b62', '#b45309', '#92400e', '#fdebc8', '#78350f'),
    'slate':    _t('Cool Slate',      'Minimal grey, executive look',   False, '#eef1f5', '#ffffff', '#f4f6f9', '#d9dee6', '#bcc4d1', '#111827', '#374151', '#6b7280', '#334155', '#1e293b', '#e2e8f0', '#0f172a'),
    'orchid':   _t('Orchid Pink',     'Vibrant fuchsia on light',       False, '#fdf4ff', '#ffffff', '#fef7ff', '#f3d9fa', '#e9b8f5', '#2a1033', '#5b3a66', '#8b6a96', '#c026d3', '#a21caf', '#fae8ff', '#701a75'),
    'obsidian': _t('Obsidian Black',  'Pure black with cyan accent',    True,  '#050505', '#0d0d0f', '#151518', '#26262b', '#3a3a41', '#f5f5f7', '#d1d1d6', '#8e8e96', '#06b6d4', '#0891b2', '#0c2f38', '#000000', onaccent='#04181c'),
    'deepsea':  _t('Deep Sea',        'Dark ocean teal',                True,  '#07181c', '#0c2329', '#112e35', '#1c4650', '#28606c', '#e3f5f7', '#bcdfe3', '#7fa9b0', '#14b8a6', '#0d9488', '#0f3d3b', '#04100f', onaccent='#031a17'),
    'crimson':  _t('Crimson Night',   'Dark mode with red highlights',  True,  '#150a0c', '#1f1013', '#29161a', '#43232a', '#5c3039', '#fbeaec', '#e6c3c8', '#b08a90', '#ef4444', '#dc2626', '#3d1519', '#0d0507'),
    'espresso': _t('Espresso Copper', 'Dark coffee with copper tone',   True,  '#140f0b', '#1e1712', '#281f18', '#3d3025', '#54432f', '#f6efe7', '#dccdb9', '#a8967f', '#d6a05a', '#c28a40', '#3a2b17', '#0c0805', onaccent='#1a1006'),
    'nord':     _t('Nordic Frost',    'Cool arctic dark palette',       True,  '#242933', '#2e3440', '#353c4a', '#434c5e', '#556079', '#eceff4', '#d8dee9', '#9aa5b8', '#88c0d0', '#6fb0c2', '#3b4a56', '#1c2028', onaccent='#1b2630'),
}

def _inl(props, vals):
    """Attribute selectors that catch inline style="prop:value" (so old hard-coded colours follow the theme)."""
    sels = []
    for p in props:
        for v in vals:
            for f in (p + ':' + v, p + ': ' + v):
                sels.append('[style*="' + f + ';" i]')
                sels.append('[style$="' + f + '" i]')
    return sels

def _build_theme_css():
    P = ':where(html[data-theme]:not([data-theme="default"])) '
    out = []
    # 1) variables for every theme
    for k, t in THEMES.items():
        out.append('html[data-theme="%s"]{--t-bg:%s;--t-surface:%s;--t-surface2:%s;--t-border:%s;--t-inborder:%s;--t-text:%s;--t-text2:%s;--t-muted:%s;--t-accent:%s;--t-hover:%s;--t-soft:%s;--t-head:%s;--t-onaccent:%s;--t-input:%s;color-scheme:%s}' % (
            k, t['bg'], t['surface'], t['surface2'], t['border'], t['inborder'], t['text'], t['text2'], t['muted'],
            t['accent'], t['hover'], t['soft'], t['head'], t['onaccent'], t['inp'], 'dark' if t['dark'] else 'light'))
    def rule(sels, body):
        out.append(P + ':is(' + sels + '){' + body + '}')
    # 2) page structure
    out.append(P + 'body{background:var(--t-bg)!important;color:var(--t-text)}')
    rule('.main-content', 'background:var(--t-bg)!important')
    rule('.top-header,.sidebar,.profile-dropdown,.panel,.metric-card,.users-card,.user-modal,.settings-form,.report-folder,.user-menu,.dropdown-content,.filter-dropdown-box',
         'background:var(--t-surface)!important;border-color:var(--t-border)!important')
    rule('.report-folder,.users-card,.user-modal,.settings-form', 'color:var(--t-text)!important')
    rule('.settings-drawer', 'background:var(--t-surface2)!important')
    rule('.sidebar a', 'color:var(--t-text2)')
    rule('.sidebar a:hover,.sidebar a.active', 'background:var(--t-surface2);color:var(--t-accent);border-left-color:var(--t-accent)')
    rule('.settings-drawer-item', 'color:var(--t-text2)!important;border-left-color:var(--t-border)!important')
    rule('.settings-drawer-item:hover,.settings-drawer-item.current', 'background:var(--t-soft)!important;color:var(--t-accent)!important;border-left-color:var(--t-accent)!important')
    rule('.settings-drawer-item:hover span:last-child,.settings-drawer-item.current span:last-child,.settings-drawer-item:hover .settings-drawer-arrow,.settings-drawer-item.current .settings-drawer-arrow', 'color:var(--t-accent)!important')
    rule('.settings-drawer-arrow', 'color:var(--t-muted)')
    # 3) header / profile
    rule('.user-avatar,.dropdown-header-info', 'background:var(--t-head)!important')
    rule('.user-name-display,.metric-info .val,.user-modal-head h3,.settings-form h3,.rf-title', 'color:var(--t-text)!important')
    rule('.user-id-display,.metric-info h3,.rf-desc,.user-modal-close', 'color:var(--t-muted)!important')
    rule('.brand-text span', 'color:var(--t-accent)')
    rule('.profile-dropdown a,.dropdown-content a', 'color:var(--t-text2)')
    rule('.profile-dropdown a:hover,.dropdown-content a:hover', 'background:var(--t-surface2);color:var(--t-accent)')
    rule('.profile-dropdown a.logout-btn,.dropdown-content a.delete-item', 'color:#ef4444')
    rule('.profile-dropdown a.logout-btn', 'border-top-color:var(--t-border)')
    # 4) tables / forms
    rule('th,.users-table th', 'background:var(--t-surface2)!important;color:var(--t-text2)!important')
    rule('th,td,.users-table th,.users-table td,.user-modal-head', 'border-bottom-color:var(--t-border)!important')
    rule('input:not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]):not([type=file]):not(.invalid-field),select,textarea',
         'background:var(--t-input)!important;color:var(--t-text)!important;border-color:var(--t-inborder)')
    rule('.users-grid label,.setting-field>label', 'color:var(--t-text2)!important')
    out.append(P + '::placeholder{color:var(--t-muted);opacity:.8}')
    out.append(P + 'input[type=checkbox],input[type=radio]{accent-color:var(--t-accent)}')
    # 5) buttons (class based buttons such as .btn-danger keep their own colour)
    out.append(P + 'button{background-color:var(--t-accent);color:var(--t-onaccent)}')
    out.append(P + 'button:hover{background-color:var(--t-hover)}')
    rule('.dropbtn', 'background:var(--t-surface2)!important;color:var(--t-text2)!important;border-color:var(--t-border)!important')
    rule('.dropbtn:hover,.user-dots:hover', 'background:var(--t-soft)!important')
    rule('.user-dots', 'color:var(--t-text2)!important;background:transparent!important')
    rule('.user-menu button', 'background:transparent!important;color:var(--t-text2)!important')
    rule('.user-menu button:hover', 'background:var(--t-surface2)!important')
    rule('.user-menu .reject-user-action', 'color:#ef4444!important')
    rule('.user-cancel-btn', 'background:var(--t-border)!important;color:var(--t-text2)!important')
    rule('.users-toolbar', 'background:var(--t-accent)!important')
    rule('.add-user-btn', 'background:var(--t-surface)!important;color:var(--t-accent)!important')
    rule('.rf-num', 'background:var(--t-accent)!important;color:var(--t-onaccent)!important')
    rule('.report-folder:hover', 'border-color:var(--t-accent)!important')
    # 6) SweetAlert popups
    rule('.swal2-popup', 'background:var(--t-surface)!important')
    rule('.swal2-title', 'color:var(--t-text)!important')
    rule('.swal2-html-container', 'color:var(--t-text2)!important')
    # 7) old inline colours -> follow the theme
    rule(','.join(_inl(['background', 'background-color'], ['#fff', '#ffffff', 'white'])), 'background:var(--t-surface)!important')
    rule(','.join(_inl(['background', 'background-color'], ['#f8fafc', '#f1f5f9'])), 'background:var(--t-surface2)!important')
    rule(','.join(_inl(['background', 'background-color'], ['#f4f6f9'])), 'background:var(--t-bg)!important')
    rule(','.join(_inl(['background', 'background-color'], ['#eff6ff', '#eaf2ff'])), 'background:var(--t-soft)!important')
    rule(','.join(_inl(['background', 'background-color'], ['#2563eb'])), 'background:var(--t-accent)!important')
    rule(','.join(_inl(['color'], ['#1e293b'])), 'color:var(--t-text)!important')
    rule(','.join(_inl(['color'], ['#64748b'])), 'color:var(--t-muted)!important')
    rule(','.join(_inl(['color'], ['#475569', '#334155', '#333'])), 'color:var(--t-text2)!important')
    rule('[style^="color:#2563eb" i],[style*=";color:#2563eb" i],[style*=" color:#2563eb" i],[style^="color: #2563eb" i],[style*="; color: #2563eb" i],[style*=" color: #2563eb" i]', 'color:var(--t-accent)!important')
    rule('[style*="solid #e2e8f0" i],[style*="solid #cbd5e1" i],[style*="solid #f1f5f9" i]', 'border-color:var(--t-border)!important')
    return '<style id="themeCss">' + '\n'.join(out) + '</style>'

THEME_CSS = _build_theme_css()

def build_theme_head(key):
    t = THEMES.get(key, THEMES['default'])
    js = '<script>if(window.Chart){Chart.defaults.color="%s";Chart.defaults.borderColor="%s";}</script>' % (
        t['text2'] if key != 'default' else '#666666', t['border'] if key != 'default' else 'rgba(0,0,0,0.1)')
    return THEME_CSS + js

def get_user_theme(user_key):
    if not user_key:
        return 'default'
    try:
        c = sqlite3.connect(DB_NAME)
        c.execute('CREATE TABLE IF NOT EXISTS user_themes(user_key TEXT PRIMARY KEY, theme TEXT NOT NULL)')
        row = c.execute('SELECT theme FROM user_themes WHERE user_key=?', (str(user_key).strip().lower(),)).fetchone()
        c.close()
    except sqlite3.Error:
        return 'default'
    return row[0] if row and row[0] in THEMES else 'default'

def set_user_theme(user_key, theme):
    c = sqlite3.connect(DB_NAME)
    c.execute('CREATE TABLE IF NOT EXISTS user_themes(user_key TEXT PRIMARY KEY, theme TEXT NOT NULL)')
    c.execute('INSERT INTO user_themes(user_key,theme) VALUES(?,?) ON CONFLICT(user_key) DO UPDATE SET theme=excluded.theme',
              (str(user_key).strip().lower(), theme))
    c.commit(); c.close()

def theme_picker_html(current):
    cards = []
    for k, t in THEMES.items():
        pv = '--p-bg:%s;--p-surface:%s;--p-border:%s;--p-accent:%s;--p-head:%s' % (t['bg'], t['surface'], t['border'], t['accent'], t['head'])
        cards.append(
            '<label class="tp-card%s" data-key="%s" data-name="%s">'
            '<input type="radio" name="theme" value="%s"%s>'
            '<div class="tp-prev" style="%s"><div class="tp-ph"><b></b><b></b><b></b></div>'
            '<div class="tp-body"><div class="tp-sb"><i class="a"></i><i></i><i></i></div>'
            '<div class="tp-ct"><div class="tp-box"></div><div class="tp-box s"></div><div class="tp-btn"></div></div></div></div>'
            '<div class="tp-name">%s<span class="tp-tag">%s</span></div><div class="tp-desc">%s</div>'
            '<span class="tp-check">&#10003;</span></label>' % (
                ' sel' if k == current else '', k, esc(t['name']), k, ' checked' if k == current else '', pv,
                esc(t['name']), 'Dark' if t['dark'] else 'Light', esc(t['desc'])))
    return '''<style>
.tp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:16px;margin:20px 0}
.tp-card{position:relative;display:block;cursor:pointer;border:2px solid var(--t-border,#e2e8f0);border-radius:12px;padding:10px;background:var(--t-surface,#fff);transition:transform .15s,box-shadow .15s,border-color .15s}
.tp-card:hover{transform:translateY(-3px);box-shadow:0 10px 22px rgba(0,0,0,.12)}
.tp-card.sel{border-color:var(--t-accent,#2563eb);box-shadow:0 0 0 3px var(--t-soft,#eff6ff)}
.tp-card input{position:absolute;opacity:0;pointer-events:none}
.tp-prev{height:96px;border:1px solid var(--p-border);border-radius:8px;overflow:hidden;background:var(--p-bg);display:flex;flex-direction:column}
.tp-ph{height:15px;background:var(--p-surface);border-bottom:1px solid var(--p-border);display:flex;align-items:center;gap:3px;padding:0 6px}
.tp-ph b{width:5px;height:5px;border-radius:50%;background:var(--p-border)}
.tp-ph b:first-child{background:var(--p-accent)}
.tp-body{flex:1;display:flex}
.tp-sb{width:27%;background:var(--p-surface);border-right:1px solid var(--p-border);padding:6px 5px}
.tp-sb i{display:block;height:4px;border-radius:2px;background:var(--p-border);margin-bottom:5px}
.tp-sb i.a{background:var(--p-accent)}
.tp-ct{flex:1;padding:7px;display:flex;flex-direction:column;gap:5px}
.tp-box{height:17px;border-radius:4px;background:var(--p-surface);border:1px solid var(--p-border)}
.tp-box.s{width:70%}
.tp-btn{width:32px;height:9px;border-radius:3px;background:var(--p-accent)}
.tp-name{margin-top:10px;font-weight:700;font-size:14px;color:var(--t-text,#1e293b);display:flex;align-items:center;justify-content:space-between;gap:6px}
.tp-tag{font-size:10px;font-weight:700;padding:2px 7px;border-radius:10px;background:var(--t-soft,#eff6ff);color:var(--t-accent,#2563eb)}
.tp-desc{font-size:12px;color:var(--t-muted,#64748b);margin-top:3px}
.tp-check{display:none;position:absolute;top:16px;right:16px;width:22px;height:22px;border-radius:50%;background:var(--t-accent,#2563eb);color:var(--t-onaccent,#fff);font-size:13px;align-items:center;justify-content:center;box-shadow:0 2px 6px rgba(0,0,0,.3)}
.tp-card.sel .tp-check{display:flex}
.tp-note{font-size:13px;color:var(--t-muted,#64748b);min-height:20px}
</style>
<div class="panel" style="max-width:980px;margin:auto;">
  <h3>&#127912; Theme Settings</h3>
  <p style="color:#64748b;margin-bottom:4px;">Choose the look you like. Click a theme to preview it instantly, then press <b>Save Theme</b>. Your choice is saved for your own account only.</p>
  <form method="POST" action="/save_theme" id="themeForm">
    <div class="tp-grid">''' + ''.join(cards) + '''</div>
    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
      <button type="submit">Save Theme</button>
      <span class="tp-note" id="tpNote">Current theme: <b>''' + esc(THEMES[current]['name']) + '''</b></span>
    </div>
  </form>
</div>
<script>
(function(){
  var saved = document.documentElement.getAttribute('data-theme') || 'default';
  document.querySelectorAll('.tp-card input').forEach(function(r){
    r.addEventListener('change', function(){
      document.querySelectorAll('.tp-card').forEach(function(c){ c.classList.remove('sel'); });
      var card = r.closest('.tp-card'); card.classList.add('sel');
      document.documentElement.setAttribute('data-theme', r.value);
      var note = document.getElementById('tpNote');
      note.innerHTML = (r.value === saved)
        ? 'Current theme: <b>' + card.dataset.name + '</b>'
        : 'Previewing <b>' + card.dataset.name + '</b> - press Save Theme to keep it.';
    });
  });
})();
</script>'''


BASE_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en" data-theme="{{ theme_key|default('default') }}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f6f9; margin: 0; padding: 0; color: #333; }
        
        /* Top Header */
        .top-header { background: #ffffff; height: 65px; border-bottom: 1px solid #e2e8f0; display: flex; justify-content: space-between; align-items: center; padding: 0 20px; position: fixed; top: 0; left: 0; right: 0; z-index: 1000; }
        
        /* Brand Logo Container */
        .brand-logo-container { display: flex; align-items: center; gap: 12px; font-family: 'Inter', system-ui, sans-serif; text-decoration: none; }
        .brand-text { font-size: 20px; font-weight: 700; color: #0F172A; letter-spacing: -0.5px; }
        .brand-text span { color: #2563EB; }
        .brand-text, .brand-anim { animation: brandColorCycle 36s linear infinite; }
        .brand-anim { transition: color 2s ease; }
        @keyframes brandColorCycle {
            0% { color: hsl(0,88%,48%); }
            8% { color: hsl(30,88%,48%); }
            17% { color: hsl(60,88%,48%); }
            25% { color: hsl(90,88%,48%); }
            33% { color: hsl(120,88%,48%); }
            42% { color: hsl(150,88%,48%); }
            50% { color: hsl(180,88%,48%); }
            58% { color: hsl(210,88%,48%); }
            67% { color: hsl(240,88%,48%); }
            75% { color: hsl(270,88%,48%); }
            83% { color: hsl(300,88%,48%); }
            92% { color: hsl(330,88%,48%); }
            100% { color: hsl(360,88%,48%); }
        }
        @media (prefers-reduced-motion: reduce) { .brand-text, .brand-anim { animation-duration: 120s; } }

        .user-profile-area { position: relative; display: flex; align-items: center; gap: 12px; cursor: pointer; user-select: none; }
        .user-avatar { width: 38px; height: 38px; background: #1e293b; color: #ffffff; border-radius: 50%; display: flex; justify-content: center; align-items: center; font-weight: bold; font-size: 15px; overflow: hidden; }
        .user-avatar img { width: 100%; height: 100%; object-fit: cover; }
        .user-meta { display: flex; flex-direction: column; text-align: right; }
        .user-name-display { font-size: 14px; font-weight: bold; color: #1e293b; display: flex; align-items: center; gap: 6px; justify-content: flex-end; }
        .user-id-display { font-size: 11px; color: #64748b; font-weight: 600; }
        
        .profile-dropdown { display: none; position: absolute; top: 52px; right: 0; background: #ffffff; min-width: 220px; box-shadow: 0 10px 25px rgba(0,0,0,0.2); border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; z-index: 99999; }
        .profile-dropdown.show { display: block !important; }
        .dropdown-header-info { padding: 15px; background: #1e293b; color: white; border-bottom: 1px solid #334155; }
        .profile-dropdown a { display: flex; align-items: center; gap: 12px; padding: 12px 16px; color: #334155; text-decoration: none; font-size: 14px; transition: background 0.2s; }
        .profile-dropdown a:hover { background-color: #f1f5f9; }
        .profile-dropdown a.logout-btn { color: #dc2626; border-top: 1px solid #e2e8f0; }

        /* Layout & Sidebar */
        .app-layout { display: flex; margin-top: 65px; min-height: calc(100vh - 65px); }
        .sidebar { width: 250px; background: #ffffff; border-right: 1px solid #e2e8f0; padding-top: 10px; position: fixed; height: calc(100vh - 65px); overflow-y: auto; }
        .sidebar a { display: flex; align-items: center; justify-content: space-between; color: #475569; text-decoration: none; padding: 11px 18px; font-size: 14px; border-left: 3px solid transparent; transition: all 0.2s; }
        .sidebar a:hover, .sidebar a.active { background: #f8fafc; color: #3b82f6; border-left-color: #3b82f6; font-weight: 600; }
        .main-content { margin-left: 250px; padding: 25px; width: calc(100% - 250px); background: #f4f6f9; }
        .settings-drawer { overflow: hidden; padding: 5px 8px 10px 18px; background: #f8fafc; animation: settingsSlideDown .24s ease-out both; }
        .settings-drawer-item { display: flex !important; justify-content: flex-start !important; gap: 9px; align-items: center; padding: 9px 10px 9px 12px !important; border: 0 !important; border-left: 2px solid #dbeafe !important; border-radius: 0 6px 6px 0; font-size: 12.5px !important; color: #475569 !important; }
        .settings-drawer-item:hover, .settings-drawer-item.current { background: #eaf2ff !important; color: #2563eb !important; border-left-color: #2563eb !important; }
        .settings-drawer-arrow { font-size: 17px; color: #94a3b8; transition: color .18s, transform .18s; }
        .settings-drawer-item:hover .settings-drawer-arrow, .settings-drawer-item.current .settings-drawer-arrow { color: #2563eb; transform: translateX(3px); }
        .settings-drawer-item span:last-child { transition: color .18s; }
        .settings-drawer-item:hover span:last-child, .settings-drawer-item.current span:last-child { color: #2563eb; }
        @keyframes settingsSlideDown { from { opacity: 0; max-height: 0; transform: translateY(-6px); } to { opacity: 1; max-height: 520px; transform: translateY(0); } }

        /* Dashboard Metric Cards */
        .metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 20px; margin-bottom: 25px; }
        .metric-card { background: #ffffff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; display: flex; align-items: center; gap: 15px; }
        a.metric-card { text-decoration: none; color: inherit; cursor: pointer; transition: transform .2s ease, box-shadow .2s ease, border-color .2s ease; }
        a.metric-card:hover { transform: translateY(-3px); box-shadow: 0 10px 22px rgba(15,23,42,.14); border-color: var(--t-accent, #2563eb); }
        .metric-icon { width: 48px; height: 48px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 22px; }
        .metric-info h3 { margin: 0 0 5px 0; font-size: 11px; color: #64748b; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; }
        .metric-info .val { font-size: 22px; font-weight: bold; color: #1e293b; }

        .panel { background: #ffffff; border-radius: 8px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; margin-bottom: 25px; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { border-bottom: 1px solid #e2e8f0; padding: 12px 10px; text-align: left; font-size: 14px; }
        th { background-color: #f8fafc; color: #475569; font-weight: 600; }
        input[type="text"], input[type="password"], input[type="number"], input[type="date"], select { padding: 8px 12px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 14px; width: 100%; box-sizing: border-box; margin-top: 5px; }
        input.invalid-field { border: 2px solid #dc2626 !important; background-color: #fef2f2; }
        button { background-color: #2563EB; color: white; padding: 8px 16px; border: none; border-radius: 6px; font-size: 14px; cursor: pointer; transition: background 0.3s; }
        button:hover { background-color: #1d4ed8; }
        .btn-danger { background-color: #dc2626; color: white; }
        .btn-danger:hover { background-color: #b91c1c; }
        .badge { padding: 3px 8px; border-radius: 12px; font-size: 10px; font-weight: bold; }
        .badge-hr { background: #e0f2fe; color: #0284c7; }
        .badge-admin { background: #fee2e2; color: #dc2626; }
        .badge-member { background: #dcfce7; color: #16a34a; }
        .badge-pending { background: #fef3c7; color: #d97706; }
        .badge-approved { background: #dcfce7; color: #16a34a; }
        .badge-rejected { background: #fee2e2; color: #dc2626; }
        
        .login-body { display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; position: relative; overflow: hidden;
            background: radial-gradient(900px 520px at 12% 8%, rgba(37,99,235,.30), transparent 62%), radial-gradient(820px 520px at 92% 92%, rgba(6,182,212,.20), transparent 62%), linear-gradient(135deg, #0b1220 0%, #0f172a 55%, #111c33 100%); }
        .login-card { background: white; padding: 40px; border-radius: 16px; width: 100%; max-width: 400px; border: 1px solid rgba(255,255,255,.10); box-shadow: 0 30px 70px -15px rgba(0,0,0,.65), 0 0 0 1px rgba(255,255,255,.04); text-align: center; position: relative; z-index: 2; }

        /* Login: professional dark backdrop */
        .login-body::before { content: ""; position: absolute; inset: 0; pointer-events: none; z-index: 0;
            background-image: linear-gradient(rgba(148,163,184,.07) 1px, transparent 1px), linear-gradient(90deg, rgba(148,163,184,.07) 1px, transparent 1px);
            background-size: 56px 56px;
            -webkit-mask-image: radial-gradient(ellipse at center, #000 20%, transparent 72%); mask-image: radial-gradient(ellipse at center, #000 20%, transparent 72%); }
        .login-card input:not([type=checkbox]) { transition: border-color .2s, box-shadow .2s; }
        .login-card input:not([type=checkbox]):focus { outline: none; border-color: #2563eb !important; box-shadow: 0 0 0 3px rgba(37,99,235,.18); }
        .login-card button[type=submit] { background: linear-gradient(135deg, #2563eb, #1d4ed8); box-shadow: 0 6px 14px -4px rgba(37,99,235,.55); font-weight: 600; letter-spacing: .3px; }
        .login-card button[type=submit]:hover { background: linear-gradient(135deg, #3b82f6, #2563eb); }
        .error { color: #dc2626; font-size: 13px; margin-bottom: 15px; }
        
        /* Table Row Dropdown Fix */
        .dropdown { position: relative; display: inline-block; }
        .dropbtn { background: #f8fafc; color: #475569; width: 32px; height: 32px; font-size: 16px; border: 1px solid #e2e8f0; border-radius: 6px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: all 0.2s ease; }
        .dropbtn:hover { background: #f1f5f9; color: #1e293b; border-color: #cbd5e1; }
        .dropdown-content { display: none; position: absolute; right: 0; top: 38px; background-color: #ffffff; min-width: 145px; box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1); z-index: 9999; border-radius: 8px; border: 1px solid #e2e8f0; overflow: hidden; padding: 6px; }
        .dropdown-content a { color: #334155; padding: 8px 12px; text-decoration: none; display: flex; align-items: center; gap: 10px; font-size: 13px; font-weight: 500; border-radius: 5px; }
        .dropdown-content a:hover { background-color: #f8fafc; color: #2563EB; }
        .dropdown-content a.delete-item { color: #dc2626; }
        .dropdown-content a.delete-item:hover { background-color: #fee2e2; color: #b91c1c; }
        .dropdown.show .dropdown-content { display: block; }
        
        input[type="checkbox"] { width: 18px; height: 18px; cursor: pointer; margin: 0; vertical-align: middle; }

        @keyframes dropIntoBin {
            0% { transform: translateY(-20px) scale(1) rotate(0deg); opacity: 1; }
            50% { transform: translateY(5px) scale(0.7) rotate(-20deg); opacity: 0.9; }
            100% { transform: translateY(22px) scale(0.2) rotate(45deg); opacity: 0; }
        }
        .falling-paper {
            display: inline-block;
            animation: dropIntoBin 0.9s ease-in-out infinite;
        }

        .filter-dropdown-box { display: none; position: absolute; right: 180px; top: 60px; background: white; border: 1px solid #cbd5e1; border-radius: 8px; padding: 15px; box-shadow: 0 10px 25px rgba(0,0,0,0.15); z-index: 1000; width: 300px; }
        .filter-dropdown-box.show { display: block; }

        /* Centered popup styling (SweetAlert2) */
        .swal2-popup { border-radius: 16px !important; padding: 1.8em 1.5em !important; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif !important; box-shadow: 0 20px 45px rgba(15, 23, 42, 0.25) !important; }
        .swal2-title { color: #1e293b !important; font-size: 22px !important; font-weight: 700 !important; }
        .swal2-html-container { color: #475569 !important; font-size: 15px !important; }
        .swal2-confirm, .swal2-cancel { border-radius: 8px !important; padding: 10px 26px !important; font-weight: 600 !important; }
        .swal2-container.swal2-backdrop-show { background: rgba(15, 23, 42, 0.55) !important; backdrop-filter: blur(2px); }
    </style>
    {{ theme_head|default('')|safe }}
</head>
<body>
    {{ content | safe }}
    <script>
        function toggleProfileMenu(event) {
            if (event) event.stopPropagation();
            const dropdown = document.getElementById('profileDropdown');
            if (dropdown) {
                dropdown.classList.toggle('show');
            }
        }

        function toggleRowDropdown(id, event) {
            event.stopPropagation();
            event.preventDefault();
            
            document.querySelectorAll('.dropdown-content').forEach(el => {
                if(el.id !== 'dropdown-content-' + id) {
                    el.style.display = 'none';
                    el.parentElement.classList.remove('show');
                }
            });

            const dropdownContainer = document.getElementById('dropdown-' + id);
            const dropdownMenu = document.getElementById('dropdown-content-' + id);
            
            if (dropdownMenu) {
                if (dropdownMenu.style.display === 'block') {
                    dropdownMenu.style.display = 'none';
                    dropdownContainer.classList.remove('show');
                } else {
                    dropdownMenu.style.display = 'block';
                    dropdownContainer.classList.add('show');
                }
            }
        }

        function toggleFilterCalendar(event) {
            event.stopPropagation();
            const box = document.getElementById('filterCalendarBox');
            if(box) box.classList.toggle('show');
        }

        function confirmDelete(id, name) {
            Swal.fire({
                title: 'Are you sure?',
                text: "You will not be able to recover this data afterwards!",
                icon: 'warning',
                iconColor: '#f97316',
                showCancelButton: true,
                confirmButtonColor: '#2563EB',
                cancelButtonColor: '#64748b',
                confirmButtonText: 'Yes, delete!',
                cancelButtonText: 'Cancel'
            }).then((result) => {
                if (result.isConfirmed) {
                    window.location.href = '/delete_member/' + id;
                }
            });
        }

        function confirmDeleteReq(id) {
            Swal.fire({
                title: 'Are you sure?',
                text: "You will not be able to recover this data afterwards!",
                icon: 'warning',
                iconColor: '#f97316',
                showCancelButton: true,
                confirmButtonColor: '#2563EB',
                cancelButtonColor: '#64748b',
                confirmButtonText: 'Yes, delete!',
                cancelButtonText: 'Cancel'
            }).then((result) => {
                if (result.isConfirmed) {
                    window.location.href = '/delete_requisition/' + id;
                }
            });
        }

        function toggleSelectAll(master, className, btnId) {
            const checkboxes = document.querySelectorAll('.' + className);
            checkboxes.forEach(cb => cb.checked = master.checked);
            checkSelectedCount(className, btnId);
        }

        function checkSelectedCount(className, btnId) {
            const checkedCount = document.querySelectorAll('.' + className + ':checked').length;
            const btn = document.getElementById(btnId);
            if (btn) {
                if (checkedCount > 0) {
                    btn.style.display = 'inline-block';
                } else {
                    btn.style.display = 'none';
                }
            }
        }

        function submitBulkDelete(formId, itemName, className) {
            const checkedCount = document.querySelectorAll('#' + formId + ' .' + className + ':checked').length;
            if (checkedCount === 0) {
                return;
            }

            Swal.fire({
                title: 'Are you sure?',
                text: "You will not be able to recover these " + checkedCount + " selected " + itemName + "(s) afterwards!",
                icon: 'warning',
                iconColor: '#f97316',
                showCancelButton: true,
                confirmButtonColor: '#2563EB',
                cancelButtonColor: '#64748b',
                confirmButtonText: 'Yes, delete!',
                cancelButtonText: 'Cancel'
            }).then((result) => {
                if (result.isConfirmed) {
                    document.getElementById(formId).submit();
                }
            });
        }

        window.onclick = function(event) {
            if (!event.target.closest('.user-profile-area') && !event.target.closest('.dropdown') && !event.target.closest('.filter-dropdown-box') && !event.target.closest('#filterBtn')) {
                const dropdowns = document.getElementsByClassName('profile-dropdown');
                for (let i = 0; i < dropdowns.length; i++) {
                    dropdowns[i].classList.remove('show');
                }
                document.querySelectorAll('.dropdown').forEach(el => {
                    el.classList.remove('show');
                    const content = el.querySelector('.dropdown-content');
                    if(content) content.style.display = 'none';
                });
                const filterBox = document.getElementById('filterCalendarBox');
                if(filterBox) filterBox.classList.remove('show');
            }
        }
    </script>
</body>
</html>
'''

@app.route('/', methods=['GET', 'POST'])
def login():
    error = ""
    identifier = ""
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '').strip()
        if identifier and password:
            if identifier.lower() == ADMIN_IDENTIFIER.lower() and password == (load_settings().get('admin_password') or ADMIN_PASSWORD):
                session['logged_in'] = True
                session['user'] = identifier
                session['role'] = 'HR'
                session['owner'] = True
                session['name'] = 'Md Tanvir Hassan'
                session['user_id'] = 'ADMIN-7242'
                session['profile_pic'] = ''
                
                conn = sqlite3.connect(DB_NAME)
                conn.execute("INSERT INTO activity_logs (action) VALUES (?)", ("HR (Owner, full access) logged in directly with password",))
                conn.commit()
                conn.close()
                return redirect(url_for('dashboard'))
            else:
                # Accounts created by an administrator use direct ID/email + password login.
                user_conn = sqlite3.connect(DB_NAME)
                user_conn.row_factory = sqlite3.Row
                account = user_conn.execute(
                    "SELECT * FROM app_users WHERE (user_id=? COLLATE NOCASE OR email=? COLLATE NOCASE) AND status='Active'",
                    (identifier, identifier)
                ).fetchone()
                user_conn.close()
                if account and check_password_hash(account['password_hash'], password):
                    session['logged_in'] = True
                    session['user'] = account['email'] or account['user_id']
                    session['role'] = account['role']
                    session['name'] = account['full_name']
                    session['user_id'] = account['user_id']
                    session['profile_pic'] = ''
                    log_conn = sqlite3.connect(DB_NAME)
                    log_conn.execute("INSERT INTO activity_logs (action) VALUES (?)",
                                     (f"{account['role']} account {account['user_id']} logged in",))
                    log_conn.commit()
                    log_conn.close()
                    return redirect(url_for('dashboard'))
                # Login failed: tell the user why. Unknown people can NOT log in (no OTP login any more).
                if identifier.lower() == ADMIN_IDENTIFIER.lower():
                    error = "Incorrect password. Please try again."
                else:
                    chk_conn = sqlite3.connect(DB_NAME)
                    chk_conn.row_factory = sqlite3.Row
                    any_acc = chk_conn.execute(
                        "SELECT password_hash,status FROM app_users WHERE user_id=? COLLATE NOCASE OR email=? COLLATE NOCASE",
                        (identifier, identifier)).fetchone()
                    chk_conn.close()
                    if not any_acc:
                        error = "No account found with this User ID / Email. Please create an account first."
                    elif not check_password_hash(any_acc['password_hash'], password):
                        error = "Incorrect password. Please try again."
                    elif any_acc['status'] == 'Pending':
                        error = "Your account is still waiting for HR approval."
                    elif any_acc['status'] == 'Rejected':
                        error = "Your account request was rejected. Please contact HR."
                    else:
                        error = "This account cannot log in right now. Please contact HR."
        else:
            error = "Please enter both User ID/Email and Password."
            
    body_content = '''
    <div class="login-body">
        <div class="login-card">
            <h2 class="brand-anim" style="color: #1e293b; margin: 0 0 24px; font-size: 28px; letter-spacing: 1px;">OpsNexus</h2>
            ''' + (f'<div class="error">{error}</div>' if error else '') + '''
            <form method="POST">
                <input type="text" name="identifier" placeholder="User ID / Email" required autocomplete="off" value="''' + esc(identifier) + '''" style="margin-bottom: 12px;">
                <input type="password" name="password" placeholder="Password" required autocomplete="off" style="margin-bottom: 15px;">
                <button type="submit" style="width: 100%; padding: 10px;">Login</button>
            </form>
            <div style="margin-top: 14px; text-align: center;">
                <a href="/forgot" style="color: #2563eb; font-size: 13px; text-decoration: none; font-weight: 600;">Forgot password?</a>
            </div>
            <div style="display: flex; align-items: center; gap: 10px; margin: 16px 0 12px; color: #94a3b8; font-size: 12px;"><span style="flex: 1; height: 1px; background: #e2e8f0;"></span>or<span style="flex: 1; height: 1px; background: #e2e8f0;"></span></div>
            <a href="/register" style="display: block; padding: 10px; border: 1px solid #2563eb; border-radius: 6px; color: #2563eb; font-size: 14px; font-weight: 600; text-decoration: none;">Create account</a>
        </div>
    </div>
    '''
    return render_template_string(BASE_TEMPLATE, title="Login - OpsNexus", content=body_content)

@app.route('/verify', methods=['GET', 'POST'])
def verify_otp():
    temp_user = session.get('temp_user')
    if not temp_user:
        return redirect(url_for('login'))
    error = ""
    if request.method == 'POST':
        entered_otp = request.form.get('otp')
        if otp_storage.get(temp_user) == entered_otp:
            # OTP one-time use: remove after successful verification
            otp_storage.pop(temp_user, None)
            session.pop('temp_user', None)
            session['logged_in'] = True
            session['user'] = temp_user
            
            if temp_user.lower() == "hr@opsnexus.com" or temp_user.lower() == "tanvir.hr7242@gmail.com":
                session['role'] = 'HR'
                session['name'] = 'HR Manager'
                session['user_id'] = 'HR-7242'
            else:
                session['role'] = 'Member'
                session['name'] = 'Team Member'
                session['user_id'] = f"EMP-{random.randint(1000, 9999)}"
                
            session['profile_pic'] = ''
            
            conn = sqlite3.connect(DB_NAME)
            conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"User logged in as {session['role']}",))
            conn.commit()
            conn.close()
            return redirect(url_for('dashboard'))
        else:
            error = "Invalid OTP! Please try again."
            
    body_content = '''
    <div class="login-body">
        <div class="login-card">
            <h2 style="color: #1e293b; margin-top:0;">Verify OTP</h2>
            <p style="color: #64748b; font-size: 14px;">Enter the 4-digit code sent to server</p>
            ''' + (f'<div class="error">{error}</div>' if error else '') + '''
            <form method="POST">
                <input type="text" name="otp" placeholder="4-digit OTP" required maxlength="4" autocomplete="off" style="margin-bottom: 15px; text-align: center; letter-spacing: 5px; font-size: 18px;">
                <button type="submit" style="width: 100%; padding: 10px;">Verify & Login</button>
            </form>
        </div>
    </div>
    '''
    return render_template_string(BASE_TEMPLATE, title="Verify OTP - OpsNexus", content=body_content)

# ===========================================================================
# FORGOT PASSWORD / CREATE ACCOUNT  (OTP is delivered to HR through the bell icon)
# Uses NEW tables only (auth_requests, hr_notifications) + one new app_users.phone column.
# ===========================================================================
EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
# Only mail from well-known providers is accepted on sign-up (company / random / mistyped domains are rejected).
ALLOWED_EMAIL_DOMAINS = {
    'gmail.com', 'googlemail.com', 'yahoo.com', 'ymail.com', 'rocketmail.com',
    'outlook.com', 'hotmail.com', 'live.com', 'msn.com', 'icloud.com', 'me.com', 'mac.com',
    'aol.com', 'proton.me', 'protonmail.com', 'pm.me', 'zoho.com', 'gmx.com', 'mail.com', 'yandex.com',
}
# regional versions of the big providers, e.g. yahoo.co.uk, hotmail.co.uk, outlook.com.bd
ALLOWED_EMAIL_REGIONAL_RE = re.compile(r'^(yahoo|hotmail|outlook|live)\.([a-z]{2,3}|co\.[a-z]{2}|com\.[a-z]{2})$')
EMAIL_LOCAL_RE = re.compile(r'^(?!\.)(?!.*\.\.)[A-Za-z0-9._%+-]{1,64}(?<!\.)$')
BD_PHONE_RE = re.compile(r'^(?:\+?88)?(01[3-9]\d{8})$')
OTP_MAX_ATTEMPTS = 5
TS_FMT = '%Y-%m-%d %H:%M:%S'
PW_RULE_TEXT = ('Password must be at least 8 characters and include an uppercase letter, '
                'a lowercase letter, a number and a special character.')


def _utc_now():
    return _DT.now(_TZ.utc).replace(tzinfo=None)


def _otp_minutes():
    return max(1, min(60, to_int(load_settings().get('otp_expiry', 5), 5)))


def _is_expired(ts):
    try:
        return _DT.strptime(ts, TS_FMT) < _utc_now()
    except (ValueError, TypeError):
        return True


def _minutes_left(ts):
    try:
        secs = (_DT.strptime(ts, TS_FMT) - _utc_now()).total_seconds()
    except (ValueError, TypeError):
        return 1
    return max(1, int(-(-secs // 60)))


def email_provider_ok(email):
    local, _, domain = email.rpartition('@')
    if not local or not EMAIL_LOCAL_RE.match(local):
        return False
    return domain in ALLOWED_EMAIL_DOMAINS or bool(ALLOWED_EMAIL_REGIONAL_RE.match(domain))


def normalize_phone(value):
    m = BD_PHONE_RE.match(re.sub(r'[\s-]', '', value or ''))
    return m.group(1) if m else None


def password_ok(pw):
    return bool(8 <= len(pw) <= 128 and re.search(r'[a-z]', pw) and re.search(r'[A-Z]', pw)
                and re.search(r'\d', pw) and re.search(r'[^A-Za-z0-9]', pw))


def _db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def _cleanup_auth(conn):
    conn.execute("DELETE FROM auth_requests WHERE created_at < datetime('now','-1 day')")
    conn.execute("DELETE FROM hr_notifications WHERE created_at < datetime('now','-30 day')")


def _recent_request(conn, purpose, email=None, target_id=None):
    if purpose == 'register':
        row = conn.execute("SELECT 1 FROM auth_requests WHERE purpose='register' AND email=? COLLATE NOCASE "
                           "AND created_at > datetime('now','-60 seconds')", (email,)).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM auth_requests WHERE purpose='reset' AND target_user_id=? "
                           "AND created_at > datetime('now','-60 seconds')", (target_id,)).fetchone()
    return bool(row)


def _new_request(conn, purpose, notif_kind, notif_title, notif_message, minutes, **f):
    """Create the OTP request and the notification that HR sees in the bell. Returns the session token."""
    otp = str(100000 + secrets.randbelow(900000))
    token = secrets.token_urlsafe(24)
    expires = (_utc_now() + _TD(minutes=minutes)).strftime(TS_FMT)
    cur = conn.execute(
        "INSERT INTO auth_requests (token,purpose,full_name,email,phone,password_hash,target_user_id,otp,expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (token, purpose, f.get('full_name'), f.get('email'), f.get('phone'), f.get('password_hash'),
         f.get('target_user_id'), otp, expires))
    conn.execute("INSERT INTO hr_notifications (kind,title,message,request_id) VALUES (?,?,?,?)",
                 (notif_kind, notif_title, notif_message, cur.lastrowid))
    return token


AUTH_CSS = '''<style>
.auth-card { text-align: left; }
.auth-heading { font-size: 19px; font-weight: 700; color: #1e293b; text-align: center; margin-top: 6px; }
.auth-sub { color: #64748b; font-size: 13.5px; text-align: center; margin: 6px 0 18px; line-height: 1.5; }
.auth-field { margin-bottom: 14px; text-align: left; }
.auth-field label { display: block; font-size: 12.5px; font-weight: 600; color: #334155; margin-bottom: 5px; }
.auth-field input { width: 100%; padding: 10px 12px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 14px; box-sizing: border-box; margin: 0; background: #fff; color: #1e293b; }
.auth-field input:focus { outline: none; border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,.15); }
.auth-field input.otp-input { text-align: center; letter-spacing: 8px; font-size: 22px; font-weight: 700; }
.pw-wrap { position: relative; }
.pw-wrap input { padding-right: 58px; }
.pw-toggle { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); background: none; border: 0; color: #64748b; padding: 4px 8px; font-size: 12px; font-weight: 600; cursor: pointer; }
.pw-toggle:hover { background: none; color: #1e293b; }
.pw-rules { list-style: none; margin: -4px 0 14px; padding: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 3px 10px; font-size: 12px; color: #94a3b8; }
.pw-rules li::before { content: '\\25CB  '; }
.pw-rules li.ok { color: #16a34a; }
.pw-rules li.ok::before { content: '\\2713  '; }
.pw-match { font-size: 12px; margin: -6px 0 12px; min-height: 14px; }
.pw-match.ok { color: #16a34a; }
.pw-match.bad { color: #dc2626; }
.auth-submit { display: block; width: 100%; padding: 11px; background: #2563eb; color: #fff; border: 0; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; text-align: center; text-decoration: none; box-sizing: border-box; }
.auth-submit:hover { background: #1d4ed8; }
.auth-note { margin-top: 14px; padding: 10px 12px; background: #f1f5f9; border-radius: 8px; color: #475569; font-size: 12.5px; line-height: 1.5; }
.auth-links { margin-top: 16px; text-align: center; font-size: 13px; }
.auth-links a { color: #2563eb; text-decoration: none; font-weight: 600; }
.auth-ok-icon { width: 58px; height: 58px; margin: 4px auto 10px; border-radius: 50%; background: #dcfce7; color: #16a34a; font-size: 30px; display: flex; align-items: center; justify-content: center; }
.auth-id-box { margin: 12px 0 18px; padding: 12px; border: 1px dashed #94a3b8; border-radius: 8px; text-align: center; color: #475569; font-size: 13px; }
.auth-id-box b { display: block; margin-top: 4px; font-size: 20px; letter-spacing: 1px; color: #0f172a; }
@media (max-width: 480px) { .pw-rules { grid-template-columns: 1fr; } }
</style>'''

PW_RULES_HTML = '''<ul class="pw-rules">
<li data-rule="len">8+ characters</li><li data-rule="up">Uppercase letter</li>
<li data-rule="low">Lowercase letter</li><li data-rule="num">Number</li>
<li data-rule="sp">Special character</li></ul>'''

PW_JS = '''<script>
(function(){
  var pw=document.getElementById('pw'), cf=document.getElementById('pw2'), form=document.getElementById('authForm');
  if(!pw) return;
  var rules={len:function(v){return v.length>=8;},up:function(v){return /[A-Z]/.test(v);},
             low:function(v){return /[a-z]/.test(v);},num:function(v){return /\\d/.test(v);},
             sp:function(v){return /[^A-Za-z0-9]/.test(v);}};
  function check(){
    var v=pw.value, all=true;
    Object.keys(rules).forEach(function(k){
      var ok=rules[k](v), li=document.querySelector('[data-rule="'+k+'"]');
      if(li) li.classList.toggle('ok',ok);
      if(!ok) all=false;
    });
    var m=document.getElementById('pwMatch');
    if(cf && m){
      if(!cf.value){m.textContent='';m.className='pw-match';}
      else if(cf.value===v){m.textContent='Passwords match';m.className='pw-match ok';}
      else{m.textContent='Passwords do not match';m.className='pw-match bad';}
    }
    return all && (!cf || cf.value===v);
  }
  pw.addEventListener('input',check);
  if(cf) cf.addEventListener('input',check);
  document.querySelectorAll('.pw-toggle').forEach(function(b){
    b.addEventListener('click',function(){
      var inp=document.getElementById(b.getAttribute('data-target'));
      var show=inp.type==='password'; inp.type=show?'text':'password'; b.textContent=show?'Hide':'Show';
    });
  });
  if(form) form.addEventListener('submit',function(e){
    if(!check()){
      e.preventDefault();
      if(window.Swal){Swal.fire({icon:'warning',title:'Check your password',text:'Use a strong password and make sure both fields match.'});}
    }
  });
})();
</script>'''


def _pw_field(label, field_id, name):
    return ('<div class="auth-field"><label for="' + field_id + '">' + label + '</label>'
            '<div class="pw-wrap"><input type="password" id="' + field_id + '" name="' + name + '" required '
            'maxlength="128" autocomplete="new-password">'
            '<button type="button" class="pw-toggle" data-target="' + field_id + '">Show</button></div></div>')


def _auth_page(title, heading, sub, inner, notice='', wide=False):
    notice_html = ('<div class="error" style="text-align:left;">' + esc(notice) + '</div>') if notice else ''
    body = (AUTH_CSS
            + '<div class="login-body" style="height:auto;min-height:100vh;padding:24px 12px;">'
            + '<div class="login-card auth-card" style="max-width:' + ('460px' if wide else '400px') + ';">'
            + '<h2 class="brand-anim" style="color:#1e293b;margin:0 0 4px;font-size:26px;letter-spacing:1px;text-align:center;">OpsNexus</h2>'
            + '<div class="auth-heading">' + esc(heading) + '</div>'
            + '<p class="auth-sub">' + esc(sub) + '</p>'
            + notice_html + inner + '</div></div>')
    return render_template_string(BASE_TEMPLATE, title=title + ' - OpsNexus', content=body)


def _auth_dead_end(purpose, message):
    link, label = ('/register', 'Create account again') if purpose == 'register' else ('/forgot', 'Request a new code')
    inner = ('<a class="auth-submit" href="' + link + '">' + label + '</a>'
             '<div class="auth-links"><a href="/">&larr; Back to login</a></div>')
    return _auth_page('Verification', 'Code no longer valid', message, inner)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if session.get('logged_in'):
        return redirect(url_for('dashboard'))
    error = ''
    vals = {'full_name': '', 'email': '', 'phone': ''}
    if request.method == 'POST':
        full_name = ' '.join(request.form.get('full_name', '').split())
        email = request.form.get('email', '').strip().lower()
        phone_raw = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        vals = {'full_name': full_name, 'email': email, 'phone': phone_raw}
        phone = normalize_phone(phone_raw)
        if not full_name or len(full_name) > 100:
            error = 'Please enter your full name.'
        elif not EMAIL_RE.match(email) or len(email) > 120:
            error = 'Please enter a valid email address.'
        elif not email_provider_ok(email):
            error = 'Please use a valid email from a known provider such as Gmail, Yahoo, Outlook, Hotmail or iCloud.'
        elif not phone:
            error = 'Please enter a valid Bangladeshi mobile number (e.g. 01712345678).'
        elif not password_ok(password):
            error = PW_RULE_TEXT
        elif password != confirm:
            error = 'Passwords do not match.'
        else:
            mins = _otp_minutes()
            conn = _db()
            try:
                _cleanup_auth(conn)
                dup = conn.execute("SELECT 1 FROM app_users WHERE email=? COLLATE NOCASE OR phone=?",
                                   (email, phone)).fetchone()
                if dup or email == ADMIN_IDENTIFIER.lower():
                    error = 'An account with this email or phone number already exists.'
                elif _recent_request(conn, 'register', email=email):
                    error = 'A request was just sent to HR. Please wait a minute before trying again.'
                else:
                    conn.execute("UPDATE auth_requests SET status='cancelled', password_hash=NULL "
                                 "WHERE status='pending' AND purpose='register' AND email=? COLLATE NOCASE", (email,))
                    token = _new_request(
                        conn, 'register', 'new_account', 'New account request',
                        f"{full_name} | {email} | {phone} wants to create a new account.", mins,
                        full_name=full_name, email=email, phone=phone,
                        password_hash=generate_password_hash(password))
                    conn.commit()
                    session['auth_token'] = token
                    return redirect(url_for('auth_otp'))
            finally:
                conn.close()

    inner = ('<form method="POST" id="authForm">'
             '<div class="auth-field"><label>Full name</label><input type="text" name="full_name" value="' + esc(vals['full_name']) + '" required maxlength="100" autocomplete="name"></div>'
             '<div class="auth-field"><label>Email</label><input type="email" name="email" value="' + esc(vals['email']) + '" required maxlength="120" autocomplete="email" placeholder="name@gmail.com"></div>'
             '<div class="auth-field"><label>Mobile number</label><input type="tel" name="phone" value="' + esc(vals['phone']) + '" required placeholder="01XXXXXXXXX" maxlength="16" autocomplete="tel"></div>'
             + _pw_field('Password', 'pw', 'password') + PW_RULES_HTML
             + _pw_field('Confirm password', 'pw2', 'confirm_password') + '<div id="pwMatch" class="pw-match"></div>'
             '<button type="submit" class="auth-submit">Create account</button></form>'
             '<div class="auth-note">A verification code will be sent to the HR panel. Ask HR for the code to finish creating your account.</div>'
             '<div class="auth-links">Already have an account? <a href="/">Login</a></div>' + PW_JS)
    return _auth_page('Create Account', 'Create account', 'Fill in your details to get started.', inner, error, wide=True)


@app.route('/forgot', methods=['GET', 'POST'])
def forgot_password():
    if session.get('logged_in'):
        return redirect(url_for('dashboard'))
    error = ''
    ident = ''
    if request.method == 'POST':
        ident = request.form.get('identifier', '').strip()[:120]
        if not ident:
            error = 'Please enter your User ID or Email.'
        else:
            mins = _otp_minutes()
            conn = _db()
            try:
                _cleanup_auth(conn)
                acc = conn.execute("SELECT * FROM app_users WHERE (user_id=? COLLATE NOCASE OR email=? COLLATE NOCASE) "
                                   "AND status='Active'", (ident, ident)).fetchone()
                is_owner_acc = bool(acc) and ADMIN_IDENTIFIER.lower() in ((acc['email'] or '').lower(), (acc['user_id'] or '').lower())
                if not acc or is_owner_acc:
                    error = 'No active account found with this User ID / Email.'
                elif _recent_request(conn, 'reset', target_id=acc['id']):
                    error = 'A request was just sent to HR. Please wait a minute before trying again.'
                else:
                    conn.execute("UPDATE auth_requests SET status='cancelled' WHERE status='pending' "
                                 "AND purpose='reset' AND target_user_id=?", (acc['id'],))
                    token = _new_request(
                        conn, 'reset', 'forgot_password', 'Forgot password request',
                        f"{acc['full_name']} ({acc['user_id']}) | {acc['email'] or 'no email'} | "
                        f"{acc['phone'] or 'no phone on file'} asked to reset the password.", mins,
                        target_user_id=acc['id'])
                    conn.commit()
                    session['auth_token'] = token
                    return redirect(url_for('auth_otp'))
            finally:
                conn.close()

    inner = ('<form method="POST">'
             '<div class="auth-field"><label>User ID / Email</label><input type="text" name="identifier" value="' + esc(ident) + '" required maxlength="120" autocomplete="off"></div>'
             '<button type="submit" class="auth-submit">Send request to HR</button></form>'
             '<div class="auth-note">The reset code appears in the HR panel. Contact HR to get the code, then enter it on the next page to set a new password.</div>'
             '<div class="auth-links"><a href="/">&larr; Back to login</a></div>')
    return _auth_page('Forgot Password', 'Forgot password?', 'Enter your User ID or Email to request a reset code.', inner, error)


@app.route('/auth/otp', methods=['GET', 'POST'])
def auth_otp():
    mins = _otp_minutes()
    conn = _db()
    try:
        token = session.get('auth_token')
        req = conn.execute("SELECT * FROM auth_requests WHERE token=?", (token,)).fetchone() if token else None
        if not req or req['status'] != 'pending':
            session.pop('auth_token', None)
            return redirect(url_for('login'))
        purpose = req['purpose']
        error = ''
        if request.method == 'POST':
            entered = re.sub(r'\D', '', request.form.get('otp', ''))
            if _is_expired(req['expires_at']):
                conn.execute("UPDATE auth_requests SET status='expired', password_hash=NULL WHERE id=?", (req['id'],))
                conn.commit()
                session.pop('auth_token', None)
                return _auth_dead_end(purpose, 'This code has expired. Please start again to get a new one.')
            if not secrets.compare_digest(entered, req['otp']):
                attempts = (req['attempts'] or 0) + 1
                if attempts >= OTP_MAX_ATTEMPTS:
                    conn.execute("UPDATE auth_requests SET status='locked', attempts=?, password_hash=NULL WHERE id=?",
                                 (attempts, req['id']))
                    conn.commit()
                    session.pop('auth_token', None)
                    return _auth_dead_end(purpose, 'Too many wrong attempts. Please start again to get a new code.')
                conn.execute("UPDATE auth_requests SET attempts=? WHERE id=?", (attempts, req['id']))
                conn.commit()
                error = f"Incorrect code. {OTP_MAX_ATTEMPTS - attempts} attempt(s) left."
            elif purpose == 'register':
                dup = conn.execute("SELECT 1 FROM app_users WHERE email=? COLLATE NOCASE OR phone=?",
                                   (req['email'], req['phone'])).fetchone()
                new_id = None
                for _ in range(40):
                    cand = f"USR-{1000 + secrets.randbelow(9000)}"
                    if not conn.execute("SELECT 1 FROM app_users WHERE user_id=? COLLATE NOCASE", (cand,)).fetchone():
                        new_id = cand
                        break
                if dup or not new_id:
                    conn.execute("UPDATE auth_requests SET status='cancelled', password_hash=NULL WHERE id=?", (req['id'],))
                    conn.commit()
                    session.pop('auth_token', None)
                    return _auth_dead_end(purpose, 'An account with this email or phone number already exists.')
                try:
                    conn.execute(
                        "INSERT INTO app_users (full_name,user_id,email,password_hash,role,status,phone,approved_by,approved_at) "
                        "VALUES (?,?,?,?,'Member','Active',?,?,CURRENT_TIMESTAMP)",
                        (req['full_name'], new_id, req['email'], req['password_hash'], req['phone'], 'HR OTP verification'))
                    conn.execute("UPDATE auth_requests SET status='used', password_hash=NULL WHERE id=?", (req['id'],))
                    conn.execute("INSERT INTO activity_logs (action) VALUES (?)",
                                 (f"New Member account {new_id} created after HR OTP verification",))
                    conn.commit()
                except sqlite3.IntegrityError:
                    conn.rollback()
                    session.pop('auth_token', None)
                    return _auth_dead_end(purpose, 'An account with this email already exists.')
                session.pop('auth_token', None)
                inner = ('<div class="auth-ok-icon">&#10003;</div>'
                         '<div class="auth-id-box">Your User ID<b>' + esc(new_id) + '</b></div>'
                         '<a class="auth-submit" href="/">Go to login</a>'
                         '<div class="auth-note">You can log in with this User ID or your email and the password you just set.</div>')
                return _auth_page('Account Created', 'Account created!', 'Your account is ready to use.', inner)
            else:
                new_exp = (_utc_now() + _TD(minutes=mins)).strftime(TS_FMT)
                conn.execute("UPDATE auth_requests SET status='verified', expires_at=? WHERE id=?", (new_exp, req['id']))
                conn.commit()
                session.pop('auth_token', None)
                session['reset_token'] = req['token']
                return redirect(url_for('auth_new_password'))

        title = 'Verify your account' if purpose == 'register' else 'Verify password reset'
        inner = ('<form method="POST"><div class="auth-field"><input type="text" name="otp" class="otp-input" inputmode="numeric" '
                 'pattern="[0-9]{6}" maxlength="6" required placeholder="------" autocomplete="one-time-code" autofocus></div>'
                 '<button type="submit" class="auth-submit">Verify</button></form>'
                 '<div class="auth-note">The 6-digit code has been sent to the HR panel. Ask HR for the code. '
                 'It expires in about ' + str(_minutes_left(req['expires_at'])) + ' minute(s).</div>'
                 '<div class="auth-links"><a href="/">&larr; Back to login</a></div>')
        return _auth_page('Verify', title, 'Enter the code you got from HR.', inner, error)
    finally:
        conn.close()


@app.route('/auth/new-password', methods=['GET', 'POST'])
def auth_new_password():
    conn = _db()
    try:
        token = session.get('reset_token')
        req = conn.execute("SELECT * FROM auth_requests WHERE token=? AND purpose='reset' AND status='verified'",
                           (token,)).fetchone() if token else None
        if not req or _is_expired(req['expires_at']):
            session.pop('reset_token', None)
            return _auth_dead_end('reset', 'Your reset session has expired. Please request a new code.')
        error = ''
        if request.method == 'POST':
            pw = request.form.get('password', '')
            confirm = request.form.get('confirm_password', '')
            if not password_ok(pw):
                error = PW_RULE_TEXT
            elif pw != confirm:
                error = 'Passwords do not match.'
            else:
                acc = conn.execute("SELECT id,user_id FROM app_users WHERE id=? AND status='Active'",
                                   (req['target_user_id'],)).fetchone()
                if not acc:
                    session.pop('reset_token', None)
                    return _auth_dead_end('reset', 'This account is no longer active.')
                conn.execute("UPDATE app_users SET password_hash=? WHERE id=?", (generate_password_hash(pw), acc['id']))
                conn.execute("UPDATE auth_requests SET status='used' WHERE id=?", (req['id'],))
                conn.execute("INSERT INTO activity_logs (action) VALUES (?)",
                             (f"Password reset for {acc['user_id']} after HR OTP verification",))
                conn.commit()
                session.pop('reset_token', None)
                inner = ('<div class="auth-ok-icon">&#10003;</div><a class="auth-submit" href="/">Go to login</a>')
                return _auth_page('Password Updated', 'Password updated!', 'You can now log in with your new password.', inner)
        inner = ('<form method="POST" id="authForm">' + _pw_field('New password', 'pw', 'password') + PW_RULES_HTML
                 + _pw_field('Confirm new password', 'pw2', 'confirm_password') + '<div id="pwMatch" class="pw-match"></div>'
                 '<button type="submit" class="auth-submit">Save new password</button></form>' + PW_JS)
        return _auth_page('New Password', 'Set a new password', 'Choose a strong password for your account.', inner, error)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HR notification bell (only HR users see it and can read these endpoints)
# ---------------------------------------------------------------------------
@app.route('/notifications/data')
def notifications_data():
    if not session.get('logged_in') or session.get('role') != 'HR':
        return jsonify({'error': 'forbidden'}), 403
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT n.id,n.kind,n.title,n.message,n.is_read,n.created_at,r.otp,r.status,r.expires_at "
            "FROM hr_notifications n LEFT JOIN auth_requests r ON r.id=n.request_id ORDER BY n.id DESC LIMIT 30").fetchall()
        unread = conn.execute("SELECT COUNT(*) FROM hr_notifications WHERE is_read=0").fetchone()[0]
    finally:
        conn.close()
    items = []
    for r in rows:
        state, otp = 'expired', None
        if r['status'] == 'pending' and not _is_expired(r['expires_at']):
            state, otp = 'waiting', r['otp']
        elif r['status'] in ('used', 'verified'):
            state = 'used'
        elif r['status'] == 'cancelled':
            state = 'cancelled'
        items.append({'id': r['id'], 'kind': r['kind'], 'title': r['title'], 'message': r['message'],
                      'is_read': bool(r['is_read']), 'created_at': r['created_at'], 'otp': otp, 'state': state})
    resp = jsonify({'unread': unread, 'items': items})
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/notifications/read', methods=['POST'])
def notifications_read():
    if not session.get('logged_in') or session.get('role') != 'HR':
        return jsonify({'error': 'forbidden'}), 403
    conn = _db()
    try:
        conn.execute("UPDATE hr_notifications SET is_read=1 WHERE is_read=0")
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})


HR_BELL_HTML = '''<style>
.notif-wrap { position: relative; }
.notif-bell { position: relative; width: 40px; height: 40px; border-radius: 50%; background: #f1f5f9; border: 1px solid #e2e8f0; color: #334155; display: flex; align-items: center; justify-content: center; cursor: pointer; padding: 0; }
.notif-bell:hover { background: #e2e8f0; }
.notif-bell svg { width: 20px; height: 20px; }
.notif-badge { position: absolute; top: -4px; right: -4px; min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px; background: #dc2626; color: #fff; font-size: 11px; font-weight: 700; display: none; align-items: center; justify-content: center; box-sizing: border-box; }
.notif-badge.show { display: flex; }
.notif-bell.ring svg { animation: bellRing .8s ease; transform-origin: 50% 10%; }
@keyframes bellRing { 0%,100% { transform: rotate(0); } 15% { transform: rotate(14deg); } 30% { transform: rotate(-12deg); } 45% { transform: rotate(9deg); } 60% { transform: rotate(-6deg); } 75% { transform: rotate(3deg); } }
.notif-panel { display: none; position: absolute; top: 50px; right: 0; width: 360px; max-width: 92vw; background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; box-shadow: 0 10px 25px rgba(0,0,0,.2); z-index: 99999; overflow: hidden; color: #1e293b; }
.notif-panel.show { display: block; }
.notif-head { padding: 12px 14px; font-weight: 700; font-size: 14px; border-bottom: 1px solid #e2e8f0; }
.notif-list { max-height: 420px; overflow-y: auto; }
.notif-item { padding: 12px 14px; border-bottom: 1px solid #f1f5f9; font-size: 13px; text-align: left; }
.notif-item.unread { background: #eff6ff; }
.notif-title { font-weight: 700; margin-bottom: 3px; }
.notif-msg { color: #475569; line-height: 1.4; }
.notif-otp { margin-top: 8px; display: flex; align-items: center; gap: 8px; }
.notif-otp code { font-size: 20px; font-weight: 700; letter-spacing: 4px; background: #f1f5f9; border-radius: 6px; padding: 4px 10px; color: #0f172a; }
.notif-otp button { padding: 4px 10px; font-size: 12px; }
.notif-meta { margin-top: 6px; font-size: 11px; color: #94a3b8; }
.notif-empty { padding: 26px; text-align: center; color: #94a3b8; font-size: 13px; }
</style>
<div class="notif-wrap">
  <button type="button" class="notif-bell" id="notifBell" aria-label="Notifications" title="Notifications">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
    <span class="notif-badge" id="notifBadge">0</span>
  </button>
  <div class="notif-panel" id="notifPanel">
    <div class="notif-head">Notifications</div>
    <div class="notif-list" id="notifList"><div class="notif-empty">Loading...</div></div>
  </div>
</div>
<script>
(function(){
  var bell=document.getElementById('notifBell'), panel=document.getElementById('notifPanel'),
      badge=document.getElementById('notifBadge'), list=document.getElementById('notifList');
  if(!bell) return;
  var lastUnread=null;
  var STATE={waiting:'Waiting for the user to enter the code',used:'Code used',expired:'Code expired',cancelled:'Replaced by a newer request'};
  function ago(ts){
    var d=new Date(String(ts).replace(' ','T')+'Z'), s=Math.floor((Date.now()-d.getTime())/1000);
    if(isNaN(s)) return '';
    if(s<60) return 'just now';
    if(s<3600) return Math.floor(s/60)+' min ago';
    if(s<86400) return Math.floor(s/3600)+' hr ago';
    return Math.floor(s/86400)+' day ago';
  }
  function render(items){
    list.textContent='';
    if(!items.length){var e=document.createElement('div');e.className='notif-empty';e.textContent='No notifications yet';list.appendChild(e);return;}
    items.forEach(function(n){
      var it=document.createElement('div'); it.className='notif-item'+(n.is_read?'':' unread');
      var t=document.createElement('div'); t.className='notif-title'; t.textContent=(n.kind==='forgot_password'?'\\uD83D\\uDD11 ':'\\uD83D\\uDC64 ')+n.title;
      var m=document.createElement('div'); m.className='notif-msg'; m.textContent=n.message;
      it.appendChild(t); it.appendChild(m);
      if(n.otp){
        var o=document.createElement('div'); o.className='notif-otp';
        var c=document.createElement('code'); c.textContent=n.otp;
        var b=document.createElement('button'); b.type='button'; b.textContent='Copy';
        b.onclick=function(ev){ev.stopPropagation(); if(navigator.clipboard){navigator.clipboard.writeText(n.otp);} b.textContent='Copied'; setTimeout(function(){b.textContent='Copy';},1500);};
        o.appendChild(c); o.appendChild(b); it.appendChild(o);
      }
      var meta=document.createElement('div'); meta.className='notif-meta'; meta.textContent=ago(n.created_at)+' \\u00B7 '+(STATE[n.state]||'');
      it.appendChild(meta); list.appendChild(it);
    });
  }
  function load(markRead){
    fetch('/notifications/data',{credentials:'same-origin',cache:'no-store'})
      .then(function(r){return r.ok?r.json():null;})
      .then(function(d){
        if(!d) return;
        render(d.items);
        badge.textContent=d.unread>99?'99+':d.unread; badge.classList.toggle('show',d.unread>0);
        if(lastUnread!==null && d.unread>lastUnread){
          bell.classList.remove('ring'); void bell.offsetWidth; bell.classList.add('ring');
          if(window.Swal && !panel.classList.contains('show')){Swal.fire({toast:true,position:'top-end',icon:'info',title:'New notification in the HR panel',showConfirmButton:false,timer:3500});}
        }
        lastUnread=d.unread;
        if(markRead && d.unread>0){
          fetch('/notifications/read',{method:'POST',credentials:'same-origin'}).then(function(){lastUnread=0;badge.classList.remove('show');});
        }
      }).catch(function(){});
  }
  bell.addEventListener('click',function(e){e.stopPropagation(); var open=panel.classList.toggle('show'); if(open) load(true);});
  panel.addEventListener('click',function(e){e.stopPropagation();});
  document.addEventListener('click',function(){panel.classList.remove('show');});
  load(false);
  setInterval(function(){ if(!document.hidden) load(panel.classList.contains('show')); },10000);
})();
</script>'''


@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
        
    user_id = session.get('user_id', 'ADMIN-7242')
    name = session.get('name', 'User')
    role = session.get('role', 'Member')
    user_email = session.get('user', 'tanvir@opsnexus.com')
    profile_pic = session.get('profile_pic', '')
    tab = request.args.get('tab', 'overview')
    theme_key = get_user_theme(session.get('user') or session.get('user_id'))

    # Escaped versions for safe HTML output
    name_e = esc(name)
    user_id_e = esc(user_id)
    user_email_e = esc(user_email)
    role_e = esc(role)
    profile_pic_e = esc(profile_pic)
    
    delete_msg = request.args.get('delete_msg', '')
    success_msg = request.args.get('success_msg', '')
    
    from_date = safe_date(request.args.get('from_date', ''))
    to_date = safe_date(request.args.get('to_date', ''))
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    
    show_no_data_popup = False
    if tab == 'employees':
        if from_date and to_date:
            employees = conn.execute("SELECT * FROM employees WHERE created_date BETWEEN ? AND ?", (from_date, to_date)).fetchall()
            if len(employees) == 0:
                show_no_data_popup = True
        else:
            employees = conn.execute("SELECT * FROM employees").fetchall()
    else:
        employees = conn.execute("SELECT * FROM employees").fetchall()

    if tab == 'requisitions':
        if from_date and to_date:
            requisitions = conn.execute("SELECT * FROM requisitions WHERE created_date BETWEEN ? AND ?", (from_date, to_date)).fetchall()
            if len(requisitions) == 0:
                show_no_data_popup = True
        else:
            requisitions = conn.execute("SELECT * FROM requisitions").fetchall()
    else:
        requisitions = conn.execute("SELECT * FROM requisitions").fetchall()
        
    logs = conn.execute("SELECT * FROM activity_logs ORDER BY id DESC LIMIT 15").fetchall()
    
    total_balance = sum(emp['balance'] for emp in employees)
    total_used = sum(emp['used'] for emp in employees)
    total_employees = len(employees)
    total_requisitions = len(requisitions)
    approved_requisitions = len([r for r in requisitions if r['status'] == 'Approved'])
    conn.close()
    
    avatar_html = f'<img src="{profile_pic_e}" alt="Profile">' if profile_pic else esc(name[0].upper() if name else '?')
    
    company_name = esc(load_settings().get('company_name', 'OpsNexus'))
    brand_logo_html = f'''
    <a href="/dashboard?tab=overview" class="brand-logo-container">
        <svg width="36" height="36" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
            <defs>
                <linearGradient id="opsGradient" x1="0%" y1="0%" x2="100%" y2="100%">
                    <stop offset="0%" stop-color="#2563EB" />
                    <stop offset="100%" stop-color="#06B6D4" />
                </linearGradient>
            </defs>
            <path d="M24 4L40 13.24V34.76L24 44L8 34.76V13.24L24 4Z" fill="url(#opsGradient)" opacity="0.15" />
            <path d="M24 8L36 15V33L24 40L12 33V15L24 8Z" stroke="url(#opsGradient)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />
            <circle cx="24" cy="24" r="5" fill="url(#opsGradient)" />
            <circle cx="24" cy="12" r="3" fill="#2563EB" />
            <circle cx="34" cy="30" r="3" fill="#06B6D4" />
            <circle cx="14" cy="30" r="3" fill="#2563EB" />
            <path d="M24 15V19" stroke="url(#opsGradient)" stroke-width="2.5" stroke-linecap="round" />
            <path d="M28 26.5L32 28.5" stroke="url(#opsGradient)" stroke-width="2.5" stroke-linecap="round" />
            <path d="M20 26.5L16 28.5" stroke="url(#opsGradient)" stroke-width="2.5" stroke-linecap="round" />
        </svg>
        <span class="brand-text">{company_name}</span>
    </a>
    '''
    
    badge_class = 'badge-member'
    if role == 'HR':
        badge_class = 'badge-hr'
    elif role == 'Admin':
        badge_class = 'badge-admin'

    popup_action_js = ""
    if delete_msg:
        popup_action_js = """
        <script>
            document.addEventListener('DOMContentLoaded', function() {
                Swal.fire({
                    title: 'Moved to Recycle Bin!',
                    html: '<div style="font-size: 36px; margin: 12px 0; position: relative; height: 50px;"><span class="falling-paper" style="position: absolute; left: calc(50% - 22px);">📄</span><span style="position: absolute; left: calc(50% - 15px); top: 15px;">🗑️</span></div>' + """ + js_str(fmt_msg(delete_msg)) + """,
                    icon: 'success',
                    confirmButtonText: 'OK',
                    confirmButtonColor: '#2563EB'
                });
                const u = new URL(window.location);
                u.searchParams.delete('delete_msg');
                window.history.replaceState({}, '', u);
            });
        </script>
        """
    elif success_msg:
        p_icon, p_title, p_text = popup_info(success_msg)
        p_color = '#dc2626' if p_icon == 'error' else '#2563EB'
        popup_action_js = """
        <script>
            document.addEventListener('DOMContentLoaded', function() {
                Swal.fire({
                    icon: """ + js_str(p_icon) + """,
                    title: """ + js_str(p_title) + """,
                    text: """ + js_str(p_text) + """,
                    confirmButtonText: 'OK',
                    confirmButtonColor: '""" + p_color + """',
                    timer: 4000,
                    timerProgressBar: true
                });
                const u = new URL(window.location);
                u.searchParams.delete('success_msg');
                window.history.replaceState({}, '', u);
            });
        </script>
        """

    content_area = ""
    _can_reports = role in ('Admin', 'HR')
    lnk_members = '/dashboard?tab=employees'
    lnk_balance = '/report/balance_report' if _can_reports else '/dashboard?tab=employees'
    lnk_used = '/report/mobile_bill' if _can_reports else '/dashboard?tab=employees'
    lnk_reqs = '/dashboard?tab=requisitions'
    if tab == 'overview':
        chart_js = """
        <script>
            const ctx = document.getElementById('financeChart').getContext('2d');
            const financeChart = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: ['Current Balance', 'Total Used / Recharged'],
                    datasets: [{
                        label: 'Amount in BDT (৳)',
                        data: [""" + str(total_balance) + """, """ + str(total_used) + """],
                        backgroundColor: ['rgba(16, 185, 129, 0.7)', 'rgba(37, 99, 235, 0.7)'],
                        borderColor: ['rgb(16, 185, 129)', 'rgb(37, 99, 235)'],
                        borderWidth: 1
                    }]
                },
                options: {
                    responsive: true,
                    scales: { y: { beginAtZero: true } }
                }
            });
        </script>
        """

        content_area = '''
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px;">
            <div>
                <h2 style="margin: 0; color: #1e293b; font-size: 20px;">Dashboard</h2>
                <p style="margin: 3px 0 0 0; color: #64748b; font-size: 13px;">Business overview at a glance</p>
            </div>
            <div>
                <select style="padding: 6px 12px; background: white; font-size: 13px;">
                    <option>All Time</option>
                    <option>Today</option>
                    <option>This Month</option>
                </select>
            </div>
        </div>

        <div class="metrics-grid">
            <a class="metric-card" href="'''+ lnk_members +'''" title="Open details">
                <div class="metric-icon" style="background: #eff6ff; color: #2563EB;">👥</div>
                <div class="metric-info">
                    <h3>TOTAL MEMBERS</h3>
                    <div class="val">''' + str(total_employees) + '''</div>
                </div>
            </a>
            <a class="metric-card" href="'''+ lnk_balance +'''" title="Open details">
                <div class="metric-icon" style="background: #dcfce7; color: #10b981;">💰</div>
                <div class="metric-info">
                    <h3>TOTAL CURRENT BALANCE</h3>
                    <div class="val" style="color: #10b981;">৳''' + str(total_balance) + '''</div>
                </div>
            </a>
            <a class="metric-card" href="'''+ lnk_used +'''" title="Open details">
                <div class="metric-icon" style="background: #eff6ff; color: #2563EB;">⚡</div>
                <div class="metric-info">
                    <h3>TOTAL USED / RECHARGED</h3>
                    <div class="val" style="color: #2563EB;">৳''' + str(total_used) + '''</div>
                </div>
            </a>
            <a class="metric-card" href="'''+ lnk_reqs +'''" title="Open details">
                <div class="metric-icon" style="background: #fef3c7; color: #f59e0b;">📋</div>
                <div class="metric-info">
                    <h3>OFFICE REQUISITIONS</h3>
                    <div class="val" style="color: #f59e0b;">''' + str(total_requisitions) + '''</div>
                </div>
            </a>
        </div>
        
        <div class="panel">
            <h3 style="margin-top: 0; font-size: 16px; color: #1e293b;">📊 Financial Overview: Balance vs Used Amount</h3>
            <p style="color: #64748b; font-size: 13px; margin-bottom: 20px;">Comparison chart showing available balance versus total used/recharged amount across the system.</p>
            <div style="width: 100%; max-width: 650px; margin: auto;">
                <canvas id="financeChart" height="130"></canvas>
            </div>
        </div>
        ''' + chart_js
        
    elif tab == 'employees':
        table_rows = ""
        if len(employees) == 0:
            table_rows = '<tr><td colspan="8" style="text-align: center; color: #64748b; padding: 20px;">No members available.</td></tr>'
        else:
            for emp in employees:
                created_dt = esc(emp['created_date'] if 'created_date' in emp.keys() and emp['created_date'] else '2026-11-10')
                e_name = esc(emp['name'])
                e_phone = esc(emp['phone'])
                e_desig = esc(emp['designation'])
                e_oper = esc(emp['operator'])
                e_name_js = js_attr(emp['name'])
                checkbox_cell = f'<td style="width: 30px; text-align: center;"><input type="checkbox" name="emp_ids" value="{emp["id"]}" class="member-checkbox" onchange="checkSelectedCount(\'member-checkbox\', \'bulkDeleteMemberBtn\')"></td>' if role in ["HR", "Admin"] else '<td></td>'
                
                table_rows += f'''
                    <tr>
                        {checkbox_cell}
                        <td><b>{e_name}</b></td>
                        <td>{e_phone}</td>
                        <td>{e_desig}</td>
                        <td>{e_oper}</td>
                        <td style="color: #10b981; font-weight: bold;">৳{emp['balance']}</td>
                        <td>{created_dt}</td>
                        <td style="text-align: right;">
                            <div class="dropdown" id="dropdown-{emp['id']}">
                                <button type="button" onclick="toggleRowDropdown({emp['id']}, event)" class="dropbtn">⋮</button>
                                <div class="dropdown-content" id="dropdown-content-{emp['id']}">
                                    <a href="#" onclick="openEditModal({emp['id']}, {e_name_js}, {emp['balance']}); return false;">✏️ Edit</a>
                                    <a href="/print_member/{emp['id']}" target="_blank">🖨️ Print</a>
                                    <a href="#" onclick="confirmDelete({emp['id']}, {e_name_js}); return false;" class="delete-item">🗑️ Delete</a>
                                </div>
                            </div>
                        </td>
                    </tr>
                '''
            
        add_form_section = ""
        if role in ["HR", "Admin"]:
            show_add = request.args.get('show_add') == '1'
            display_value = 'block' if show_add else 'none'
            add_form_section = """
            <div id="addMemberModal" style="display: """ + display_value + """; background: #ffffff; padding: 20px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #e2e8f0; box-shadow: 0 2px 4px rgba(0,0,0,0.02);">
                <h4 style="margin-top: 0; color: #1e293b;">+ Add New Member</h4>
                <form id="addMemberForm" action="/add_employee" method="POST" onsubmit="return validateForm()" style="display: grid; grid-template-columns: 1fr 1fr 1fr 1fr 1fr auto; gap: 10px; align-items: end;">
                    <div>
                        <label style="font-size: 12px; color: #64748b;">Name</label>
                        <input type="text" name="name" id="nameInput" placeholder="Full Name">
                    </div>
                    <div>
                        <label style="font-size: 12px; color: #64748b;">Phone</label>
                        <input type="text" name="phone" id="phoneInput" placeholder="017xxxxxxxx" maxlength="11">
                    </div>
                    <div>
                        <label style="font-size: 12px; color: #64748b;">Designation</label>
                        <input type="text" name="designation" id="desigInput" placeholder="Designation">
                    </div>
                    <div>
                        <label style="font-size: 12px; color: #64748b;">Operator</label>
                        <select name="operator" id="operatorSelect">
                            <option value="GP">GP</option>
                            <option value="Robi">Robi</option>
                            <option value="Banglalink">Banglalink</option>
                            <option value="Airtel">Airtel</option>
                            <option value="Teletalk">Teletalk</option>
                        </select>
                    </div>
                    <div>
                        <label style="font-size: 12px; color: #64748b;">Balance (৳)</label>
                        <input type="number" name="balance" id="balanceInput" placeholder="0" value="0">
                    </div>
                    <div>
                        <button type="submit">Save Member</button>
                    </div>
                </form>
            </div>
            
            <script>
                function validateForm() {
                    const fields = ['nameInput', 'phoneInput', 'desigInput', 'balanceInput'];
                    let hasEmpty = false;
                    
                    fields.forEach(id => {
                        const input = document.getElementById(id);
                        if (!input.value.trim()) {
                            input.classList.add('invalid-field');
                            hasEmpty = true;
                        } else {
                            input.classList.remove('invalid-field');
                        }
                    });

                    if (hasEmpty) {
                        Swal.fire({
                            icon: 'error',
                            title: 'Required Fields Empty',
                            text: 'Please fill in all the highlighted fields to save the member!',
                            confirmButtonColor: '#dc2626'
                        });
                        return false;
                    }

                    const phone = document.getElementById('phoneInput').value.trim();
                    const operator = document.getElementById('operatorSelect').value;
                    const phoneRegex = /^01[0-9]{9}$/;

                    if (!phoneRegex.test(phone)) {
                        document.getElementById('phoneInput').classList.add('invalid-field');
                        Swal.fire({
                            icon: 'error',
                            title: 'Invalid Phone Number',
                            text: 'Please enter a valid 11-digit Bangladeshi mobile number starting with 01.',
                            confirmButtonColor: '#2563EB'
                        });
                        return false;
                    }
                    
                    const prefix = phone.substring(0, 3);
                    let valid = false;
                    let msg = '';

                    if (operator === 'GP') {
                        if (prefix === '017' || prefix === '013') valid = true;
                        else msg = 'GP number must start with 017 or 013!';
                    } else if (operator === 'Robi') {
                        if (prefix === '018') valid = true;
                        else msg = 'Robi number must start with 018!';
                    } else if (operator === 'Banglalink') {
                        if (prefix === '019' || prefix === '014') valid = true;
                        else msg = 'Banglalink number must start with 019 or 014!';
                    } else if (operator === 'Airtel') {
                        if (prefix === '016') valid = true;
                        else msg = 'Airtel number must start with 016!';
                    } else if (operator === 'Teletalk') {
                        if (prefix === '015') valid = true;
                        else msg = 'Teletalk number must start with 015!';
                    }

                    if (!valid) {
                        document.getElementById('phoneInput').classList.add('invalid-field');
                        Swal.fire({
                            icon: 'warning',
                            title: 'Operator Mismatch',
                            text: msg + ' You selected ' + operator + '.',
                            confirmButtonColor: '#2563EB'
                        });
                        return false;
                    }

                    return true;
                }
            </script>
            """

        bulk_delete_btn = f'''<button type="button" id="bulkDeleteMemberBtn" onclick="submitBulkDelete('bulkDeleteMemberForm', 'member', 'member-checkbox')" class="btn-danger" style="display: none; padding: 7px 14px; font-weight: bold; margin-right: 8px;">🗑️ Delete Selected</button>''' if role in ["HR", "Admin"] else ""
        add_button_html = f'''<a href="/dashboard?tab=employees&show_add=1" style="background: #2563EB; color: white; padding: 8px 16px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: bold; display: inline-flex; align-items: center; gap: 6px;">+ Add Member</a>''' if role in ["HR", "Admin"] else ""

        filter_button_html = '''
        <div style="position: relative; display: inline-block;">
            <button type="button" id="filterBtn" onclick="toggleFilterCalendar(event)" style="background: #2563EB; color: white; padding: 8px 16px; border-radius: 6px; border: none; font-size: 14px; font-weight: bold; cursor: pointer; display: inline-flex; align-items: center; gap: 6px;">
                🔍 Filter
            </button>
            <div id="filterCalendarBox" class="filter-dropdown-box">
                <h4 style="margin-top: 0; font-size: 14px; color: #1e293b; border-bottom: 1px solid #e2e8f0; padding-bottom: 6px;">Filter by Date Range</h4>
                <form method="GET" action="/dashboard" style="display: flex; flex-direction: column; gap: 10px;">
                    <input type="hidden" name="tab" value="employees">
                    <div>
                        <label style="font-size: 11px; color: #64748b; display: block;">From Date</label>
                        <input type="date" name="from_date" value="''' + from_date + '''" style="margin-top: 0; padding: 6px;">
                    </div>
                    <div>
                        <label style="font-size: 11px; color: #64748b; display: block;">To Date</label>
                        <input type="date" name="to_date" value="''' + to_date + '''" style="margin-top: 0; padding: 6px;">
                    </div>
                    <div style="display: flex; justify-content: space-between; margin-top: 5px;">
                        <button type="submit" style="padding: 6px 12px; font-size: 13px;">Apply Filter</button>
                        <a href="/dashboard?tab=employees" style="font-size: 13px; color: #64748b; text-decoration: none; align-self: center;">Reset</a>
                    </div>
                </form>
            </div>
        </div>
        '''

        popup_js = ""
        if show_no_data_popup:
            popup_js = f"""
            <script>
                document.addEventListener('DOMContentLoaded', function() {{
                    Swal.fire({{
                        icon: 'warning',
                        title: 'No Data Found',
                        text: 'No member record found between {esc(from_date)} and {esc(to_date)}!',
                        confirmButtonColor: '#2563EB'
                    }});
                }});
            </script>
            """

        edit_modal_html = """
        <div id="editBalanceModal" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 2000; justify-content: center; align-items: center;">
            <div style="background: white; padding: 30px; border-radius: 10px; width: 100%; max-width: 400px; box-shadow: 0 10px 25px rgba(0,0,0,0.2);">
                <h3 style="margin-top: 0; color: #1e293b;">Edit Balance for <span id="editEmpName" style="color: #2563EB;"></span></h3>
                <form action="/update_balance" method="POST">
                    <input type="hidden" name="emp_id" id="editEmpId">
                    <div style="margin-bottom: 20px;">
                        <label style="font-size: 13px; color: #64748b; font-weight: bold;">New Balance Amount (৳)</label>
                        <input type="number" name="new_balance" id="editEmpBalance" required style="margin-top: 8px;">
                    </div>
                    <div style="display: flex; justify-content: flex-end; gap: 10px;">
                        <button type="button" onclick="closeEditModal()" style="background: #64748b;">Cancel</button>
                        <button type="submit" style="background: #10b981;">Save Changes</button>
                    </div>
                </form>
            </div>
        </div>
        <script>
            function openEditModal(id, name, balance) {
                document.getElementById('editEmpId').value = id;
                document.getElementById('editEmpName').innerText = name;
                document.getElementById('editEmpBalance').value = balance;
                document.getElementById('editBalanceModal').style.display = 'flex';
            }
            function closeEditModal() {
                document.getElementById('editBalanceModal').style.display = 'none';
            }
        </script>
        """

        master_chk = f'<input type="checkbox" id="selectAllMember" onclick="toggleSelectAll(this, \'member-checkbox\', \'bulkDeleteMemberBtn\')">' if role in ["HR", "Admin"] else ''

        metrics_cards_html = f'''
        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-icon" style="background: #eff6ff; color: #2563EB;">👥</div>
                <div class="metric-info">
                    <h3>Total Members</h3>
                    <div class="val">{total_employees}</div>
                </div>
            </div>
            <div class="metric-card">
                <div class="metric-icon" style="background: #dcfce7; color: #10b981;">✅</div>
                <div class="metric-info">
                    <h3>Active Members</h3>
                    <div class="val">{total_employees}</div>
                </div>
            </div>
            <div class="metric-card">
                <div class="metric-icon" style="background: #fee2e2; color: #dc2626;">💰</div>
                <div class="metric-info">
                    <h3>Total Cost Balance</h3>
                    <div class="val" style="color: #dc2626;">৳{total_balance}</div>
                </div>
            </div>
            <div class="metric-card">
                <div class="metric-icon" style="background: #fef3c7; color: #d97706;">⚠️</div>
                <div class="metric-info">
                    <h3>Need Balance</h3>
                    <div class="val" style="color: #d97706;">৳0</div>
                </div>
            </div>
        </div>
        '''

        content_area = '''
        <div style="background: #2563EB; padding: 12px 20px; border-radius: 8px; display: flex; justify-content: flex-end; align-items: center; margin-bottom: 25px; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
            <div style="display: flex; gap: 10px; align-items: center;">
                ''' + filter_button_html + '''
                ''' + add_button_html + '''
            </div>
        </div>

        ''' + metrics_cards_html + '''

        ''' + add_form_section + '''

        <div class="panel">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                <h3 style="margin: 0;">👥 All Members List (''' + str(len(employees)) + ''')</h3>
                <div>''' + bulk_delete_btn + '''</div>
            </div>
            <form id="bulkDeleteMemberForm" action="/bulk_delete_members" method="POST">
                <table>
                    <thead>
                        <tr>
                            <th style="width: 30px; text-align: center;">''' + master_chk + '''</th>
                            <th>Name</th>
                            <th>Phone</th>
                            <th>Designation</th>
                            <th>Operator</th>
                            <th>Balance</th>
                            <th>Date Added</th>
                            <th style="text-align: right;">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        ''' + table_rows + '''
                    </tbody>
                </table>
            </form>
        </div>
        ''' + edit_modal_html + popup_js
        
    elif tab == 'requisitions':
        req_rows = ""
        if len(requisitions) == 0:
            req_rows = '<tr><td colspan="7" style="text-align: center; color: #64748b;">No requisitions submitted yet.</td></tr>'
        else:
            for req in requisitions:
                if req['status'] == "Pending":
                    status_badge = '<span class="badge badge-pending">Pending</span>'
                elif req['status'] == "Approved":
                    status_badge = '<span class="badge badge-approved">Approved</span>'
                else:
                    status_badge = '<span class="badge badge-rejected">Rejected</span>'
                
                req_remark = req['remark'] if 'remark' in req.keys() else None
                if req['status'] == "Rejected" and req_remark:
                    status_badge += f'<div style="font-size: 11px; color: #dc2626; margin-top: 4px; max-width: 220px;">Remark: {esc(req_remark)}</div>'

                approve_links = ""
                if role in ["HR", "Admin"] and req['status'] == "Pending":
                    approve_links = f'''
                        <a href="/update_requisition/{req['id']}/Approved" style="color: #10b981; font-weight: bold; text-decoration: none;">Approve</a>
                        <a href="#" onclick="rejectRequisition({req['id']}); return false;" style="color: #dc2626; font-weight: bold; text-decoration: none;">Reject</a>
                    '''

                r_type_js = js_attr(req['type'])
                r_details_js = js_attr(req['details'])

                action_dropdown = f'''
                    <div class="dropdown" id="dropdown-{req['id']}">
                        <button type="button" onclick="toggleRowDropdown({req['id']}, event)" class="dropbtn">⋮</button>
                        <div class="dropdown-content" id="dropdown-content-{req['id']}">
                            <a href="#" onclick="openEditReqModal({req['id']}, {r_type_js}, {r_details_js}); return false;">✏️ Edit</a>
                            <a href="/print_requisition/{req['id']}" target="_blank">🖨️ Print</a>
                            <a href="#" onclick="confirmDeleteReq({req['id']}); return false;" class="delete-item">🗑️ Delete</a>
                        </div>
                    </div>
                '''
                
                chk_cell = f'<td style="width: 30px; text-align: center;"><input type="checkbox" name="req_ids" value="{req["id"]}" class="req-checkbox" onchange="checkSelectedCount(\'req-checkbox\', \'bulkDeleteReqBtn\')"></td>' if role in ["HR", "Admin"] else '<td></td>'

                req_rows += f'''
                    <tr>
                        {chk_cell}
                        <td>#{req['id']}</td>
                        <td><b>{esc(req['emp_name'])}</b></td>
                        <td>{esc(req['type'])}</td>
                        <td>{esc(req['details'])}</td>
                        <td>{status_badge}</td>
                        <td>
                            <div style="display: flex; align-items: center; justify-content: flex-end; gap: 10px;">
                                {approve_links}
                                {action_dropdown}
                            </div>
                        </td>
                    </tr>
                '''
            
        req_bulk_btn = f'''<button type="button" id="bulkDeleteReqBtn" onclick="submitBulkDelete('bulkDeleteReqForm', 'requisition', 'req-checkbox')" class="btn-danger" style="display: none; padding: 7px 14px; font-weight: bold;">🗑️ Delete Selected</button>''' if role in ["HR", "Admin"] else ""
        master_req_chk = f'<input type="checkbox" id="selectAllReq" onclick="toggleSelectAll(this, \'req-checkbox\', \'bulkDeleteReqBtn\')">' if role in ["HR", "Admin"] else ''

        req_filter_button_html = '''
        <div style="position: relative; display: inline-block;">
            <button type="button" id="filterBtn" onclick="toggleFilterCalendar(event)" style="background: #2563EB; color: white; padding: 8px 16px; border-radius: 6px; border: none; font-size: 14px; font-weight: bold; cursor: pointer; display: inline-flex; align-items: center; gap: 6px;">
                🔍 Filter
            </button>
            <div id="filterCalendarBox" class="filter-dropdown-box">
                <h4 style="margin-top: 0; font-size: 14px; color: #1e293b; border-bottom: 1px solid #e2e8f0; padding-bottom: 6px;">Filter by Date Range</h4>
                <form method="GET" action="/dashboard" style="display: flex; flex-direction: column; gap: 10px;">
                    <input type="hidden" name="tab" value="requisitions">
                    <div>
                        <label style="font-size: 11px; color: #64748b; display: block;">From Date</label>
                        <input type="date" name="from_date" value="''' + from_date + '''" style="margin-top: 0; padding: 6px;">
                    </div>
                    <div>
                        <label style="font-size: 11px; color: #64748b; display: block;">To Date</label>
                        <input type="date" name="to_date" value="''' + to_date + '''" style="margin-top: 0; padding: 6px;">
                    </div>
                    <div style="display: flex; justify-content: space-between; margin-top: 5px;">
                        <button type="submit" style="padding: 6px 12px; font-size: 13px;">Apply Filter</button>
                        <a href="/dashboard?tab=requisitions" style="font-size: 13px; color: #64748b; text-decoration: none; align-self: center;">Reset</a>
                    </div>
                </form>
            </div>
        </div>
        '''

        # Popup for "no data" in requisitions filter (was missing in the original)
        req_popup_js = ""
        if show_no_data_popup:
            req_popup_js = f"""
            <script>
                document.addEventListener('DOMContentLoaded', function() {{
                    Swal.fire({{
                        icon: 'warning',
                        title: 'No Data Found',
                        text: 'No requisition record found between {esc(from_date)} and {esc(to_date)}!',
                        confirmButtonColor: '#2563EB'
                    }});
                }});
            </script>
            """

        show_add_req = request.args.get('show_add_req') == '1'
        add_req_modal_html = f'''
        <div id="addReqModal" style="display: {'flex' if show_add_req else 'none'}; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 2000; justify-content: center; align-items: center;">
            <div style="background: white; padding: 30px; border-radius: 10px; width: 100%; max-width: 450px; box-shadow: 0 10px 25px rgba(0,0,0,0.2);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                    <h3 style="margin: 0; color: #1e293b;">Submit New Requisition</h3>
                    <a href="/dashboard?tab=requisitions" style="font-size: 18px; color: #64748b; text-decoration: none; font-weight: bold;">&times;</a>
                </div>
                <form action="/add_requisition" method="POST" style="display: flex; flex-direction: column; gap: 15px;">
                    <div>
                        <label style="font-size: 13px; color: #64748b; font-weight: bold;">Request Type</label>
                        <select name="req_type" required>
                            <option value="Phone Bill Increase">Phone Bill Increase</option>
                            <option value="Accessories">Accessories (Headphone/Charger)</option>
                            <option value="Others">Others</option>
                        </select>
                    </div>
                    <div>
                        <label style="font-size: 13px; color: #64748b; font-weight: bold;">Details Description</label>
                        <input type="text" name="details" placeholder="Describe your request..." required style="margin-top: 5px;">
                    </div>
                    <div style="display: flex; justify-content: flex-end; gap: 10px; margin-top: 10px;">
                        <a href="/dashboard?tab=requisitions" style="background: #64748b; color: white; padding: 8px 16px; border-radius: 6px; text-decoration: none; font-size: 14px;">Cancel</a>
                        <button type="submit" style="background: #2563EB;">Submit Request</button>
                    </div>
                </form>
            </div>
        </div>
        '''

        edit_req_modal_html = """
        <div id="editReqModal" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 2000; justify-content: center; align-items: center;">
            <div style="background: white; padding: 30px; border-radius: 10px; width: 100%; max-width: 450px; box-shadow: 0 10px 25px rgba(0,0,0,0.2);">
                <h3 style="margin-top: 0; color: #1e293b;">Edit Requisition #<span id="editReqIdTitle"></span></h3>
                <form action="/update_requisition_details" method="POST" style="display: flex; flex-direction: column; gap: 15px;">
                    <input type="hidden" name="req_id" id="editReqId">
                    <div>
                        <label style="font-size: 13px; color: #64748b; font-weight: bold;">Request Type</label>
                        <select name="req_type" id="editReqType" required>
                            <option value="Phone Bill Increase">Phone Bill Increase</option>
                            <option value="Accessories">Accessories (Headphone/Charger)</option>
                            <option value="Others">Others</option>
                        </select>
                    </div>
                    <div>
                        <label style="font-size: 13px; color: #64748b; font-weight: bold;">Details Description</label>
                        <input type="text" name="details" id="editReqDetails" required style="margin-top: 5px;">
                    </div>
                    <div style="display: flex; justify-content: flex-end; gap: 10px; margin-top: 10px;">
                        <button type="button" onclick="closeEditReqModal()" style="background: #64748b;">Cancel</button>
                        <button type="submit" style="background: #10b981;">Save Changes</button>
                    </div>
                </form>
            </div>
        </div>
        <script>
            function openEditReqModal(id, type, details) {
                document.getElementById('editReqId').value = id;
                document.getElementById('editReqIdTitle').innerText = id;
                document.getElementById('editReqType').value = type;
                document.getElementById('editReqDetails').value = details;
                document.getElementById('editReqModal').style.display = 'flex';
            }
            function closeEditReqModal() {
                document.getElementById('editReqModal').style.display = 'none';
            }

            function rejectRequisition(id) {
                Swal.fire({
                    title: 'Reject Requisition #' + id,
                    input: 'textarea',
                    inputLabel: 'Remark / Reason for rejection',
                    inputPlaceholder: 'Write your remark here...',
                    inputAttributes: { 'aria-label': 'Remark' },
                    showCancelButton: true,
                    confirmButtonText: 'Submit',
                    confirmButtonColor: '#dc2626',
                    cancelButtonColor: '#64748b',
                    cancelButtonText: 'Cancel',
                    inputValidator: (value) => {
                        if (!value || !value.trim()) {
                            return 'Remark is required to reject a requisition!';
                        }
                    }
                }).then((result) => {
                    if (result.isConfirmed) {
                        const form = document.createElement('form');
                        form.method = 'POST';
                        form.action = '/reject_requisition';

                        const idInput = document.createElement('input');
                        idInput.type = 'hidden';
                        idInput.name = 'req_id';
                        idInput.value = id;
                        form.appendChild(idInput);

                        const remarkInput = document.createElement('input');
                        remarkInput.type = 'hidden';
                        remarkInput.name = 'remark';
                        remarkInput.value = result.value.trim();
                        form.appendChild(remarkInput);

                        document.body.appendChild(form);
                        form.submit();
                    }
                });
            }
        </script>
        """

        add_req_btn_html = '''<a href="/dashboard?tab=requisitions&show_add_req=1" style="background: #2563EB; color: white; padding: 8px 16px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: bold; display: inline-flex; align-items: center; gap: 6px;">+ Add New Requisition</a>'''

        req_metrics_cards_html = f'''
        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-icon" style="background: #eff6ff; color: #2563EB;">📋</div>
                <div class="metric-info">
                    <h3>Total Requisitions</h3>
                    <div class="val">{total_requisitions}</div>
                </div>
            </div>
            <div class="metric-card">
                <div class="metric-icon" style="background: #dcfce7; color: #10b981;">✅</div>
                <div class="metric-info">
                    <h3>Active Requisitions</h3>
                    <div class="val">{approved_requisitions}</div>
                </div>
            </div>
            <div class="metric-card">
                <div class="metric-icon" style="background: #fee2e2; color: #dc2626;">💰</div>
                <div class="metric-info">
                    <h3>Total Cost Balance</h3>
                    <div class="val" style="color: #dc2626;">৳{total_balance}</div>
                </div>
            </div>
            <div class="metric-card">
                <div class="metric-icon" style="background: #fef3c7; color: #d97706;">⚠️</div>
                <div class="metric-info">
                    <h3>Need Balance</h3>
                    <div class="val" style="color: #d97706;">৳0</div>
                </div>
            </div>
        </div>
        '''

        content_area = '''
        <div style="background: #2563EB; padding: 12px 20px; border-radius: 8px; display: flex; justify-content: flex-end; align-items: center; margin-bottom: 25px; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
            <div style="display: flex; gap: 10px; align-items: center;">
                ''' + req_filter_button_html + '''
                ''' + add_req_btn_html + '''
            </div>
        </div>

        ''' + req_metrics_cards_html + '''

        <div class="panel">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                <h3 style="margin: 0;">📋 Office Requisition List (''' + str(len(requisitions)) + ''')</h3>
                <div>''' + req_bulk_btn + '''</div>
            </div>
            <form id="bulkDeleteReqForm" action="/bulk_delete_requisitions" method="POST">
                <table>
                    <thead>
                        <tr>
                            <th style="width: 30px; text-align: center;">''' + master_req_chk + '''</th>
                            <th>ID</th>
                            <th>Employee</th>
                            <th>Request Type</th>
                            <th>Details</th>
                            <th>Status</th>
                            <th style="text-align: right;">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        ''' + req_rows + '''
                    </tbody>
                </table>
            </form>
        </div>
        ''' + add_req_modal_html + edit_req_modal_html + req_popup_js
        
    elif tab == 'users':
        if role not in ('Admin', 'HR'):
            content_area = '<h2>Access denied</h2><p>Only Admin and HR can manage user accounts.</p>'
        else:
            user_conn = sqlite3.connect(DB_NAME)
            user_conn.row_factory = sqlite3.Row
            user_rows = user_conn.execute(
                "SELECT id,full_name,user_id,email,role,status,created_at FROM app_users ORDER BY id DESC"
            ).fetchall()
            user_conn.close()
            rows_html = ''
            for u in user_rows:
                status = u['status']
                status_style = 'background:#dcfce7;color:#15803d' if status == 'Active' else ('background:#fef3c7;color:#92400e' if status == 'Pending' else 'background:#fee2e2;color:#b91c1c')
                rows_html += f'''<tr>
                  <td><b>{esc(u["full_name"])}</b></td><td>{esc(u["user_id"])}</td>
                  <td>{esc(u["email"] or "-")}</td><td>{esc(u["role"])}</td>
                  <td><span style="display:inline-block;padding:4px 9px;border-radius:20px;{status_style};font-size:11px;font-weight:700">{esc(status)}</span></td>
                  <td>{esc(u["created_at"])}</td>
                  <td><div class="user-row-menu">
                    <button type="button" class="user-dots" onclick="toggleUserMenu({u['id']})" aria-label="User options">⋮</button>
                    <div class="user-menu" id="userMenu{u['id']}">
                      {('<button type="button" onclick="openEditUser(' + str(u['id']) + ', this)" data-name="' + esc(u["full_name"]) + '" data-uid="' + esc(u["user_id"]) + '" data-email="' + esc(u["email"] or "") + '" data-role="' + esc(u["role"]) + '">✎ Edit Profile</button>') if is_full() or u["role"] == 'Member' else ''}
                      {('<form method="POST" action="/approve_user/' + str(u['id']) + '"><button type="submit">✓ Approve</button></form><form method="POST" action="/reject_user/' + str(u['id']) + '" onsubmit="return confirm(&quot;Reject this account?&quot;)"><button type="submit" class="reject-user-action">✕ Reject</button></form>') if role == 'HR' and status == 'Pending' else ''}
                      {('<form method="POST" action="/delete_user/' + str(u['id']) + '" onsubmit="return confirm(&quot;Delete this user permanently? This cannot be undone.&quot;)"><button type="submit" class="reject-user-action">🗑️ Delete</button></form>') if (is_full() or u["role"] == 'Member') and (u["user_id"] or '').lower() != (session.get('user_id') or '').lower() and ADMIN_IDENTIFIER.lower() not in ((u["email"] or '').lower(), (u["user_id"] or '').lower()) else ''}
                    </div>
                  </div></td></tr>'''
            if not rows_html:
                rows_html = '<tr><td colspan="7" style="text-align:center;padding:24px;color:#64748b">No user accounts created yet.</td></tr>'
            role_options = '<option value="Member">Member</option>' if not is_full() else '<option value="Member">Member</option><option value="HR">HR</option><option value="Admin">Admin</option>'
            content_area = f'''<style>
            .users-toolbar{{background:#2563eb;padding:12px 20px;border-radius:8px;display:flex;justify-content:flex-end;margin-bottom:22px}}
            .add-user-btn{{background:#fff;color:#2563eb;border:0;border-radius:6px;padding:10px 17px;font-size:14px;font-weight:700;cursor:pointer}}
            .users-card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:22px}}
            .users-table{{width:100%;border-collapse:collapse;font-size:13px}}
            .users-table th,.users-table td{{padding:12px 11px;border-bottom:1px solid #e2e8f0;text-align:left;white-space:nowrap}}
            .users-table th{{background:#f8fafc;color:#334155}}
            .user-modal-backdrop{{display:none;position:fixed;inset:0;background:rgba(15,23,42,.58);z-index:3000;align-items:flex-start;justify-content:center;padding:7vh 16px 24px;overflow-y:auto}}
            .user-modal-backdrop.show{{display:flex}}
            .user-modal{{background:#fff;width:100%;max-width:560px;border-radius:13px;box-shadow:0 22px 60px rgba(15,23,42,.28);overflow:hidden}}
            .user-modal-head{{display:flex;justify-content:space-between;align-items:center;padding:19px 22px;border-bottom:1px solid #e2e8f0}}
            .user-modal-head h3{{margin:0;color:#1e293b;font-size:18px}}
            .user-modal-close{{background:transparent;color:#64748b;font-size:25px;border:0;cursor:pointer}}
            .user-modal-body{{padding:22px}}
            .users-grid{{display:grid;grid-template-columns:1fr 1fr;gap:15px}}
            .users-grid label{{display:block;font-size:13px;font-weight:600;color:#334155;margin-bottom:6px}}
            .users-grid input,.users-grid select{{box-sizing:border-box;width:100%;padding:10px 11px;border:1px solid #cbd5e1;border-radius:7px;font-size:14px}}
            .user-form-actions{{display:flex;justify-content:flex-end;gap:10px;margin-top:22px}}
            .user-form-actions button{{padding:9px 15px;border:0;border-radius:6px;cursor:pointer;font-weight:600}}
            .user-cancel-btn{{background:#e2e8f0;color:#334155}}
            .user-row-menu{{position:relative;display:inline-block}}
            .user-dots{{border:0;background:transparent;border-radius:6px;font-size:23px;line-height:25px;cursor:pointer;padding:3px 10px;color:#475569}}
            .user-dots:hover{{background:#eff6ff}}
            .user-menu{{display:none;position:absolute;right:0;top:32px;min-width:155px;background:#fff;border:1px solid #e2e8f0;border-radius:8px;box-shadow:0 8px 24px #0f172a22;z-index:20;padding:5px}}
            .user-menu.show{{display:block}}
            .user-menu button{{width:100%;text-align:left;padding:9px 10px;border:0;background:#fff;border-radius:5px;cursor:pointer;color:#334155;font-size:13px}}
            .user-menu button:hover{{background:#f1f5f9}}
            .user-menu .reject-user-action{{color:#b91c1c}}
            @media(max-width:700px){{.users-grid{{grid-template-columns:1fr}}.users-card{{padding:12px}}}}
            </style>
            <div class="users-toolbar"><button type="button" class="add-user-btn" onclick="openUserModal()">＋ Add New User</button></div>
            <div class="users-card">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
                <div><h2 style="margin:0;color:#1e293b;font-size:20px">👤 User Management</h2><p style="margin:4px 0 0;color:#64748b;font-size:13px">Accounts and approval status</p></div>
                <b style="font-size:12px;color:#64748b">{len(user_rows)} user(s)</b>
              </div>
              <div style="overflow-x:auto"><table class="users-table">
                <thead><tr><th>Name</th><th>User ID</th><th>Email</th><th>Role</th><th>Status</th><th>Created</th><th>Options</th></tr></thead>
                <tbody>{rows_html}</tbody>
              </table></div>
            </div>
            <div id="addUserModal" class="user-modal-backdrop" role="dialog" aria-modal="true">
              <div class="user-modal"><div class="user-modal-head"><h3>＋ Create New User</h3><button class="user-modal-close" type="button" onclick="closeUserModal()">×</button></div>
                <div class="user-modal-body"><form method="POST" action="/create_user">
                  <div class="users-grid">
                    <div><label>Full Name</label><input name="full_name" required maxlength="100"></div>
                    <div><label>User ID</label><input name="user_id" required minlength="3" maxlength="40" pattern="[A-Za-z0-9._-]+"></div>
                    <div><label>Email</label><input type="email" name="email" maxlength="120"></div>
                    <div><label>Password</label><input type="password" name="password" required minlength="8" autocomplete="new-password"></div>
                    <div><label>Role</label><select name="role" required>{role_options}</select></div>
                  </div>
                  <p style="font-size:12px;color:#92400e;margin:14px 0 0">New accounts remain Pending until HR approves them.</p>
                  <div class="user-form-actions"><button type="button" class="user-cancel-btn" onclick="closeUserModal()">Cancel</button><button type="submit">Create User</button></div>
                </form></div>
              </div>
            </div>
            <div id="editUserModal" class="user-modal-backdrop" role="dialog" aria-modal="true">
              <div class="user-modal"><div class="user-modal-head"><h3>✎ Edit User Profile</h3><button class="user-modal-close" type="button" onclick="closeEditUser()">×</button></div>
                <div class="user-modal-body"><form method="POST" action="/edit_user">
                  <input type="hidden" name="id" id="editUserId">
                  <div class="users-grid">
                    <div><label>Full Name</label><input name="full_name" id="editFullName" required maxlength="100"></div>
                    <div><label>User ID</label><input name="user_id" id="editLoginId" required minlength="3" maxlength="40" pattern="[A-Za-z0-9._-]+"></div>
                    <div><label>Email</label><input type="email" name="email" id="editEmail" maxlength="120"></div>
                    <div><label>New Password (optional)</label><input type="password" name="password" minlength="8" autocomplete="new-password" placeholder="Leave blank to keep current"></div>
                    <div><label>Role</label><select name="role" id="editRole">{role_options}</select></div>
                  </div>
                  <div class="user-form-actions"><button type="button" class="user-cancel-btn" onclick="closeEditUser()">Cancel</button><button type="submit">Save Changes</button></div>
                </form></div>
              </div>
            </div>
            <script>
            function openUserModal(){{document.getElementById('addUserModal').classList.add('show');document.body.style.overflow='hidden';}}
            function closeUserModal(){{document.getElementById('addUserModal').classList.remove('show');document.body.style.overflow='';}}
            function closeEditUser(){{document.getElementById('editUserModal').classList.remove('show');document.body.style.overflow='';}}
            function openEditUser(id,button){{
              document.getElementById('editUserId').value=id;
              document.getElementById('editFullName').value=button.dataset.name;
              document.getElementById('editLoginId').value=button.dataset.uid;
              document.getElementById('editEmail').value=button.dataset.email;
              document.getElementById('editRole').value=button.dataset.role;
              document.getElementById('editUserModal').classList.add('show');
              document.body.style.overflow='hidden';
              document.querySelectorAll('.user-menu').forEach(m=>m.classList.remove('show'));
            }}
            function toggleUserMenu(id){{
              const target=document.getElementById('userMenu'+id);
              document.querySelectorAll('.user-menu').forEach(m=>{{if(m!==target)m.classList.remove('show');}});
              target.classList.toggle('show');
            }}
            document.addEventListener('click',function(e){{
              if(!e.target.closest('.user-row-menu'))document.querySelectorAll('.user-menu').forEach(m=>m.classList.remove('show'));
              if(e.target.classList.contains('user-modal-backdrop')){{e.target.classList.remove('show');document.body.style.overflow='';}}
            }});
            document.addEventListener('keydown',function(e){{if(e.key==='Escape'){{closeUserModal();closeEditUser();}}}});
            </script>'''
    elif tab == 'recycle':
        if role != 'HR':
            content_area = '<div class="panel"><h3>Access denied</h3><p>Only HR can open the Recycle Bin.</p></div>'
        else:
            rb_conn = sqlite3.connect(DB_NAME)
            rb_conn.row_factory = sqlite3.Row
            recycle_ensure(rb_conn)
            rb_rows = rb_conn.execute("SELECT * FROM recycle_bin ORDER BY id DESC").fetchall()
            rb_conn.close()
            type_icon = {'User': '👤', 'Member': '👥', 'Requisition': '📋'}
            rb_html = ''
            for r_ in rb_rows:
                try:
                    payload = json.loads(r_['item_data'] or '{}')
                except ValueError:
                    payload = {}
                detail = {
                    'type': r_['item_type'], 'title': r_['item_title'], 'ref': r_['item_ref'],
                    'by_name': r_['deleted_by_name'], 'by_role': r_['deleted_by_role'],
                    'by_uid': r_['deleted_by_uid'], 'by_email': r_['deleted_by_email'],
                    'at': r_['deleted_at'], 'data': payload}
                rb_html += (
                    '<tr data-type="' + esc(r_['item_type']) + '">'
                    '<td>' + str(r_['id']) + '</td>'
                    '<td><span class="rb-type rb-' + esc(r_['item_type']) + '">' + type_icon.get(r_['item_type'], '🗂️') + ' ' + esc(r_['item_type']) + '</span></td>'
                    '<td><b>' + esc(r_['item_title']) + '</b></td>'
                    '<td>#' + esc(r_['item_ref']) + '</td>'
                    '<td>' + esc(r_['deleted_by_name'] or '-') + '<div class="rb-sub">' + esc(r_['deleted_by_role'] or '') + ' &middot; ID: ' + esc(r_['deleted_by_uid'] or '-') + '</div></td>'
                    '<td>' + esc(r_['deleted_at']) + '</td>'
                    '<td><button type="button" class="rb-view" data-detail="' + esc(json.dumps(detail, ensure_ascii=False)) + '" onclick="rbOpen(this)">View details</button></td>'
                    '</tr>')
            if not rb_html:
                rb_html = '<tr><td colspan="7" style="text-align:center;padding:30px;color:#64748b">Recycle Bin is empty. Deleted users, members and requisitions will appear here.</td></tr>'
            content_area = """<style>
            .rb-card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:22px}
            .rb-table{width:100%;border-collapse:collapse;font-size:13px}
            .rb-table th,.rb-table td{padding:11px 10px;border-bottom:1px solid #e2e8f0;text-align:left;vertical-align:top}
            .rb-sub{font-size:11px;color:#64748b;margin-top:2px}
            .rb-type{display:inline-block;padding:3px 9px;border-radius:20px;font-size:11px;font-weight:700;background:#eff6ff;color:#1d4ed8}
            .rb-Member{background:#dcfce7;color:#15803d}.rb-Requisition{background:#fef3c7;color:#92400e}
            .rb-view{padding:6px 12px;font-size:12px}
            .rb-tools{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
            .rb-tools input,.rb-tools select{padding:8px 10px;border:1px solid #cbd5e1;border-radius:6px;font-size:13px}
            .rb-back{position:fixed;inset:0;background:rgba(15,23,42,.55);display:none;align-items:center;justify-content:center;z-index:5000;padding:16px}
            .rb-back.show{display:flex}
            .rb-modal{background:#fff;border-radius:12px;width:100%;max-width:640px;max-height:88vh;overflow:auto;box-shadow:0 20px 50px rgba(0,0,0,.35)}
            .rb-mh{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid #e2e8f0}
            .rb-mh h3{margin:0;font-size:17px}.rb-x{background:transparent!important;color:#64748b!important;font-size:22px;padding:0 6px}
            .rb-mb{padding:18px 20px}.rb-sec{font-size:12px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;color:#64748b;margin:16px 0 8px}
            .rb-kv{width:100%;border-collapse:collapse;font-size:13px}.rb-kv td{padding:8px 6px;border-bottom:1px solid #f1f5f9;vertical-align:top}
            .rb-kv td:first-child{width:38%;color:#64748b;font-weight:600}
            .rb-del{background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:4px 10px}
            </style>
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:18px">
              <span style="font-size:26px">🗑️</span>
              <div><h2 style="margin:0;color:#1e293b">Recycle Bin</h2>
              <p style="margin:3px 0 0;color:#64748b;font-size:13px">Everything deleted from the system, with who deleted it. Visible to HR only.</p></div>
            </div>
            <div class="rb-card">
              <div class="rb-tools">
                <input type="search" id="rbSearch" placeholder="Search name, ID, deleted by..." oninput="rbFilter()" style="min-width:240px">
                <select id="rbType" onchange="rbFilter()"><option value="">All types</option><option>User</option><option>Member</option><option>Requisition</option></select>
                <b style="margin-left:auto;align-self:center;font-size:12px;color:#64748b">""" + str(len(rb_rows)) + """ item(s)</b>
              </div>
              <div style="overflow-x:auto"><table class="rb-table">
                <thead><tr><th>#</th><th>Type</th><th>Deleted item</th><th>Original ID</th><th>Deleted by</th><th>Deleted at</th><th></th></tr></thead>
                <tbody id="rbBody">""" + rb_html + """</tbody>
              </table></div>
            </div>
            <div class="rb-back" id="rbBack"><div class="rb-modal">
              <div class="rb-mh"><h3 id="rbTitle">Details</h3><button type="button" class="rb-x" onclick="rbClose()">&times;</button></div>
              <div class="rb-mb" id="rbBodyM"></div>
            </div></div>
            <script>
            function rbEsc(v){var d=document.createElement('div');d.textContent=(v===null||v===undefined||v==='')?'-':String(v);return d.innerHTML;}
            function rbLabel(k){return k.replace(/_/g,' ').replace(/\\b\\w/g,function(c){return c.toUpperCase();});}
            function rbOpen(btn){
              var d=JSON.parse(btn.getAttribute('data-detail'));
              document.getElementById('rbTitle').textContent=d.type+' deleted: '+d.title;
              var h='<div class="rb-sec">Deleted by</div><table class="rb-kv">'
                +'<tr><td>Name</td><td>'+rbEsc(d.by_name)+'</td></tr>'
                +'<tr><td>Role</td><td>'+rbEsc(d.by_role)+'</td></tr>'
                +'<tr><td>Login ID used</td><td>'+rbEsc(d.by_uid)+'</td></tr>'
                +'<tr><td>Email</td><td>'+rbEsc(d.by_email)+'</td></tr>'
                +'<tr><td>Deleted at</td><td>'+rbEsc(d.at)+'</td></tr></table>';
              h+='<div class="rb-sec">Deleted '+rbEsc(d.type)+' (original ID #'+rbEsc(d.ref)+')</div><table class="rb-kv">';
              Object.keys(d.data||{}).forEach(function(k){h+='<tr><td>'+rbEsc(rbLabel(k))+'</td><td>'+rbEsc(d.data[k])+'</td></tr>';});
              h+='</table>';
              document.getElementById('rbBodyM').innerHTML=h;
              document.getElementById('rbBack').classList.add('show');
            }
            function rbClose(){document.getElementById('rbBack').classList.remove('show');}
            document.getElementById('rbBack').addEventListener('click',function(e){if(e.target===this)rbClose();});
            document.addEventListener('keydown',function(e){if(e.key==='Escape')rbClose();});
            function rbFilter(){
              var q=document.getElementById('rbSearch').value.toLowerCase(),t=document.getElementById('rbType').value;
              document.querySelectorAll('#rbBody tr').forEach(function(tr){
                var okT=!t||tr.getAttribute('data-type')===t, okQ=!q||tr.textContent.toLowerCase().indexOf(q)>-1;
                tr.style.display=(okT&&okQ)?'':'none';
              });
            }
            </script>"""
    elif tab == 'profile':
        role_input_html = f'''
            <select name="role">
                <option value="HR" {"selected" if role == "HR" else ""}>HR</option>
                <option value="Admin" {"selected" if role == "Admin" else ""}>Admin</option>
                <option value="Member" {"selected" if role == "Member" else ""}>Member</option>
            </select>
        ''' if role in ["HR", "Admin"] and not is_owner() else f'''
            <input type="text" value="{role_e}" readonly style="background: #f1f5f9; cursor: not-allowed;">
        '''

        js_code = """
        <script>
            function compressAndPreview(event) {
                const file = event.target.files[0];
                if (!file) return;
                const reader = new FileReader();
                reader.onload = function(e) {
                    const img = new Image();
                    img.onload = function() {
                        const canvas = document.createElement('canvas');
                        const ctx = canvas.getContext('2d');
                        const size = Math.min(img.width, img.height);
                        canvas.width = 100;
                        canvas.height = 100;
                        const startX = (img.width - size) / 2;
                        const startY = (img.height - size) / 2;
                        ctx.drawImage(img, startX, startY, size, size, 0, 0, 100, 100);
                        const compressedDataUrl = canvas.toDataURL('image/jpeg', 0.7);
                        document.getElementById('profileB64Input').value = compressedDataUrl;
                        document.getElementById('avatarPreviewContainer').innerHTML = '<img src="' + compressedDataUrl + '" alt="Profile" style="width:100%; height:100%; object-fit:cover;">';
                    };
                    img.src = e.target.result;
                };
                reader.readAsDataURL(file);
            }
        </script>
        """

        avatar_content = f'<img src="{profile_pic_e}" alt="Profile" style="width:100%; height:100%; object-fit:cover;">' if profile_pic else esc(name[0].upper() if name else '?')

        content_area = '''
        <div class="panel" style="max-width: 600px; margin: auto;">
            <form action="/update_profile" method="POST">
                <input type="hidden" name="profile_b64" id="profileB64Input" value="''' + profile_pic_e + '''">
                <div style="display: flex; align-items: center; gap: 15px; margin-bottom: 25px;">
                    <div id="avatarPreviewContainer" onclick="document.getElementById('profileImageInput').click();" style="width: 70px; height: 70px; background: #1e293b; color: white; border-radius: 50%; display: flex; justify-content: center; align-items: center; font-size: 26px; font-weight: bold; overflow: hidden; cursor: pointer; border: 3px solid #2563EB; position: relative;" title="Click to change profile picture">
                        ''' + avatar_content + '''
                        <div style="position: absolute; bottom: 0; background: rgba(0,0,0,0.6); width: 100%; font-size: 9px; text-align: center; color: white; padding: 2px;">Edit</div>
                    </div>
                    <input type="file" id="profileImageInput" accept="image/*" style="display: none;" onchange="compressAndPreview(event)">
                    <div>
                        <h3 style="margin: 0; color: #1e293b;">Profile Customization</h3>
                        <p style="margin: 2px 0 0 0; color: #64748b; font-size: 13px;">Click on the avatar icon to choose a picture from your gallery.</p>
                    </div>
                </div>
                <div style="margin-bottom: 15px;">
                    <label style="font-size: 13px; color: #64748b; font-weight: bold;">Full Name</label>
                    <input type="text" name="name" value="''' + name_e + '''" required>
                </div>
                <div style="margin-bottom: 15px;">
                    <label style="font-size: 13px; color: #64748b; font-weight: bold;">User ID</label>
                    <input type="text" name="user_id" value="''' + user_id_e + '''" required>
                </div>
                <div style="margin-bottom: 15px;">
                    <label style="font-size: 13px; color: #64748b; font-weight: bold;">Role</label>
                    ''' + role_input_html + '''
                </div>
                <button type="submit">Save Changes</button>
            </form>
        </div>
        ''' + js_code

    elif tab == 'activity_log':
        log_rows = ""
        for log in logs:
            log_rows += f'<tr><td>{esc(log["timestamp"])}</td><td>{esc(log["action"])}</td></tr>'
            
        content_area = f'''
        <div class="panel">
            <h3>📜 System Activity Log</h3>
            <p style="color: #64748b; margin-bottom: 15px;">Recent system actions and user activities.</p>
            <table>
                <thead>
                    <tr>
                        <th>Timestamp</th>
                        <th>Activity Description</th>
                    </tr>
                </thead>
                <tbody>
                    {log_rows if log_rows else '<tr><td colspan="2">No logs found.</td></tr>'}
                </tbody>
            </table>
        </div>
        '''

    elif tab == 'theme':
        content_area = theme_picker_html(theme_key)

    elif tab == 'reports':
        if role not in ('Admin', 'HR'):
            content_area = '<div class="panel"><h3>Access denied</h3></div>'
        else:
            cards = ''.join(
                f'''<a class="report-folder" href="/report/{k}" style="animation-delay:{i*0.08}s">
                    <div class="rf-icon">📁</div>
                    <div class="rf-num">{i}</div>
                    <div class="rf-emoji">{ic}</div>
                    <div class="rf-title">{esc(title)}</div>
                    <div class="rf-desc">{esc(desc)}</div>
                </a>''' for i, (k, ic, title, desc) in enumerate(REPORT_META, 1))
            content_area = """<style>
            .report-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:18px;margin-top:18px}
            .report-folder{position:relative;display:block;background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:22px 20px;text-decoration:none;color:#1e293b;box-shadow:0 1px 3px rgba(0,0,0,.05);transition:transform .3s ease,box-shadow .3s ease,border-color .3s ease;animation:rfIn .6s ease both}
            .report-folder:hover{transform:translateY(-5px);box-shadow:0 12px 24px rgba(37,99,235,.15);border-color:#2563EB}
            .rf-icon{font-size:46px;line-height:1}
            .rf-num{position:absolute;top:14px;right:16px;background:#2563EB;color:#fff;width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700}
            .rf-emoji{font-size:22px;margin-top:10px}
            .rf-title{font-weight:700;font-size:16px;margin-top:6px}
            .rf-desc{color:#64748b;font-size:13px;margin-top:6px;line-height:1.45}
            @keyframes rfIn{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
            </style>
            <div style="display:flex;align-items:center;gap:10px"><span style="font-size:25px">📑</span><div>
              <h2 style="margin:0;color:#1e293b">Reports</h2>
              <p style="margin:3px 0 0;color:#64748b;font-size:13px">Open a folder to view a report. Every report can be printed on paper (A4).</p></div></div>
            <div class="report-grid">""" + cards + "</div>"

    elif tab == 'settings':
        settings=load_settings()
        sections=[('general','1. General / Company',[('company_name','Company name','text'),('currency_symbol','Currency symbol','text'),('date_format','Date format','text'),('rows_per_page','Default rows per page','number')]),('operator','2. Operator ও Recharge',[('active_operators','Active operators (comma separated)','text'),('operator_prefixes','Operator phone prefixes JSON','json'),('default_balance','New member default balance','number'),('min_balance','Minimum balance','number'),('max_balance','Maximum balance','number'),('low_balance_limit','Low balance alert limit','number')]),('member','3. Member Settings',[('designations','Designation list','text'),('allow_duplicate_phone','Allow duplicate phone number','bool'),('default_member_status','Default member status','text')]),('requisition','4. Requisition Settings',[('requisition_types','Requisition types','text'),('reject_remark_required','Reject remark compulsory','bool'),('max_requisition_amount','Maximum amount limit','number'),('approval_roles','Approval roles (comma separated)','text')]),('roles','5. Role ও Permission',[(x,x.replace('_',' ').title(),'bool') for x in ['admin_can_add','admin_can_delete','admin_can_bulk_delete','admin_can_approve','hr_can_add','hr_can_delete','hr_can_bulk_delete','hr_can_approve','member_can_view_balance','member_can_view_logs']]),('security','6. Security',[('admin_password','New admin password (blank = unchanged)','password'),('otp_expiry','OTP expiry minutes','number'),('session_timeout','Session timeout minutes','number'),('wrong_login_limit','Wrong login attempt limit','number'),('otp_login_enabled','OTP login on/off','bool')]),('notifications','7. Notifications',[('notifications_enabled','All notifications','bool'),('requisition_notifications','Requisition alerts','bool'),('low_balance_notifications','Low balance alerts','bool')]),('activity','8. Activity Log',[('log_retention_days','Log retention days','number')]),('backup','9. Data ও Backup',[('backup_note','Backup note','text')]),('print','10. Print Settings',[('print_header','Print header text','text'),('print_footer','Print footer text','text'),('show_prepared_by','Prepared by signature','bool'),('show_approved_by','Approved by signature','bool')]),('theme','11. Theme / Display',[('theme_mode','Light / Dark / System','text')])]
        key=request.args.get('section','general'); section=next((x for x in sections if x[0]==key),sections[0])
        fields=''
        for k,label,kind in section[2]:
            v=settings.get(k,DEFAULT_SETTINGS.get(k,''))
            if kind=='bool': ctrl=f'<label><input type="checkbox" name="{k}" value="true" {"checked" if v else ""}> Enabled</label>'
            elif kind=='json': ctrl=f'<textarea name="{k}" rows="5">{esc(json.dumps(v,ensure_ascii=False,indent=2))}</textarea>'
            else: ctrl=f'<input type="{kind}" name="{k}" value="{esc("" if kind=="password" else (", ".join(v) if isinstance(v,list) else v))}">'
            fields+=f'<div class="setting-field"><label>{esc(label)}</label>{ctrl}</div>'
        extra=''
        if key=='backup': extra='<a href="/settings/backup">Download database backup</a> | <a href="/settings/export">Export settings JSON</a>'
        if key=='activity': extra='<a href="/settings/export-logs">Export activity logs</a> | <a href="/settings/clear-logs" onclick="return confirm(\'Clear logs?\')">Clear activity logs</a>'
        content_area=f'''<style>
        .settings-form{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:26px;max-width:900px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
        .settings-form h3{{margin:0 0 22px;color:#1e293b;font-size:20px}}
        .setting-field{{margin-bottom:18px}}
        .setting-field>label{{display:block;font-size:13px;font-weight:600;margin-bottom:7px;color:#334155}}
        .setting-field input:not([type=checkbox]),.setting-field textarea{{width:100%;padding:11px 12px;border:1px solid #cbd5e1;border-radius:7px;background:#fff}}
        .settings-extra{{margin-top:18px;font-size:13px}}
        @media(max-width:800px){{.settings-form{{padding:18px}}}}
        </style>
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:20px">
            <span style="font-size:25px">⚙️</span><div><h2 style="margin:0;color:#1e293b">Settings</h2>
            <p style="margin:3px 0 0;color:#64748b;font-size:13px">Manage your system preferences</p></div>
        </div>
        <div class="settings-form">
            <h3>{esc(section[1])}</h3>
            <form method="POST" action="/save_settings?section={section[0]}">{fields}<button type="submit">Save Changes</button></form>
            <div class="settings-extra">{extra}</div>
        </div>'''

    notif_bell_html = HR_BELL_HTML if role == 'HR' else ''
    if role == 'HR':
        try:
            _rc = sqlite3.connect(DB_NAME)
            recycle_ensure(_rc)
            _bin_count = _rc.execute("SELECT COUNT(*) FROM recycle_bin").fetchone()[0]
            _rc.close()
        except sqlite3.Error:
            _bin_count = 0
        notif_bell_html = ('<a href="/dashboard?tab=recycle" class="notif-bell recycle-link" id="recycleBtn" title="Recycle Bin (' + str(_bin_count) + ')" aria-label="Recycle Bin" style="text-decoration:none;margin-right:10px;">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>'
            + ('<span class="notif-badge show" style="background:#64748b">' + (str(_bin_count) if _bin_count < 100 else '99+') + '</span>' if _bin_count else '') + '</a>') + notif_bell_html
    body_content = f'''
    <div class="top-header">
        {brand_logo_html}
        <div style="display:flex;align-items:center;gap:18px;">
        {notif_bell_html}
        <div class="user-profile-area" onclick="toggleProfileMenu(event)">
            <div class="user-meta">
                <div class="user-name-display">
                    <span class="badge {badge_class}">{role_e}</span>
                    {name_e}
                </div>
                <div class="user-id-display">ID: {user_id_e}</div>
            </div>
            <div class="user-avatar">
                {avatar_html}
            </div>
            <div class="profile-dropdown" id="profileDropdown">
                <div class="dropdown-header-info">
                    <div class="d-name">{name_e}</div>
                    <div class="d-email">{user_email_e}</div>
                </div>
                <a href="/dashboard?tab=profile">👤 Profile</a>
                <a href="/dashboard?tab=activity_log">🕒 Activity Log</a>
                <a href="/dashboard?tab=theme">🎨 Theme</a>
                <a href="/logout" class="logout-btn">🔴 Logout</a>
            </div>
        </div>
        </div>
    </div>
    <div class="app-layout">
        <div class="sidebar">
            <a href="/dashboard?tab=overview" class="{'active' if tab == 'overview' else ''}">📊 Dashboard <span>›</span></a>
            <a href="/dashboard?tab=employees" class="{'active' if tab == 'employees' else ''}">👥 All Members List <span>›</span></a>
            <a href="/dashboard?tab=requisitions" class="{'active' if tab == 'requisitions' else ''}">📋 Office Requisition <span>›</span></a>
            {('<a href="/dashboard?tab=users" class="' + ('active' if tab == 'users' else '') + '">👤 Users <span>›</span></a>') if role in ['Admin', 'HR'] else ''}
            <a href="/dashboard?tab=settings" class=""{'active' if tab == 'settings' else ''}" aria-expanded="{'true' if tab == 'settings' else 'false'}">⚙️ Settings <span>{'⌄' if tab == 'settings' else '›'}</span></a>
            {('<div class="settings-drawer">' + ''.join(f'<a class="settings-drawer-item {"current" if request.args.get("section", "general") == k else ""}" href="/dashboard?tab=settings&section={k}"><span class="settings-drawer-arrow">›</span><span>{esc(v)}</span></a>' for i,(k,v) in enumerate([('general','General / Company'),('operator','Operator & Recharge'),('member','Member Settings'),('requisition','Requisition Settings'),('roles','Role & Permission'),('security','Security'),('notifications','Notifications'),('activity','Activity Log'),('backup','Data & Backup'),('print','Print Settings'),('theme','Theme / Display')],1)) + '</div>') if tab == 'settings' and is_full() else ''}
            {('<a href="/dashboard?tab=reports" class="' + ('active' if tab == 'reports' else '') + '">📑 Report <span>›</span></a>') if role in ['Admin', 'HR'] else ''}
        </div>
        <div class="main-content">
            {content_area}
        </div>
    </div>
    ''' + popup_action_js
    return render_template_string(BASE_TEMPLATE, title="Dashboard - OpsNexus", content=body_content, theme_key=theme_key, theme_head=build_theme_head(theme_key))

@app.route('/create_user', methods=['POST'])
def create_user():
    actor_role = session.get('role')
    if not session.get('logged_in') or actor_role not in ('Admin', 'HR'):
        return redirect(url_for('login'))
    full_name = request.form.get('full_name', '').strip()
    user_id = request.form.get('user_id', '').strip()
    email = request.form.get('email', '').strip().lower() or None
    password = request.form.get('password', '').strip()
    new_role = request.form.get('role', 'Member')
    allowed_roles = ('Admin', 'HR', 'Member') if is_full() else ('Member',)
    email_valid = email is None or re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)
    if (not full_name or len(full_name) > 100
            or not re.fullmatch(r'[A-Za-z0-9._-]{3,40}', user_id)
            or len(password) < 8 or not email_valid or new_role not in allowed_roles):
        return redirect(url_for('dashboard', tab='users', success_msg='Invalid user details. Check fields and password (minimum 8 characters).'))
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute("INSERT INTO app_users (full_name,user_id,email,password_hash,role,status) VALUES (?,?,?,?,?,'Pending')",
                     (full_name, user_id, email, generate_password_hash(password), new_role))
        conn.execute("INSERT INTO activity_logs (action) VALUES (?)",
                     (f"{actor_role} created {new_role} account awaiting HR approval: {user_id}",))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return redirect(url_for('dashboard', tab='users', success_msg='Error: This User ID or email already exists.'))
    conn.close()
    return redirect(url_for('dashboard', tab='users', success_msg=f"User {user_id} created and sent for HR approval."))


@app.route('/edit_user', methods=['POST'])
def edit_user():
    actor_role = session.get('role')
    if not session.get('logged_in') or actor_role not in ('Admin', 'HR'):
        return redirect(url_for('login'))
    try:
        target_id = int(request.form.get('id', '0'))
    except ValueError:
        return redirect(url_for('dashboard', tab='users'))
    full_name = request.form.get('full_name', '').strip()
    user_id = request.form.get('user_id', '').strip()
    email = request.form.get('email', '').strip().lower() or None
    password = request.form.get('password', '').strip()
    new_role = request.form.get('role', 'Member')
    allowed_roles = ('Admin', 'HR', 'Member') if is_full() else ('Member',)
    email_valid = email is None or re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)
    if (not full_name or len(full_name) > 100
            or not re.fullmatch(r'[A-Za-z0-9._-]{3,40}', user_id)
            or (password and len(password) < 8) or not email_valid or new_role not in allowed_roles):
        return redirect(url_for('dashboard', tab='users', success_msg='Invalid profile details.'))
    conn = sqlite3.connect(DB_NAME)
    if actor_role == 'HR' and not is_full():
        target = conn.execute("SELECT role FROM app_users WHERE id=?", (target_id,)).fetchone()
        if not target or target[0] != 'Member':
            conn.close()
            return redirect(url_for('dashboard', tab='users', success_msg='HR can edit Member profiles only.'))
    # Owner account stays on the HR post permanently
    if (email or '').lower() == ADMIN_IDENTIFIER.lower() or user_id.lower() == ADMIN_IDENTIFIER.lower():
        new_role = 'HR'
    else:
        _t = conn.execute("SELECT email,user_id FROM app_users WHERE id=?", (target_id,)).fetchone()
        if _t and ((_t[0] or '').lower() == ADMIN_IDENTIFIER.lower() or (_t[1] or '').lower() == ADMIN_IDENTIFIER.lower()):
            new_role = 'HR'
    try:
        fields = [full_name, user_id, email, new_role]
        sql = "UPDATE app_users SET full_name=?,user_id=?,email=?,role=?"
        if password:
            sql += ",password_hash=?"
            fields.append(generate_password_hash(password))
        sql += " WHERE id=?"
        fields.append(target_id)
        cur = conn.execute(sql, fields)
        if cur.rowcount != 1:
            conn.rollback()
            return redirect(url_for('dashboard', tab='users', success_msg='User not found.'))
        conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"{actor_role} edited user profile ID {target_id}",))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return redirect(url_for('dashboard', tab='users', success_msg='That User ID or email is already in use.'))
    finally:
        conn.close()
    return redirect(url_for('dashboard', tab='users', success_msg='User profile updated successfully.'))


@app.route('/approve_user/<int:user_id>', methods=['POST'])
def approve_user(user_id):
    if not session.get('logged_in') or session.get('role') != 'HR':
        return redirect(url_for('login'))
    conn = sqlite3.connect(DB_NAME)
    cur = conn.execute("UPDATE app_users SET status='Active',approved_by=?,approved_at=CURRENT_TIMESTAMP WHERE id=? AND status='Pending'",
                       (session.get('name') or session.get('user'), user_id))
    if cur.rowcount:
        conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"HR approved user account ID {user_id}",))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', tab='users', success_msg='User approved and can now log in.' if cur.rowcount else 'Pending user was not found.'))


@app.route('/reject_user/<int:user_id>', methods=['POST'])
def reject_user(user_id):
    if not session.get('logged_in') or session.get('role') != 'HR':
        return redirect(url_for('login'))
    conn = sqlite3.connect(DB_NAME)
    cur = conn.execute("UPDATE app_users SET status='Rejected' WHERE id=? AND status='Pending'", (user_id,))
    if cur.rowcount:
        conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"HR rejected user account ID {user_id}",))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', tab='users', success_msg='User request rejected.' if cur.rowcount else 'Pending user was not found.'))


@app.route('/delete_user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    actor_role = session.get('role')
    if not session.get('logged_in') or actor_role not in ('Admin', 'HR'):
        return redirect(url_for('login'))
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    full_row = conn.execute("SELECT * FROM app_users WHERE id=?", (user_id,)).fetchone()
    target = (full_row['role'], full_row['email'], full_row['user_id']) if full_row else None
    if not target:
        conn.close()
        return redirect(url_for('dashboard', tab='users', success_msg='User not found.'))
    t_role, t_email, t_uid = target
    # The fixed owner account can never be deleted
    if ADMIN_IDENTIFIER.lower() in ((t_email or '').lower(), (t_uid or '').lower()):
        conn.close()
        return redirect(url_for('dashboard', tab='users', success_msg='The owner account cannot be deleted.'))
    # Nobody can delete their own account
    if (t_uid or '').lower() == (session.get('user_id') or '').lower():
        conn.close()
        return redirect(url_for('dashboard', tab='users', success_msg='You cannot delete your own account.'))
    # HR (without full access) can delete Member accounts only
    if not is_full() and t_role != 'Member':
        conn.close()
        return redirect(url_for('dashboard', tab='users', success_msg='HR can delete Member accounts only.'))
    snap = {k: full_row[k] for k in full_row.keys() if k != 'password_hash'}
    recycle_add(conn, 'User', user_id, f"{full_row['full_name']} ({t_uid})", snap)
    conn.execute("DELETE FROM app_users WHERE id=?", (user_id,))
    conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"{actor_role} deleted user account {t_uid} (ID {user_id})",))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', tab='users', success_msg=f'User {t_uid} deleted successfully.'))


@app.route('/save_settings',methods=['POST'])
def save_app_settings():
 if not session.get('logged_in') or not is_full(): return redirect(url_for('login'))
 d=load_settings(); sec=request.args.get('section','general')
 groups={'general':['company_name','currency_symbol','date_format','rows_per_page'],'operator':['active_operators','operator_prefixes','default_balance','min_balance','max_balance','low_balance_limit'],'member':['designations','allow_duplicate_phone','default_member_status'],'requisition':['requisition_types','reject_remark_required','max_requisition_amount','approval_roles'],'roles':['admin_can_add','admin_can_delete','admin_can_bulk_delete','admin_can_approve','hr_can_add','hr_can_delete','hr_can_bulk_delete','hr_can_approve','member_can_view_balance','member_can_view_logs'],'security':['admin_password','otp_expiry','session_timeout','wrong_login_limit','otp_login_enabled'],'notifications':['notifications_enabled','requisition_notifications','low_balance_notifications'],'activity':['log_retention_days'],'backup':['backup_note'],'print':['print_header','print_footer','show_prepared_by','show_approved_by'],'theme':['theme_mode']}
 bools={'allow_duplicate_phone','reject_remark_required','admin_can_add','admin_can_delete','admin_can_bulk_delete','admin_can_approve','hr_can_add','hr_can_delete','hr_can_bulk_delete','hr_can_approve','member_can_view_balance','member_can_view_logs','otp_login_enabled','notifications_enabled','requisition_notifications','low_balance_notifications','show_prepared_by','show_approved_by'}
 for k in groups.get(sec,[]):
  if k in bools:d[k]=request.form.get(k)=='true'
  elif k=='operator_prefixes':
   try:d[k]=json.loads(request.form.get(k,'{}'))
   except ValueError:return redirect(url_for('dashboard',tab='settings',section=sec,success_msg='Error: Invalid JSON'))
  elif k=='admin_password':
   if request.form.get(k,'').strip():d[k]=request.form[k].strip()
  else:d[k]=request.form.get(k,'').strip()
 save_settings(d)
 return redirect(url_for('dashboard',tab='settings',section=sec,success_msg='Settings saved successfully!'))

@app.route('/settings/backup')
def settings_backup():
 if not session.get('logged_in') or not is_full():return redirect(url_for('login'))
 from flask import send_file
 return send_file(DB_NAME,as_attachment=True,download_name='fu_infotech_backup.db')
@app.route('/settings/export')
def settings_export():
 if not session.get('logged_in') or not is_full():return redirect(url_for('login'))
 from flask import jsonify
 return jsonify(load_settings())
@app.route('/settings/export-logs')
def settings_export_logs():
 if not session.get('logged_in') or not is_full():return redirect(url_for('login'))
 from flask import make_response
 c=sqlite3.connect(DB_NAME); rows=c.execute('SELECT timestamp,action FROM activity_logs').fetchall(); c.close()
 data='Timestamp,Action\\n'+'\\n'.join('"'+str(a)+'","'+str(b).replace('"','""')+'"' for a,b in rows)
 r=make_response(data); r.headers['Content-Type']='text/csv'; r.headers['Content-Disposition']='attachment; filename=activity_logs.csv'; return r
@app.route('/settings/clear-logs')
def settings_clear_logs():
 if not session.get('logged_in') or not is_full():return redirect(url_for('login'))
 c=sqlite3.connect(DB_NAME); c.execute('DELETE FROM activity_logs'); c.commit(); c.close()
 return redirect(url_for('dashboard',tab='settings',section='activity',success_msg='Activity logs cleared'))

@app.route('/update_profile', methods=['POST'])
def update_profile():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    new_name = (request.form.get('name') or '').strip()
    new_user_id = (request.form.get('user_id') or '').strip()
    profile_b64 = request.form.get('profile_b64') or ''
    if new_name: session['name'] = new_name
    if new_user_id: session['user_id'] = new_user_id
    # Only accept real image data URLs for the profile picture
    if profile_b64 and profile_b64.startswith('data:image/'):
        session['profile_pic'] = profile_b64
    if session.get('role') in ['HR', 'Admin'] and not is_owner():
        new_role = request.form.get('role')
        if new_role in ['HR', 'Admin', 'Member']:
            session['role'] = new_role
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT INTO activity_logs (action) VALUES (?)", ("Updated profile settings",))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', tab='profile', success_msg="Profile settings updated successfully!"))

@app.route('/add_employee', methods=['POST'])
def add_employee():
    if not session.get('logged_in') or session.get('role') not in ['HR', 'Admin']:
        return redirect(url_for('login'))
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    designation = request.form.get('designation', '').strip()
    operator = request.form.get('operator')
    app_settings = load_settings()
    balance_raw = request.form.get('balance', '').strip()
    balance = to_int(balance_raw, to_int(app_settings.get('default_balance', 0)))
    if balance_raw == '':
        balance = to_int(app_settings.get('default_balance', 0))
    if balance < to_int(app_settings.get('min_balance', 0)) or balance > to_int(app_settings.get('max_balance', 100000)):
        return redirect(url_for('dashboard', tab='employees', success_msg='Error: Balance is outside the configured limits!'))
    active_ops = app_settings.get('active_operators', DEFAULT_SETTINGS['active_operators'])
    if isinstance(active_ops, str):
        active_ops = [x.strip() for x in active_ops.split(',') if x.strip()]
    if operator not in active_ops:
        return redirect(url_for('dashboard', tab='employees', success_msg='Error: This operator is disabled in Settings!'))
    
    if not name or not phone or not designation:
        return redirect(url_for('dashboard', tab='employees', success_msg="Error: All fields are required!"))

    if not phone.isdigit() or len(phone) != 11 or not phone.startswith('01'):
        return redirect(url_for('dashboard', tab='employees', success_msg="Error: Invalid phone number format!"))
    
    prefix = phone[:3]
    prefix_rules = app_settings.get('operator_prefixes', DEFAULT_SETTINGS['operator_prefixes'])
    allowed_prefixes = [x.strip() for x in str(prefix_rules.get(operator, '')).split(',') if x.strip()]
    valid = prefix in allowed_prefixes

    if not valid:
        return redirect(url_for('dashboard', tab='employees', success_msg=f"Error: Phone prefix {prefix} does not match selected operator {operator}!"))
    
    conn = sqlite3.connect(DB_NAME)
    if not app_settings.get('allow_duplicate_phone', False):
        duplicate = conn.execute("SELECT 1 FROM employees WHERE phone = ? LIMIT 1", (phone,)).fetchone()
        if duplicate:
            conn.close()
            return redirect(url_for('dashboard', tab='employees', success_msg='Error: This phone number already exists!'))
    conn.execute("INSERT INTO employees (name, phone, designation, operator, balance, used, created_date) VALUES (?, ?, ?, ?, ?, 0, CURRENT_DATE)",
                 (name, phone, designation, operator, balance))
    conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"New member added: {name} ({phone})",))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', tab='employees', success_msg=f"New member '{name}' added successfully!"))

@app.route('/delete_member/<int:emp_id>')
def delete_member(emp_id):
    if not session.get('logged_in') or session.get('role') not in ['HR', 'Admin']:
        return redirect(url_for('login'))
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    emp = conn.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
    
    if emp:
        recycle_add(conn, 'Member', emp['id'], emp['name'], dict(emp))
        conn.execute("DELETE FROM employees WHERE id = ?", (emp_id,))
        conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"Deleted member: {emp['name']}",))
        conn.commit()
        msg = f"Member <b>'{emp['name']}'</b> has been moved to the recycle bin."
    else:
        msg = "Member not found!"
        
    conn.close()
    return redirect(url_for('dashboard', tab='employees', delete_msg=msg))

@app.route('/bulk_delete_members', methods=['POST'])
def bulk_delete_members():
    if not session.get('logged_in') or session.get('role') not in ['HR', 'Admin']:
        return redirect(url_for('login'))
        
    emp_ids = request.form.getlist('emp_ids')
    if emp_ids:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        placeholders = ', '.join(['?'] * len(emp_ids))
        for _r in conn.execute(f"SELECT * FROM employees WHERE id IN ({placeholders})", emp_ids).fetchall():
            recycle_add(conn, 'Member', _r['id'], _r['name'], dict(_r))
        conn.execute(f"DELETE FROM employees WHERE id IN ({placeholders})", emp_ids)
        conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"Bulk deleted {len(emp_ids)} members",))
        conn.commit()
        conn.close()
        return redirect(url_for('dashboard', tab='employees', delete_msg=f"Successfully moved <b>{len(emp_ids)}</b> selected member(s) to the recycle bin!"))
        
    return redirect(url_for('dashboard', tab='employees', success_msg="No members were selected."))

@app.route('/delete_requisition/<int:req_id>')
def delete_requisition(req_id):
    if not session.get('logged_in') or session.get('role') not in ['HR', 'Admin']:
        return redirect(url_for('login'))
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    _r = conn.execute("SELECT * FROM requisitions WHERE id = ?", (req_id,)).fetchone()
    if _r:
        recycle_add(conn, 'Requisition', _r['id'], f"Requisition #{_r['id']} - {_r['type']} ({_r['emp_name']})", dict(_r))
    conn.execute("DELETE FROM requisitions WHERE id = ?", (req_id,))
    conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"Deleted requisition #{req_id}",))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', tab='requisitions', delete_msg=f"Requisition <b>#{req_id}</b> has been moved to the recycle bin."))

@app.route('/bulk_delete_requisitions', methods=['POST'])
def bulk_delete_requisitions():
    if not session.get('logged_in') or session.get('role') not in ['HR', 'Admin']:
        return redirect(url_for('login'))
        
    req_ids = request.form.getlist('req_ids')
    if req_ids:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        placeholders = ', '.join(['?'] * len(req_ids))
        for _r in conn.execute(f"SELECT * FROM requisitions WHERE id IN ({placeholders})", req_ids).fetchall():
            recycle_add(conn, 'Requisition', _r['id'], f"Requisition #{_r['id']} - {_r['type']} ({_r['emp_name']})", dict(_r))
        conn.execute(f"DELETE FROM requisitions WHERE id IN ({placeholders})", req_ids)
        conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"Bulk deleted {len(req_ids)} requisitions",))
        conn.commit()
        conn.close()
        return redirect(url_for('dashboard', tab='requisitions', delete_msg=f"Successfully moved <b>{len(req_ids)}</b> selected requisition(s) to the recycle bin!"))
        
    return redirect(url_for('dashboard', tab='requisitions', success_msg="No requisitions were selected."))

@app.route('/update_balance', methods=['POST'])
def update_balance():
    if not session.get('logged_in') or session.get('role') not in ['HR', 'Admin']:
        return redirect(url_for('login'))
    emp_id = request.form.get('emp_id')
    new_balance = to_int(request.form.get('new_balance', 0))
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    emp = conn.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
    if emp:
        conn.execute("UPDATE employees SET balance = ? WHERE id = ?", (new_balance, emp_id))
        conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"Updated balance for {emp['name']} to ৳{new_balance}",))
        conn.commit()
    conn.close()
    return redirect(url_for('dashboard', tab='employees', success_msg=f"Balance updated successfully for {emp['name'] if emp else 'Member'}!"))

@app.route('/print_member/<int:emp_id>')
def print_member(emp_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    emp = conn.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
    conn.close()
    if not emp:
        return "Member not found", 404

    created = emp['created_date'] if ('created_date' in emp.keys() and emp['created_date']) else '2026-11-10'

    # Values are passed as Jinja variables (auto-escaped) instead of f-string injection
    print_template = '''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Print Report - {{ emp['name'] }}</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 40px; color: #333; }
            .print-container { max-width: 700px; margin: auto; border: 2px solid #1e293b; padding: 35px; border-radius: 8px; background: #fff; }
            .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #2563EB; padding-bottom: 20px; margin-bottom: 25px; }
            .logo-area { display: flex; align-items: center; gap: 12px; }
            .company-title { font-size: 24px; font-weight: bold; color: #1e293b; letter-spacing: 1px; }
            .report-title { text-align: center; font-size: 18px; font-weight: bold; color: #475569; margin-bottom: 25px; text-transform: uppercase; letter-spacing: 1px; }
            .info-table { width: 100%; border-collapse: collapse; margin-bottom: 30px; }
            .info-table th, .info-table td { border: 1px solid #cbd5e1; padding: 12px 15px; font-size: 15px; text-align: left; }
            .info-table th { background-color: #f8fafc; color: #1e293b; width: 35%; }
            .footer { display: flex; justify-content: space-between; margin-top: 50px; padding-top: 20px; border-top: 1px solid #cbd5e1; font-size: 14px; color: #64748b; }
            .signature-box { text-align: center; margin-top: 60px; }
            .signature-line { border-top: 1px solid #333; width: 200px; margin: auto; margin-bottom: 5px; }
            @media print {
                body { margin: 0; background: #fff; }
                .print-container { border: none; padding: 0; }
                .no-print { display: none; }
            }
        </style>
    </head>
    <body onload="window.print()">
        <div class="print-container">
            <div class="header">
                <div class="logo-area">
                    <div class="company-title">OpsNexus</div>
                </div>
                <div style="text-align: right; font-size: 13px; color: #64748b;">
                    <div>Official Member Report</div>
                    <div>Date: {{ created }}</div>
                </div>
            </div>
            <div class="report-title">Member Financial Summary & Details</div>
            <table class="info-table">
                <tr>
                    <th>Member Name</th>
                    <td><b>{{ emp['name'] }}</b></td>
                </tr>
                <tr>
                    <th>Mobile Number</th>
                    <td>{{ emp['phone'] }}</td>
                </tr>
                <tr>
                    <th>Designation</th>
                    <td>{{ emp['designation'] }}</td>
                </tr>
                <tr>
                    <th>Operator</th>
                    <td>{{ emp['operator'] }}</td>
                </tr>
                <tr>
                    <th>Current Balance</th>
                    <td style="color: #10b981; font-weight: bold; font-size: 16px;">৳{{ emp['balance'] }}</td>
                </tr>
                <tr>
                    <th>Registration Date</th>
                    <td>{{ created }}</td>
                </tr>
            </table>
            <div style="display: flex; justify-content: space-between; margin-top: 80px;">
                <div class="signature-box">
                    <div class="signature-line"></div>
                    <div style="font-size: 13px; font-weight: bold;">Prepared By</div>
                </div>
                <div class="signature-box">
                    <div class="signature-line"></div>
                    <div style="font-size: 13px; font-weight: bold;">Authorized Signature</div>
                </div>
            </div>
            <div class="footer">
                <div>OpsNexus Management System</div>
                <div>System Generated Report</div>
            </div>
        </div>
        <div style="text-align: center; margin-top: 25px;" class="no-print">
            <button onclick="window.print()" style="background: #2563EB; padding: 10px 20px; font-size: 14px; border-radius: 6px; color: white; border: none; cursor: pointer;">Print Again</button>
            <button onclick="window.close()" style="background: #64748b; padding: 10px 20px; font-size: 14px; border-radius: 6px; color: white; border: none; cursor: pointer; margin-left: 10px;">Close</button>
        </div>
    </body>
    </html>
    '''
    return render_template_string(print_template, emp=emp, created=created)

@app.route('/print_requisition/<int:req_id>')
def print_requisition(req_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    req = conn.execute("SELECT * FROM requisitions WHERE id = ?", (req_id,)).fetchone()
    conn.close()
    if not req:
        return "Requisition not found", 404

    created = req['created_date'] if ('created_date' in req.keys() and req['created_date']) else '2026-11-10'
    status_color = '#10b981' if req['status'] == 'Approved' else ('#d97706' if req['status'] == 'Pending' else '#dc2626')

    print_template = '''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Requisition Pad - #{{ req['id'] }}</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 40px; color: #333; }
            .print-container { max-width: 700px; margin: auto; border: 2px solid #1e293b; padding: 35px; border-radius: 8px; background: #fff; }
            .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #2563EB; padding-bottom: 20px; margin-bottom: 25px; }
            .company-title { font-size: 24px; font-weight: bold; color: #1e293b; letter-spacing: 1px; }
            .report-title { text-align: center; font-size: 18px; font-weight: bold; color: #475569; margin-bottom: 25px; text-transform: uppercase; letter-spacing: 1px; }
            .info-table { width: 100%; border-collapse: collapse; margin-bottom: 30px; }
            .info-table th, .info-table td { border: 1px solid #cbd5e1; padding: 12px 15px; font-size: 15px; text-align: left; }
            .info-table th { background-color: #f8fafc; color: #1e293b; width: 35%; }
            .footer { display: flex; justify-content: space-between; margin-top: 50px; padding-top: 20px; border-top: 1px solid #cbd5e1; font-size: 14px; color: #64748b; }
            .signature-box { text-align: center; margin-top: 60px; }
            .signature-line { border-top: 1px solid #333; width: 200px; margin: auto; margin-bottom: 5px; }
            @media print {
                body { margin: 0; background: #fff; }
                .print-container { border: none; padding: 0; }
                .no-print { display: none; }
            }
        </style>
    </head>
    <body onload="window.print()">
        <div class="print-container">
            <div class="header">
                <div class="company-title">OpsNexus</div>
                <div style="text-align: right; font-size: 13px; color: #64748b;">
                    <div>Official Requisition Pad</div>
                    <div>Date: {{ created }}</div>
                </div>
            </div>
            <div class="report-title">Office Requisition Slip #{{ req['id'] }}</div>
            <table class="info-table">
                <tr>
                    <th>Requisition ID</th>
                    <td><b>#{{ req['id'] }}</b></td>
                </tr>
                <tr>
                    <th>Submitted By</th>
                    <td>{{ req['emp_name'] }}</td>
                </tr>
                <tr>
                    <th>Request Type</th>
                    <td>{{ req['type'] }}</td>
                </tr>
                <tr>
                    <th>Details Description</th>
                    <td>{{ req['details'] }}</td>
                </tr>
                <tr>
                    <th>Status</th>
                    <td><b style="color: {{ status_color }}">{{ req['status'] }}</b></td>
                </tr>
                {% if remark %}
                <tr>
                    <th>Rejection Remark</th>
                    <td>{{ remark }}</td>
                </tr>
                {% endif %}
                <tr>
                    <th>Submission Date</th>
                    <td>{{ created }}</td>
                </tr>
            </table>
            <div style="display: flex; justify-content: space-between; margin-top: 80px;">
                <div class="signature-box">
                    <div class="signature-line"></div>
                    <div style="font-size: 13px; font-weight: bold;">Applicant Signature</div>
                </div>
                <div class="signature-box">
                    <div class="signature-line"></div>
                    <div style="font-size: 13px; font-weight: bold;">Authorized Approval</div>
                </div>
            </div>
            <div class="footer">
                <div>OpsNexus Management System</div>
                <div>System Generated Slip</div>
            </div>
        </div>
        <div style="text-align: center; margin-top: 25px;" class="no-print">
            <button onclick="window.print()" style="background: #2563EB; padding: 10px 20px; font-size: 14px; border-radius: 6px; color: white; border: none; cursor: pointer;">Print Again</button>
            <button onclick="window.close()" style="background: #64748b; padding: 10px 20px; font-size: 14px; border-radius: 6px; color: white; border: none; cursor: pointer; margin-left: 10px;">Close</button>
        </div>
    </body>
    </html>
    '''
    remark = req['remark'] if ('remark' in req.keys() and req['remark'] and req['status'] == 'Rejected') else ''
    return render_template_string(print_template, req=req, created=created, status_color=status_color, remark=remark)

@app.route('/add_requisition', methods=['POST'])
def add_requisition():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    user = session.get('user_id', session.get('user'))
    req_type = request.form.get('req_type')
    details = request.form.get('details')
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO requisitions (emp_name, type, details, status, created_date) VALUES (?, ?, ?, 'Pending', CURRENT_DATE)",
                     (user, req_type, details))
    except sqlite3.OperationalError:
        cursor.execute("CREATE TABLE IF NOT EXISTS requisitions (id INTEGER PRIMARY KEY AUTOINCREMENT, emp_name TEXT NOT NULL, type TEXT NOT NULL, details TEXT NOT NULL, status TEXT DEFAULT 'Pending', created_date DATE DEFAULT CURRENT_DATE, remark TEXT)")
        cursor.execute("INSERT INTO requisitions (emp_name, type, details, status, created_date) VALUES (?, ?, ?, 'Pending', CURRENT_DATE)",
                     (user, req_type, details))
    conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"User submitted a requisition for {req_type}",))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', tab='requisitions', success_msg="Requisition submitted successfully!"))

@app.route('/update_requisition_details', methods=['POST'])
def update_requisition_details():
    if not session.get('logged_in') or session.get('role') not in ['HR', 'Admin']:
        return redirect(url_for('login'))
    req_id = request.form.get('req_id')
    req_type = request.form.get('req_type')
    details = request.form.get('details')
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE requisitions SET type = ?, details = ? WHERE id = ?", (req_type, details, req_id))
    conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"Updated requisition #{req_id}",))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', tab='requisitions', success_msg=f"Requisition #{req_id} updated successfully!"))

@app.route('/update_requisition/<int:req_id>/<status>')
def update_requisition(req_id, status):
    if not session.get('logged_in') or session.get('role') not in ['HR', 'Admin']:
        return redirect(url_for('login'))
    # Only allow valid status values
    # Rejection must go through /reject_requisition (remark is required)
    if status == 'Rejected':
        return redirect(url_for('dashboard', tab='requisitions', success_msg="Error: A remark is required to reject a requisition!"))
    if status not in ['Approved', 'Pending']:
        return redirect(url_for('dashboard', tab='requisitions', success_msg="Invalid status!"))
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE requisitions SET status = ?, remark = NULL WHERE id = ?", (status, req_id))
    conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"Marked requisition #{req_id} as {status}",))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', tab='requisitions', success_msg=f"Requisition has been {status.lower()}!"))

@app.route('/reject_requisition', methods=['POST'])
def reject_requisition():
    if not session.get('logged_in') or session.get('role') not in ['HR', 'Admin']:
        return redirect(url_for('login'))
    req_id = to_int(request.form.get('req_id'), 0)
    remark = (request.form.get('remark') or '').strip()

    if not remark and load_settings().get('reject_remark_required', True):
        return redirect(url_for('dashboard', tab='requisitions', success_msg="Error: A remark is required to reject a requisition!"))

    conn = sqlite3.connect(DB_NAME)
    row = conn.execute("SELECT id FROM requisitions WHERE id = ?", (req_id,)).fetchone()
    if not row:
        conn.close()
        return redirect(url_for('dashboard', tab='requisitions', success_msg="Requisition not found!"))

    conn.execute("UPDATE requisitions SET status = 'Rejected', remark = ? WHERE id = ?", (remark, req_id))
    conn.execute("INSERT INTO activity_logs (action) VALUES (?)", (f"Rejected requisition #{req_id} with remark: {remark}",))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', tab='requisitions', success_msg=f"Requisition #{req_id} has been rejected!"))

# ---------------------------------------------------------------------------
# Reports (printable on A4 paper / pad)
# ---------------------------------------------------------------------------
MONTH_RE = re.compile(r'^\d{4}-\d{2}$')

REPORT_PAGE = """
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }} - {{ month_label }}</title>
<style>
  @page { size: A4; margin: 14mm 12mm; }
  * { box-sizing: border-box; }
  body { font-family: 'Segoe UI', Tahoma, Arial, sans-serif; background:#f1f5f9; margin:0; padding:20px; color:#1e293b; }
  .toolbar { max-width:900px; margin:0 auto 14px; display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
  .toolbar form { display:flex; gap:8px; align-items:center; margin:0; }
  .toolbar input[type=month]{ padding:8px 10px; border:1px solid #cbd5e1; border-radius:6px; }
  .btn { background:#2563EB; color:#fff; border:none; padding:9px 18px; border-radius:6px; font-size:14px; cursor:pointer; text-decoration:none; display:inline-block; }
  .btn.gray { background:#64748b; }
  .btn.green { background:#059669; }
  .sheet { max-width:900px; margin:auto; background:#fff; padding:32px 36px; border:1px solid #cbd5e1; border-radius:6px; }
  .head { display:flex; justify-content:space-between; align-items:flex-start; border-bottom:3px solid #2563EB; padding-bottom:14px; margin-bottom:16px; }
  .company { font-size:26px; font-weight:800; letter-spacing:1px; }
  .sub { font-size:12px; color:#64748b; text-align:right; line-height:1.6; }
  .rtitle { text-align:center; font-size:18px; font-weight:700; text-transform:uppercase; letter-spacing:1px; margin:6px 0 4px; }
  .rmonth { text-align:center; color:#475569; font-size:14px; margin-bottom:18px; }
  .cards { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:18px; }
  .card { flex:1 1 140px; border:1px solid #cbd5e1; border-left:4px solid #2563EB; border-radius:6px; padding:10px 12px; background:#f8fafc; }
  .card .v { font-size:20px; font-weight:800; }
  .card .l { font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:.5px; }
  h3 { font-size:14px; margin:20px 0 8px; padding-bottom:4px; border-bottom:1px solid #cbd5e1; color:#1e293b; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; margin-bottom:6px; }
  th, td { border:1px solid #cbd5e1; padding:6px 8px; text-align:left; vertical-align:top; }
  th { background:#f1f5f9; }
  tr { page-break-inside:avoid; }
  thead { display:table-header-group; }
  .empty { color:#64748b; font-style:italic; padding:8px 0; }
  .note { font-size:11.5px; color:#64748b; margin:6px 0; }
  .sign { display:flex; justify-content:space-between; margin-top:70px; }
  .sign div { text-align:center; width:200px; border-top:1px solid #333; padding-top:5px; font-size:13px; font-weight:600; }
  .foot { display:flex; justify-content:space-between; margin-top:28px; padding-top:10px; border-top:1px solid #cbd5e1; font-size:11.5px; color:#64748b; }
  @media print {
    body { background:#fff; padding:0; }
    .toolbar { display:none; }
    .sheet { border:none; padding:0; max-width:none; border-radius:0; }
    * { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  }
</style></head>
<body>
<div class="toolbar">
  <a class="btn gray" href="/dashboard?tab=reports">← Back to Reports</a>
  {% if use_month %}
  <form method="GET"><label>Month: <input type="month" name="month" value="{{ month }}"></label><button class="btn" type="submit">Show</button></form>
  {% endif %}
  <button class="btn green" onclick="window.print()">🖨️ Print</button>
</div>
<div class="sheet">
  <div class="head">
    <div class="company">{{ company }}</div>
    <div class="sub"><div>{{ print_header }}</div><div>Generated: {{ generated }}</div><div>By: {{ prepared_by }}</div></div>
  </div>
  <div class="rtitle">{{ title }}</div>
  <div class="rmonth">{{ month_label }}</div>
  {% if cards %}<div class="cards">{% for l, v in cards %}<div class="card"><div class="v">{{ v }}</div><div class="l">{{ l }}</div></div>{% endfor %}</div>{% endif %}
  {% for s in sections %}
    <h3>{{ s.heading }}</h3>
    {% if s.rows %}
    <table><thead><tr>{% for h in s.headers %}<th>{{ h }}</th>{% endfor %}</tr></thead>
    <tbody>{% for r in s.rows %}<tr>{% for c in r %}<td>{{ c }}</td>{% endfor %}</tr>{% endfor %}</tbody></table>
    {% else %}<div class="empty">No data found.</div>{% endif %}
    {% if s.note %}<div class="note">{{ s.note }}</div>{% endif %}
  {% endfor %}
  {% if show_prepared or show_approved %}
  <div class="sign">
    {% if show_prepared %}<div>Prepared By</div>{% else %}<span></span>{% endif %}
    {% if show_approved %}<div>Approved By</div>{% else %}<span></span>{% endif %}
  </div>
  {% endif %}
  <div class="foot"><div>{{ company }} Management System</div><div>{{ print_footer }}</div></div>
</div>
</body></html>
"""


@app.route('/report/<kind>')
def report_view(kind):
    if not session.get('logged_in') or session.get('role') not in ('Admin', 'HR'):
        return redirect(url_for('login'))
    meta = {k: (ic, title) for k, ic, title, _ in REPORT_META}
    if kind not in meta:
        return "Report not found", 404

    import datetime
    month = (request.args.get('month') or '').strip()
    if not MONTH_RE.match(month):
        month = datetime.date.today().strftime('%Y-%m')
    try:
        month_label = datetime.datetime.strptime(month, '%Y-%m').strftime('%B %Y')
    except ValueError:
        month = datetime.date.today().strftime('%Y-%m')
        month_label = datetime.date.today().strftime('%B %Y')

    st = load_settings()
    cur = str(st.get('currency_symbol', '৳'))
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cards, sections, use_month = [], [], True
    title = meta[kind][1]

    if kind == 'mobile_bill':
        emps = conn.execute("SELECT * FROM employees WHERE substr(created_date,1,7) <= ? ORDER BY name", (month,)).fetchall()
        tb = sum(e['balance'] or 0 for e in emps); tu = sum(e['used'] or 0 for e in emps)
        bills = conn.execute("SELECT * FROM requisitions WHERE substr(created_date,1,7)=? AND lower(type) LIKE '%phone bill%' ORDER BY id", (month,)).fetchall()
        upd = conn.execute("SELECT timestamp, action FROM activity_logs WHERE substr(timestamp,1,7)=? AND action LIKE 'Updated balance%' ORDER BY id", (month,)).fetchall()
        cards = [('Members', len(emps)), ('Total Balance', f"{cur}{tb}"), ('Total Used', f"{cur}{tu}"),
                 ('Bill Requests', len(bills)), ('Approved', len([b for b in bills if b['status'] == 'Approved']))]
        sections.append({'heading': '1. Member-wise Mobile Bill', 'headers': ['#', 'Name', 'Mobile', 'Operator', 'Designation', 'Balance', 'Used'],
                         'rows': [[i, e['name'], e['phone'], e['operator'], e['designation'], f"{cur}{e['balance'] or 0}", f"{cur}{e['used'] or 0}"] for i, e in enumerate(emps, 1)],
                         'note': 'Balance and Used are the current figures for each member registered up to this month.'})
        ops = {}
        for e in emps:
            o = ops.setdefault(e['operator'], [0, 0, 0]); o[0] += 1; o[1] += e['balance'] or 0; o[2] += e['used'] or 0
        sections.append({'heading': '2. Operator Summary', 'headers': ['Operator', 'Members', 'Balance', 'Used'],
                         'rows': [[k, v[0], f"{cur}{v[1]}", f"{cur}{v[2]}"] for k, v in sorted(ops.items())]})
        sections.append({'heading': '3. Phone Bill Requisitions (this month)', 'headers': ['ID', 'Member', 'Details', 'Status', 'Date'],
                         'rows': [[b['id'], b['emp_name'], b['details'], b['status'], b['created_date']] for b in bills]})
        sections.append({'heading': '4. Balance Updates (this month)', 'headers': ['Date & Time', 'Action'],
                         'rows': [[u['timestamp'], u['action']] for u in upd]})

    elif kind == 'requisition_stats':
        reqs = conn.execute("SELECT * FROM requisitions WHERE substr(created_date,1,7)=? ORDER BY id", (month,)).fetchall()
        cnt = lambda s: len([r for r in reqs if r['status'] == s])
        total = len(reqs); ap = cnt('Approved')
        cards = [('Total', total), ('Approved', ap), ('Pending', cnt('Pending')), ('Rejected', cnt('Rejected')),
                 ('Approval Rate', f"{(ap * 100 // total) if total else 0}%")]
        types = {}
        for r in reqs:
            d = types.setdefault(r['type'], {'Approved': 0, 'Pending': 0, 'Rejected': 0, 'n': 0})
            d['n'] += 1
            if r['status'] in d: d[r['status']] += 1
        sections.append({'heading': '1. Type-wise Statistics', 'headers': ['Type', 'Total', 'Approved', 'Pending', 'Rejected'],
                         'rows': [[k, v['n'], v['Approved'], v['Pending'], v['Rejected']] for k, v in sorted(types.items())]})
        per = {}
        for r in reqs:
            d = per.setdefault(r['emp_name'], [0, 0, 0, 0]); d[0] += 1
            if r['status'] == 'Approved': d[1] += 1
            elif r['status'] == 'Pending': d[2] += 1
            elif r['status'] == 'Rejected': d[3] += 1
        sections.append({'heading': '2. Member-wise Statistics', 'headers': ['Member', 'Total', 'Approved', 'Pending', 'Rejected'],
                         'rows': [[k] + v for k, v in sorted(per.items())]})
        sections.append({'heading': '3. All Requisitions', 'headers': ['ID', 'Member', 'Type', 'Details', 'Status', 'Date', 'Remark'],
                         'rows': [[r['id'], r['emp_name'], r['type'], r['details'], r['status'], r['created_date'], r['remark'] or '-'] for r in reqs]})

    elif kind == 'user_report':
        users = conn.execute("SELECT * FROM app_users ORDER BY id").fetchall()
        new_u = [u for u in users if (u['created_at'] or '')[:7] == month]
        emp_n = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
        cards = [('System Users', len(users)), ('Active', len([u for u in users if u['status'] == 'Active'])),
                 ('Pending', len([u for u in users if u['status'] == 'Pending'])),
                 ('New This Month', len(new_u)), ('Members (employees)', emp_n)]
        roles = {}
        for u in users:
            roles.setdefault(u['role'], [0, 0, 0]); roles[u['role']][0] += 1
            if u['status'] == 'Active': roles[u['role']][1] += 1
            else: roles[u['role']][2] += 1
        sections.append({'heading': '1. Role Summary', 'headers': ['Role', 'Total', 'Active', 'Other / Pending'],
                         'rows': [[k] + v for k, v in sorted(roles.items())]})
        sections.append({'heading': '2. User List', 'headers': ['#', 'Name', 'User ID', 'Email', 'Role', 'Status', 'Created', 'Approved By'],
                         'rows': [[i, u['full_name'], u['user_id'], u['email'] or '-', u['role'], u['status'], (u['created_at'] or '')[:10], u['approved_by'] or '-'] for i, u in enumerate(users, 1)]})
        sections.append({'heading': '3. New Users (this month)', 'headers': ['Name', 'User ID', 'Role', 'Status', 'Created'],
                         'rows': [[u['full_name'], u['user_id'], u['role'], u['status'], (u['created_at'] or '')[:10]] for u in new_u]})

    elif kind == 'balance_report':
        use_month = False
        month_label = 'As of ' + datetime.date.today().strftime('%d %B %Y')
        emps = conn.execute("SELECT * FROM employees ORDER BY operator, name").fetchall()
        limit = to_int(st.get('low_balance_limit', 100), 100)
        low = [e for e in emps if (e['balance'] or 0) <= limit]
        cards = [('Members', len(emps)), ('Total Balance', f"{cur}{sum(e['balance'] or 0 for e in emps)}"),
                 ('Total Used', f"{cur}{sum(e['used'] or 0 for e in emps)}"), (f'Low Balance (≤{limit})', len(low))]
        ops = {}
        for e in emps:
            o = ops.setdefault(e['operator'], [0, 0, 0]); o[0] += 1; o[1] += e['balance'] or 0; o[2] += e['used'] or 0
        sections.append({'heading': '1. Operator-wise Summary', 'headers': ['Operator', 'Members', 'Balance', 'Used'],
                         'rows': [[k, v[0], f"{cur}{v[1]}", f"{cur}{v[2]}"] for k, v in sorted(ops.items())]})
        sections.append({'heading': '2. Low Balance Members', 'headers': ['Name', 'Mobile', 'Operator', 'Balance'],
                         'rows': [[e['name'], e['phone'], e['operator'], f"{cur}{e['balance'] or 0}"] for e in low]})

    elif kind == 'activity_report':
        logs = conn.execute("SELECT * FROM activity_logs WHERE substr(timestamp,1,7)=? ORDER BY id DESC LIMIT 1000", (month,)).fetchall()
        days = {}
        for l in logs: days[(l['timestamp'] or '')[:10]] = days.get((l['timestamp'] or '')[:10], 0) + 1
        cards = [('Total Activities', len(logs)), ('Active Days', len(days))]
        sections.append({'heading': '1. Activity Log', 'headers': ['Date & Time', 'Action'],
                         'rows': [[l['timestamp'], l['action']] for l in logs],
                         'note': 'Shows up to the latest 1000 entries of the month.'})
    conn.close()

    return render_template_string(
        REPORT_PAGE, title=title, month=month, month_label=month_label, use_month=use_month,
        company=st.get('company_name', 'OpsNexus'), print_header=st.get('print_header', 'Official Company Report'),
        print_footer=st.get('print_footer', 'System Generated Report'),
        show_prepared=st.get('show_prepared_by', True), show_approved=st.get('show_approved_by', True),
        generated=datetime.datetime.now().strftime('%d-%m-%Y %H:%M'), prepared_by=session.get('name', ''),
        cards=cards, sections=sections)


@app.route('/save_theme', methods=['POST'])
def save_theme():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    key = (request.form.get('theme') or '').strip()
    if key not in THEMES:
        return redirect(url_for('dashboard', tab='theme', success_msg='Invalid theme selected.'))
    set_user_theme(session.get('user') or session.get('user_id'), key)
    return redirect(url_for('dashboard', tab='theme', success_msg='Theme changed to ' + THEMES[key]['name'] + '.'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    # debug=True with host 0.0.0.0 exposes the Werkzeug debugger to the network.
    # Development-e chalate: set FLASK_DEBUG=1
    app.run(host='0.0.0.0', port=5000, debug=os.environ.get('FLASK_DEBUG') == '1')
            phone TEXT,
            password_hash TEXT,
            target_user_id INTEGER,
            otp TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
