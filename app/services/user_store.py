from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "addref.sqlite3"
SESSION_TTL_DAYS = 30
PASSWORD_ITERATIONS = 260_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VERIFICATION_CODE_TTL_MINUTES = 10
VERIFICATION_CODE_COOLDOWN_SECONDS = 60
REGISTER_CODE_PURPOSE = "register"
RESET_PASSWORD_CODE_PURPOSE = "reset_password"
REGISTRATION_DAILY_LIMIT_PER_IP = 10


class UserStoreError(RuntimeError):
    pass


class AuthenticationError(UserStoreError):
    pass


class ValidationError(UserStoreError):
    pass


_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def init_db() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return

    with _INIT_LOCK:
        if _INITIALIZED:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _connect() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    usage_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    text_length INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    used_default_openai INTEGER NOT NULL,
                    used_default_ncbi INTEGER NOT NULL,
                    used_default_service INTEGER NOT NULL,
                    endpoint TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS email_verification_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS registration_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip_address TEXT NOT NULL,
                    registration_date TEXT NOT NULL,
                    email TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_user_id
                ON sessions(user_id);

                CREATE INDEX IF NOT EXISTS idx_sessions_expires_at
                ON sessions(expires_at);

                CREATE INDEX IF NOT EXISTS idx_usage_logs_user_date
                ON usage_logs(user_id, usage_date);

                CREATE INDEX IF NOT EXISTS idx_email_codes_lookup
                ON email_verification_codes(email, purpose, created_at);

                CREATE INDEX IF NOT EXISTS idx_registration_logs_ip_date
                ON registration_logs(ip_address, registration_date);
                """
            )
        _INITIALIZED = True


def create_user_with_verification(
    email: str,
    password: str,
    verification_code: str,
    *,
    ip_address: str,
) -> dict[str, Any]:
    init_db()
    normalized_email = _normalize_email(email)
    _validate_email(normalized_email)
    _validate_password(password)
    _validate_verification_code(verification_code)
    normalized_ip = (ip_address or "").strip() or "unknown"
    created_at = _now_iso()
    registration_date = _now().date().isoformat()
    password_hash = _hash_password(password)

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        registration_count = _count_successful_registrations_by_ip(
            conn,
            ip_address=normalized_ip,
            registration_date=registration_date,
        )
        if registration_count >= REGISTRATION_DAILY_LIMIT_PER_IP:
            raise ValidationError("同一 IP 今日注册已达 10 次上限，请明天再试。")

        code_row = _find_matching_verification_code(
            conn,
            email=normalized_email,
            purpose=REGISTER_CODE_PURPOSE,
            verification_code=verification_code,
        )
        if code_row is None:
            raise ValidationError("验证码错误或已过期。")

        try:
            cursor = conn.execute(
                """
                INSERT INTO users (email, password_hash, created_at)
                VALUES (?, ?, ?)
                """,
                (normalized_email, password_hash, created_at),
            )
            user_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValidationError("该邮箱已注册。") from exc

        updated = conn.execute(
            """
            UPDATE email_verification_codes
            SET consumed_at = ?
            WHERE id = ? AND consumed_at IS NULL
            """,
            (_now_iso(), int(code_row["id"])),
        )
        if updated.rowcount != 1:
            raise ValidationError("验证码已失效，请重新获取。")

        conn.execute(
            """
            INSERT INTO registration_logs (
                ip_address,
                registration_date,
                email,
                user_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (normalized_ip, registration_date, normalized_email, user_id, created_at),
        )

    return {"id": user_id, "email": normalized_email, "created_at": created_at}


def ensure_user_password(email: str, password: str) -> dict[str, Any]:
    init_db()
    normalized_email = _normalize_email(email)
    if not normalized_email or not password:
        raise ValidationError("缺少 owner 账号配置。")
    _validate_email(normalized_email)
    _validate_password(password)

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, email, password_hash, created_at
            FROM users
            WHERE email = ?
            """,
            (normalized_email,),
        ).fetchone()
        if row is None:
            created_at = _now_iso()
            password_hash = _hash_password(password)
            cursor = conn.execute(
                """
                INSERT INTO users (email, password_hash, created_at)
                VALUES (?, ?, ?)
                """,
                (normalized_email, password_hash, created_at),
            )
            return {"id": int(cursor.lastrowid), "email": normalized_email, "created_at": created_at}

    return _row_to_user(row)


def authenticate_user(email: str, password: str) -> dict[str, Any]:
    init_db()
    normalized_email = _normalize_email(email)
    _validate_email(normalized_email)

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, email, password_hash, created_at
            FROM users
            WHERE email = ?
            """,
            (normalized_email,),
        ).fetchone()

    if row is None or not _verify_password(password, row["password_hash"]):
        raise AuthenticationError("邮箱或密码错误。")

    return _row_to_user(row)


