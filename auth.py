"""User auth module: SQLite shared_users.db + admin approval flow.

Supports registration and password-reset requests that require admin approval.
"""
import os
import sqlite3
import secrets
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps

from flask import session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.environ.get("SHARED_USERS_DB", "/root/webapps/shared_users.db")

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('%(asctime)s auth %(levelname)s %(message)s'))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


def _smtp_creds():
    """Read SMTP settings at send-time (not import-time) so PM2 env always applies."""
    return (
        os.getenv('NOTIFY_SMTP_USER', '') or '',
        os.getenv('NOTIFY_SMTP_PASS', '') or '',
        os.getenv('NOTIFY_ADMIN_EMAIL', 'chaitanya@cpcmail.in') or 'chaitanya@cpcmail.in',
    )


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id          TEXT    NOT NULL UNIQUE,
                name                 TEXT    NOT NULL,
                password_hash        TEXT    NOT NULL,
                created_at           TEXT    DEFAULT (datetime('now')),
                upload_password_hash TEXT    DEFAULT NULL,
                status               TEXT    NOT NULL DEFAULT 'approved',
                approval_token       TEXT    DEFAULT NULL
            )
        ''')
        # Safe migrations for existing databases
        for col, defn in [
            ('upload_password_hash', 'TEXT DEFAULT NULL'),
            ('status',               "TEXT NOT NULL DEFAULT 'approved'"),
            ('approval_token',       'TEXT DEFAULT NULL'),
            ('pending_password_hash','TEXT DEFAULT NULL'),
            ('reset_token',          'TEXT DEFAULT NULL'),
        ]:
            try:
                conn.execute(f'ALTER TABLE users ADD COLUMN {col} {defn}')
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()


def register_user(employee_id: str, name: str, password: str,
                  app_name: str = '', app_url: str = ''):
    """Insert pending user, send approval email. Returns None on success, error string on failure."""
    if not employee_id.strip() or not name.strip() or not password:
        return 'All fields are required.'
    if len(password) < 6:
        return 'Password must be at least 6 characters.'
    token = secrets.token_urlsafe(32)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'INSERT INTO users (employee_id, name, password_hash, status, approval_token) '
                'VALUES (?, ?, ?, ?, ?)',
                (employee_id.strip(), name.strip(),
                 generate_password_hash(password), 'pending', token)
            )
            conn.commit()
        _send_approval_email(employee_id.strip(), name.strip(), token, app_name, app_url)
        return None
    except sqlite3.IntegrityError:
        return 'Employee ID already registered.'


def request_password_reset(employee_id: str, new_password: str,
                           app_name: str = '', app_url: str = ''):
    """Queue a password reset for admin approval. Returns None on success, error string on failure."""
    if not employee_id.strip() or not new_password:
        return 'Employee ID and new password are required.'
    if len(new_password) < 6:
        return 'Password must be at least 6 characters.'

    eid = employee_id.strip()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            'SELECT id, name, status, reset_token FROM users WHERE employee_id = ?',
            (eid,)
        ).fetchone()
        if not row:
            # Avoid account enumeration — look like success to the requester.
            return None
        if (row[2] or 'approved') == 'pending':
            return 'This account is still awaiting registration approval.'
        if row[3]:
            return 'A password reset is already pending admin approval for this account.'

        token = secrets.token_urlsafe(32)
        conn.execute(
            'UPDATE users SET pending_password_hash = ?, reset_token = ? WHERE id = ?',
            (generate_password_hash(new_password), token, row[0])
        )
        conn.commit()
        name = row[1]

    _send_reset_approval_email(eid, name, token, app_name, app_url)
    return None


def approve_password_reset(token: str):
    """Apply a pending password reset. Returns user dict or None if token invalid."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            'SELECT id, employee_id, name, pending_password_hash FROM users '
            'WHERE reset_token = ? AND pending_password_hash IS NOT NULL',
            (token,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            'UPDATE users SET password_hash = ?, pending_password_hash = NULL, '
            'reset_token = NULL WHERE id = ?',
            (row[3], row[0])
        )
        conn.commit()
    return {'employee_id': row[1], 'name': row[2]}


def authenticate_user(employee_id: str, password: str):
    """Return user dict on success, 'pending' if awaiting approval, None on bad credentials."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            'SELECT id, employee_id, name, password_hash, status FROM users WHERE employee_id = ?',
            (employee_id.strip(),)
        ).fetchone()
    if not row:
        return None
    if not check_password_hash(row[3], password):
        return None
    status = row[4] if len(row) > 4 else 'approved'
    if status == 'pending':
        return 'pending'
    return {'id': row[0], 'employee_id': row[1], 'name': row[2]}


def approve_user(token: str):
    """Approve a pending registration by token. Returns user dict or None if token invalid."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, employee_id, name FROM users "
            "WHERE approval_token = ? AND status = 'pending'",
            (token,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE users SET status = 'approved', approval_token = NULL WHERE id = ?",
            (row[0],)
        )
        conn.commit()
    return {'employee_id': row[1], 'name': row[2]}


def _send_approval_email(employee_id: str, name: str, token: str,
                         app_name: str, app_url: str):
    smtp_user, smtp_pass, admin_email = _smtp_creds()
    if not smtp_user or not smtp_pass:
        log.warning('NOTIFY_SMTP_USER/PASS not set — approval email not sent for %s', employee_id)
        return
    approve_url = f'{app_url.rstrip("/")}/admin/approve/{token}'
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'[CPCL] New Registration Pending Approval — {employee_id}'
    msg['From']    = smtp_user
    msg['To']      = admin_email
    body = (
        f'New account registration request on {app_name}.\n\n'
        f'  Employee ID : {employee_id}\n'
        f'  Name        : {name}\n'
        f'  App         : {app_name}\n\n'
        f'To approve, open this link:\n\n'
        f'  {approve_url}\n\n'
        f'This link is single-use and expires once clicked.\n'
        f'If you did not expect this, ignore this email.\n'
    )
    msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(smtp_user, smtp_pass)
            smtp.sendmail(smtp_user, admin_email, msg.as_string())
        log.info('Approval email sent for %s → %s', employee_id, admin_email)
    except Exception as exc:
        log.error('Approval email failed for %s: %s', employee_id, exc)


def _send_reset_approval_email(employee_id: str, name: str, token: str,
                               app_name: str, app_url: str):
    smtp_user, smtp_pass, admin_email = _smtp_creds()
    if not smtp_user or not smtp_pass:
        log.warning('NOTIFY_SMTP_USER/PASS not set — reset email not sent for %s', employee_id)
        return
    approve_url = f'{app_url.rstrip("/")}/admin/approve-reset/{token}'
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'[CPCL] Password Reset Pending Approval — {employee_id}'
    msg['From']    = smtp_user
    msg['To']      = admin_email
    body = (
        f'Password reset requested on {app_name}.\n\n'
        f'  Employee ID : {employee_id}\n'
        f'  Name        : {name}\n'
        f'  App         : {app_name}\n\n'
        f'To approve the new password, open this link:\n\n'
        f'  {approve_url}\n\n'
        f'Until approved, the current password still works.\n'
        f'This link is single-use. If you did not expect this, ignore this email.\n'
    )
    msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(smtp_user, smtp_pass)
            smtp.sendmail(smtp_user, admin_email, msg.as_string())
        log.info('Password-reset approval email sent for %s → %s', employee_id, admin_email)
    except Exception as exc:
        log.error('Password-reset approval email failed for %s: %s', employee_id, exc)


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated
