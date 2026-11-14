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