def reset_password_with_verification(email: str, verification_code: str, new_password: str) -> dict[str, Any]:
    init_db()
    normalized_email = _normalize_email(email)
    _validate_email(normalized_email)
    _validate_verification_code(verification_code)
    _validate_password(new_password)

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, email, password_hash, created_at
            FROM users
            WHERE email = ?
            """,
            (normalized_email,),
        ).fetchone()
        if row is None:
            raise ValidationError("该邮箱未注册。")

        code_row = _find_matching_verification_code(
            conn,
            email=normalized_email,
            purpose=RESET_PASSWORD_CODE_PURPOSE,
            verification_code=verification_code,
        )
        if code_row is None:
            raise ValidationError("验证码错误或已过期。")

        conn.execute(
            """
            UPDATE users
            SET password_hash = ?
            WHERE id = ?
            """,
            (_hash_password(new_password), int(row["id"])),
        )
        updated = conn.execute(
            """
            UPDATE email_verification_codes
            SET consumed_at = ?
            WHERE id = ? AND consumed_at IS NULL
            """,
            (_now_iso(), int(code_row["id"])),
        )
        if updated.rowcount != 1:
            raise ValidationError("验证码已失效，请重新获取。")

        conn.execute("DELETE FROM sessions WHERE user_id = ?", (int(row["id"]),))
        return _row_to_user(row)


def change_user_password(user_id: int, current_password: str, new_password: str) -> dict[str, Any]:
    init_db()
    if not current_password:
        raise ValidationError("请输入当前密码。")
    _validate_password(new_password)
    if current_password == new_password:
        raise ValidationError("新密码不能与当前密码相同。")

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, email, password_hash, created_at
            FROM users
            WHERE id = ?
            """,
            (int(user_id),),
        ).fetchone()
        if row is None:
            raise AuthenticationError("用户不存在。")
        if not _verify_password(current_password, row["password_hash"]):
            raise AuthenticationError("当前密码错误。")

        conn.execute(
            """
            UPDATE users
            SET password_hash = ?
            WHERE id = ?
            """,
            (_hash_password(new_password), int(user_id)),
        )
        return _row_to_user(row)


def create_email_verification_code(email: str, purpose: str = REGISTER_CODE_PURPOSE) -> dict[str, Any]:
    init_db()
    normalized_email = _normalize_email(email)
    _validate_email(normalized_email)
    normalized_purpose = (purpose or "").strip().lower() or REGISTER_CODE_PURPOSE
    now = _now()

    with _connect() as conn:
        if normalized_purpose == REGISTER_CODE_PURPOSE:
            existing_user = conn.execute(
                """
                SELECT 1
                FROM users
                WHERE email = ?
                LIMIT 1
                """,
                (normalized_email,),
            ).fetchone()
            if existing_user is not None:
                raise ValidationError("该邮箱已注册，请直接登录。")
        elif normalized_purpose == RESET_PASSWORD_CODE_PURPOSE:
            existing_user = conn.execute(
                """
                SELECT 1
                FROM users
                WHERE email = ?
                LIMIT 1
                """,
                (normalized_email,),
            ).fetchone()
            if existing_user is None:
                raise ValidationError("该邮箱未注册。")

        row = conn.execute(
            """
            SELECT created_at
            FROM email_verification_codes
            WHERE email = ? AND purpose = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (normalized_email, normalized_purpose),
        ).fetchone()
        if row is not None:
            created_at = datetime.fromisoformat(str(row["created_at"]))
            elapsed_seconds = int((now - created_at).total_seconds())
            if elapsed_seconds < VERIFICATION_CODE_COOLDOWN_SECONDS:
                remaining = VERIFICATION_CODE_COOLDOWN_SECONDS - max(0, elapsed_seconds)
                raise ValidationError(f"验证码发送过于频繁，请在 {remaining} 秒后重试。")

        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(16)
        created_at_iso = now.isoformat()
        expires_at_iso = (now + timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES)).isoformat()
        cursor = conn.execute(
            """
            INSERT INTO email_verification_codes (
                email,
                purpose,
                code_hash,
                salt,
                created_at,
                expires_at,
                consumed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                normalized_email,
                normalized_purpose,
                _hash_verification_code(code, salt),
                salt,
                created_at_iso,
                expires_at_iso,
            ),
        )

    return {
        "id": int(cursor.lastrowid),
        "email": normalized_email,
        "purpose": normalized_purpose,
        "code": code,
        "created_at": created_at_iso,
        "expires_at": expires_at_iso,
        "expires_in_minutes": VERIFICATION_CODE_TTL_MINUTES,
        "cooldown_seconds": VERIFICATION_CODE_COOLDOWN_SECONDS,
    }


