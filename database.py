try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    print("[Database] Warning: psycopg2 not found. Database features will be unavailable.")

from datetime import datetime, timezone
from typing import Optional

DATABASE_URL = os.getenv('DATABASE_URL', '')


def get_connection():
    """Get a new connection to the PostgreSQL database."""
    if not PSYCOPG2_AVAILABLE:
        raise ImportError("psycopg2 is not installed or failed to load.")
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(DATABASE_URL, sslmode='require')


def init_db():
    """Create tables if they don't exist. Call once at startup."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Users table — keyed by Yahoo GUID
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    guid TEXT PRIMARY KEY,
                    trial_start TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    is_paid BOOLEAN NOT NULL DEFAULT FALSE,
                    promo_code_used TEXT,
                    stripe_customer_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            # Promo codes table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS promo_codes (
                    code TEXT PRIMARY KEY,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    used_by_guid TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            # Settings table (key-value)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

            # Insert default settings if they don't exist yet
            cur.execute("""
                INSERT INTO settings (key, value)
                VALUES ('trial_days', '14')
                ON CONFLICT (key) DO NOTHING;
            """)

            conn.commit()


# ─── Settings ─────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = '') -> str:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else default


def set_setting(key: str, value: str):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO settings (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
            """, (key, value))
            conn.commit()


def get_trial_days() -> int:
    return int(get_setting('trial_days', '14'))


# ─── Users ────────────────────────────────────────────────────────────────────

def get_or_create_user(guid: str) -> dict:
    """Get an existing user by GUID, or create a new one (starts trial)."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE guid = %s", (guid,))
            row = cur.fetchone()
            if row:
                return dict(row)

            # New user — register with trial start now
            cur.execute("""
                INSERT INTO users (guid, trial_start, is_paid)
                VALUES (%s, NOW(), FALSE)
                RETURNING *;
            """, (guid,))
            conn.commit()
            return dict(cur.fetchone())


def get_user(guid: str) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE guid = %s", (guid,))
            row = cur.fetchone()
            return dict(row) if row else None


def set_user_paid(guid: str, stripe_customer_id: str = None):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users SET is_paid = TRUE, stripe_customer_id = %s
                WHERE guid = %s;
            """, (stripe_customer_id, guid))
            conn.commit()


def get_all_users() -> list:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users ORDER BY created_at DESC;")
            return [dict(r) for r in cur.fetchall()]


def is_trial_active(user: dict) -> bool:
    """Return True if the user's free trial has not expired."""
    trial_days = get_trial_days()
    trial_start = user.get('trial_start')
    if not trial_start:
        return False
    # Ensure timezone-aware comparison
    if trial_start.tzinfo is None:
        trial_start = trial_start.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - trial_start
    return delta.days < trial_days


def has_access(guid: str) -> bool:
    """Check for any form of valid access: paid, trial, or promo code."""
    user = get_or_create_user(guid)
    if user.get('is_paid'):
        return True
    if user.get('promo_code_used'):
        return True
    return is_trial_active(user)


# ─── Promo Codes ──────────────────────────────────────────────────────────────

def create_promo_code(code: str) -> bool:
    """Create a new active promo code. Returns False if it already exists."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO promo_codes (code, is_active)
                    VALUES (%s, TRUE);
                """, (code.upper().strip(),))
                conn.commit()
                return True
    except psycopg2.errors.UniqueViolation:
        return False


def redeem_promo_code(code: str, guid: str) -> tuple[bool, str]:
    """
    Redeem a promo code for a user.
    Returns (success: bool, message: str).
    """
    code = code.upper().strip()
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM promo_codes WHERE code = %s", (code,))
            promo = cur.fetchone()

            if not promo:
                return False, 'קוד הטבה לא קיים'

            if not promo['is_active']:
                return False, 'קוד הטבה כבר נוצל'

            # Check if this user already used a promo
            cur.execute("SELECT promo_code_used FROM users WHERE guid = %s", (guid,))
            user_row = cur.fetchone()
            if user_row and user_row['promo_code_used']:
                return False, 'כבר השתמשת בקוד הטבה'

            # Mark code as used
            cur.execute("""
                UPDATE promo_codes SET is_active = FALSE, used_by_guid = %s WHERE code = %s;
            """, (guid, code))

            # Mark user as having used a promo
            cur.execute("""
                UPDATE users SET promo_code_used = %s WHERE guid = %s;
            """, (code, guid))

            conn.commit()
            return True, 'קוד הטבה אושר! גישה מלאה ניתנת'


def get_all_promo_codes() -> list:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM promo_codes ORDER BY created_at DESC;")
            return [dict(r) for r in cur.fetchall()]


def deactivate_promo_code(code: str):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE promo_codes SET is_active = FALSE WHERE code = %s", (code,))
            conn.commit()