def delete_email_verification_code(code_id: int) -> None:
    init_db()
    if not code_id:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM email_verification_codes WHERE id = ?", (int(code_id),))


def count_successful_registrations_by_ip(ip_address: str, registration_date: str) -> int:
    init_db()
    normalized_ip = (ip_address or "").strip() or "unknown"
    with _connect() as conn:
        return _count_successful_registrations_by_ip(
            conn,
            ip_address=normalized_ip,
            registration_date=registration_date,
        )


def create_session(user_id: int) -> str:
    init_db()
    token = secrets.token_urlsafe(32)
    created_at = _now()
    expires_at = created_at + timedelta(days=SESSION_TTL_DAYS)

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions (token, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, user_id, created_at.isoformat(), expires_at.isoformat()),
        )
    return token


def get_user_by_session(token: str) -> dict[str, Any] | None:
    init_db()
    if not token:
        return None

    now_iso = _now_iso()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT users.id, users.email, users.created_at
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ? AND sessions.expires_at > ?
            """,
            (token, now_iso),
        ).fetchone()
    return _row_to_user(row) if row else None


def delete_session(token: str) -> None:
    init_db()
    if not token:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def count_successful_default_uses(user_id: int, usage_date: str) -> int:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM usage_logs
            WHERE user_id = ?
              AND usage_date = ?
              AND success = 1
              AND used_default_service = 1
              AND endpoint = 'cite'
            """,
            (user_id, usage_date),
        ).fetchone()
    return int(row["count"]) if row else 0


def record_usage(
    *,
    user_id: int,
    usage_date: str,
    text_length: int,
    success: bool,
    used_default_openai: bool,
    used_default_ncbi: bool,
    endpoint: str,
) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO usage_logs (
                user_id,
                usage_date,
                created_at,
                text_length,
                success,
                used_default_openai,
                used_default_ncbi,
                used_default_service,
                endpoint
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                usage_date,
                _now_iso(),
                int(text_length),
                1 if success else 0,
                1 if used_default_openai else 0,
                1 if used_default_ncbi else 0,
                1 if (used_default_openai or used_default_ncbi) else 0,
                endpoint,
            ),
        )


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "email": str(row["email"]),
        "created_at": str(row["created_at"]),
    }


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _validate_email(email: str) -> None:
    if not EMAIL_RE.match(email):
        raise ValidationError("请输入有效的邮箱地址。")


def _validate_password(password: str) -> None:
    if len(password or "") < 8:
        raise ValidationError("密码至少需要 8 位。")


def _validate_verification_code(verification_code: str) -> None:
    code = (verification_code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ValidationError("请输入 6 位验证码。")


def _hash_verification_code(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


def _find_matching_verification_code(
    conn: sqlite3.Connection,
    *,
    email: str,
    purpose: str,
    verification_code: str,
) -> sqlite3.Row | None:
    now_iso = _now_iso()
    rows = conn.execute(
        """
        SELECT id, code_hash, salt, created_at, expires_at, consumed_at
        FROM email_verification_codes
        WHERE email = ?
          AND purpose = ?
          AND consumed_at IS NULL
        ORDER BY created_at DESC
        LIMIT 10
        """,
        (email, purpose),
    ).fetchall()

    active_rows = [row for row in rows if str(row["expires_at"]) > now_iso]
    if not active_rows:
        return None

    for row in active_rows:
        expected_hash = _hash_verification_code(verification_code.strip(), str(row["salt"]))
        if hmac.compare_digest(expected_hash, str(row["code_hash"])):
            return row
    return None


def _count_successful_registrations_by_ip(
    conn: sqlite3.Connection,
    *,
    ip_address: str,
    registration_date: str,
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM registration_logs
        WHERE ip_address = ?
          AND registration_date = ?
        """,
        (ip_address, registration_date),
    ).fetchone()
    return int(row["count"]) if row else 0


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "$".join(
        [
            "pbkdf2_sha256",
            str(PASSWORD_ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected_digest = base64.b64decode(digest_b64.encode("ascii"))
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )
        return hmac.compare_digest(derived, expected_digest)
    except Exception:  # noqa: BLE001
        return False


def _now() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now().isoformat()
