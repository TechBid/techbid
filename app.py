import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import random
import re
import secrets
import smtplib
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import mysql.connector
import mysql.connector.pooling
import requests
try:
    import bcrypt
    BCRYPT_AVAILABLE = True
except ImportError:
    BCRYPT_AVAILABLE = False
    bcrypt = None
from dotenv import load_dotenv
try:
    import jwt
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False
    jwt = None
from flask import (
    Flask, flash, g, jsonify, make_response, redirect,
    render_template, request, session, url_for, abort as flask_abort, send_from_directory, Response
)
from flask_compress import Compress
from flask_socketio import SocketIO, emit, join_room, leave_room

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

LOG = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ── Hardcoded connects packages (USD prices shown in UI, KES charged) ──────────
CONNECTS_PACKAGES = [
    {"id": "starter", "name": "Starter",   "connects": 200,  "price_usd": 6.99},
    {"id": "pro",     "name": "Pro",        "connects": 1000, "price_usd": 9.99},
    {"id": "power",   "name": "Power",      "connects": 2500, "price_usd": 20.00},
]
EMPLOYER_TRIAL_DAYS = 14       # Free trial period for new employers (14 days)

# ── Employer subscription pricing (Option B: Hybrid PAYG + subscription) ──
EMPLOYER_PAYG_JOB_PRICE = 2.99      # Pay-as-you-go: $2.99 per job
EMPLOYER_TIERS = {
    "basic": {
        "name": "Basic",
        "price": 8.00,
        "period": "month",
        "jobs_per_month": 10,
        "annual_months": 12,
        "annual_discount": 0,  # No discount on Basic currently
        "features": ["10 jobs per month", "All job features"],
    },
    "pro": {
        "name": "Pro",
        "price": 18.00,
        "period": "month",
        "jobs_per_month": float('inf'),  # Unlimited
        "annual_months": 12,
        "annual_discount": 0,  # No discount on Pro currently
        "features": [
            "Unlimited job postings",
            "AI-powered job descriptions",
            "Featured job listings",
            "Advanced hiring analytics",
            "Dedicated account manager",
            "API access for integrations",
        ],
    },
}
EMPLOYER_SUB_USD_OLD = 5.00    # Legacy: kept for reference, now use EMPLOYER_TIERS
WORKER_SIGNUP_CONNECTS = 100   # Free connects on signup
MONTHLY_BONUS_CONNECTS = 30    # Free connects every 30 days
JOB_COMPLETION_REWARD = 15     # Connects earned when job completes successfully
APPLICATION_REFUND = 3         # Connects refunded when application rejected
MIN_JOB_CONNECTS   = 6         # Minimum connects to apply for any job (lowered from 10)
ROBOT_PROPOSAL_DELAY_MINUTES = 10
ROBOT_ACTIVITY_WINDOW_MINUTES = 24 * 60

# ── Freelance job categories ───────────────────────────────────────────────────
JOB_CATEGORIES = [
    "Accounting & Finance", "Animation & Motion Graphics", "Blockchain & Web3",
    "Business Analysis", "Content Writing & Copywriting", "Customer Support",
    "Cybersecurity", "Data Science & Analytics", "Database Administration",
    "DevOps & Cloud", "E-commerce & Shopify", "Email Marketing",
    "Game Development", "Graphic Design", "Legal Services",
    "Machine Learning / AI", "Mobile Development", "Photo Editing",
    "Project Management", "Quality Assurance & Testing", "Sales & Lead Generation",
    "SEO & Digital Marketing", "Social Media Management", "Technical Writing",
    "Translation & Localization", "UI/UX Design", "Video Editing",
    "Virtual Assistance", "Web Development", "WordPress Development",
]

JOB_TYPES   = ["hourly", "daily", "fixed"]
USER_ROLES  = ["worker", "employer"]

# ── Online status tracking (session-based, no timestamp comparisons) ────────────
# Format: {job_id: {user_id: {"session_id": str, "last_keepalive": timestamp}}}
# This avoids timezone/clock issues by only tracking active sessions, not comparing times
CONTRACT_ROOM_SESSIONS = {}
ONLINE_KEEPALIVE_TIMEOUT_SECONDS = 15  # User offline if no keep-alive for 15 seconds

# ── Message queue system (Option 1: In-memory queue with background processing) ──
# Messages are queued immediately and returned to user for fast UX
# Background worker processes them asynchronously to DB
MESSAGE_QUEUE = queue.Queue(maxsize=10000)  # Max 10k pending messages
MESSAGE_WORKER_RUNNING = False
MESSAGE_WORKER_THREAD = None


def _start_message_worker():
    """Start the background message worker thread (called at app startup)."""
    global MESSAGE_WORKER_RUNNING, MESSAGE_WORKER_THREAD
    if MESSAGE_WORKER_RUNNING:
        return
    MESSAGE_WORKER_RUNNING = True
    MESSAGE_WORKER_THREAD = threading.Thread(target=_message_worker_loop, daemon=True)
    MESSAGE_WORKER_THREAD.start()
    LOG.info("Message queue worker started (in-memory mode)")


def _message_worker_loop():
    """Background worker: Process queued messages and insert into database."""
    while MESSAGE_WORKER_RUNNING:
        try:
            # Get message from queue (5s timeout to check shutdown flag)
            msg_data = MESSAGE_QUEUE.get(timeout=5)
            if msg_data is None:  # Shutdown signal
                break
            
            job_id, user_id, message = msg_data
            
            # Try to insert into database
            max_retries = 3
            retry_count = 0
            inserted = False
            
            while retry_count < max_retries and not inserted:
                try:
                    STORE.execute(
                        f"INSERT INTO {STORE.t('messages')} (job_id, sender_id, message) VALUES (%s,%s,%s)",
                        (job_id, user_id, message)
                    )
                    inserted = True
                    LOG.debug(f"Message inserted for job_id={job_id}, user_id={user_id}")
                except Exception as db_err:
                    retry_count += 1
                    if retry_count < max_retries:
                        LOG.warning(f"Message insert failed (retry {retry_count}/{max_retries}): {db_err}")
                        time.sleep(0.5 * retry_count)  # Exponential backoff
                    else:
                        LOG.error(f"Message insert failed after {max_retries} retries: {db_err}")
            
            MESSAGE_QUEUE.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            LOG.error(f"Message worker error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Settings
# ═══════════════════════════════════════════════════════════════════════════════
def _env(key: str, default: str = "") -> str:
    v = os.getenv(key, default)
    return v.strip() if v else ""

def _env_first(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k, "")
        if v and v.strip():
            return v.strip()
    return default

def _env_bool(key: str, default: bool = False) -> bool:
    return _env(key, "true" if default else "false").lower() in {"1", "true", "yes", "on"}

def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default

def _env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default

def sanitize_prefix(p: str) -> str:
    c = re.sub(r"[^a-zA-Z0-9_]", "", p or "")
    if not c:
        c = "tbm_"
    if not c.endswith("_"):
        c += "_"
    return c.lower()


@dataclass(slots=True)
class Settings:
    secret_key:     str = field(default_factory=lambda: _env("FLASK_SECRET", secrets.token_hex(16)))
    app_base_url:   str = field(default_factory=lambda: _env("APP_BASE_URL", "http://127.0.0.1:5000"))
    server_name:    str = field(default_factory=lambda: _env("SERVER_NAME", ""))
    preferred_url_scheme: str = field(default_factory=lambda: _env("PREFERRED_URL_SCHEME", "http"))

    mysql_host:     str = field(default_factory=lambda: _env_first("MYSQL_HOST", "DB_HOST"))
    mysql_port:     int = field(default_factory=lambda: _env_int("MYSQL_PORT", 3306))
    mysql_database: str = field(default_factory=lambda: _env_first("MYSQL_DATABASE", "DB_NAME"))
    mysql_user:     str = field(default_factory=lambda: _env_first("MYSQL_USER", "DB_USER"))
    mysql_password: str = field(default_factory=lambda: _env_first("MYSQL_PASSWORD", "DB_PASSWORD"))
    mysql_ssl_ca:   str = field(default_factory=lambda: _env("MYSQL_SSL_CA"))
    mysql_ssl_disabled: bool = field(default_factory=lambda: _env_bool("MYSQL_SSL_DISABLED", False))
    mysql_connect_timeout: int = field(default_factory=lambda: _env_int("MYSQL_CONNECT_TIMEOUT", 10))
    mysql_use_pool: bool = field(default_factory=lambda: _env_bool("MYSQL_USE_POOL", True))
    table_prefix:   str = field(default_factory=lambda: _env("DB_TABLE_PREFIX", "tbm_"))

    paystack_secret_key:    str = field(default_factory=lambda: _env("PAYSTACK_SECRET_KEY"))
    paystack_public_key:    str = field(default_factory=lambda: _env("PAYSTACK_PUBLIC_KEY"))
    paystack_webhook_secret: str = field(default_factory=lambda: _env("PAYSTACK_WEBHOOK_SECRET"))
    paystack_callback_url:  str = field(default_factory=lambda: _env("PAYSTACK_CALLBACK_URL"))
    paystack_currency:      str = field(default_factory=lambda: _env("PAYSTACK_CURRENCY", "KES"))

    pesapal_consumer_key:    str = field(default_factory=lambda: _env("PESAPAL_CONSUMER_KEY"))
    pesapal_consumer_secret: str = field(default_factory=lambda: _env("PESAPAL_CONSUMER_SECRET"))
    pesapal_callback_url:    str = field(default_factory=lambda: _env("PESAPAL_CALLBACK_URL"))
    pesapal_ipn_url:         str = field(default_factory=lambda: _env("PESAPAL_IPN_URL"))
    pesapal_currency:        str = field(default_factory=lambda: _env("PESAPAL_CURRENCY", "KES"))
    pesapal_api_url:         str = field(default_factory=lambda: _env("PESAPAL_API_URL", "https://pay.pesapal.com/v3"))

    usd_to_kes: float = field(default_factory=lambda: _env_float("USD_TO_KES", 130.0))

    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_model:   str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-2.5-flash"))
    ai_jobs_per_run: int = field(default_factory=lambda: _env_int("AI_JOBS_PER_RUN", 10))

    groq_api_keys: list = field(default_factory=lambda: [
        _env("GROQ_API_KEY1", ""),
        _env("GROQ_API_KEY2", ""),
        _env("GROQ_API_KEY3", ""),
    ])
    groq_model:    str = field(default_factory=lambda: _env("GROQ_MODEL", "llama-3.1-8b-instant"))
    groq_enabled:  bool = field(default_factory=lambda: bool(_env("GROQ_API_KEY1")))

    github_token:  str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    github_repo:   str = field(default_factory=lambda: _env("GITHUB_REPO"))
    github_branch: str = field(default_factory=lambda: _env("GITHUB_BRANCH", "main"))

    admin_username:      str = field(default_factory=lambda: _env("ADMIN_USERNAME", "admin"))
    admin_password:      str = field(default_factory=lambda: _env("ADMIN_PASSWORD", "change-me"))
    admin_google_emails: str = field(default_factory=lambda: _env("ADMIN_GOOGLE_EMAILS"))

    smtp_host:       str  = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port:       int  = field(default_factory=lambda: _env_int("SMTP_PORT", 587))
    smtp_user:       str  = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password:   str  = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    smtp_from_email: str  = field(default_factory=lambda: _env("SMTP_FROM_EMAIL"))
    smtp_use_tls:    bool = field(default_factory=lambda: _env_bool("SMTP_USE_TLS", True))

    jwt_secret:      str  = field(default_factory=lambda: _env("JWT_SECRET", secrets.token_hex(32)))
    jwt_algorithm:   str  = field(default_factory=lambda: _env("JWT_ALGORITHM", "HS256"))
    jwt_expiry_mins: int  = field(default_factory=lambda: _env_int("JWT_EXPIRY_MINS", 1440))

    @property
    def mysql_enabled(self) -> bool:
        return all([self.mysql_host, self.mysql_database, self.mysql_user])

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_port and self.smtp_user and self.smtp_password)

    @property
    def admin_google_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_google_emails.split(",") if e.strip()}

    def usd_to_kes_cents(self, usd: float) -> int:
        """Convert USD amount to KES in smallest unit (cents for Paystack = kobo/cents ×100)."""
        return int(round(usd * self.usd_to_kes * 100))

    def usd_to_kes_amount(self, usd: float) -> float:
        return round(usd * self.usd_to_kes, 2)


SETTINGS = Settings()


# ═══════════════════════════════════════════════════════════════════════════════
# Groq Client Manager (Rate Limiting & Key Rotation)
# ═══════════════════════════════════════════════════════════════════════════════
class GroqClientManager:
    """Manages 3 Groq API keys with round-robin distribution and rate limiting.
    
    Rate Limits per key:
    - 30 requests/minute
    - 14.4K requests/day (12 per hour)
    - 6K tokens/minute
    - 500K tokens/day
    """
    def __init__(self, api_keys: list[str]):
        self.api_keys = [k for k in api_keys if k]  # Filter out empty keys
        self.current_key_index = 0
        self.usage = {}
        self._reset_usage()
        self._lock = threading.Lock()
    
    def _reset_usage(self):
        """Initialize usage tracking for all keys."""
        now = time.time()
        for i in range(len(self.api_keys)):
            self.usage[i] = {
                'requests_today': 0,
                'tokens_today': 0,
                'requests_this_minute': 0,
                'last_reset_day': now,
                'last_reset_minute': now,
            }
    
    def _check_reset(self):
        """Reset daily/minute counters if needed."""
        now = time.time()
        for i in self.usage:
            # Reset daily counter every 24 hours
            if now - self.usage[i]['last_reset_day'] >= 86400:
                self.usage[i]['requests_today'] = 0
                self.usage[i]['tokens_today'] = 0
                self.usage[i]['last_reset_day'] = now
            # Reset minute counter every 60 seconds
            if now - self.usage[i]['last_reset_minute'] >= 60:
                self.usage[i]['requests_this_minute'] = 0
                self.usage[i]['last_reset_minute'] = now
    
    def get_next_available_key(self) -> tuple[str | None, int]:
        """Get next available API key index with round-robin and rate limit checking.
        
        Returns: (api_key, key_index) or (None, -1) if all keys exhausted
        """
        with self._lock:
            self._check_reset()
            
            # Try up to 3 times (once per key)
            for _ in range(len(self.api_keys)):
                idx = self.current_key_index % len(self.api_keys)
                key = self.api_keys[idx]
                
                # Check if this key is available
                if (self.usage[idx]['requests_today'] < 14 and  # Conservative: max 14/day per key
                    self.usage[idx]['requests_this_minute'] < 2):  # Max 2 per minute per key
                    self.current_key_index = (idx + 1) % len(self.api_keys)
                    return (key, idx)
                
                # Try next key
                self.current_key_index = (idx + 1) % len(self.api_keys)
            
            # All keys exhausted
            return (None, -1)
    
    def mark_request(self, key_index: int, tokens_used: int = 0):
        """Mark a successful request for rate limit tracking."""
        with self._lock:
            if key_index in self.usage:
                self.usage[key_index]['requests_today'] += 1
                self.usage[key_index]['requests_this_minute'] += 1
                self.usage[key_index]['tokens_today'] += tokens_used
    
    def get_status(self) -> dict:
        """Get current usage status for all keys."""
        with self._lock:
            self._check_reset()
            return {
                f'key_{i}': {
                    'requests_today': self.usage[i]['requests_today'],
                    'tokens_today': self.usage[i]['tokens_today'],
                    'requests_this_minute': self.usage[i]['requests_this_minute'],
                }
                for i in range(len(self.api_keys))
            }


GROQ_MANAGER = GroqClientManager(SETTINGS.groq_api_keys) if SETTINGS.groq_enabled else None


# ═══════════════════════════════════════════════════════════════════════════════
# Query Caching Layer (Option D: Database Query Caching)
# ═══════════════════════════════════════════════════════════════════════════════
class QueryCache:
    """Simple in-memory cache for frequently-accessed database queries."""
    def __init__(self):
        self._cache = {}  # Format: {cache_key: {"data": result, "expires_at": timestamp}}
        self._lock = threading.Lock()
    
    def get(self, key: str) -> Any | None:
        """Retrieve cached value if exists and not expired."""
        with self._lock:
            if key not in self._cache:
                return None
            entry = self._cache[key]
            if time.time() > entry["expires_at"]:
                del self._cache[key]
                return None
            return entry["data"]
    
    def set(self, key: str, value: Any, ttl_seconds: int = 60) -> None:
        """Cache a value with TTL (time-to-live)."""
        with self._lock:
            self._cache[key] = {
                "data": value,
                "expires_at": time.time() + ttl_seconds
            }
    
    def invalidate(self, pattern: str = "") -> None:
        """Invalidate cache entries matching pattern (or all if pattern empty)."""
        with self._lock:
            if not pattern:
                self._cache.clear()
            else:
                keys_to_delete = [k for k in self._cache.keys() if pattern in k]
                for k in keys_to_delete:
                    del self._cache[k]
    
    def clear(self) -> None:
        """Clear all cached data."""
        with self._lock:
            self._cache.clear()

QUERY_CACHE = QueryCache()


# ═══════════════════════════════════════════════════════════════════════════════
# MySQL Store
# ═══════════════════════════════════════════════════════════════════════════════
class MySQLStore:
    def __init__(self, settings: Settings) -> None:
        if not settings.mysql_enabled:
            raise RuntimeError("MySQL not configured. Set MYSQL_HOST, MYSQL_DATABASE, MYSQL_USER, MYSQL_PASSWORD.")
        self.settings = settings
        self.prefix = sanitize_prefix(settings.table_prefix)
        self._kw: dict[str, Any] = {
            "host":               settings.mysql_host,
            "port":               settings.mysql_port,
            "database":           settings.mysql_database,
            "user":               settings.mysql_user,
            "password":           settings.mysql_password,
            "charset":            "utf8mb4",
            "autocommit":         True,
            "connection_timeout": settings.mysql_connect_timeout,
            "ssl_disabled":       settings.mysql_ssl_disabled,
        }
        if not settings.mysql_ssl_disabled and settings.mysql_ssl_ca:
            self._kw["ssl_ca"] = settings.mysql_ssl_ca
        self._pool = None
        pk = dict(self._kw)
        pk.update({"pool_name": "tbm_pool", "pool_size": 16, "pool_reset_session": False})
        try:
            self._pool = mysql.connector.pooling.MySQLConnectionPool(**pk)
        except Exception as exc:
            LOG.warning("Connection pool init failed (%s), falling back to per-request connections.", exc)
        self._local = threading.local()

    def t(self, name: str) -> str:
        return f"{self.prefix}{re.sub(r'[^a-zA-Z0-9_]', '', name)}"

    def _get_conn(self):
        """Return the thread-local request-scoped connection if available, else a fresh one."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.ping(reconnect=True, attempts=1, delay=0)
                return conn, False
            except Exception:
                pass
        raw = self._pool.get_connection() if self._pool else mysql.connector.connect(**self._kw)
        return raw, True

    def _connect(self):
        last = None
        for _ in range(2):
            conn = None
            try:
                conn = self._pool.get_connection() if self._pool else mysql.connector.connect(**self._kw)
                conn.ping(reconnect=True, attempts=2, delay=0)
                return conn
            except Exception as exc:
                last = exc
                if conn:
                    try: conn.close()
                    except: pass
                time.sleep(0.05)
        raise last or RuntimeError("Cannot get MySQL connection.")

    def open_request_conn(self) -> None:
        """Open and cache a connection for the lifetime of the current request."""
        try:
            conn = self._pool.get_connection() if self._pool else mysql.connector.connect(**self._kw)
            conn.ping(reconnect=True, attempts=2, delay=0)
            self._local.conn = conn
        except Exception as exc:
            LOG.warning("Could not open request-scoped connection: %s", exc)

    def close_request_conn(self) -> None:
        """Release the request-scoped connection back to the pool."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try: conn.close()
            except: pass
            self._local.conn = None

    @staticmethod
    def _close(conn) -> None:
        try: conn.close()
        except: pass

    def query_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        req_conn = getattr(self._local, "conn", None)
        if req_conn is not None:
            try:
                cur = req_conn.cursor(dictionary=True)
                cur.execute(sql, params)
                return list(cur.fetchall())
            except mysql.connector.Error as exc:
                LOG.warning("query_all on request conn failed: %s", exc)

        last = None
        for _ in range(2):
            conn = self._connect()
            try:
                cur = conn.cursor(dictionary=True)
                cur.execute(sql, params)
                return list(cur.fetchall())
            except mysql.connector.Error as exc:
                last = exc; time.sleep(0.05)
            finally: self._close(conn)
        raise last or RuntimeError("query_all failed.")

    def query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        req_conn = getattr(self._local, "conn", None)
        if req_conn is not None:
            try:
                cur = req_conn.cursor(dictionary=True)
                cur.execute(sql, params)
                return cur.fetchone()
            except mysql.connector.Error as exc:
                LOG.warning("query_one on request conn failed: %s", exc)

        last = None
        for _ in range(2):
            conn = self._connect()
            try:
                cur = conn.cursor(dictionary=True)
                cur.execute(sql, params)
                return cur.fetchone()
            except mysql.connector.Error as exc:
                last = exc; time.sleep(0.05)
            finally: self._close(conn)
        raise last or RuntimeError("query_one failed.")

    def execute(self, sql: str, params: tuple = ()) -> int:
        last = None
        for _ in range(2):
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(sql, params)
                conn.commit()
                # For INSERT, return lastrowid; for UPDATE/DELETE, return rowcount
                if sql.strip().upper().startswith('INSERT'):
                    return int(cur.lastrowid or 0)
                else:
                    return int(cur.rowcount or 0)
            except mysql.connector.Error as exc:
                last = exc
                try: conn.rollback()
                except: pass
                time.sleep(0.05)
            finally: self._close(conn)
        raise last or RuntimeError("execute failed.")

    def ensure_schema(self) -> None:
        users   = self.t("users")
        resets  = self.t("password_resets")
        emp     = self.t("employer_profiles")
        jobs    = self.t("jobs")
        winners = self.t("robot_winners")
        rnames  = self.t("robot_name_pool")
        enames  = self.t("employer_name_pool")
        apps    = self.t("applications")
        pays    = self.t("payments")
        epays   = self.t("employer_payments")
        subs    = self.t("subscriptions")
        notifs  = self.t("notifications")
        ai_runs = self.t("ai_generation_runs")
        p       = self.prefix

        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {users} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    google_sub VARCHAR(191) UNIQUE,
                    password_hash TEXT,
                    role ENUM('worker','employer') NOT NULL,
                    full_name VARCHAR(180),
                    mobile VARCHAR(30),
                    country VARCHAR(80),
                    bio TEXT,
                    profile_pic_url TEXT,
                    skills JSON,
                    specialty VARCHAR(120),
                    connects_balance INT NOT NULL DEFAULT 0,
                    profile_complete TINYINT NOT NULL DEFAULT 0,
                    portfolio_links TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            try:
                cur.execute(f"ALTER TABLE {users} ADD COLUMN portfolio_links TEXT;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {users} ADD COLUMN can_worker TINYINT NOT NULL DEFAULT 0;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {users} ADD COLUMN can_employer TINYINT NOT NULL DEFAULT 0;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {users} ADD COLUMN worker_bonus_granted TINYINT NOT NULL DEFAULT 0;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {users} ADD COLUMN employer_trial_granted TINYINT NOT NULL DEFAULT 0;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {users} ADD COLUMN last_monthly_bonus_date DATETIME;")
            except Exception:
                pass
            # Backfill role capabilities for older rows.
            try:
                cur.execute(
                    f"UPDATE {users} SET can_worker=1 WHERE role='worker' AND (can_worker IS NULL OR can_worker=0)"
                )
            except Exception:
                pass
            try:
                cur.execute(
                    f"UPDATE {users} SET can_employer=1 WHERE role='employer' AND (can_employer IS NULL OR can_employer=0)"
                )
            except Exception:
                pass
            try:
                cur.execute(
                    f"UPDATE {users} SET worker_bonus_granted=1 WHERE role='worker' AND (worker_bonus_granted IS NULL OR worker_bonus_granted=0)"
                )
            except Exception:
                pass
            try:
                cur.execute(
                    f"UPDATE {users} SET employer_trial_granted=1 WHERE role='employer' AND (employer_trial_granted IS NULL OR employer_trial_granted=0)"
                )
            except Exception:
                pass
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {emp} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL UNIQUE,
                    company_name VARCHAR(200),
                    website VARCHAR(300),
                    is_subscribed TINYINT NOT NULL DEFAULT 0,
                    subscription_expires_at DATETIME NULL,
                    CONSTRAINT fk_{p}ep_user FOREIGN KEY (user_id) REFERENCES {users}(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {jobs} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    employer_id BIGINT NULL,
                    title VARCHAR(255) NOT NULL,
                    description TEXT NOT NULL,
                    category VARCHAR(120) NOT NULL,
                    job_type ENUM('hourly','daily','fixed') NOT NULL DEFAULT 'fixed',
                    budget_usd DECIMAL(12,2) NOT NULL DEFAULT 0,
                    duration VARCHAR(80),
                    connects_required INT NOT NULL DEFAULT 10,
                    is_robot TINYINT NOT NULL DEFAULT 0,
                    status ENUM('open','closed','filled') NOT NULL DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_{p}jobs_cat (category),
                    INDEX idx_{p}jobs_robot (is_robot),
                    INDEX idx_{p}jobs_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {winners} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    job_id BIGINT NOT NULL UNIQUE,
                    robot_name VARCHAR(120) NOT NULL,
                    connects_shown INT NOT NULL DEFAULT 500,
                    completed_jobs INT NOT NULL DEFAULT 0,
                    success_rate DECIMAL(5,2) NOT NULL DEFAULT 0,
                    selection_style VARCHAR(24) NOT NULL DEFAULT 'connects',
                    awarded_at DATETIME NULL,
                    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT fk_{p}win_job FOREIGN KEY (job_id) REFERENCES {jobs}(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {rnames} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    display_name VARCHAR(120) NOT NULL UNIQUE,
                    is_active TINYINT NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {enames} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    display_name VARCHAR(120) NOT NULL UNIQUE,
                    is_active TINYINT NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            try:
                cur.execute(f"ALTER TABLE {winners} ADD COLUMN awarded_at DATETIME;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {winners} ADD COLUMN completed_jobs INT NOT NULL DEFAULT 0;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {winners} ADD COLUMN success_rate DECIMAL(5,2) NOT NULL DEFAULT 0;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {winners} ADD COLUMN selection_style VARCHAR(24) NOT NULL DEFAULT 'connects';")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {jobs} ADD COLUMN ai_employer_name VARCHAR(255) NULL;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {jobs} ADD COLUMN slug VARCHAR(255) UNIQUE;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {jobs} ADD INDEX idx_{p}jobs_slug (slug);")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {jobs} ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP;")
            except Exception:
                pass
            # ── Job Templates Table ─────────────────────────────────────────────────────────────
            try:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {p}job_templates (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        title VARCHAR(255) NOT NULL,
                        description TEXT NOT NULL,
                        category VARCHAR(120) NOT NULL,
                        job_type ENUM('hourly','daily','fixed') NOT NULL DEFAULT 'fixed',
                        budget_usd DECIMAL(12,2) NOT NULL DEFAULT 500,
                        duration VARCHAR(80),
                        connects_required INT NOT NULL DEFAULT 20,
                        is_active TINYINT NOT NULL DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_{p}templates_cat (category),
                        INDEX idx_{p}templates_active (is_active)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
            except Exception as e:
                LOG.debug(f"Job templates table creation: {e}")
            # ── Populate slugs for existing jobs without them ──────────────────────────────────────
            try:
                cur.execute(f"SELECT id, title, slug FROM {jobs} WHERE slug IS NULL OR slug='' LIMIT 10000")
                jobs_to_update = cur.fetchall()
                if jobs_to_update:
                    for job in jobs_to_update:
                        job_id = job[0]
                        title = job[1] or "job"
                        slug = _generate_job_slug(title, job_id)
                        try:
                            cur.execute(f"UPDATE {jobs} SET slug=%s WHERE id=%s", (slug, job_id))
                        except Exception as slug_err:
                            LOG.debug(f"Slug update failed for job {job_id}: {slug_err}")
            except Exception as e:
                LOG.debug(f"Job slug migration: {e}")
            # ── Email Notification Preferences ──────────────────────────────────────
            try:
                cur.execute(f"ALTER TABLE {users} ADD COLUMN email_job_notifications BOOLEAN DEFAULT TRUE;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {users} ADD COLUMN email_notification_frequency ENUM('instant','daily','weekly') DEFAULT 'instant';")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {users} ADD COLUMN categories VARCHAR(255);")
            except Exception:
                pass
            # ── Notifications Table - Update Structure ──────────────────────────────────────
            # Recreate notifications table with new schema (or migrate existing)
            try:
                # Check if new columns exist
                cur.execute(f"SHOW COLUMNS FROM {notifs} LIKE 'type'")
                if not cur.fetchone():
                    # Add new columns for enhanced notifications
                    cur.execute(f"ALTER TABLE {notifs} ADD COLUMN type VARCHAR(32) NOT NULL DEFAULT 'notification';")
                    cur.execute(f"ALTER TABLE {notifs} ADD COLUMN title VARCHAR(255);")
                    cur.execute(f"ALTER TABLE {notifs} ADD COLUMN job_id BIGINT;")
                    cur.execute(f"ALTER TABLE {notifs} ADD COLUMN match_score INT DEFAULT 0;")
                    # Create indexes
                    cur.execute(f"ALTER TABLE {notifs} ADD INDEX idx_{p}notif_user_read (user_id, is_read);")
                    cur.execute(f"ALTER TABLE {notifs} ADD INDEX idx_{p}notif_type (type);")
                    cur.execute(f"ALTER TABLE {notifs} ADD INDEX idx_{p}notif_created (created_at);")
            except Exception as e:
                LOG.debug(f"Notification table update: {e}")
            # Seed initial simulated applicant names if table is empty.
            cur.execute(f"SELECT COUNT(*) FROM {rnames}")
            existing_names = int(cur.fetchone()[0] or 0)
            if existing_names == 0:
                for n in _AI_ROBOT_NAMES:
                    try:
                        cur.execute(
                            f"INSERT INTO {rnames} (display_name, is_active) VALUES (%s, 1)",
                            (n,),
                        )
                    except Exception:
                        pass
            
            # Seed initial employer names if table is empty.
            cur.execute(f"SELECT COUNT(*) FROM {enames}")
            existing_enames = int(cur.fetchone()[0] or 0)
            if existing_enames == 0:
                for n in _AI_EMPLOYER_NAMES:
                    try:
                        cur.execute(
                            f"INSERT INTO {enames} (display_name, is_active) VALUES (%s, 1)",
                            (n,),
                        )
                    except Exception:
                        pass

            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {apps} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    job_id BIGINT NOT NULL,
                    connects_spent INT NOT NULL DEFAULT 10,
                    cover_letter TEXT,
                    attachment_links TEXT,
                    status ENUM('pending','accepted','rejected') NOT NULL DEFAULT 'pending',
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_{p}app (user_id, job_id),
                    INDEX idx_{p}app_job (job_id),
                    CONSTRAINT fk_{p}app_user FOREIGN KEY (user_id) REFERENCES {users}(id) ON DELETE CASCADE,
                    CONSTRAINT fk_{p}app_job  FOREIGN KEY (job_id)  REFERENCES {jobs}(id)  ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {pays} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    provider VARCHAR(32) NOT NULL,
                    payment_type VARCHAR(24) NOT NULL DEFAULT 'connects',
                    contract_id BIGINT NULL,
                    amount_usd DECIMAL(12,2) NOT NULL,
                    amount_kes DECIMAL(12,2) NOT NULL,
                    connects_awarded INT NOT NULL DEFAULT 0,
                    status VARCHAR(24) NOT NULL DEFAULT 'pending',
                    reference VARCHAR(191) NOT NULL UNIQUE,
                    provider_reference VARCHAR(191),
                    confirmed_at DATETIME NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_{p}pays_user (user_id),
                    CONSTRAINT fk_{p}pays_user FOREIGN KEY (user_id) REFERENCES {users}(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {epays} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    employer_id BIGINT NOT NULL,
                    worker_user_id BIGINT NOT NULL,
                    job_id BIGINT NOT NULL,
                    amount_kes DECIMAL(12,2) NOT NULL,
                    status ENUM('pending','disbursed') NOT NULL DEFAULT 'pending',
                    admin_note TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {subs} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    employer_id BIGINT NOT NULL,
                    reference VARCHAR(191) NOT NULL UNIQUE,
                    amount_usd DECIMAL(12,2) NOT NULL DEFAULT 5.00,
                    status VARCHAR(24) NOT NULL DEFAULT 'pending',
                    expires_at DATETIME NULL,
                    confirmed_at DATETIME NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            try:
                cur.execute(f"ALTER TABLE {apps} ADD COLUMN attachment_links TEXT;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {pays} ADD COLUMN payment_type VARCHAR(24) NOT NULL DEFAULT 'connects';")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {pays} ADD COLUMN contract_id BIGINT NULL;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {pays} ADD COLUMN confirmed_at DATETIME NULL;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {subs} ADD COLUMN confirmed_at DATETIME NULL;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {subs} ADD COLUMN tier VARCHAR(24);")
            except Exception:
                pass
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {notifs} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    message TEXT NOT NULL,
                    link_url TEXT NULL,
                    is_read TINYINT NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_{p}notif_user (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            try:
                cur.execute(f"ALTER TABLE {notifs} ADD COLUMN link_url TEXT NULL;")
            except Exception:
                pass
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {ai_runs} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    triggered_by_user_id BIGINT NULL,
                    status ENUM('running','success','failed','blocked') NOT NULL DEFAULT 'running',
                    jobs_created INT NOT NULL DEFAULT 0,
                    message VARCHAR(255) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finished_at DATETIME NULL,
                    INDEX idx_{p}ai_runs_created (created_at),
                    INDEX idx_{p}ai_runs_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {resets} (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    token_hash CHAR(64) NOT NULL UNIQUE,
                    expires_at DATETIME NOT NULL,
                    used_at DATETIME NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_{p}resets_user (user_id),
                    INDEX idx_{p}resets_exp (expires_at),
                    CONSTRAINT fk_{p}resets_user FOREIGN KEY (user_id) REFERENCES {users}(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {p}messages (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    job_id BIGINT NOT NULL,
                    sender_id BIGINT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_{p}msg_job (job_id),
                    CONSTRAINT fk_{p}msg_job FOREIGN KEY (job_id) REFERENCES {jobs}(id) ON DELETE CASCADE,
                    CONSTRAINT fk_{p}msg_snd FOREIGN KEY (sender_id) REFERENCES {users}(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {p}audit_log (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT,
                    action VARCHAR(64) NOT NULL,
                    entity_type VARCHAR(64) NOT NULL,
                    entity_id BIGINT,
                    old_value TEXT,
                    new_value TEXT,
                    ip_address VARCHAR(45),
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_{p}audit_user (user_id),
                    INDEX idx_{p}audit_action (action),
                    INDEX idx_{p}audit_entity (entity_type, entity_id),
                    INDEX idx_{p}audit_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            # ── Escrow/Trust System Tables ──────────────────────────────────────
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {p}contracts (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    job_id BIGINT NOT NULL,
                    employer_id BIGINT NOT NULL,
                    freelancer_id BIGINT NOT NULL,
                    scope TEXT NOT NULL,
                    price_usd DECIMAL(12,2) NOT NULL,
                    deliverables TEXT NOT NULL,
                    deadline DATETIME NOT NULL,
                    status ENUM('pending','offer_accepted','offer_rejected','funded','in_progress','submitted','under_review','approved','completed','disputed','refunded') NOT NULL DEFAULT 'pending',
                    escrow_amount_usd DECIMAL(12,2) NOT NULL,
                    offer_note TEXT NULL,
                    review_days INT NOT NULL DEFAULT 7,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    offer_sent_at DATETIME NULL,
                    offer_responded_at DATETIME NULL,
                    offer_accepted_at DATETIME NULL,
                    offer_rejected_at DATETIME NULL,
                    funded_at DATETIME NULL,
                    funding_reference VARCHAR(191) NULL,
                    submitted_at DATETIME NULL,
                    review_window_starts DATETIME NULL,
                    review_deadline DATETIME NULL,
                    completed_at DATETIME NULL,
                    INDEX idx_{p}contract_job (job_id),
                    INDEX idx_{p}contract_employer (employer_id),
                    INDEX idx_{p}contract_freelancer (freelancer_id),
                    INDEX idx_{p}contract_status (status),
                    CONSTRAINT fk_{p}contract_job FOREIGN KEY (job_id) REFERENCES {jobs}(id) ON DELETE CASCADE,
                    CONSTRAINT fk_{p}contract_emp FOREIGN KEY (employer_id) REFERENCES {users}(id) ON DELETE CASCADE,
                    CONSTRAINT fk_{p}contract_freel FOREIGN KEY (freelancer_id) REFERENCES {users}(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            try:
                cur.execute(
                    f"ALTER TABLE {p}contracts MODIFY COLUMN status "
                    "ENUM('pending','offer_accepted','offer_rejected','funded','in_progress','submitted','under_review','approved','completed','disputed','refunded') "
                    "NOT NULL DEFAULT 'pending'"
                )
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {p}contracts ADD COLUMN offer_note TEXT NULL;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {p}contracts ADD COLUMN offer_sent_at DATETIME NULL;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {p}contracts ADD COLUMN offer_responded_at DATETIME NULL;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {p}contracts ADD COLUMN offer_accepted_at DATETIME NULL;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {p}contracts ADD COLUMN offer_rejected_at DATETIME NULL;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {p}contracts ADD COLUMN funding_reference VARCHAR(191) NULL;")
            except Exception:
                pass
            try:
                cur.execute(f"ALTER TABLE {p}contracts ADD COLUMN completion_reward_granted TINYINT NOT NULL DEFAULT 0;")
            except Exception:
                pass
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {p}work_submissions (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    contract_id BIGINT NOT NULL,
                    deliverables_links JSON NOT NULL,
                    submitted_message TEXT,
                    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_{p}work_contract (contract_id),
                    CONSTRAINT fk_{p}work_contract FOREIGN KEY (contract_id) REFERENCES {p}contracts(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {p}disputes (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    contract_id BIGINT NOT NULL,
                    opened_by_id BIGINT NOT NULL,
                    status ENUM('open','evidence_submitted','resolved','appealed') NOT NULL DEFAULT 'open',
                    claimant_evidence JSON,
                    respondent_evidence JSON,
                    admin_decision VARCHAR(64),
                    decision_percentage INT,
                    decided_by_id BIGINT,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    decided_at DATETIME NULL,
                    INDEX idx_{p}disp_contract (contract_id),
                    INDEX idx_{p}disp_opened_by (opened_by_id),
                    INDEX idx_{p}disp_status (status),
                    CONSTRAINT fk_{p}disp_contract FOREIGN KEY (contract_id) REFERENCES {p}contracts(id) ON DELETE CASCADE,
                    CONSTRAINT fk_{p}disp_opened FOREIGN KEY (opened_by_id) REFERENCES {users}(id) ON DELETE CASCADE,
                    CONSTRAINT fk_{p}disp_decided FOREIGN KEY (decided_by_id) REFERENCES {users}(id) ON DELETE SET NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {p}contract_events (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    contract_id BIGINT NOT NULL,
                    event_type VARCHAR(64) NOT NULL,
                    triggered_by_id BIGINT,
                    details JSON,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_{p}evt_contract (contract_id),
                    INDEX idx_{p}evt_type (event_type),
                    CONSTRAINT fk_{p}evt_contract FOREIGN KEY (contract_id) REFERENCES {p}contracts(id) ON DELETE CASCADE,
                    CONSTRAINT fk_{p}evt_triggered FOREIGN KEY (triggered_by_id) REFERENCES {users}(id) ON DELETE SET NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {p}job_reports (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    job_id BIGINT NOT NULL,
                    reporter_id BIGINT NOT NULL,
                    reason VARCHAR(255) NOT NULL,
                    details TEXT,
                    status ENUM('pending','reviewed','dismissed','action_taken') NOT NULL DEFAULT 'pending',
                    admin_notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at DATETIME NULL,
                    reviewed_by_id BIGINT,
                    INDEX idx_{p}rep_job (job_id),
                    INDEX idx_{p}rep_reporter (reporter_id),
                    INDEX idx_{p}rep_status (status),
                    CONSTRAINT fk_{p}rep_job FOREIGN KEY (job_id) REFERENCES {jobs}(id) ON DELETE CASCADE,
                    CONSTRAINT fk_{p}rep_reporter FOREIGN KEY (reporter_id) REFERENCES {users}(id) ON DELETE CASCADE,
                    CONSTRAINT fk_{p}rep_reviewed FOREIGN KEY (reviewed_by_id) REFERENCES {users}(id) ON DELETE SET NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            conn.commit()

            # ─ Add hourly job support columns ─────────────────────────────────────
            # These columns enable proper hourly rate tracking and hour verification
            try:
                cur.execute(f"ALTER TABLE {jobs} ADD COLUMN hourly_rate DECIMAL(8,2) NULL DEFAULT NULL;")
                conn.commit()
            except Exception:
                conn.rollback()
            
            try:
                cur.execute(f"ALTER TABLE {jobs} ADD COLUMN max_hours INT NULL DEFAULT NULL;")
                conn.commit()
            except Exception:
                conn.rollback()
            
            try:
                cur.execute(f"ALTER TABLE {p}work_submissions ADD COLUMN hours_worked DECIMAL(10,2) NULL DEFAULT NULL;")
                conn.commit()
            except Exception:
                conn.rollback()
            
            try:
                cur.execute(f"ALTER TABLE {p}work_submissions ADD COLUMN hours_verified_by_employer DECIMAL(10,2) NULL DEFAULT NULL;")
                conn.commit()
            except Exception:
                conn.rollback()
            
            try:
                cur.execute(f"ALTER TABLE {p}work_submissions ADD COLUMN time_entries JSON NULL DEFAULT NULL;")
                conn.commit()
            except Exception:
                conn.rollback()
            
            try:
                cur.execute(f"ALTER TABLE {p}contracts ADD COLUMN hours_logged DECIMAL(10,2) NULL DEFAULT NULL;")
                conn.commit()
            except Exception:
                conn.rollback()

            # ─ Add daily job support columns ──────────────────────────────────────
            # These columns enable proper daily rate tracking and day verification
            try:
                cur.execute(f"ALTER TABLE {jobs} ADD COLUMN daily_rate DECIMAL(8,2) NULL DEFAULT NULL;")
                conn.commit()
            except Exception:
                conn.rollback()
            
            try:
                cur.execute(f"ALTER TABLE {jobs} ADD COLUMN max_days INT NULL DEFAULT NULL;")
                conn.commit()
            except Exception:
                conn.rollback()
            
            try:
                cur.execute(f"ALTER TABLE {p}work_submissions ADD COLUMN days_worked DECIMAL(10,2) NULL DEFAULT NULL;")
                conn.commit()
            except Exception:
                conn.rollback()
            
            try:
                cur.execute(f"ALTER TABLE {p}work_submissions ADD COLUMN days_verified_by_employer DECIMAL(10,2) NULL DEFAULT NULL;")
                conn.commit()
            except Exception:
                conn.rollback()
            
            try:
                cur.execute(f"ALTER TABLE {p}contracts ADD COLUMN days_logged DECIMAL(10,2) NULL DEFAULT NULL;")
                conn.commit()
            except Exception:
                conn.rollback()

            # Add indexes that may be missing on existing deployments
            for idx_sql in [
                f"ALTER TABLE {users} ADD INDEX idx_{p}users_role (role)",
                f"ALTER TABLE {users} ADD INDEX idx_{p}users_created (created_at)",
                f"ALTER TABLE {apps} ADD INDEX idx_{p}app_user (user_id)",
                f"ALTER TABLE {apps} ADD INDEX idx_{p}app_status (status)",
                f"ALTER TABLE {jobs} ADD INDEX idx_{p}jobs_employer (employer_id)",
                f"ALTER TABLE {jobs} ADD INDEX idx_{p}jobs_created (created_at)",
                f"ALTER TABLE {epays} ADD INDEX idx_{p}epays_employer (employer_id)",
                f"ALTER TABLE {epays} ADD INDEX idx_{p}epays_status (status)",
                f"ALTER TABLE {notifs} ADD INDEX idx_{p}notif_read (is_read)",
            ]:
                try:
                    cur.execute(idx_sql)
                    conn.commit()
                except Exception:
                    conn.rollback()

            LOG.info("TechBid schema verified.")
        finally:
            self._close(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# Payment Clients
# ═══════════════════════════════════════════════════════════════════════════════
class PaystackClient:
    _session = requests.Session()
    BASE = "https://api.paystack.co"

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.s.paystack_secret_key}", "Content-Type": "application/json"}

    def initialize(self, *, email: str, amount_cents: int, reference: str,
                   callback_url: str, currency: str, metadata: dict | None = None) -> tuple[int, dict]:
        payload: dict = {"email": email, "amount": amount_cents, "reference": reference,
                          "callback_url": callback_url, "currency": currency}
        if metadata:
            payload["metadata"] = metadata
        try:
            r = self._session.post(f"{self.BASE}/transaction/initialize", headers=self._h(), json=payload, timeout=20)
            return r.status_code, r.json() if r.content else {}
        except Exception as exc:
            return 599, {"message": str(exc)}

    def verify(self, reference: str) -> tuple[int, dict]:
        try:
            r = self._session.get(f"{self.BASE}/transaction/verify/{reference}", headers=self._h(), timeout=20)
            return r.status_code, r.json() if r.content else {}
        except Exception as exc:
            return 599, {"message": str(exc)}

    def valid_sig(self, raw: bytes, sig: str | None) -> bool:
        secret = self.s.paystack_webhook_secret or self.s.paystack_secret_key or ""
        if not secret:
            return False
        expected = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()
        return hmac.compare_digest(expected, sig or "")


class PesapalClient:
    _session = requests.Session()

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.base = (settings.pesapal_api_url or "https://pay.pesapal.com/v3").rstrip("/")

    @staticmethod
    def _body(r: requests.Response) -> dict:
        if not r.content:
            return {}
        try:
            d = r.json()
            return d if isinstance(d, dict) else {"data": d}
        except Exception:
            return {"message": r.text}

    def get_token(self) -> tuple[str | None, dict | None]:
        try:
            r = self._session.post(f"{self.base}/api/Auth/RequestToken",
                json={"consumer_key": self.s.pesapal_consumer_key, "consumer_secret": self.s.pesapal_consumer_secret},
                headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=30)
            body = self._body(r)
        except Exception as exc:
            return None, {"message": str(exc)}
        token = body.get("token") if isinstance(body, dict) else None
        return (str(token), None) if token else (None, body)

    def register_ipn(self, token: str, ipn_url: str) -> tuple[str | None, dict | None]:
        headers = {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {token}"}
        try:
            r = self._session.post(f"{self.base}/api/URLSetup/RegisterIPN",
                json={"url": ipn_url, "ipn_notification_type": "GET"}, headers=headers, timeout=30)
            body = self._body(r)
        except Exception as exc:
            return None, {"message": str(exc)}
        ipn_id = body.get("ipn_id") if isinstance(body, dict) else None
        return (str(ipn_id), None) if ipn_id else (None, body)

    def submit_order(self, token: str, ipn_id: str, reference: str, email: str,
                     amount: float, callback_url: str, currency: str, phone: str) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {token}"}
        payload = {
            "id": reference, "currency": currency, "amount": float(amount),
            "description": "TechBid connects purchase",
            "callback_url": callback_url, "notification_id": ipn_id,
            "billing_address": {"email_address": email or "user@techbid.io",
                                 "phone_number": phone or "254700000000",
                                 "first_name": "TechBid", "last_name": "User"},
        }
        try:
            r = self._session.post(f"{self.base}/api/Transactions/SubmitOrderRequest",
                json=payload, headers=headers, timeout=30)
            return r.status_code, self._body(r)
        except Exception as exc:
            return 599, {"message": str(exc)}

    def get_tx_status(self, token: str, order_tracking_id: str) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {token}"}
        try:
            r = self._session.get(f"{self.base}/api/Transactions/GetTransactionStatus",
                params={"orderTrackingId": order_tracking_id}, headers=headers, timeout=30)
            return r.status_code, self._body(r)
        except Exception as exc:
            return 599, {"message": str(exc)}


# ═══════════════════════════════════════════════════════════════════════════════
# Rate Limiter
# ═══════════════════════════════════════════════════════════════════════════════
class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and q[0] < now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


# ═══════════════════════════════════════════════════════════════════════════════
# SMTP Helper
# ═══════════════════════════════════════════════════════════════════════════════
_email_q: queue.Queue = queue.Queue()

def _email_worker() -> None:
    while True:
        item = _email_q.get()
        if item is None:
            break
        to_addr, subject, html_body = item
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = SETTINGS.smtp_from_email or SETTINGS.smtp_user
            msg["To"] = to_addr
            msg.attach(MIMEText(html_body, "html"))
            with smtplib.SMTP(SETTINGS.smtp_host, SETTINGS.smtp_port, timeout=15) as smtp:
                if SETTINGS.smtp_use_tls:
                    smtp.starttls()
                smtp.login(SETTINGS.smtp_user, SETTINGS.smtp_password)
                smtp.sendmail(msg["From"], [to_addr], msg.as_string())
        except Exception as exc:
            LOG.warning("Email send failed to %s: %s", to_addr, exc)
        finally:
            _email_q.task_done()

def send_email(to: str, subject: str, html: str) -> None:
    if SETTINGS.smtp_enabled and to:
        _email_q.put((to, subject, html))

def _calculate_connects_required(budget_usd: float) -> int:
    """
    Calculate connects required for a job based on budget (tiered pricing).
    Applies to both real jobs and AI-generated jobs.
    """
    if budget_usd <= 50:
        return 4
    elif budget_usd <= 150:
        return 6
    elif budget_usd <= 500:
        return 10
    elif budget_usd <= 1000:
        return 15
    else:
        return 20


def _grant_monthly_bonus(user_id: int) -> bool:
    """
    Grant 30 connects if 30+ days since last bonus.
    Returns True if bonus was granted, False otherwise.
    """
    import datetime
    user = STORE.query_one(f"SELECT last_monthly_bonus_date, connects_balance FROM {STORE.t('users')} WHERE id=%s", (user_id,))
    if not user:
        return False
    
    last_bonus = user.get("last_monthly_bonus_date")
    now = datetime.datetime.utcnow()
    
    # Grant bonus if never received or 30+ days ago
    if last_bonus is None or (now - last_bonus).total_seconds() >= (30 * 86400):
        STORE.execute(
            f"UPDATE {STORE.t('users')} SET connects_balance=connects_balance+%s, last_monthly_bonus_date=%s WHERE id=%s",
            (MONTHLY_BONUS_CONNECTS, now, user_id),
        )
        audit_log(user_id, "MONTHLY_BONUS_GRANTED", "user", user_id, old_value=str(user["connects_balance"]), 
                  new_value=str(user["connects_balance"] + MONTHLY_BONUS_CONNECTS))
        return True
    return False


def _initial_connects_for_role(role: str) -> int:
    return WORKER_SIGNUP_CONNECTS if role == "worker" else 0

def _build_welcome_email_html(role: str, name: str | None = None, expiry_date: str | None = None) -> str:
    safe_name = (name or "there").strip() or "there"
    if role == "worker":
        return (
            f"<h2>Welcome, {safe_name}!</h2>"
            "<p>Your worker account is ready.</p>"
            f"<p>You have received <b>{WORKER_SIGNUP_CONNECTS} free connects</b> to start applying for jobs.</p>"
        )
    employer_msg = f"<p>You have a <b>free {EMPLOYER_TRIAL_DAYS}-day trial</b> to post jobs and hire talent."
    if expiry_date:
        employer_msg += f" Trial expires on <b>{expiry_date}</b>.</p>"
    else:
        employer_msg += "</p>"
    return (
        f"<h2>Welcome, {safe_name}!</h2>"
        "<p>Your employer account is ready.</p>"
        + employer_msg
    )

def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def _flatten_nested_description(desc) -> str:
    """Flatten nested dict/list descriptions into a clean string format."""
    if isinstance(desc, str):
        return desc
    
    if isinstance(desc, dict):
        parts = []
        section_order = [
            "Overview", "Project Scope", "Responsibilities", "Requirements", 
            "Key Features", "Nice to Have", "Tech Stack", "Deliverables"
        ]
        
        # Helper to check if a value has content
        def has_content(value):
            if isinstance(value, list):
                return any(str(item).strip() for item in value)
            elif isinstance(value, str):
                return bool(value.strip())
            return bool(value)
        
        # Process sections in order
        for section in section_order:
            if section in desc:
                value = desc[section]
                # Only add section if it has content
                if not has_content(value):
                    continue
                
                parts.append(f"{section}:")
                
                if isinstance(value, list):
                    for item in value:
                        item_str = str(item).strip()
                        if item_str:
                            # Ensure each item starts with a dash
                            if not item_str.startswith(("-", "*")):
                                parts.append(f"- {item_str}")
                            else:
                                parts.append(item_str)
                elif isinstance(value, str):
                    value = value.strip()
                    if value:
                        if not value.startswith(("-", "*")):
                            parts.append(f"- {value}")
                        else:
                            parts.append(value)
        
        # Add any remaining sections not in order (only if they have content)
        for key, value in desc.items():
            if key not in section_order and has_content(value):
                parts.append(f"{key}:")
                if isinstance(value, list):
                    for item in value:
                        item_str = str(item).strip()
                        if item_str and not item_str.startswith(("-", "*")):
                            parts.append(f"- {item_str}")
                        elif item_str:
                            parts.append(item_str)
                else:
                    value_str = str(value).strip()
                    if value_str and not value_str.startswith(("-", "*")):
                        parts.append(f"- {value_str}")
                    elif value_str:
                        parts.append(value_str)
        
        return "\n\n".join(parts) if parts else ""
    
    return str(desc)


def _normalize_ai_job_description(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return (
            "Overview:\n"
            "- Short project summary.\n\n"
            "Responsibilities:\n"
            "- Build and deliver the requested solution.\n\n"
            "Requirements:\n"
            "- Relevant technical experience."
        )
    
    # Check if this is just headers with no actual content (empty sections)
    # Pattern: section headers followed by nothing or only whitespace
    lines = body.split('\n')
    has_actual_content = False
    for line in lines:
        stripped = line.strip()
        # Check if line is actual content (not just a section header, not empty, not just dash/asterisk)
        if stripped and not stripped.endswith(':') and stripped not in ['-', '*', '']:
            has_actual_content = True
            break
    
    # If only section headers but no content, use fallback
    if not has_actual_content:
        lines = [ln.strip() for ln in re.split(r"[.\n]+", body) if ln.strip() and not ln.strip().endswith(':')]
        bullets = lines[:9]
        return (
            "Overview:\n"
            f"- {bullets[0] if bullets else 'Project details provided on request.'}\n\n"
            "Project Scope:\n"
            f"- {bullets[1] if len(bullets) > 1 else 'Build and deliver the requested solution.'}\n\n"
            "Responsibilities:\n"
            f"- {bullets[2] if len(bullets) > 2 else 'Deliver a production-ready implementation.'}\n"
            f"- {bullets[3] if len(bullets) > 3 else 'Communicate progress and blockers clearly.'}\n\n"
            "Requirements:\n"
            f"- {bullets[4] if len(bullets) > 4 else 'Strong relevant technical skills.'}\n"
            f"- {bullets[5] if len(bullets) > 5 else 'Experience with similar projects.'}\n\n"
            "Nice to Have:\n"
            f"- {bullets[6] if len(bullets) > 6 else 'Portfolio or past examples.'}"
        )[:4000]
    
    # If already structured with sections/bullets, preserve it (up to 4000 chars for richer detail)
    if any(mark in body for mark in ("\n- ", "\n* ", "Overview:", "Project Scope:", "Key Features:", 
                                      "Responsibilities:", "Requirements:", "Tech Stack:", 
                                      "What to Include:", "Nice to Have:", "Important:", "Goal:")):
        return body[:4000]
    # Fallback: parse plain text and structure it
    lines = [ln.strip() for ln in re.split(r"[.\n]+", body) if ln.strip()]
    bullets = lines[:9]
    return (
        "Overview:\n"
        f"- {bullets[0] if bullets else 'Project details provided on request.'}\n\n"
        "Project Scope:\n"
        f"- {bullets[1] if len(bullets) > 1 else 'Build and deliver the requested solution.'}\n\n"
        "Responsibilities:\n"
        f"- {bullets[2] if len(bullets) > 2 else 'Deliver a production-ready implementation.'}\n"
        f"- {bullets[3] if len(bullets) > 3 else 'Communicate progress and blockers clearly.'}\n\n"
        "Requirements:\n"
        f"- {bullets[4] if len(bullets) > 4 else 'Strong relevant technical skills.'}\n"
        f"- {bullets[5] if len(bullets) > 5 else 'Experience with similar projects.'}\n\n"
        "Nice to Have:\n"
        f"- {bullets[6] if len(bullets) > 6 else 'Portfolio or past examples.'}"
    )[:4000]


# ═══════════════════════════════════════════════════════════════════════════════
# GitHub Profile Picture Upload
# ═══════════════════════════════════════════════════════════════════════════════
def upload_profile_pic(file_bytes: bytes, filename: str) -> str | None:
    """Upload profile picture to GitHub repo. Returns public URL or None."""
    if not SETTINGS.github_token or not SETTINGS.github_repo:
        # Fallback: encode as base64 and store directly
        import base64
        b64 = base64.b64encode(file_bytes).decode('utf-8')
        return f"data:image/jpeg;base64,{b64}"
    try:
        from github import Github
        gh = Github(SETTINGS.github_token)
        repo = gh.get_repo(SETTINGS.github_repo)
        path = f"profile_pics/{filename}"
        try:
            existing = repo.get_contents(path, ref=SETTINGS.github_branch)
            repo.update_file(path, f"Update {filename}", file_bytes, existing.sha, branch=SETTINGS.github_branch)
        except Exception:
            repo.create_file(path, f"Upload {filename}", file_bytes, branch=SETTINGS.github_branch)
        return f"/cdn/profile_pics/{filename}"
    except Exception as exc:
        LOG.warning("GitHub upload failed: %s, falling back to base64", exc)
        # Fallback: encode as base64 and store directly
        import base64
        b64 = base64.b64encode(file_bytes).decode('utf-8')
        return f"data:image/jpeg;base64,{b64}"


def rewrite_pic_url(url: str | None) -> str | None:
    """Convert any raw.githubusercontent.com URLs to local proxy path."""
    if url and "raw.githubusercontent.com" in url:
        fname = url.split("/profile_pics/")[-1] if "/profile_pics/" in url else url.split("/")[-1]
        return f"/cdn/profile_pics/{fname}"
    return url

# ═══════════════════════════════════════════════════════════════════════════════
# AI Job Generator (background thread)
# ═══════════════════════════════════════════════════════════════════════════════
_AI_ROBOT_NAMES = [
    "Aisha M.", "Kenji S.", "Lucas R.", "Priya N.", "Amara O.", "Diego F.",
    "Elif K.", "Tomás V.", "Yuki H.", "Fatima A.", "Carlos B.", "Nadia P.",
    "Ravi T.", "Zara L.", "Ivan C.", "Mei X.", "Omar D.", "Sofia G.",
    "Kwame J.", "Elena W.", "Arjun M.", "Leila Z.", "Martín R.", "Nia B.",
]

_AI_EMPLOYER_NAMES = [
    "TechVision Solutions", "Digital Innovations Ltd", "Creative Studios Hub", 
    "Cloud Enterprise Co.", "Data Insights Group", "NextGen Development",
    "Smart Solutions Inc", "Digital Growth Partners", "Innovation Labs",
    "Tech Forward Systems", "Web Dynamics LLC", "Future Tech Group",
    "Quantum Digital", "Apex Solutions", "Global Tech Partners",
    "Premier Development Co.", "Elite Digital Services", "Connected Systems",
    "Modern Tech Solutions", "Progressive Digital Inc", "Strategic Tech Group",
    "Intelligent Systems Corp", "Innovative Tech Labs", "Dynamic Solutions Team",
    "Professional Tech Services", "Advanced Solutions Group", "Tech Excellence Ltd",
]

def _get_active_robot_names(store: MySQLStore) -> list[str]:
    """Load active robot names from database. Falls back to hardcoded list if DB empty."""
    try:
        names = store.query_all(
            f"SELECT display_name FROM {store.t('robot_name_pool')} WHERE is_active=1 ORDER BY display_name"
        )
        if names:
            return [n.get("display_name") for n in names]
    except Exception as exc:
        LOG.warning("Failed to load robot names from DB: %s", exc)
    return _AI_ROBOT_NAMES  # Fallback to hardcoded

def _get_active_employer_names(store: MySQLStore) -> list[str]:
    """Load active employer names from database. Falls back to hardcoded list if DB empty."""
    try:
        names = store.query_all(
            f"SELECT display_name FROM {store.t('employer_name_pool')} WHERE is_active=1 ORDER BY display_name"
        )
        if names:
            return [n.get("display_name") for n in names]
    except Exception as exc:
        LOG.warning("Failed to load employer names from DB: %s", exc)
    return _AI_EMPLOYER_NAMES  # Fallback to hardcoded

def _generate_ai_jobs(store: MySQLStore) -> int:
    """Generate AI-powered job postings using hybrid Groq + Gemini approach.
    
    Strategy:
    - Use Groq Llama 3.1 8B as primary (faster, no rate limits)
    - Fall back to Gemini if Groq unavailable
    - Groq has 3 keys for load distribution
    """
    # Try Groq first if available
    if SETTINGS.groq_enabled and GROQ_MANAGER and GROQ_MANAGER.api_keys:
        result = _generate_ai_jobs_groq(store)
        if result > 0:
            return result
    
    # Fall back to Gemini
    if SETTINGS.gemini_api_key:
        return _generate_ai_jobs_gemini(store)
    
    LOG.warning("AI job generator: No API keys configured (Groq or Gemini)")
    return 0


def _generate_ai_jobs_groq(store: MySQLStore) -> int:
    """Generate AI jobs using Groq API (faster, more reliable for bulk generation)."""
    try:
        from groq import Groq
    except ImportError:
        LOG.warning("Groq library not installed. Install with: pip install groq")
        return 0
    
    # Get next available Groq API key
    api_key, key_index = GROQ_MANAGER.get_next_available_key()
    if not api_key:
        LOG.warning("Groq rate limit reached for all keys. Waiting for reset.")
        return 0
    
    try:
        client = Groq(api_key=api_key)
        model = SETTINGS.groq_model
        
        # FREE TIER SAFETY CHECK: Limit total AI jobs created in the last 24h.
        daily_limit = max(1, min(SETTINGS.ai_jobs_per_run, 12))  # Groq: more aggressive (12/day vs 10)
        today_count = store.query_one(
            f"SELECT COUNT(*) as cnt FROM {store.t('jobs')} WHERE is_robot=1 AND created_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)",
            ()
        )["cnt"]
        
        if today_count >= daily_limit:
            LOG.warning("⚠️ Daily generation limit reached (%d/day). Skipping to protect rate limits.", daily_limit)
            return 0
        
        category = random.choice(JOB_CATEGORIES)
        count = 0
        
        LOG.info("Groq AI job generation started for category: %s (Key: %d, %d jobs today)", 
                 category, key_index + 1, today_count)
        
        prompt = (
            f"Generate exactly 1 job posting for the '{category}' category.\n\n"
            "RETURN VALID JSON ONLY:\n"
            "[{\"title\": \"...\", \"description\": \"...\", \"job_type\": \"hourly\", \"budget_usd\": 1500, \"duration\": \"2 weeks\", \"robot_winner_name\": \"Maria S.\"}]\n\n"
            "KEY RULES:\n"
            "1. 'description' is ONLY the job posting text - DO NOT include budget, job_type, duration, or robot_winner_name in it\n"
            "2. description MUST be 200+ words with multiple sections separated by blank lines\n"
            "3. description MUST include: Overview, Project Scope, Responsibilities, Requirements, Key Features, Nice to Have\n"
            "4. Each section header ends with colon, bullet points start with dash\n"
            "5. budget_usd, job_type, duration, robot_winner_name are SEPARATE JSON fields, NOT in description\n\n"
            "DESCRIPTION TEXT EXAMPLE (what goes in 'description' field ONLY):\n"
            "Overview:\n- We need a skilled React developer for an e-commerce platform\n- Must handle 10k+ products efficiently\n\n"
            "Project Scope:\n- Build responsive frontend components\n- Integrate with REST API\n- Add payment processing\n\n"
            "Responsibilities:\n- Code feature development\n- Unit testing\n- Performance optimization\n\n"
            "Requirements:\n- 5+ years React experience\n- Strong JavaScript/TypeScript\n- Experience with e-commerce systems\n\n"
            "Key Features:\n- Real-time updates\n- Mobile responsive\n- SEO optimized\n\n"
            "Nice to Have:\n- Portfolio links\n- Previous e-commerce work\n\n"
            "SEPARATE JSON FIELDS (these go in the JSON, NOT in description):\n"
            "- 'title': catchy job title like 'Senior React Developer for E-commerce Platform'\n"
            "- 'job_type': 'fixed' or 'hourly' or 'daily'\n"
            "- 'budget_usd': number between 200-5000 (choose based on type and scope)\n"
            "- 'duration': '2 weeks' or '1 month' or similar\n"
            "- 'robot_winner_name': random name like 'Alex P.' or 'Jordan M.'\n\n"
            "OUTPUT ONLY VALID JSON ARRAY."
        )
        
        # Call Groq API
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a professional freelance job posting generator. Generate realistic, detailed job postings."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=1000,
        )
        
        raw = response.choices[0].message.content.strip() if response.choices else ""
        tokens_used = response.usage.completion_tokens + response.usage.prompt_tokens if response.usage else 0
        
        if not raw:
            LOG.warning("Empty response from Groq API")
            return 0
        
        # Clean up JSON
        raw = raw.strip("```json").strip("```").strip()
        
        try:
            items = json.loads(raw)
            if not isinstance(items, list):
                items = [items]
        except json.JSONDecodeError as e:
            LOG.error("Invalid JSON from Groq: %s... (first 200 chars)", raw[:200])
            return 0
        
        # Process each job
        for item in items:
            try:
                title = str(item.get("title", "")).strip()[:255]
                description = _normalize_ai_job_description(_flatten_nested_description(item.get("description", "")))
                job_type = str(item.get("job_type", "fixed")).lower().strip()
                raw_budget = item.get("budget_usd", 0)
                duration = str(item.get("duration", "1 month")).strip()[:80]
                suggested_name = str(item.get("robot_winner_name", "")).strip()[:80]
                robot_name = suggested_name or _pick_robot_name(store)
                
                # Validation
                if not title or not description:
                    continue
                if job_type not in JOB_TYPES:
                    job_type = "fixed"
                budget_ranges = {
                    "hourly": (80.0, 1500.0),
                    "daily": (120.0, 2500.0),
                    "fixed": (200.0, 5000.0),
                }
                min_budget, max_budget = budget_ranges.get(job_type, (200.0, 5000.0))
                try:
                    base_budget = float(raw_budget)
                except (TypeError, ValueError):
                    base_budget = 0.0
                # Keep AI suggestions but apply randomized market-like variation
                if base_budget < min_budget or base_budget > max_budget:
                    base_budget = random.uniform(min_budget, max_budget)
                jitter = random.uniform(0.72, 1.33)
                budget = round(min(max_budget, max(min_budget, base_budget * jitter)), 2)
                
                connects_req = random.randint(10, 40)
                winner_conn, winner_completed_jobs, winner_success_rate, winner_style = _build_robot_winner_profile()
                ai_employer_name = _pick_employer_name(store)
                
                # Insert job with randomized AI employer name
                job_id = store.execute(
                    f"INSERT INTO {store.t('jobs')} "
                    "(employer_id, title, description, category, job_type, budget_usd, duration, connects_required, is_robot, ai_employer_name, status) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s,'open')",
                    (None, title, description, category, job_type, budget, duration, connects_req, ai_employer_name),
                )
                
                if not job_id:
                    LOG.warning("Failed to insert job from Groq")
                    continue
                
                # Generate and set slug for the job
                slug = _generate_job_slug(title, job_id)
                store.execute(f"UPDATE {store.t('jobs')} SET slug=%s WHERE id=%s", (slug, job_id))
                
                # Insert robot winner record
                awarded_at = datetime.utcnow() + timedelta(hours=random.randint(24, 36))
                store.execute(
                    f"INSERT INTO {store.t('robot_winners')} "
                    "(job_id, robot_name, connects_shown, completed_jobs, success_rate, selection_style, awarded_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (job_id, robot_name, winner_conn, winner_completed_jobs, winner_success_rate, winner_style, awarded_at),
                )
                
                # Notify matching workers asynchronously
                budget_display = f"${budget:,.2f}" if budget else "Negotiable"
                posted_time = "Just now"
                try:
                    notify_thread = Thread(
                        target=_notify_matching_workers,
                        args=(job_id, title, description, category, budget_display, posted_time),
                        daemon=True
                    )
                    notify_thread.start()
                except Exception as notify_err:
                    LOG.warning(f"Failed to start notification thread for Groq AI job: {notify_err}")
                
                count += 1
                LOG.info("✓ Created Groq AI job #%d: '%s' (ID: %d, Key: %d)", 
                        count, title[:50], job_id, key_index + 1)
                
            except (ValueError, KeyError, TypeError) as item_err:
                LOG.debug("Skipping malformed job item from Groq: %s", item_err)
                continue
        
        # Track usage for rate limiting
        GROQ_MANAGER.mark_request(key_index, tokens_used)
        
        if count > 0:
            LOG.info("✓ Groq AI jobs created successfully: %d jobs (Key: %d)", count, key_index + 1)
        else:
            LOG.warning("No jobs created from Groq response")
        return count
        
    except Exception as exc:
        LOG.error("✗ Groq AI job generation failed: %s", exc)
        return 0


def _generate_ai_jobs_gemini(store: MySQLStore) -> int:
    """Generate AI-powered job postings using Gemini (fallback or primary)."""
    if not SETTINGS.gemini_api_key:
        LOG.warning("AI job generator: GEMINI_API_KEY not set.")
        return 0
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=SETTINGS.gemini_api_key)
        model = genai.GenerativeModel(SETTINGS.gemini_model)
        
        # FREE TIER SAFETY CHECK: Limit total AI jobs created in the last 24h.
        daily_limit = max(1, min(SETTINGS.ai_jobs_per_run, 10))
        today_count = store.query_one(
            f"SELECT COUNT(*) as cnt FROM {store.t('jobs')} WHERE is_robot=1 AND created_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)",
            ()
        )["cnt"]
        
        if today_count >= daily_limit:
            LOG.warning("⚠️ Daily generation limit reached (%d/day). Skipping to protect free tier quota.", daily_limit)
            return 0
        
        category = random.choice(JOB_CATEGORIES)
        count = 0
        
        LOG.info("Gemini AI job generation started for category: %s (%d jobs today)", category, today_count)
        
        prompt = (
            f"Generate exactly 1 professional freelance job posting for the '{category}' category.\n\n"
            "Return ONLY a JSON array (no markdown, no explanation). Each job object must have:\n"
            '{"title": "...", "description": "...", "job_type": "hourly|daily|fixed", "budget_usd": <number>, "duration": "...", "robot_winner_name": "FirstName L."}\n\n'
            "- description: Create a detailed, well-structured job posting (200-350 words, max 400). Use these sections:\n"
            "  Overview:\n"
            "  - Clear 1-2 sentence project summary\n"
            "  Project Scope:\n"
            "  - 2-3 key deliverables\n"
            "  Responsibilities:\n"
            "  - 3-4 main tasks or areas\n"
            "  Requirements:\n"
            "  - 3-4 required skills/experience\n"
            "  Key Features (if applicable):\n"
            "  - 2-3 What should be included\n"
            "  Nice to Have:\n"
            "  - 1-2 bonus requirements\n"
            "- Use bullet points with clear, specific wording\n"
            "- Ensure description is professional and compelling\n"
            "- budget_usd: Between 200 and 5000\n"
            "- job_type: One of hourly, daily, fixed\n"
            "- duration: Realistic timeline like '3 weeks', '2 months', '1 month'\n"
            "- robot_winner_name: Like 'Aisha M.' or 'Kenji S.'\n\n"
            "Output valid JSON ONLY. No extra text."
        )
        
        # Simple direct API call
        resp = model.generate_content(prompt)
        raw = resp.text.strip() if resp.text else ""
        
        if not raw:
            LOG.warning("Empty response from Gemini API")
            return 0
        
        # Clean up JSON
        raw = raw.strip("```json").strip("```").strip()
        
        try:
            items = json.loads(raw)
            if not isinstance(items, list):
                items = [items]
        except json.JSONDecodeError as e:
            LOG.error("Invalid JSON from Gemini: %s... (first 200 chars)", raw[:200])
            return 0
        
        # Process each job
        for item in items:
            try:
                title = str(item.get("title", "")).strip()[:255]
                description = _normalize_ai_job_description(str(item.get("description", "")))
                job_type = str(item.get("job_type", "fixed")).lower().strip()
                raw_budget = item.get("budget_usd", 0)
                duration = str(item.get("duration", "1 month")).strip()[:80]
                suggested_name = str(item.get("robot_winner_name", "")).strip()[:80]
                robot_name = suggested_name or _pick_robot_name(store)
                
                # Validation
                if not title or not description:
                    continue
                if job_type not in JOB_TYPES:
                    job_type = "fixed"
                budget_ranges = {
                    "hourly": (80.0, 1500.0),
                    "daily": (120.0, 2500.0),
                    "fixed": (200.0, 5000.0),
                }
                min_budget, max_budget = budget_ranges.get(job_type, (200.0, 5000.0))
                try:
                    base_budget = float(raw_budget)
                except (TypeError, ValueError):
                    base_budget = 0.0
                if base_budget < min_budget or base_budget > max_budget:
                    base_budget = random.uniform(min_budget, max_budget)
                jitter = random.uniform(0.72, 1.33)
                budget = round(min(max_budget, max(min_budget, base_budget * jitter)), 2)
                
                connects_req = random.randint(10, 40)
                winner_conn, winner_completed_jobs, winner_success_rate, winner_style = _build_robot_winner_profile()
                ai_employer_name = _pick_employer_name(store)
                
                # Insert job with randomized AI employer name
                job_id = store.execute(
                    f"INSERT INTO {store.t('jobs')} "
                    "(employer_id, title, description, category, job_type, budget_usd, duration, connects_required, is_robot, ai_employer_name, status) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s,'open')",
                    (None, title, description, category, job_type, budget, duration, connects_req, ai_employer_name),
                )
                
                if not job_id:
                    LOG.warning("Failed to insert job")
                    continue
                
                # Generate and set slug for the job (includes job_id for uniqueness)
                slug = _generate_job_slug(title, job_id)
                store.execute(f"UPDATE {store.t('jobs')} SET slug=%s WHERE id=%s", (slug, job_id))
                
                # Insert robot winner record
                awarded_at = datetime.utcnow() + timedelta(hours=random.randint(24, 36))
                store.execute(
                    f"INSERT INTO {store.t('robot_winners')} "
                    "(job_id, robot_name, connects_shown, completed_jobs, success_rate, selection_style, awarded_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (job_id, robot_name, winner_conn, winner_completed_jobs, winner_success_rate, winner_style, awarded_at),
                )
                
                # Notify matching workers asynchronously
                budget_display = f"${budget:,.2f}" if budget else "Negotiable"
                posted_time = "Just now"
                try:
                    # Run in background to not block AI generation
                    notify_thread = Thread(
                        target=_notify_matching_workers,
                        args=(job_id, title, description, category, budget_display, posted_time),
                        daemon=True
                    )
                    notify_thread.start()
                except Exception as notify_err:
                    LOG.warning(f"Failed to start notification thread for AI job: {notify_err}")
                
                count += 1
                LOG.info("✓ Created Gemini AI job #%d: '%s' (ID: %d)", count, title[:50], job_id)
                
            except (ValueError, KeyError, TypeError) as item_err:
                LOG.debug("Skipping malformed job item: %s", item_err)
                continue
        
        if count > 0:
            LOG.info("✓ Gemini AI jobs created successfully: %d jobs", count)
        else:
            LOG.warning("No jobs created from Gemini response")
        return count
        
    except Exception as exc:
        LOG.error("✗ Gemini AI job generation failed: %s", exc)
        return 0


# ═══════════════════════════════════════════════════════════════════════════════
# Skill Matching & Notification Functions

import re
import time
from threading import Thread
from queue import Queue

# Email notification queue
_email_queue = Queue()
_MATCH_THRESHOLD = 60  # 60% minimum match score to notify

def _extract_keywords_from_text(text: str) -> list:
    """Extract keywords from job title/description."""
    if not text:
        return []
    
    # Convert to lowercase and remove special chars
    text = text.lower()
    # Remove punctuation and split
    words = re.findall(r'\b[a-z0-9]+\b', text)
    
    # Filter common words
    stopwords = {
        'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
        'of', 'with', 'from', 'is', 'are', 'be', 'was', 'were', 'have', 'has',
        'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may', 'might',
        'must', 'can', 'you', 'we', 'they', 'it', 'that', 'this', 'as', 'if'
    }
    
    # Return unique keywords (len > 2 and not stopwords)
    keywords = set(w for w in words if len(w) > 2 and w not in stopwords)
    return list(keywords)

def _calculate_match_score(job_title: str, job_desc: str, job_category: str,
                          user_skills: str, user_categories: str) -> int:
    """Calculate match score between job and worker skills.
    
    Returns: 0-100 match percentage
    """
    try:
        if not user_skills or not job_title:
            return 0
        
        # Level 1: Category matching (50 points if category matches)
        score = 0
        if user_categories and job_category:
            user_cats = [c.strip().lower() for c in user_categories.split(',')]
            job_cat = job_category.strip().lower()
            if job_cat in user_cats:
                score += 50
        
        # Level 2: Keyword extraction and matching
        user_keywords = set(_extract_keywords_from_text(user_skills.lower()))
        job_keywords = set(_extract_keywords_from_text(job_title + ' ' + (job_desc or '')))
        
        if job_keywords and user_keywords:
            # Calculate keyword overlap percentage
            overlap = len(user_keywords & job_keywords)
            total = len(job_keywords)
            keyword_match = (overlap / total * 100) if total > 0 else 0
            
            # Add keyword score (capped at 50 points if not already scored from category)
            if score == 0:
                score = int(keyword_match * 0.5)
            else:
                score += int(keyword_match * 0.25)
        
        return min(score, 100)
    
    except Exception as e:
        LOG.error(f"Match score calculation failed: {e}")
        return 0

def _create_notification(user_id: int, notif_type: str, title: str, 
                        message: str, job_id: int = None, match_score: int = 0) -> bool:
    """Create a notification record."""
    try:
        p = STORE.prefix
        STORE.execute(
            f"""INSERT INTO {p}notifications 
               (user_id, type, title, message, job_id, match_score, is_read, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, FALSE, NOW())""",
            (user_id, notif_type, title, message, job_id, match_score)
        )
        LOG.debug(f"✓ Notification created for user {user_id}: {title}")
        return True
    except Exception as e:
        LOG.error(f"✗ Failed to create notification: {e}")
        return False

def _send_email_notification(user_id: int, user_email: str, job_title: str, 
                           job_category: str, budget: str, job_link: str, 
                           match_score: int, posted_time: str):
    """Queue email notification to be sent asynchronously."""
    try:
        email_data = {
            'to': user_email,
            'subject': f'New Job Match: {job_title}',
            'job_title': job_title,
            'job_category': job_category,
            'budget': budget,
            'job_link': job_link,
            'match_score': match_score,
            'posted_time': posted_time
        }
        _email_queue.put(email_data)
        LOG.debug(f"✓ Email queued for {user_email}")
    except Exception as e:
        LOG.error(f"✗ Failed to queue email: {e}")

def _notify_matching_workers(job_id: int, job_title: str, job_desc: str,
                            job_category: str, budget: str, posted_time: str):
    """Find workers matching job skills and notify them.
    
    This runs asynchronously in background thread to not block job creation.
    """
    try:
        p = STORE.prefix
        
        # Get all workers with email notifications enabled
        workers = STORE.query_all(
            f"""SELECT id, email, skills, categories, email_job_notifications,
                     email_notification_frequency
                FROM {p}users 
                WHERE role = 'worker' AND email_job_notifications = TRUE
                LIMIT 1000"""
        )
        
        if not workers:
            LOG.debug("No workers to notify for new job")
            return
        
        notified_count = 0
        for worker in workers:
            try:
                worker_id = worker.get('id')
                worker_email = worker.get('email')
                worker_skills = worker.get('skills', '')
                worker_categories = worker.get('categories', '')
                freq = worker.get('email_notification_frequency', 'instant')
                
                # Calculate match score
                match_score = _calculate_match_score(
                    job_title=job_title,
                    job_desc=job_desc or '',
                    job_category=job_category,
                    user_skills=worker_skills,
                    user_categories=worker_categories
                )
                
                # Only notify if score meets threshold
                if match_score >= _MATCH_THRESHOLD:
                    # Create notification
                    _create_notification(
                        user_id=worker_id,
                        notif_type='NEW_JOB_MATCH',
                        title=f'New job: {job_title}',
                        message=f'A new {job_category} job matches your skills ({match_score}% match)',
                        job_id=job_id,
                        match_score=match_score
                    )
                    
                    # Send email if frequency is 'instant'
                    if freq == 'instant' and worker_email:
                        _send_email_notification(
                            user_id=worker_id,
                            user_email=worker_email,
                            job_title=job_title,
                            job_category=job_category,
                            budget=budget,
                            job_link=f"/worker/jobs/{job_id}",
                            match_score=match_score,
                            posted_time=posted_time
                        )
                    
                    notified_count += 1
            
            except Exception as w_err:
                LOG.warning(f"Failed to notify worker {worker.get('id')}: {w_err}")
                continue
        
        LOG.info(f"✓ Notified {notified_count} workers for job {job_id}")
    
    except Exception as e:
        LOG.error(f"✗ Worker notification batch failed: {e}")

def _email_worker_thread():
    """Background thread to send queued emails."""
    while True:
        try:
            if _email_queue.empty():
                time.sleep(2)
                continue
            
            email_data = _email_queue.get(timeout=5)
            
            try:
                # Build HTML email
                html_body = f"""
                <html><body style="font-family: Arial, sans-serif;">
                    <h2>✨ New Job Match Found!</h2>
                    <p>Hi there,</p>
                    <p>A new job was posted that matches your skills!</p>
                    
                    <div style="background: #f5f5f5; padding: 15px; border-radius: 5px;">
                        <p><strong>📋 Job:</strong> {email_data.get('job_title')}</p>
                        <p><strong>🏢 Category:</strong> {email_data.get('job_category')}</p>
                        <p><strong>💰 Budget:</strong> {email_data.get('budget')}</p>
                        <p><strong>⏰ Posted:</strong> {email_data.get('posted_time')}</p>
                        <p><strong>✅ Your Match Score:</strong> {email_data.get('match_score')}%</p>
                    </div>
                    
                    <p><a href="{email_data.get('job_link')}" 
                          style="background: #007bff; color: white; padding: 10px 20px; 
                                 text-decoration: none; border-radius: 5px;">
                        Apply Now
                    </a></p>
                    
                    <p>---</p>
                    <p style="font-size: 12px; color: #666;">
                        You're receiving this because you enabled job notifications.
                        <a href="/worker/settings">Manage preferences</a>
                    </p>
                </body></html>
                """
                
                # Send email
                import smtplib
                from email.mime.text import MIMEText
                from email.mime.multipart import MIMEMultipart
                
                msg = MIMEMultipart('alternative')
                msg['Subject'] = email_data.get('subject')
                msg['From'] = _env('SMTP_FROM_EMAIL', 'noreply@freelancing.com')
                msg['To'] = email_data.get('to')
                
                msg.attach(MIMEText(html_body, 'html'))
                
                with smtplib.SMTP(_env('SMTP_HOST'), _env_int('SMTP_PORT', 587)) as server:
                    server.starttls()
                    server.login(_env('SMTP_USER'), _env('SMTP_PASSWORD'))
                    server.send_message(msg)
                
                LOG.debug(f"✓ Email sent to {email_data.get('to')}")
            
            except Exception as send_err:
                LOG.error(f"✗ Email send failed: {send_err}")
            
            _email_queue.task_done()
        
        except Exception as e:
            LOG.warning(f"Email worker error: {e}")
            time.sleep(2)


# ═══════════════════════════════════════════════════════════════════════════════
# Flask App
# ═══════════════════════════════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = SETTINGS.secret_key
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB upload limit
if SETTINGS.server_name:
    app.config["SERVER_NAME"] = SETTINGS.server_name
app.config["PREFERRED_URL_SCHEME"] = SETTINGS.preferred_url_scheme

# Enable gzip compression for all responses (60-70% size reduction)
Compress(app)

# Initialize Flask-SocketIO for real-time messaging
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

STORE = MySQLStore(SETTINGS)
PAYSTACK = PaystackClient(SETTINGS)
PESAPAL  = PesapalClient(SETTINGS)
LIMITER  = RateLimiter()

# ── AI Generation Rate Limiter (Gemini Free Tier Protection) ──────────────────
AI_LAST_MANUAL_TRIGGER_AT: datetime | None = None
AI_TRIGGER_LOCK = threading.Lock()
AI_MIN_INTERVAL_SECONDS = 60   # Require >= 1 minute between manual trigger requests

# ── Email Worker Thread (for async email notifications) ──────────────────────────
_email_worker_running = False
def _start_email_worker():
    """Start background email worker thread."""
    global _email_worker_running
    if not _email_worker_running:
        _email_worker_running = True
        email_thread = Thread(target=_email_worker_thread, daemon=True)
        email_thread.start()
        LOG.info("✓ Email notification worker started")

# ── Before request initialization ──────────────────────────────────────────────
@app.before_request
def before_request():
    """Initialize message queue worker on first request."""
    global MESSAGE_WORKER_RUNNING
    if not MESSAGE_WORKER_RUNNING:
        _start_message_worker()


# ── Request lifecycle — one connection per request ──────────────────────────────
@app.before_request
def _before():
    # Start email worker on first request
    _start_email_worker()
    
    if session.get("user_id"):
        STORE.open_request_conn()

@app.teardown_request
def _teardown(exc):
    STORE.close_request_conn()


# ── Context processors ─────────────────────────────────────────────────────────
@app.context_processor
def inject_globals():
    uid = session.get("user_id")
    notif_count = session.get("notif_count", 0)
    return {
        "current_user_id": uid,
        "current_role": session.get("role"),
        "can_worker": bool(session.get("can_worker")),
        "can_employer": bool(session.get("can_employer")),
        "current_name": session.get("full_name"),
        "current_connects": session.get("connects", 0),
        "notif_count": notif_count,
        "categories": JOB_CATEGORIES,
        "packages": CONNECTS_PACKAGES,
        "now": datetime.utcnow()
    }

@app.template_filter("time_ago")
def time_ago(value):
    if not value:
        return "just now"
    now = datetime.utcnow()
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    months = days // 30
    return f"{months} month{'s' if months != 1 else ''} ago"

def _ramp_simulated_count(elapsed_minutes: int, start_minutes: int, end_minutes: int, target: int) -> int:
    if target <= 0 or elapsed_minutes < start_minutes:
        return 0
    span = max(1, end_minutes - start_minutes)
    pct = min(1.0, (elapsed_minutes - start_minutes) / span)
    return min(target, int(round(target * pct)))


def _get_max_simulated_applicants(category: str) -> int:
    """Get max simulated applicants based on job category difficulty and specialization."""
    
    # TOUGHEST (max 5): High specialization, years of expertise required
    toughest = {
        "Game Development",
        "Machine Learning / AI",
        "Cybersecurity",
        "DevOps & Cloud",
        "Database Administration",
        "Blockchain & Web3",
    }
    if category in toughest:
        return 5
    
    # SIMPLER (max 20): Low barrier to entry, broad talent pool
    simpler = {
        "Content Writing & Copywriting",
        "Animation & Motion Graphics",
        "Virtual Assistance",
        "Customer Support",
        "Project Management",
        "SEO & Digital Marketing",
        "Photo Editing",
        "Email Marketing",
        "Sales & Lead Generation",
        "Social Media Management",
    }
    if category in simpler:
        return 20
    
    # MEDIUM (max 13): Professional skills, moderate barrier to entry
    # (Web Development, Mobile Development, UI/UX Design, Data Science & Analytics,
    #  Technical Writing, Quality Assurance & Testing, E-commerce & Shopify,
    #  WordPress Development, Business Analysis, Graphic Design, Video Editing,
    #  Translation & Localization, Accounting & Finance, Legal Services)
    return 13


def _simulate_robot_activity(job_id: int, created_at, awarded_at, category: str = "") -> tuple[int, int, int]:
    """Return generated (proposals, invites, interviews) for robot jobs."""
    if not created_at:
        return (0, 0, 0)

    now = datetime.utcnow()
    elapsed_minutes = max(0, int((now - created_at).total_seconds() // 60))
    total_minutes = ROBOT_ACTIVITY_WINDOW_MINUTES
    if awarded_at:
        total_minutes = max(
            ROBOT_ACTIVITY_WINDOW_MINUTES,
            int(max(0, (awarded_at - created_at).total_seconds()) // 60),
        )
    end_minutes = max(ROBOT_PROPOSAL_DELAY_MINUTES + 1, total_minutes)

    import random

    seed = int(hashlib.sha256(f"robot-activity-{job_id}".encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    
    # Get max applicants based on category difficulty
    max_applicants = _get_max_simulated_applicants(category)
    min_applicants = max(2, max_applicants // 2)  # Min is at least 2 or half of max
    proposal_target = rng.randint(min_applicants, max_applicants)
    invite_target = rng.randint(2, min(10, max(2, proposal_target // 3)))
    interview_target = rng.randint(1, min(5, max(1, invite_target)))

    invites_start = min(end_minutes - 1, max(60, int(end_minutes * 0.35)))
    interviews_start = min(end_minutes - 1, max(invites_start + 60, int(end_minutes * 0.68)))

    proposals = _ramp_simulated_count(
        elapsed_minutes, ROBOT_PROPOSAL_DELAY_MINUTES, end_minutes, proposal_target
    )
    invites = _ramp_simulated_count(elapsed_minutes, invites_start, end_minutes, invite_target)
    interviews = _ramp_simulated_count(elapsed_minutes, interviews_start, end_minutes, interview_target)
    return (proposals, invites, interviews)


def _apply_robot_activity(job: dict[str, Any], real_application_count: int) -> None:
    real_count = max(0, int(real_application_count or 0))
    job["real_applicants"] = real_count

    if job.get("is_robot"):
        proposals, invites, interviews = _simulate_robot_activity(
            int(job.get("id") or 0),
            job.get("created_at"),
            job.get("awarded_at"),
            job.get("category", ""),
        )
        job["simulated_applicants"] = proposals
        job["simulated_invites"] = invites
        job["simulated_interviews"] = interviews
        job["display_applicants"] = real_count + proposals
        return

    job["simulated_applicants"] = 0
    job["simulated_invites"] = 0
    job["simulated_interviews"] = 0
    job["display_applicants"] = real_count

def _build_robot_winner_profile() -> tuple[int, int, float, str]:
    """
    Returns:
    - connects_shown
    - completed_jobs
    - success_rate
    - selection_style: 'connects' or 'rating'
    80% of the time robot winners are connects-heavy (higher bid than rating profile).
    """
    import random

    connects_heavy = random.random() < 0.8
    if connects_heavy:
        connects_shown = random.randint(700, 1800)
        completed_jobs = random.randint(1, 5)
        success_rate = round(random.uniform(74.0, 90.0), 1)
        return (connects_shown, completed_jobs, success_rate, "connects")

    connects_shown = random.randint(220, 900)
    completed_jobs = random.randint(1, 5)
    success_rate = round(random.uniform(91.0, 99.3), 1)
    return (connects_shown, completed_jobs, success_rate, "rating")

def _get_simulated_name_pool(store: MySQLStore) -> list[str]:
    rows = store.query_all(
        f"SELECT display_name FROM {store.t('robot_name_pool')} WHERE is_active=1 ORDER BY id ASC",
        (),
    )
    names = [str(r.get("display_name", "")).strip() for r in rows if r.get("display_name")]
    return names or list(_AI_ROBOT_NAMES)

def _get_employer_name_pool(store: MySQLStore) -> list[str]:
    rows = store.query_all(
        f"SELECT display_name FROM {store.t('employer_name_pool')} WHERE is_active=1 ORDER BY id ASC",
        (),
    )
    names = [str(r.get("display_name", "")).strip() for r in rows if r.get("display_name")]
    return names or list(_AI_EMPLOYER_NAMES)

def _pick_robot_name(store: MySQLStore) -> str:
    import random
    names = _get_simulated_name_pool(store)
    return random.choice(names)

def _pick_employer_name(store: MySQLStore) -> str:
    import random
    names = _get_employer_name_pool(store)
    return random.choice(names)

def _calculate_payment_amount(contract: dict, job: dict) -> tuple[float, str]:
    """
    Calculate payment amount based on job type (hourly, daily, or fixed).
    Returns (amount_usd, description_text)
    For hourly: amount = hourly_rate × hours_logged
    For daily: amount = daily_rate × days_logged
    For fixed: amount = contract price_usd
    """
    if job and job.get("job_type") == "hourly":
        hourly_rate = job.get("hourly_rate", 0)
        hours_logged = contract.get("hours_logged", 0)
        if hourly_rate and hours_logged:
            amount = float(hourly_rate) * float(hours_logged)
            return amount, f"Hourly ({hours_logged}h × ${hourly_rate}/h)"
    
    elif job and job.get("job_type") == "daily":
        daily_rate = job.get("daily_rate", 0)
        days_logged = contract.get("days_logged", 0)
        if daily_rate and days_logged:
            amount = float(daily_rate) * float(days_logged)
            return amount, f"Daily ({days_logged}d × ${daily_rate}/d)"
    
    # Default to full contract price (fixed-price or fallback)
    amount = float(contract.get("price_usd") or 0)
    return amount, "Fixed-price contract"

def _interview_winner_names(store: MySQLStore, job_id: int, count: int) -> list[str]:
    if count <= 0:
        return []
    names = _get_simulated_name_pool(store)
    seed = int(hashlib.sha256(f"robot-job-{job_id}".encode()).hexdigest()[:8], 16)
    import random
    rng = random.Random(seed)
    if len(names) >= count:
        return rng.sample(names, count)
    # recycle deterministically when pool smaller than requested count
    winners: list[str] = []
    while len(winners) < count:
        winners.append(names[len(winners) % len(names)])
    return winners

def _grant_worker_mode(user_id: int) -> bool:
    user = STORE.query_one(
        f"SELECT can_worker, worker_bonus_granted, connects_balance FROM {STORE.t('users')} WHERE id=%s",
        (user_id,),
    )
    if not user:
        return False
    if user.get("can_worker") and user.get("worker_bonus_granted"):
        return False
    bonus = WORKER_SIGNUP_CONNECTS if not user.get("worker_bonus_granted") else 0
    STORE.execute(
        f"UPDATE {STORE.t('users')} SET can_worker=1, worker_bonus_granted=1, connects_balance=connects_balance+%s WHERE id=%s",
        (bonus, user_id),
    )
    return bonus > 0

def _grant_employer_mode(user_id: int) -> bool:
    user = STORE.query_one(
        f"SELECT can_employer, employer_trial_granted FROM {STORE.t('users')} WHERE id=%s",
        (user_id,),
    )
    if not user:
        return False
    if not user.get("can_employer"):
        STORE.execute(f"UPDATE {STORE.t('users')} SET can_employer=1 WHERE id=%s", (user_id,))
    existing = STORE.query_one(f"SELECT id FROM {STORE.t('employer_profiles')} WHERE user_id=%s", (user_id,))
    if not existing:
        STORE.execute(
            f"INSERT INTO {STORE.t('employer_profiles')} (user_id, is_subscribed, subscription_expires_at) VALUES (%s,%s,%s)",
            (user_id, 0, None),
        )
    granted_trial = False
    if not user.get("employer_trial_granted"):
        trial_expires = datetime.utcnow() + timedelta(days=EMPLOYER_TRIAL_DAYS)
        STORE.execute(
            f"UPDATE {STORE.t('employer_profiles')} SET is_subscribed=1, subscription_expires_at=%s WHERE user_id=%s",
            (trial_expires, user_id),
        )
        STORE.execute(f"UPDATE {STORE.t('users')} SET employer_trial_granted=1 WHERE id=%s", (user_id,))
        granted_trial = True
    return granted_trial


def _refresh_session(user_id: int) -> None:
    u = STORE.query_one(
        f"SELECT u.*, COALESCE(n.cnt,0) as unread_notifs "
        f"FROM {STORE.t('users')} u "
        f"LEFT JOIN (SELECT user_id, COUNT(*) as cnt FROM {STORE.t('notifications')} "
        f"WHERE is_read=0 GROUP BY user_id) n ON n.user_id=u.id "
        f"WHERE u.id=%s", (user_id,))
    if u:
        session["user_id"]    = u["id"]
        session["role"]       = u["role"]
        session["full_name"]  = u["full_name"] or u["email"]
        session["connects"]   = int(u["connects_balance"])
        session["profile_ok"] = bool(u["profile_complete"])
        session["email"]      = u["email"]
        session["can_worker"] = bool(u.get("can_worker"))
        session["can_employer"] = bool(u.get("can_employer"))


# ── Decorators ─────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def worker_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if session.get("role") != "worker":
            flash("Access restricted to workers.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


def employer_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if session.get("role") != "employer":
            flash("Access restricted to employers.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Admin access required.", "error")
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


def profile_required(f):
    """Redirect workers/employers to complete their profile first."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("profile_ok"):
            flash("Please complete your profile first.", "info")
            return redirect(url_for("complete_profile"))
        return f(*args, **kwargs)
    return wrapper


def csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def verify_csrf() -> bool:
    expected = session.get("csrf_token")
    provided = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    return bool(expected and provided and str(expected) == str(provided))


app.jinja_env.globals["csrf_token"] = csrf_token


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "").strip()
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "unknown")


def push_notif(user_id: int, message: str, link_url: str | None = None) -> None:
    try:
        STORE.execute(
            f"INSERT INTO {STORE.t('notifications')} (user_id, message, link_url) VALUES (%s,%s,%s)",
            (user_id, message, link_url),
        )
    except Exception:
        pass


def _create_session_for_user(user_id: int, job_id: int) -> str:
    """Create a new keep-alive session for user in contract room (no timestamp comparison)."""
    session_id = secrets.token_hex(16)
    if job_id not in CONTRACT_ROOM_SESSIONS:
        CONTRACT_ROOM_SESSIONS[job_id] = {}
    CONTRACT_ROOM_SESSIONS[job_id][user_id] = {
        "session_id": session_id,
        "last_keepalive": datetime.utcnow()
    }
    return session_id


def _keepalive_session(user_id: int, job_id: int) -> bool:
    """Update session keep-alive time. Returns True if session exists and updated."""
    if job_id not in CONTRACT_ROOM_SESSIONS:
        return False
    if user_id not in CONTRACT_ROOM_SESSIONS[job_id]:
        return False
    
    CONTRACT_ROOM_SESSIONS[job_id][user_id]["last_keepalive"] = datetime.utcnow()
    return True


def _is_user_online(user_id: int, job_id: int) -> bool:
    """Check if user has an active session (keep-alive within timeout window)."""
    try:
        if job_id not in CONTRACT_ROOM_SESSIONS:
            return False
        if user_id not in CONTRACT_ROOM_SESSIONS[job_id]:
            return False
        
        session_data = CONTRACT_ROOM_SESSIONS[job_id][user_id]
        last_keepalive = session_data.get("last_keepalive")
        
        # Ensure last_keepalive is a valid number (Unix timestamp)
        if last_keepalive is None or not isinstance(last_keepalive, (int, float)):
            return False
        
        # Check if keep-alive is recent (within timeout window)
        # last_keepalive is a Unix timestamp (float), so use time.time() for comparison
        seconds_since_keepalive = time.time() - last_keepalive
        return seconds_since_keepalive < ONLINE_KEEPALIVE_TIMEOUT_SECONDS
    except Exception as e:
        LOG.debug(f"Error checking online status: {e}")
        return False


def _get_user_online_status(user_id: int, job_id: int) -> dict[str, Any]:
    """Get user's online status in a specific contract room (session-based, no timestamps to compare)."""
    is_online = _is_user_online(user_id, job_id)
    
    # Clean up expired sessions
    try:
        if job_id in CONTRACT_ROOM_SESSIONS:
            current_time = time.time()
            expired = []
            for uid, data in CONTRACT_ROOM_SESSIONS[job_id].items():
                last_keepalive = data.get("last_keepalive")
                # Only mark as expired if it has a valid timestamp and it's old
                if isinstance(last_keepalive, (int, float)) and (current_time - last_keepalive) > ONLINE_KEEPALIVE_TIMEOUT_SECONDS:
                    expired.append(uid)
            for uid in expired:
                del CONTRACT_ROOM_SESSIONS[job_id][uid]
    except Exception as e:
        LOG.debug(f"Error cleaning up sessions: {e}")
    
    return {
        "is_online": is_online,
        "last_seen": None,  # No timestamp comparison needed anymore
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Cached Query Helpers (Option D: Database Query Caching)
# ═══════════════════════════════════════════════════════════════════════════════
def get_job_counts() -> dict[str, int]:
    """Get cached job counts (total, open, robot)."""
    cache_key = "job_counts_all"
    cached = QUERY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    
    total = (STORE.query_one(f"SELECT COUNT(*) as c FROM {STORE.t('jobs')}") or {}).get("c", 0)
    open_jobs = (STORE.query_one(f"SELECT COUNT(*) as c FROM {STORE.t('jobs')} WHERE status='open'") or {}).get("c", 0)
    robot_jobs = (STORE.query_one(f"SELECT COUNT(*) as c FROM {STORE.t('jobs')} WHERE is_robot=1") or {}).get("c", 0)
    
    result = {"total": total, "open": open_jobs, "robot": robot_jobs}
    QUERY_CACHE.set(cache_key, result, ttl_seconds=300)  # Cache for 5 minutes
    return result


def get_user_profile(user_id: int) -> dict[str, Any] | None:
    """Get cached user profile by ID."""
    cache_key = f"user_profile_{user_id}"
    cached = QUERY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    
    user = STORE.query_one(f"SELECT * FROM {STORE.t('users')} WHERE id=%s", (user_id,))
    if user:
        QUERY_CACHE.set(cache_key, user, ttl_seconds=600)  # Cache for 10 minutes
    return user


def get_job_by_id(job_id: int) -> dict[str, Any] | None:
    """Get cached job by ID."""
    cache_key = f"job_{job_id}"
    cached = QUERY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    
    job = STORE.query_one(f"SELECT * FROM {STORE.t('jobs')} WHERE id=%s", (job_id,))
    if job:
        QUERY_CACHE.set(cache_key, job, ttl_seconds=300)  # Cache for 5 minutes
    return job


def invalidate_job_cache(job_id: int = None) -> None:
    """Invalidate job-related caches."""
    QUERY_CACHE.invalidate("job_counts")
    if job_id:
        QUERY_CACHE.invalidate(f"job_{job_id}")


def invalidate_user_cache(user_id: int = None) -> None:
    """Invalidate user-related caches."""
    if user_id:
        QUERY_CACHE.invalidate(f"user_profile_{user_id}")
    else:
        QUERY_CACHE.invalidate("user_profile_")


def _normalize_external_link(raw_url: str) -> str | None:
    url = (raw_url or "").strip()
    if not url:
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url[:1000]


def _parse_link_blob(raw_value: Any) -> list[str]:
    if not raw_value:
        return []
    if isinstance(raw_value, list):
        values = raw_value
    else:
        text = str(raw_value).strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
            values = decoded if isinstance(decoded, list) else [decoded]
        except Exception:
            values = [line.strip() for line in text.splitlines() if line.strip()]

    cleaned: list[str] = []
    for value in values:
        normalized = _normalize_external_link(str(value))
        if normalized and normalized not in cleaned:
            cleaned.append(normalized)
    return cleaned


def _serialize_link_blob(values: list[str]) -> str:
    cleaned: list[str] = []
    for value in values:
        normalized = _normalize_external_link(value)
        if normalized and normalized not in cleaned:
            cleaned.append(normalized)
    return json.dumps(cleaned)


def _prepare_notifications(notifs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    contract_url_cache: dict[int, str | None] = {}

    for row in notifs:
        notif = dict(row)
        message = str(notif.get("message") or "").strip()
        target_url = notif.get("link_url")
        display_message = message

        msg_match = re.match(r"^New message from (.+?) on contract ['#]?(\d+)'?$", message)
        if msg_match:
            display_message = f"New message from {msg_match.group(1)}"
            if not target_url:
                target_url = url_for("contract_room", job_id=int(msg_match.group(2)))

        contract_match = re.search(r"contract #(\d+)", message)
        if not target_url and contract_match:
            contract_id = int(contract_match.group(1))
            if contract_id not in contract_url_cache:
                contract = STORE.query_one(
                    f"SELECT job_id FROM {STORE.t('contracts')} WHERE id=%s",
                    (contract_id,),
                )
                contract_url_cache[contract_id] = (
                    url_for("contract_room", job_id=int(contract["job_id"]))
                    if contract and contract.get("job_id")
                    else None
                )
            target_url = contract_url_cache[contract_id]

        notif["display_message"] = display_message
        notif["target_url"] = target_url
        prepared.append(notif)
    return prepared


def _serialize_notifications(notifs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for notif in _prepare_notifications(notifs):
        created_at = notif.get("created_at")
        items.append({
            "id": int(notif.get("id") or 0),
            "message": str(notif.get("display_message") or notif.get("message") or "").strip(),
            "target_url": notif.get("target_url"),
            "created_at": created_at.isoformat() if created_at else None,
            "created_at_human": time_ago(created_at) if created_at else "just now",
        })
    return items


def _serialize_contract_message(message_row: dict[str, Any]) -> dict[str, Any]:
    created_at = message_row.get("created_at")
    return {
        "id": int(message_row.get("id") or 0),
        "sender_id": int(message_row.get("sender_id") or 0),
        "sender_name": str(message_row.get("full_name") or "Anonymous"),
        "message": str(message_row.get("message") or ""),
        "created_at": created_at.isoformat() if created_at else None,
        "created_at_label": created_at.strftime("%I:%M %p") if created_at else "",
    }


def _contract_state_token(contract: dict[str, Any] | None) -> str:
    if not contract:
        return "no-contract"
    parts = [
        str(contract.get("id") or ""),
        str(contract.get("status") or ""),
        f"{float(contract.get('price_usd') or 0):.2f}",
        str(contract.get("offer_sent_at") or ""),
        str(contract.get("offer_accepted_at") or ""),
        str(contract.get("offer_rejected_at") or ""),
        str(contract.get("funded_at") or ""),
        str(contract.get("submitted_at") or ""),
        str(contract.get("review_deadline") or ""),
        str(contract.get("funding_reference") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


def _normalize_deliverables(raw_deliverables: Any) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    if not isinstance(raw_deliverables, list):
        return normalized

    for item in raw_deliverables:
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            raw_url = item.get("url")
        else:
            title = ""
            raw_url = item
        url = _normalize_external_link(str(raw_url or ""))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        normalized.append({
            "title": (title or "Deliverable")[:100],
            "url": url,
        })
    return normalized


# ═══════════════════════════════════════════════════════════════════════════════
# Auth — Google OAuth
# ═══════════════════════════════════════════════════════════════════════════════
GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO  = "https://www.googleapis.com/oauth2/v3/userinfo"

_GOOGLE_CLIENT_ID     = _env("GOOGLE_CLIENT_ID")
_GOOGLE_CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET")
_GOOGLE_REDIRECT_URI  = _env("GOOGLE_REDIRECT_URI", f"{SETTINGS.app_base_url}/auth/google/callback")


@app.route("/auth/google")
def google_auth():
    role = request.args.get("role", "worker")
    session["pending_role"] = role if role in USER_ROLES else "worker"
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    params = urlencode({
        "client_id": _GOOGLE_CLIENT_ID,
        "redirect_uri": _GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    })
    return redirect(f"{GOOGLE_AUTH_URL}?{params}")


@app.route("/auth/google/callback")
def google_callback():
    error = request.args.get("error")
    if error:
        flash(f"Google sign-in cancelled: {error}", "error")
        return redirect(url_for("login"))
    if request.args.get("state") != session.pop("oauth_state", None):
        flash("Invalid OAuth state. Please try again.", "error")
        return redirect(url_for("login"))
    code = request.args.get("code")
    try:
        token_resp = requests.post(GOOGLE_TOKEN_URL, data={
            "code": code, "client_id": _GOOGLE_CLIENT_ID,
            "client_secret": _GOOGLE_CLIENT_SECRET,
            "redirect_uri": _GOOGLE_REDIRECT_URI, "grant_type": "authorization_code",
        }, timeout=15).json()
        access_token = token_resp.get("access_token")
        userinfo = requests.get(GOOGLE_USERINFO,
            headers={"Authorization": f"Bearer {access_token}"}, timeout=10).json()
    except Exception as exc:
        LOG.warning("Google OAuth error: %s", exc)
        flash("Google sign-in failed. Please try again.", "error")
        return redirect(url_for("login"))

    email    = (userinfo.get("email") or "").lower().strip()
    sub      = userinfo.get("sub", "")
    name     = userinfo.get("name", "")
    role     = session.pop("pending_role", "worker")

    if not email:
        flash("Could not retrieve your email from Google.", "error")
        return redirect(url_for("login"))

    # Check if user exists
    user = STORE.query_one(f"SELECT * FROM {STORE.t('users')} WHERE email=%s", (email,))
    if user:
        # Update google_sub if not set
        if not user.get("google_sub"):
            STORE.execute(f"UPDATE {STORE.t('users')} SET google_sub=%s WHERE id=%s", (sub, user["id"]))
        if role == "worker":
            got_bonus = _grant_worker_mode(user["id"])
            if got_bonus:
                flash(f"Worker mode enabled. You received {WORKER_SIGNUP_CONNECTS} free connects.", "success")
        elif role == "employer":
            got_trial = _grant_employer_mode(user["id"])
            if got_trial:
                flash(f"Hiring mode enabled. Your free {EMPLOYER_TRIAL_DAYS}-day employer trial is active.", "success")
        STORE.execute(f"UPDATE {STORE.t('users')} SET role=%s WHERE id=%s", (role, user["id"]))
        # Check admin
        if email in SETTINGS.admin_google_email_set:
            session["is_admin"] = True
        _refresh_session(user["id"])
        return redirect(url_for("worker_dashboard") if role == "worker" else url_for("employer_dashboard"))
    else:
        # New user — create account
        can_worker = 1 if role == "worker" else 0
        can_employer = 1 if role == "employer" else 0
        worker_bonus_granted = 1 if role == "worker" else 0
        employer_trial_granted = 1 if role == "employer" else 0
        uid = STORE.execute(
            f"INSERT INTO {STORE.t('users')} (email, google_sub, role, full_name, connects_balance, can_worker, can_employer, worker_bonus_granted, employer_trial_granted) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                email, sub, role, name, _initial_connects_for_role(role),
                can_worker, can_employer, worker_bonus_granted, employer_trial_granted,
            ),
        )
        if role == "employer":
            trial_expires = datetime.utcnow() + timedelta(days=EMPLOYER_TRIAL_DAYS)
            STORE.execute(
                f"INSERT INTO {STORE.t('employer_profiles')} (user_id, is_subscribed, subscription_expires_at) VALUES (%s,%s,%s)",
                (uid, 1, trial_expires),
            )
        _refresh_session(uid)
        if email in SETTINGS.admin_google_email_set:
            session["is_admin"] = True
        send_email(email, "Welcome to TechBid Marketplace!", _build_welcome_email_html(role, name))
        flash("Account created! Please complete your profile.", "success")
        return redirect(url_for("complete_profile"))


# ═══════════════════════════════════════════════════════════════════════════════
# Auth — Register / Login / Logout
# ═══════════════════════════════════════════════════════════════════════════════
def _hash_pw(pw: str) -> str:
    """Hash password using bcrypt (secure, salted, work-factor resistant to brute force)."""
    if not BCRYPT_AVAILABLE:
        raise RuntimeError("bcrypt not installed. Run: pip install bcrypt")
    try:
        return bcrypt.hashpw(pw.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode('utf-8')
    except Exception as e:
        LOG.error("Password hashing failed: %s", e)
        raise

def _verify_pw(pw: str, pw_hash: str) -> tuple[bool, bool]:
    """Verify password with backward compatibility for SHA256 legacy hashes.
    
    Returns:
        (is_valid, needs_upgrade): 
        - is_valid: True if password matches
        - needs_upgrade: True if using legacy SHA256 and should upgrade to bcrypt
    """
    if not pw or not pw_hash:
        return False, False
    
    # Try bcrypt first (modern format, starts with $2a$, $2b$, $2x$, or $2y$)
    if pw_hash.startswith(('$2a$', '$2b$', '$2x$', '$2y$')):
        if not BCRYPT_AVAILABLE:
            return False, False
        try:
            is_valid = bcrypt.checkpw(pw.encode('utf-8'), pw_hash.encode('utf-8'))
            return is_valid, False  # No upgrade needed for bcrypt
        except Exception as e:
            LOG.warning("Bcrypt verification failed: %s", e)
            return False, False
    
    # Fall back to legacy SHA256 verification (for pre-migration accounts)
    try:
        sha256_hash = hashlib.sha256(pw.encode('utf-8')).hexdigest()
        is_valid = sha256_hash == pw_hash
        if is_valid:
            LOG.info("Legacy SHA256 password matched - will upgrade to bcrypt on next request")
            return True, True  # Password valid but needs bcrypt upgrade
        return False, False
    except Exception as e:
        LOG.error("Password verification failed: %s", e)
        return False, False


def generate_token(user_id: int, role: str, expiry_minutes: int | None = None) -> str:
    """Generate a JWT token for API authentication. Returns token string or empty string on error."""
    if not JWT_AVAILABLE:
        return ""
    try:
        expiry = expiry_minutes or SETTINGS.jwt_expiry_mins
        payload = {
            "user_id": user_id,
            "role": role,
            "exp": datetime.utcnow() + timedelta(minutes=expiry),
            "iat": datetime.utcnow(),
        }
        return jwt.encode(payload, SETTINGS.jwt_secret, algorithm=SETTINGS.jwt_algorithm)
    except Exception as e:
        LOG.error("Token generation failed: %s", e)
        return ""


def verify_token(token: str) -> dict | None:
    """Verify JWT token and return payload. Returns None if invalid."""
    if not JWT_AVAILABLE or not token:
        return None
    try:
        payload = jwt.decode(token, SETTINGS.jwt_secret, algorithms=[SETTINGS.jwt_algorithm])
        return payload
    except jwt.ExpiredSignatureError:
        LOG.warning("Token expired")
        return None
    except jwt.InvalidTokenError as e:
        LOG.warning("Invalid token: %s", e)
        return None
    except Exception as e:
        LOG.error("Token verification error: %s", e)
        return None


def token_required(f):
    """Decorator for routes that require valid JWT token in Authorization header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if "Authorization" in request.headers:
            auth_header = request.headers["Authorization"]
            try:
                token = auth_header.split(" ")[1]
            except IndexError:
                return jsonify({"error": "Invalid authorization header format"}), 401
        elif request.form.get("_token"):
            token = request.form.get("_token")
        
        if not token:
            return jsonify({"error": "Missing token"}), 401
        
        payload = verify_token(token)
        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401
        
        g.user_id = payload.get("user_id")
        g.role = payload.get("role")
        return f(*args, **kwargs)
    
    return decorated


def audit_log(user_id: int | None, action: str, entity_type: str, entity_id: int | None = None,
              old_value: str | None = None, new_value: str | None = None, details: str | None = None) -> None:
    """Log audit event for compliance and security monitoring."""
    try:
        ip = client_ip() if request else None
        STORE.execute(
            f"INSERT INTO {STORE.t('audit_log')} (user_id, action, entity_type, entity_id, old_value, new_value, ip_address, details) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (user_id, action, entity_type, entity_id, old_value, new_value, ip, details),
        )
    except Exception as e:
        LOG.error("Audit log failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# Contract/Escrow Helpers
# ═══════════════════════════════════════════════════════════════════════════════
def log_contract_event(contract_id: int, event_type: str, triggered_by_id: int | None = None, details: dict | None = None) -> None:
    """Log contract state changes for audit trail."""
    try:
        STORE.execute(
            f"INSERT INTO {STORE.t('contract_events')} (contract_id, event_type, triggered_by_id, details) VALUES (%s,%s,%s,%s)",
            (contract_id, event_type, triggered_by_id, json.dumps(details or {})),
        )
    except Exception as e:
        LOG.error("Contract event log failed: %s", e)


def update_contract_status(contract_id: int, new_status: str, triggered_by_id: int | None = None) -> bool:
    """
    Update contract status with validation.
    Valid transitions:
    pending → offer_accepted, offer_rejected
    offer_rejected → pending
    offer_accepted → funded
    funded → in_progress
    in_progress → submitted
    submitted → under_review
    under_review → approved, disputed
    disputed → resolved
    Any → refunded (special)
    """
    contract = STORE.query_one(f"SELECT status FROM {STORE.t('contracts')} WHERE id=%s", (contract_id,))
    if not contract:
        return False
    
    current = contract["status"]
    valid_transitions = {
        "pending": {"offer_accepted", "offer_rejected"},
        "offer_rejected": {"pending"},
        "offer_accepted": {"funded"},
        "funded": {"in_progress"},
        "in_progress": {"submitted"},
        "submitted": {"under_review"},
        "under_review": {"approved", "disputed"},
        "disputed": {"completed", "refunded"},
        "approved": {"completed"},
    }
    
    if new_status not in valid_transitions.get(current, set()):
        LOG.warning("Invalid transition: %s → %s", current, new_status)
        return False
    
    STORE.execute(
        f"UPDATE {STORE.t('contracts')} SET status=%s WHERE id=%s",
        (new_status, contract_id)
    )
    log_contract_event(contract_id, f"status_changed_to_{new_status}", triggered_by_id)
    return True


def create_contract_from_job(job_id: int, freelancer_id: int) -> int | None:
    """
    Create a contract from accepted job application.
    Returns contract_id or None on failure.
    """
    try:
        existing = STORE.query_one(f"SELECT id FROM {STORE.t('contracts')} WHERE job_id=%s", (job_id,))
        if existing:
            return int(existing["id"])

        job = STORE.query_one(f"SELECT * FROM {STORE.t('jobs')} WHERE id=%s", (job_id,))
        if not job:
            return None
        
        # Extract contract details from job posting
        scope = job.get("description", "")[:500]  # First 500 chars as scope
        price_usd = float(job.get("budget_usd") or 0)
        deliverables = job.get("description", "")[:300]
        deadline = job.get("deadline") or (datetime.utcnow() + timedelta(days=30))
        
        # Default review window: 7 days
        review_days = 7
        
        # Start in negotiation mode. Employer must send an offer and the freelancer must accept it.
        contract_id = STORE.execute(
            f"INSERT INTO {STORE.t('contracts')} "
            f"(job_id, employer_id, freelancer_id, scope, price_usd, deliverables, deadline, "
            f"status, escrow_amount_usd, review_days, created_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
            (job_id, job["employer_id"], freelancer_id, scope, price_usd, deliverables, 
             deadline, "pending", price_usd, review_days)
        )
        
        log_contract_event(contract_id, "created_from_job_acceptance", freelancer_id, 
                          {"job_id": job_id, "scope": scope[:100]})
        
        return contract_id
    except Exception as e:
        LOG.error("Failed to create contract: %s", e)
        return None


def _contract_room_access(job_id: int, uid: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, int | None, int | None]:
    job = STORE.query_one(f"SELECT * FROM {STORE.t('jobs')} WHERE id=%s", (job_id,))
    if not job:
        return None, None, None, None, None

    accepted_app = STORE.query_one(
        f"SELECT * FROM {STORE.t('applications')} WHERE job_id=%s AND status='accepted'",
        (job_id,),
    )
    if not accepted_app:
        return job, None, None, None, None

    employer_id = int(job["employer_id"])
    worker_id = int(accepted_app["user_id"])
    if uid != employer_id and uid != worker_id:
        return job, accepted_app, None, employer_id, worker_id

    contract = STORE.query_one(f"SELECT * FROM {STORE.t('contracts')} WHERE job_id=%s", (job_id,))
    if not contract:
        contract_id = create_contract_from_job(job_id, worker_id)
        if contract_id:
            contract = STORE.query_one(f"SELECT * FROM {STORE.t('contracts')} WHERE id=%s", (contract_id,))

    return job, accepted_app, contract, employer_id, worker_id


def _payment_redirect_target(reference: str):
    pay = STORE.query_one(
        f"SELECT payment_type, contract_id FROM {STORE.t('payments')} WHERE reference=%s",
        (reference,),
    )
    if pay:
        if (pay.get("payment_type") or "connects") == "escrow" and pay.get("contract_id"):
            contract = STORE.query_one(
                f"SELECT job_id FROM {STORE.t('contracts')} WHERE id=%s",
                (pay["contract_id"],),
            )
            if contract and contract.get("job_id"):
                return url_for("contract_room", job_id=int(contract["job_id"]))
        return url_for("worker_buy_connects")

    sub = STORE.query_one(f"SELECT id, tier FROM {STORE.t('subscriptions')} WHERE reference=%s", (reference,))
    if sub:
        # For PAYG purchases (tier='starter'), we need to get the job_id from the last posted job
        # The job was created during payment processing, so we find the most recent job for this subscription's employer
        if sub.get("tier") == "starter":
            # After PAYG payment confirmation, the job is already created
            # We need to query the subscription to get employer_id, then find their latest job
            sub_full = STORE.query_one(f"SELECT employer_id FROM {STORE.t('subscriptions')} WHERE reference=%s", (reference,))
            if sub_full:
                latest_job = STORE.query_one(
                    f"SELECT id FROM {STORE.t('jobs')} WHERE employer_id=%s ORDER BY created_at DESC LIMIT 1",
                    (sub_full["employer_id"],),
                )
                if latest_job:
                    return url_for("employer_applicants", job_id=latest_job["id"])
        return url_for("employer_dashboard")

    return url_for("index")


# ═══════════════════════════════════════════════════════════════════════════════
# SEO Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════
def _generate_job_structured_data(job: dict) -> str:
    """Generate JSON-LD structured data for a job listing."""
    import json
    
    # Convert created_at to ISO format string
    created_at = job.get("created_at")
    if created_at:
        if isinstance(created_at, datetime):
            date_posted = created_at.isoformat()
        else:
            date_posted = str(created_at)
    else:
        date_posted = datetime.utcnow().isoformat()
    
    structured_data = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": job.get("title", ""),
        "description": (job.get("description", "") or "")[:200],  # First 200 chars
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressCountry": "KE"
            }
        },
        "employmentType": "CONTRACT",
        "baseSalary": {
            "@type": "PriceSpecification",
            "currency": "USD",
            "price": float(job.get("budget_usd", 0) or 0)
        },
        "datePosted": date_posted,
    }
    return json.dumps(structured_data)


def _generate_organization_schema() -> str:
    """Generate JSON-LD for organization."""
    import json
    schema = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "TechBid Marketplace",
        "url": url_for("index", _external=True),
        "logo": url_for("static", filename="logo.png", _external=True),
        "sameAs": [],
        "contactPoint": {
            "@type": "ContactPoint",
            "contactType": "Customer Service",
            "url": url_for("contact", _external=True)
        }
    }
    return json.dumps(schema)


def _generate_job_slug(title: str, job_id: int | None = None) -> str:
    """Generate URL-friendly slug from job title."""
    # Convert to lowercase and replace spaces with hyphens
    slug = title.lower().strip()
    # Remove special characters, keeping only alphanumeric and hyphens
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    # Replace multiple spaces with single hyphen
    slug = re.sub(r'\s+', '-', slug)
    # Remove leading/trailing hyphens
    slug = slug.strip('-')
    # Truncate to 80 chars to keep URLs reasonable
    slug = slug[:80]
    # Remove trailing hyphens again after truncation
    slug = slug.rstrip('-')
    # If we have a job_id, append it for uniqueness (handles similar titles)
    if job_id:
        slug = f"{slug}-{job_id}"
    return slug or f"job-{job_id}" if job_id else "job"


@app.route("/")
def index():
    if session.get("user_id"):
        role = session.get("role")
        return redirect(url_for("worker_dashboard") if role == "worker" else url_for("employer_dashboard"))
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("index"))
    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid request.", "error")
            return redirect(url_for("register"))
        ip = client_ip()
        if not LIMITER.allow(f"reg:{ip}", 5, 3600):
            flash("Too many registration attempts. Try again later.", "error")
            return redirect(url_for("register"))
        email = request.form.get("email", "").lower().strip()
        pw    = request.form.get("password", "")
        role  = request.form.get("role", "worker")
        agreed_terms = request.form.get("agree_terms")
        if not email or not pw or role not in USER_ROLES:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))
        if agreed_terms != "yes":
            flash("You must accept the Terms of Service and Privacy Policy to create an account.", "error")
            return redirect(url_for("register"))
        if len(pw) < 8:
            flash("Password must be at least 8 characters.", "error")
            return redirect(url_for("register"))
        existing = STORE.query_one(f"SELECT id FROM {STORE.t('users')} WHERE email=%s", (email,))
        if existing:
            flash("An account with that email already exists. Log in, then switch to the role you want to use.", "info")
            return redirect(url_for("register"))
        can_worker = 1 if role == "worker" else 0
        can_employer = 1 if role == "employer" else 0
        worker_bonus_granted = 1 if role == "worker" else 0
        employer_trial_granted = 1 if role == "employer" else 0
        uid = STORE.execute(
            f"INSERT INTO {STORE.t('users')} (email, password_hash, role, connects_balance, can_worker, can_employer, worker_bonus_granted, employer_trial_granted) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                email, _hash_pw(pw), role, _initial_connects_for_role(role),
                can_worker, can_employer, worker_bonus_granted, employer_trial_granted,
            ),
        )
        if role == "employer":
            trial_expires = datetime.utcnow() + timedelta(days=EMPLOYER_TRIAL_DAYS)
            STORE.execute(
                f"INSERT INTO {STORE.t('employer_profiles')} (user_id, is_subscribed, subscription_expires_at) VALUES (%s,%s,%s)",
                (uid, 1, trial_expires),
            )
            # Send notification about free trial
            expiry_date = trial_expires.strftime("%B %d, %Y")
            trial_notif_message = f"You have a free 14-day trial to post jobs and hire talent. Trial expires on {expiry_date}."
            push_notif(uid, trial_notif_message)
            send_email(email, "Welcome to TechBid!", _build_welcome_email_html(role, expiry_date=expiry_date))
        else:
            # Send notification about free connects for freelancers
            push_notif(uid, f"Welcome! You have received {WORKER_SIGNUP_CONNECTS} free connects to start applying for jobs.")
            send_email(email, "Welcome to TechBid!", _build_welcome_email_html(role))
        flash("Account created successfully. Please log in to continue.", "success")
        return redirect(url_for("login"))
    return render_template("auth/register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))
    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid request.", "error")
            return redirect(url_for("login"))
        ip = client_ip()
        if not LIMITER.allow(f"login:{ip}", 5, 900):  # 5 attempts per 15 minutes
            flash("Too many login attempts. Please wait 15 minutes.", "error")
            return redirect(url_for("login"))
        email = request.form.get("email", "").lower().strip()
        pw    = request.form.get("password", "")
        user  = STORE.query_one(f"SELECT * FROM {STORE.t('users')} WHERE email=%s", (email,))
        
        # Verify password with backward compatibility for legacy SHA256 hashes
        is_valid, needs_upgrade = False, False
        if user:
            is_valid, needs_upgrade = _verify_pw(pw, user.get("password_hash", ""))
        
        if not is_valid:
            audit_log(None, "LOGIN_FAILED", "user", None, details=f"Email: {email}")
            flash("Invalid email or password.", "error")
            return redirect(url_for("login"))
        
        # Upgrade legacy SHA256 password to bcrypt
        if needs_upgrade:
            try:
                new_hash = _hash_pw(pw)
                STORE.execute(
                    f"UPDATE {STORE.t('users')} SET password_hash=%s WHERE id=%s",
                    (new_hash, user["id"])
                )
                LOG.info("Upgraded user %d password from SHA256 to bcrypt", user["id"])
                audit_log(user["id"], "PASSWORD_UPGRADED", "user", user["id"])
            except Exception as e:
                LOG.error("Password upgrade failed for user %d: %s", user["id"], e)
                # Continue with login anyway since old password was valid
        
        # Successful login
        audit_log(user["id"], "LOGIN", "user", user["id"])
        _refresh_session(user["id"])
        if JWT_AVAILABLE:
            token = generate_token(user["id"], user["role"])
            if token:
                response = redirect(url_for("worker_dashboard") if user["role"] == "worker" else url_for("employer_dashboard"))
                response.set_cookie("auth_token", token, httponly=True, secure=False, samesite="Lax")
                return response
        if email in SETTINGS.admin_google_email_set:
            session["is_admin"] = True
        dest = url_for("worker_dashboard") if user["role"] == "worker" else url_for("employer_dashboard")
        return redirect(dest)
    return render_template("auth/login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid request.", "error")
            return redirect(url_for("forgot_password"))
        ip = client_ip()
        if not LIMITER.allow(f"forgot:{ip}", 6, 3600):
            flash("Too many reset requests. Please try again later.", "error")
            return redirect(url_for("forgot_password"))
        email = request.form.get("email", "").lower().strip()
        if not email:
            flash("Email is required.", "error")
            return redirect(url_for("forgot_password"))

        user = STORE.query_one(f"SELECT id,email,password_hash FROM {STORE.t('users')} WHERE email=%s", (email,))
        # Do not reveal whether an account exists.
        if user and user.get("password_hash"):
            raw_token = secrets.token_urlsafe(32)
            token_hash = _hash_reset_token(raw_token)
            expires_at = datetime.utcnow() + timedelta(hours=1)
            STORE.execute(
                f"INSERT INTO {STORE.t('password_resets')} (user_id, token_hash, expires_at) VALUES (%s,%s,%s)",
                (user["id"], token_hash, expires_at),
            )
            reset_url = url_for("reset_password", token=raw_token, _external=True)
            send_email(
                email,
                "Reset your TechBid password",
                (
                    "<h2>Password reset request</h2>"
                    "<p>We received a request to reset your password.</p>"
                    f"<p><a href=\"{reset_url}\">Click here to reset your password</a></p>"
                    "<p>This link expires in 1 hour. If you did not request this, you can ignore this email.</p>"
                ),
            )
        flash("If an account exists for that email, a reset link has been sent.", "info")
        return redirect(url_for("login"))
    return render_template("auth/forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    token_hash = _hash_reset_token(token or "")
    record = STORE.query_one(
        f"SELECT pr.id, pr.user_id, pr.expires_at, pr.used_at, u.email, u.password_hash "
        f"FROM {STORE.t('password_resets')} pr "
        f"JOIN {STORE.t('users')} u ON u.id=pr.user_id "
        f"WHERE pr.token_hash=%s",
        (token_hash,),
    )
    if not record or record.get("used_at") or datetime.utcnow() > record["expires_at"]:
        flash("That reset link is invalid or expired. Request a new one.", "error")
        return redirect(url_for("forgot_password"))
    if not record.get("password_hash"):
        flash("Password reset is only available for email/password accounts.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid request.", "error")
            return redirect(url_for("reset_password", token=token))
        pw = request.form.get("password", "")
        pw2 = request.form.get("confirm_password", "")
        if len(pw) < 8:
            flash("Password must be at least 8 characters.", "error")
            return redirect(url_for("reset_password", token=token))
        if pw != pw2:
            flash("Passwords do not match.", "error")
            return redirect(url_for("reset_password", token=token))

        STORE.execute(f"UPDATE {STORE.t('users')} SET password_hash=%s WHERE id=%s", (_hash_pw(pw), record["user_id"]))
        STORE.execute(
            f"UPDATE {STORE.t('password_resets')} SET used_at=UTC_TIMESTAMP() WHERE user_id=%s AND used_at IS NULL",
            (record["user_id"],),
        )
        audit_log(record["user_id"], "PASSWORD_RESET", "user", record["user_id"])
        flash("Password updated. You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template("auth/reset_password.html", token=token)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


@app.route("/switch-role/<target>", methods=["POST"])
@login_required
def switch_role(target: str):
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("index"))
    target = (target or "").strip().lower()
    uid = int(session["user_id"])
    if target not in USER_ROLES:
        flash("Invalid role selected.", "error")
        return redirect(url_for("index"))
    if target == "worker":
        got_bonus = _grant_worker_mode(uid)
        if got_bonus:
            flash(f"Worker mode enabled. {WORKER_SIGNUP_CONNECTS} free connects added.", "success")
    else:
        got_trial = _grant_employer_mode(uid)
        if got_trial:
            flash(f"Hiring mode enabled. Free {EMPLOYER_TRIAL_DAYS}-day subscription trial applied.", "success")
    STORE.execute(f"UPDATE {STORE.t('users')} SET role=%s WHERE id=%s", (target, uid))
    _refresh_session(uid)
    return redirect(url_for("worker_dashboard") if target == "worker" else url_for("employer_dashboard"))


@app.route("/api/auth/token", methods=["POST"])
def api_get_token():
    """API endpoint for token generation. Accepts email and password."""
    if not JWT_AVAILABLE:
        return jsonify({"error": "Tokenization not available"}), 503
    
    email = request.form.get("email", "").lower().strip()
    pw = request.form.get("password", "")
    
    if not email or not pw:
        return jsonify({"error": "Email and password required"}), 400
    
    user = STORE.query_one(f"SELECT * FROM {STORE.t('users')} WHERE email=%s", (email,))
    
    # Verify password with backward compatibility for legacy SHA256 hashes
    is_valid, needs_upgrade = False, False
    if user:
        is_valid, needs_upgrade = _verify_pw(pw, user.get("password_hash", ""))
    
    if not is_valid:
        audit_log(None, "API_LOGIN_FAILED", "user", None, details=f"Email: {email}")
        return jsonify({"error": "Invalid credentials"}), 401
    
    # Upgrade legacy SHA256 password to bcrypt
    if needs_upgrade:
        try:
            new_hash = _hash_pw(pw)
            STORE.execute(
                f"UPDATE {STORE.t('users')} SET password_hash=%s WHERE id=%s",
                (new_hash, user["id"])
            )
            LOG.info("Upgraded user %d password from SHA256 to bcrypt", user["id"])
        except Exception as e:
            LOG.error("Password upgrade failed for user %d: %s", user["id"], e)
            # Continue anyway since old password was valid
    
    token = generate_token(user["id"], user["role"])
    if not token:
        return jsonify({"error": "Could not generate token"}), 500
    
    audit_log(user["id"], "API_TOKEN_GENERATED", "user", user["id"])
    return jsonify({
        "token": token,
        "user_id": user["id"],
        "role": user["role"],
        "expires_in": SETTINGS.jwt_expiry_mins * 60
    }), 200


@app.route("/api/auth/refresh", methods=["POST"])
@token_required
def api_refresh_token():
    """Refresh an existing token."""
    if not JWT_AVAILABLE:
        return jsonify({"error": "Tokenization not available"}), 503
    
    new_token = generate_token(g.user_id, g.role)
    if not new_token:
        return jsonify({"error": "Could not generate token"}), 500
    
    audit_log(g.user_id, "TOKEN_REFRESHED", "user", g.user_id)
    return jsonify({
        "token": new_token,
        "expires_in": SETTINGS.jwt_expiry_mins * 60
    }), 200


@app.route("/profile/complete", methods=["GET", "POST"])
@login_required
def complete_profile():
    uid  = session["user_id"]
    role = session.get("role")
    user = STORE.query_one(f"SELECT * FROM {STORE.t('users')} WHERE id=%s", (uid,))
    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid request.", "error")
            return redirect(url_for("complete_profile"))
        full_name = request.form.get("full_name", "").strip()
        mobile    = request.form.get("mobile", "").strip()
        country   = request.form.get("country", "").strip()
        bio       = request.form.get("bio", "").strip()
        portfolio = request.form.get("portfolio_links", "").strip()
        skills_raw = request.form.get("skills", "")
        specialty  = request.form.get("specialty", "").strip()

        skills_list = [s.strip() for s in skills_raw.split(",") if s.strip()][:20]

        # Handle profile picture upload
        pic_url = user.get("profile_pic_url") if user else None
        pic_file = request.files.get("profile_pic")
        if pic_file and pic_file.filename:
            ext = pic_file.filename.rsplit(".", 1)[-1].lower()
            if ext in {"jpg", "jpeg", "png", "webp"}:
                file_bytes = pic_file.read()
                # Check file size (max 2MB for base64, 5MB for GitHub)
                if len(file_bytes) <= 5242880:  # 5MB
                    fname = f"{uid}_{int(time.time())}.{ext}"
                    uploaded = upload_profile_pic(file_bytes, fname)
                    if uploaded:
                        pic_url = uploaded
                        flash("Profile picture updated successfully!", "success")
                    else:
                        flash("Failed to upload picture. Please try again.", "error")
                else:
                    flash("Image file is too large (max 5MB).", "error")
            else:
                flash("Invalid image format. Use JPG, PNG, or WebP.", "error")

        # Employer extra fields
        if role == "employer":
            company = request.form.get("company_name", "").strip()
            website = request.form.get("website", "").strip()
            STORE.execute(
                f"UPDATE {STORE.t('employer_profiles')} SET company_name=%s, website=%s WHERE user_id=%s",
                (company, website, uid),
            )

        STORE.execute(
            f"UPDATE {STORE.t('users')} SET full_name=%s, mobile=%s, country=%s, bio=%s, "
            f"portfolio_links=%s, skills=%s, specialty=%s, profile_pic_url=%s, profile_complete=1 WHERE id=%s",
            (full_name, mobile, country, bio, portfolio, json.dumps(skills_list), specialty, pic_url, uid),
        )
        _refresh_session(uid)
        if not pic_file:  # Only show this message if they didn't upload a picture
            flash("Profile updated!", "success")
        return redirect(url_for("worker_dashboard") if role == "worker" else url_for("employer_dashboard"))

    emp_profile = None
    if role == "employer":
        emp_profile = STORE.query_one(f"SELECT * FROM {STORE.t('employer_profiles')} WHERE user_id=%s", (uid,))
    if user:
        user = dict(user)
        user["profile_pic_url"] = rewrite_pic_url(user.get("profile_pic_url"))
    return render_template("auth/complete_profile.html", user=user, emp_profile=emp_profile, categories=JOB_CATEGORIES)


# ═══════════════════════════════════════════════════════════════════════════════
# Worker Routes
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/worker/dashboard")
@worker_required
def worker_dashboard():
    uid = session["user_id"]
    # Grant monthly bonus if eligible
    bonus_granted = _grant_monthly_bonus(uid)
    if bonus_granted:
        push_notif(uid, f"Bonus! You've earned {MONTHLY_BONUS_CONNECTS} free connects for this month.")
    _refresh_session(uid)
    user = STORE.query_one(f"SELECT * FROM {STORE.t('users')} WHERE id=%s", (uid,))
    recent_apps = STORE.query_all(
        f"SELECT a.*, j.title, j.category, j.budget_usd, j.job_type, j.status as job_status, j.is_robot, j.slug, rw.awarded_at "
        f"FROM {STORE.t('applications')} a "
        f"JOIN {STORE.t('jobs')} j ON j.id=a.job_id "
        f"LEFT JOIN {STORE.t('robot_winners')} rw ON rw.job_id=j.id "
        f"WHERE a.user_id=%s ORDER BY a.applied_at DESC LIMIT 5",
        (uid,),
    )
    
    # AUTO-UPDATE ROBOT JOB APPLICATION STATUSES
    # When a robot job is awarded, automatically mark pending applications as rejected
    for a in recent_apps:
        if a.get("is_robot") and a.get("awarded_at") and datetime.utcnow() >= a["awarded_at"] and a.get("status") == "pending":
            STORE.execute(
                f"UPDATE {STORE.t('applications')} SET status='rejected' WHERE id=%s",
                (a["id"],)
            )
            a["status"] = "rejected"
    
    for a in recent_apps:
        a["display_status"] = a.get("status")
        if a.get("status") == "pending" and a.get("job_status") in {"closed", "filled"}:
            a["display_status"] = "closed"
    notifs = _prepare_notifications(STORE.query_all(
        f"SELECT * FROM {STORE.t('notifications')} WHERE user_id=%s ORDER BY created_at DESC LIMIT 5", (uid,)))
    return render_template("worker/dashboard.html", user=user, recent_apps=recent_apps, notifs=notifs)


@app.route("/notifications")
@login_required
def notifications_page():
    uid = session["user_id"]
    notifs = _prepare_notifications(STORE.query_all(
        f"SELECT * FROM {STORE.t('notifications')} WHERE user_id=%s ORDER BY created_at DESC LIMIT 100",
        (uid,),
    ))
    STORE.execute(f"UPDATE {STORE.t('notifications')} SET is_read=1 WHERE user_id=%s", (uid,))
    return render_template("notifications.html", notifs=notifs)


@app.route("/api/notifications/live")
@login_required
def notifications_live_api():
    uid = session["user_id"]
    limit = min(100, max(1, request.args.get("limit", type=int) or 5))
    mark_read = request.args.get("mark_read", "0") == "1"

    if mark_read:
        STORE.execute(f"UPDATE {STORE.t('notifications')} SET is_read=1 WHERE user_id=%s", (uid,))

    notifs = STORE.query_all(
        f"SELECT * FROM {STORE.t('notifications')} WHERE user_id=%s ORDER BY created_at DESC LIMIT {limit}",
        (uid,),
    )
    unread_count = 0
    if not mark_read:
        row = STORE.query_one(
            f"SELECT COUNT(*) AS c FROM {STORE.t('notifications')} WHERE user_id=%s AND is_read=0",
            (uid,),
        )
        unread_count = int((row or {}).get("c") or 0)

    return jsonify({
        "notifications": _serialize_notifications(notifs),
        "unread_count": unread_count,
    })


@app.route("/worker/jobs")
@worker_required
@profile_required
def worker_jobs():
    cat     = request.args.get("category", "")
    jtype   = request.args.get("type", "")
    search  = request.args.get("q", "").strip()
    page    = max(1, int(request.args.get("page", 1)))
    per_page = 15
    offset  = (page - 1) * per_page

    wheres = []
    params: list = []
    if cat and cat in JOB_CATEGORIES:
        wheres.append("j.category=%s"); params.append(cat)
    if jtype and jtype in JOB_TYPES:
        wheres.append("j.job_type=%s"); params.append(jtype)
    if search:
        wheres.append("(j.title LIKE %s OR j.description LIKE %s)")
        params += [f"%{search}%", f"%{search}%"]
    where_sql = " AND ".join(wheres) if wheres else "1=1"
    where_sql += " AND (j.is_robot=0 OR rw.awarded_at IS NULL OR rw.awarded_at > UTC_TIMESTAMP())"

    total_row = STORE.query_one(
        f"SELECT COUNT(*) as c FROM {STORE.t('jobs')} j "
        f"LEFT JOIN {STORE.t('robot_winners')} rw ON rw.job_id=j.id "
        f"WHERE {where_sql}", tuple(params))
    total = total_row["c"] if total_row else 0

    jobs = STORE.query_all(
        f"SELECT j.*, rw.robot_name, rw.connects_shown, rw.completed_jobs, rw.success_rate, rw.selection_style, rw.awarded_at, "
        f"COALESCE(ac.real_applications, 0) as real_applications "
        f"FROM {STORE.t('jobs')} j "
        f"LEFT JOIN {STORE.t('robot_winners')} rw ON rw.job_id=j.id "
        f"LEFT JOIN (SELECT job_id, COUNT(*) as real_applications FROM {STORE.t('applications')} GROUP BY job_id) ac ON ac.job_id=j.id "
        f"WHERE {where_sql} ORDER BY j.created_at DESC LIMIT %s OFFSET %s",
        tuple(params) + (per_page, offset),
    )
    
    # Process simulated applicant logic
    for j in jobs:
        _apply_robot_activity(j, int(j.get("real_applications") or 0))
    uid = session["user_id"]
    job_ids_on_page = tuple(j["id"] for j in jobs)
    if job_ids_on_page:
        placeholders = ",".join(["%s"] * len(job_ids_on_page))
        applied_ids = {r["job_id"] for r in STORE.query_all(
            f"SELECT job_id FROM {STORE.t('applications')} WHERE user_id=%s AND job_id IN ({placeholders})",
            (uid,) + job_ids_on_page)}
    else:
        applied_ids = set()

    pages = max(1, (total + per_page - 1) // per_page)
    
    # SEO metadata for job listings
    if search:
        seo_title = f"Jobs matching '{search}' | TechBid"
        seo_description = f"Find {total} freelance jobs matching '{search}' on TechBid Marketplace (includes job history)."
    elif cat:
        seo_title = f"{cat} Jobs | TechBid"
        seo_description = f"Find {total} {cat} freelance jobs on TechBid Marketplace (including job history)."
    else:
        seo_title = "Find Freelance Jobs | TechBid"
        seo_description = f"Browse {total} freelance jobs on TechBid Marketplace. View complete job history (open and closed projects)."
    
    return render_template("worker/jobs.html", jobs=jobs, applied_ids=applied_ids,
                           page=page, pages=pages, total=total,
                           cat=cat, jtype=jtype, search=search,
                           seo_title=seo_title,
                           seo_description=seo_description)



def _get_employer_stats(employer_id: int | None, is_robot: bool = False) -> dict:
    """Calculate employer stats: jobs posted, completion rate, etc.
    
    Returns:
    {
        'jobs_posted': int,
        'completion_rate': float (0-100),
        'completed_contracts': int,
        'total_contracts': int,
        'hire_rate': float (0-100),
        'avg_rating': float (0-5),
        'is_verified': bool,
    }
    """
    if is_robot or not employer_id:
        # Simulate stats for robot/AI jobs
        import random
        jobs_posted = random.randint(1, 15)
        completed = random.randint(int(jobs_posted * 0.70), int(jobs_posted * 0.95))
        total_contracts = completed + random.randint(1, max(1, int(jobs_posted * 0.20)))
        hire_rate = random.uniform(60, 98)
        avg_rating = random.uniform(4.5, 5.0)
        
        return {
            'jobs_posted': jobs_posted,
            'completion_rate': (completed / total_contracts * 100) if total_contracts > 0 else 0,
            'completed_contracts': completed,
            'total_contracts': total_contracts,
            'hire_rate': hire_rate,
            'avg_rating': avg_rating,
            'is_verified': True,
        }
    
    # Calculate real stats for actual employer
    try:
        # Total jobs posted
        jobs_posted_result = STORE.query_one(
            f"SELECT COUNT(*) as cnt FROM {STORE.t('jobs')} WHERE employer_id=%s",
            (employer_id,)
        )
        jobs_posted = jobs_posted_result.get('cnt', 0) if jobs_posted_result else 0
        
        # Contracts stats
        contracts_result = STORE.query_all(
            f"SELECT status FROM {STORE.t('contracts')} WHERE employer_id=%s",
            (employer_id,)
        )
        
        total_contracts = len(contracts_result) if contracts_result else 0
        completed_contracts = sum(1 for c in contracts_result if c.get('status') in ['completed', 'approved'])
        
        completion_rate = (completed_contracts / total_contracts * 100) if total_contracts > 0 else 0
        
        # Hire rate (percentage of jobs with accepted applications)
        jobs_with_hires = 0
        for job_row in STORE.query_all(
            f"SELECT j.id FROM {STORE.t('jobs')} j WHERE j.employer_id=%s",
            (employer_id,)
        ):
            app = STORE.query_one(
                f"SELECT id FROM {STORE.t('applications')} WHERE job_id=%s AND status='accepted'",
                (job_row['id'],)
            )
            if app:
                jobs_with_hires += 1
        
        hire_rate = (jobs_with_hires / jobs_posted * 100) if jobs_posted > 0 else 0
        
        # Average rating (if we had ratings, but for now use completion rate as proxy)
        avg_rating = 4.5 + (completion_rate / 100) * 0.5  # Max 5.0
        
        # Check if verified (has completed contracts)
        is_verified = completed_contracts > 0
        
        return {
            'jobs_posted': jobs_posted,
            'completion_rate': completion_rate,
            'completed_contracts': completed_contracts,
            'total_contracts': total_contracts,
            'hire_rate': hire_rate,
            'avg_rating': min(avg_rating, 5.0),
            'is_verified': is_verified,
        }
    except Exception as e:
        LOG.error("Error calculating employer stats: %s", e)
        return {
            'jobs_posted': 0,
            'completion_rate': 0,
            'completed_contracts': 0,
            'total_contracts': 0,
            'hire_rate': 0,
            'avg_rating': 0,
            'is_verified': False,
        }


def _get_worker_stats(worker_id: int | None) -> dict:
    """Calculate worker stats: jobs completed, success rate, acceptance rate, etc.
    
    Returns:
    {
        'jobs_completed': int,
        'success_rate': float (0-100),
        'total_applications': int,
        'acceptance_rate': float (0-100),
        'avg_rating': float (0-5),
        'is_verified': bool,
    }
    """
    if not worker_id:
        return {
            'jobs_completed': 0,
            'success_rate': 0,
            'total_applications': 0,
            'acceptance_rate': 0,
            'avg_rating': 0,
            'is_verified': False,
        }
    
    try:
        # Total applications submitted
        total_apps_result = STORE.query_one(
            f"SELECT COUNT(*) as cnt FROM {STORE.t('applications')} WHERE user_id=%s",
            (worker_id,)
        )
        total_applications = total_apps_result.get('cnt', 0) if total_apps_result else 0
        
        # Accepted applications
        accepted_apps_result = STORE.query_one(
            f"SELECT COUNT(*) as cnt FROM {STORE.t('applications')} WHERE user_id=%s AND status='accepted'",
            (worker_id,)
        )
        accepted_applications = accepted_apps_result.get('cnt', 0) if accepted_apps_result else 0
        
        # Acceptance rate
        acceptance_rate = (accepted_applications / total_applications * 100) if total_applications > 0 else 0
        
        # Completed contracts
        completed_contracts_result = STORE.query_one(
            f"SELECT COUNT(*) as cnt FROM {STORE.t('contracts')} WHERE user_id=%s AND status IN ('completed', 'approved')",
            (worker_id,)
        )
        jobs_completed = completed_contracts_result.get('cnt', 0) if completed_contracts_result else 0
        
        # Total contracts
        total_contracts_result = STORE.query_one(
            f"SELECT COUNT(*) as cnt FROM {STORE.t('contracts')} WHERE user_id=%s",
            (worker_id,)
        )
        total_contracts = total_contracts_result.get('cnt', 0) if total_contracts_result else 0
        
        # Success rate (completed / total contracts)
        success_rate = (jobs_completed / total_contracts * 100) if total_contracts > 0 else 0
        
        # Average rating (use completion rate as proxy for now)
        avg_rating = 4.2 + (success_rate / 100) * 0.8  # Range 4.2-5.0
        
        # Verified if has completed at least 1 contract
        is_verified = jobs_completed > 0
        
        return {
            'jobs_completed': jobs_completed,
            'success_rate': success_rate,
            'total_applications': total_applications,
            'acceptance_rate': acceptance_rate,
            'avg_rating': min(avg_rating, 5.0),
            'is_verified': is_verified,
        }
    except Exception as e:
        LOG.error("Error calculating worker stats: %s", e)
        return {
            'jobs_completed': 0,
            'success_rate': 0,
            'total_applications': 0,
            'acceptance_rate': 0,
            'avg_rating': 0,
            'is_verified': False,
        }


@app.route("/worker/jobs/<slug_or_id>")
@worker_required
@profile_required
def worker_job_detail(slug_or_id):
    uid = session["user_id"]
    # Try to parse as job_id (numeric) first, then as slug
    job_id = None
    try:
        job_id = int(slug_or_id)
    except ValueError:
        pass
    
    if job_id:
        # Query by numeric ID
        job = STORE.query_one(
            f"SELECT j.*, rw.robot_name, rw.connects_shown, rw.completed_jobs, rw.success_rate, rw.selection_style, rw.awarded_at FROM {STORE.t('jobs')} j "
            f"LEFT JOIN {STORE.t('robot_winners')} rw ON rw.job_id=j.id WHERE j.id=%s", (job_id,))
    else:
        # Query by slug
        job = STORE.query_one(
            f"SELECT j.*, rw.robot_name, rw.connects_shown, rw.completed_jobs, rw.success_rate, rw.selection_style, rw.awarded_at FROM {STORE.t('jobs')} j "
            f"LEFT JOIN {STORE.t('robot_winners')} rw ON rw.job_id=j.id WHERE j.slug=%s", (slug_or_id,))
    
    if not job:
        flash("Job not found.", "error"); return redirect(url_for("worker_jobs"))
    
    # Ensure job_id is set from job object (in case we looked up by slug)
    if not job_id:
        job_id = job.get("id")
    
    # Redirect numeric IDs to slug-based URLs for SEO (not vice versa)
    if (slug_or_id.isdigit() if isinstance(slug_or_id, str) else False) and job.get("slug"):
        return redirect(url_for("worker_job_detail", slug_or_id=job["slug"]))
        
    # Process simulated applicant logic for job detail
    interview_names: list[str] = []
    real_application_count = int((STORE.query_one(
        f"SELECT COUNT(*) as c FROM {STORE.t('applications')} WHERE job_id=%s", (job_id,))
        or {}).get("c", 0))

    if job.get("is_robot"):
        _apply_robot_activity(job, real_application_count)
        if job.get("simulated_interviews", 0) > 0:
            interview_names = _interview_winner_names(
                STORE, int(job_id), min(int(job.get("simulated_interviews") or 0), 5)
            )
        
        # AUTO-UPDATE APPLICATION STATUSES: When robot job is awarded, mark all pending apps as rejected
        # This ensures consistency between dashboard and job detail view
        if job.get("awarded_at") and datetime.utcnow() >= job["awarded_at"]:
            STORE.execute(
                f"UPDATE {STORE.t('applications')} SET status='rejected' WHERE job_id=%s AND status='pending'",
                (job_id,)
            )
        elif job.get("awarded_at"):
            # Hide robot winner until awarded_at time passes
            job["robot_name"] = None
    else:
        # For real jobs, get actual application statuses and populate same metrics
        status_counts = STORE.query_one(
            f"SELECT "
            f"  SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END) as accepted, "
            f"  SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending "
            f"FROM {STORE.t('applications')} WHERE job_id=%s",
            (job_id,)
        )
        if status_counts:
            # Map real job metrics to the same format as robot jobs
            # Pending apps = invites sent, Accepted apps = interviews underway
            job["simulated_invites"] = int(status_counts.get("pending") or 0)
            job["simulated_interviews"] = int(status_counts.get("accepted") or 0)
            
    already_applied = STORE.query_one(
        f"SELECT id, status FROM {STORE.t('applications')} WHERE user_id=%s AND job_id=%s", (uid, job_id))

    display_applicants = job.get("display_applicants", real_application_count)
    
    # Get employer info and stats
    employer_info = None
    employer_stats = None
    employer_tier = None
    if job.get("employer_id"):
        employer_info = STORE.query_one(
            f"SELECT id, full_name, profile_pic_url FROM {STORE.t('users')} WHERE id=%s",
            (job["employer_id"],)
        )
        employer_stats = _get_employer_stats(job["employer_id"], is_robot=False)
        # Get employer's current subscription tier
        employer_sub = STORE.query_one(
            f"SELECT tier FROM {STORE.t('subscriptions')} WHERE employer_id=%s AND status='confirmed' ORDER BY created_at DESC LIMIT 1",
            (job["employer_id"],)
        )
        employer_tier = employer_sub.get('tier') if employer_sub else None
    else:
        # Robot/AI job - simulate employer with company name or use generic name
        employer_stats = _get_employer_stats(None, is_robot=True)
        company_name = job.get("ai_employer_name") or "TechBid Verified Client"
        employer_info = {"full_name": company_name, "profile_pic_url": None}
        employer_tier = None

    # SEO metadata
    seo_title = f"{job.get('title')} - {job.get('category')} Job on TechBid"
    seo_description = job.get("description", "")[:160]
    seo_keywords = f"{job.get('category')}, {job.get('job_type')}, {job.get('title')}, freelance, TechBid"
    job_structured_data = _generate_job_structured_data(job)

    return render_template("worker/job_detail.html", job=job,
                           already_applied=bool(already_applied),
                           applicant_count=display_applicants,
                           interview_names=interview_names,
                           employer_info=employer_info,
                           employer_stats=employer_stats,
                           employer_tier=employer_tier,
                           seo_title=seo_title,
                           seo_description=seo_description,
                           seo_keywords=seo_keywords,
                           job_structured_data=job_structured_data)


@app.route("/worker/jobs/<slug_or_id>/apply", methods=["POST"])
@worker_required
@profile_required
def worker_apply(slug_or_id):
    # Convert slug to job_id if needed
    job_id = None
    try:
        job_id = int(slug_or_id)
    except ValueError:
        job = STORE.query_one(f"SELECT id FROM {STORE.t('jobs')} WHERE slug=%s", (slug_or_id,))
        if job:
            job_id = job["id"]
    
    if not job_id:
        flash("Job not found.", "error"); return redirect(url_for("worker_jobs"))
    if not verify_csrf():
        flash("Invalid request.", "error"); return redirect(url_for("worker_job_detail", slug_or_id=slug_or_id))
    uid = session["user_id"]
    job = STORE.query_one(f"SELECT j.*, rw.awarded_at FROM {STORE.t('jobs')} j LEFT JOIN {STORE.t('robot_winners')} rw ON rw.job_id=j.id WHERE j.id=%s AND status='open'", (job_id,))
    if not job:
        flash("Job not available.", "error"); return redirect(url_for("worker_jobs"))
    if job.get("is_robot") and job.get("awarded_at") and datetime.utcnow() >= job["awarded_at"]:
        flash("This robot job is already in shortlist/interview finalization.", "warning"); return redirect(url_for("worker_job_detail", slug_or_id=job.get("slug") or job_id))
        
    already = STORE.query_one(
        f"SELECT id FROM {STORE.t('applications')} WHERE user_id=%s AND job_id=%s", (uid, job_id))
    if already:
        flash("You've already applied for this job.", "warning"); return redirect(url_for("worker_job_detail", slug_or_id=job.get("slug") or job_id))
    bid_connects = request.form.get("bid_connects", type=int)
    if not bid_connects or bid_connects < job["connects_required"]:
        bid_connects = job["connects_required"]
    connects_needed = bid_connects

    user = STORE.query_one(f"SELECT id, email, full_name, connects_balance FROM {STORE.t('users')} WHERE id=%s", (uid,))
    if not user or user["connects_balance"] < connects_needed:
        flash(f"You need {connects_needed} connects to apply with that bid. Buy more connects.", "warning")
        return redirect(url_for("worker_buy_connects"))
    cover = request.form.get("cover_letter", "").strip()[:2000]
    attachment_links: list[str] = []
    for raw_link in request.form.getlist("attachment_links"):
        if not raw_link or not raw_link.strip():
            continue
        normalized = _normalize_external_link(raw_link)
        if not normalized:
            flash("One of the supporting links is invalid. Use a full website URL.", "error")
            return redirect(url_for("worker_job_detail", slug_or_id=job.get("slug") or job_id))
        if normalized not in attachment_links:
            attachment_links.append(normalized)

    # Deduct connects atomically
    affected = STORE.execute(
        f"UPDATE {STORE.t('users')} SET connects_balance=connects_balance-%s "
        f"WHERE id=%s AND connects_balance>=%s",
        (connects_needed, uid, connects_needed),
    )
    if not affected:
        flash("Insufficient connects. The balance may have changed. Refresh and try again.", "warning")
        return redirect(url_for("worker_buy_connects"))
    
    # Log connects deduction
    audit_log(uid, "CONNECTS_DEDUCTED", "application", None, old_value=str(user["connects_balance"]), 
              new_value=str(user["connects_balance"] - connects_needed), details=f"Job ID: {job_id}, Amount: {connects_needed}")
    
    # Insert application
    app_id = STORE.execute(
        f"INSERT INTO {STORE.t('applications')} (user_id, job_id, connects_spent, cover_letter, attachment_links) VALUES (%s,%s,%s,%s,%s)",
        (uid, job_id, connects_needed, cover, _serialize_link_blob(attachment_links)),
    )
    if not app_id:
        # Rollback connects deduction if application insert failed
        STORE.execute(
            f"UPDATE {STORE.t('users')} SET connects_balance=connects_balance+%s WHERE id=%s",
            (connects_needed, uid),
        )
        audit_log(uid, "CONNECTS_ROLLBACK", "application", None, details=f"Job ID: {job_id}, Amount: {connects_needed}")
        flash("Failed to submit application. Please try again.", "error")
        return redirect(url_for("worker_job_detail", slug_or_id=job.get("slug") or job_id))
    
    # If it's a robot job, systematically outbid the applicant
    if job.get("is_robot"):
        bot_info = STORE.query_one(f"SELECT connects_shown FROM {STORE.t('robot_winners')} WHERE job_id=%s", (job_id,))
        if bot_info and bid_connects >= bot_info["connects_shown"]:
            import random
            STORE.execute(f"UPDATE {STORE.t('robot_winners')} SET connects_shown=%s WHERE job_id=%s", 
                          (bid_connects + random.randint(5, 20), job_id))

    _refresh_session(uid)
    audit_log(uid, "APPLICATION_SUBMITTED", "application", app_id, details=f"Job ID: {job_id}")
    
    # Send email notification to applicant
    user_email = user["email"]
    job_title = job.get("title", "Unknown Job")
    email_html = f"""
    <h2>Application Submitted</h2>
    <p>Hi {user.get("full_name", "there")},</p>
    <p>Your application for <strong>{job_title}</strong> has been successfully submitted.</p>
    <p><strong>Bid Amount:</strong> {connects_needed} connects</p>
    <p>Please await the employer's response. You can track your applications in your <a href="{request.host_url}worker/dashboard">dashboard</a>.</p>
    <hr>
    <p style="color: #666; font-size: 0.9rem;">Need help? <a href="{request.host_url}pages/support">Contact support</a></p>
    """
    try:
        send_email(user_email, f"Application Submitted: {job_title}", email_html)
    except Exception as e:
        print(f"Warning: Failed to send application notification email: {e}")
    
    flash(f"Application submitted! {connects_needed} connects used. Check your dashboard.", "success")
    return redirect(url_for("worker_dashboard"))


@app.route("/worker/report-job", methods=["POST"])
@worker_required
def worker_report_job():
    """Report a job as fake or suspicious."""
    if not request.is_json:
        return jsonify({"success": False, "message": "Invalid request"}), 400
    
    # Verify CSRF for JSON requests
    expected = session.get("csrf_token")
    provided = request.json.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not (expected and provided and str(expected) == str(provided)):
        return jsonify({"success": False, "message": "Invalid CSRF token"}), 403
    
    try:
        job_id = int(request.json.get("job_id") or 0)
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "Invalid job_id"}), 400
    
    reason = request.json.get("reason", "").strip()
    details = request.json.get("details", "").strip()[:1000]
    uid = session["user_id"]
    
    if not job_id or not reason:
        return jsonify({"success": False, "message": "Missing required fields"}), 400
    
    # Verify job exists
    job = STORE.query_one(f"SELECT id FROM {STORE.t('jobs')} WHERE id=%s", (job_id,))
    if not job:
        return jsonify({"success": False, "message": "Job not found"}), 404
    
    # Check if user already reported this job
    existing = STORE.query_one(
        f"SELECT id FROM {STORE.t('job_reports')} WHERE job_id=%s AND reporter_id=%s",
        (job_id, uid)
    )
    if existing:
        return jsonify({"success": False, "message": "You've already reported this job"}), 400
    
    # Insert report
    try:
        report_id = STORE.execute(
            f"INSERT INTO {STORE.t('job_reports')} (job_id, reporter_id, reason, details) "
            f"VALUES (%s, %s, %s, %s)",
            (job_id, uid, reason, details)
        )
        audit_log(uid, "JOB_REPORTED", "job_report", report_id, details=f"Job ID: {job_id}, Reason: {reason}")
        LOG.info(f"Job {job_id} reported by user {uid}: {reason}")
        return jsonify({"success": True, "message": "Report submitted"}), 200
    except Exception as e:
        LOG.error(f"Error reporting job: {e}")
        return jsonify({"success": False, "message": "Failed to submit report"}), 500


@app.route("/worker/connects")
@worker_required
def worker_buy_connects():
    uid = session["user_id"]
    # Grant monthly bonus if eligible
    _grant_monthly_bonus(uid)
    _refresh_session(uid)
    history = STORE.query_all(
        f"SELECT * FROM {STORE.t('payments')} WHERE user_id=%s ORDER BY created_at DESC LIMIT 20", (uid,))
    return render_template("worker/buy_connects.html",
                           packages=CONNECTS_PACKAGES,
                           paystack_pub=SETTINGS.paystack_public_key,
                           history=history)


@app.route("/worker/connects/checkout", methods=["POST"])
@worker_required
def worker_connects_checkout():
    if not verify_csrf():
        return jsonify({"error": "Invalid request"}), 403
    pkg_id   = request.json.get("package_id", "") if request.is_json else request.form.get("package_id", "")
    provider = request.json.get("provider", "paystack") if request.is_json else request.form.get("provider", "paystack")
    pkg = next((p for p in CONNECTS_PACKAGES if p["id"] == pkg_id), None)
    if not pkg:
        return jsonify({"error": "Invalid package"}), 400

    uid   = session["user_id"]
    email = session.get("email", "")
    ref   = f"tbm_{uid}_{pkg_id}_{secrets.token_hex(8)}"
    amount_usd = pkg["price_usd"]
    amount_kes = SETTINGS.usd_to_kes_amount(amount_usd)

    STORE.execute(
        f"INSERT INTO {STORE.t('payments')} (user_id, provider, payment_type, amount_usd, amount_kes, connects_awarded, status, reference) "
        f"VALUES (%s,%s,'connects',%s,%s,%s,'pending',%s)",
        (uid, provider, amount_usd, amount_kes, pkg["connects"], ref),
    )

    if provider == "paystack":
        cents = SETTINGS.usd_to_kes_cents(amount_usd)
        status, data = PAYSTACK.initialize(
            email=email, amount_cents=cents, reference=ref,
            callback_url=SETTINGS.paystack_callback_url, currency=SETTINGS.paystack_currency,
            metadata={"user_id": uid, "package_id": pkg_id},
        )
        if status == 200 and data.get("status"):
            return jsonify({"redirect_url": data["data"]["authorization_url"]})
        STORE.execute(
            f"UPDATE {STORE.t('payments')} SET status='failed' WHERE reference=%s AND status='pending'",
            (ref,),
        )
        return jsonify({"error": data.get("message", "Payment init failed")}), 400

    elif provider == "pesapal":
        token, err = PESAPAL.get_token()
        if err or not token:
            return jsonify({"error": "PesaPal auth failed"}), 500
        ipn_id, err2 = PESAPAL.register_ipn(token, SETTINGS.pesapal_ipn_url)
        if err2 or not ipn_id:
            return jsonify({"error": "PesaPal IPN registration failed"}), 500
        mobile = STORE.query_one(f"SELECT mobile FROM {STORE.t('users')} WHERE id=%s", (uid,))
        phone  = mobile["mobile"] if mobile else ""
        status, data = PESAPAL.submit_order(
            token=token, ipn_id=ipn_id, reference=ref, email=email,
            amount=amount_kes, callback_url=SETTINGS.pesapal_callback_url,
            currency=SETTINGS.pesapal_currency, phone=phone,
        )
        if status in (200, 201) and data.get("redirect_url"):
            return jsonify({"redirect_url": data["redirect_url"]})
        return jsonify({"error": data.get("message", "PesaPal order failed")}), 400

    return jsonify({"error": "Unknown provider"}), 400


# ═══════════════════════════════════════════════════════════════════════════════
# Notification Endpoints

@app.route("/api/worker/notifications", methods=["GET"])
@worker_required
def get_notifications():
    """Get user's unread notifications (JSON API)."""
    uid = session.get("user_id")
    p = STORE.prefix
    
    try:
        # Get unread notifications (last 20)
        notifs = STORE.query_all(
            f"""SELECT id, type, title, message, job_id, match_score, is_read, created_at
                FROM {p}notifications
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 20""",
            (uid,)
        )
        
        unread_count = STORE.query_one(
            f"SELECT COUNT(*) as cnt FROM {p}notifications WHERE user_id=%s AND is_read=0",
            (uid,)
        )["cnt"]
        
        return jsonify({
            "notifications": notifs,
            "unread_count": unread_count
        })
    except Exception as e:
        LOG.error(f"Failed to fetch notifications: {e}")
        return jsonify({"error": "Failed to fetch notifications"}), 500

@app.route("/api/worker/notifications/<int:notif_id>/read", methods=["POST"])
@worker_required
def mark_notification_read(notif_id):
    """Mark a notification as read."""
    uid = session.get("user_id")
    p = STORE.prefix
    
    try:
        STORE.execute(
            f"UPDATE {p}notifications SET is_read=1 WHERE id=%s AND user_id=%s",
            (notif_id, uid)
        )
        return jsonify({"status": "ok"})
    except Exception as e:
        LOG.error(f"Failed to mark notification as read: {e}")
        return jsonify({"error": "Failed to update notification"}), 500

@app.route("/api/worker/notifications/clear-all", methods=["POST"])
@worker_required
def clear_all_notifications():
    """Mark all notifications as read."""
    uid = session.get("user_id")
    p = STORE.prefix
    
    try:
        STORE.execute(
            f"UPDATE {p}notifications SET is_read=1 WHERE user_id=%s",
            (uid,)
        )
        return jsonify({"status": "ok"})
    except Exception as e:
        LOG.error(f"Failed to clear notifications: {e}")
        return jsonify({"error": "Failed to clear notifications"}), 500

@app.route("/worker/notifications", methods=["GET", "POST"])
@worker_required
def worker_notifications():
    """Display worker's notifications page."""
    uid = session.get("user_id")
    p = STORE.prefix
    
    if request.method == "POST":
        action = request.form.get("action")
        if action == "mark_all_read":
            if verify_csrf():
                STORE.execute(
                    f"UPDATE {p}notifications SET is_read=1 WHERE user_id=%s",
                    (uid,)
                )
                flash("All notifications marked as read.", "info")
        return redirect(url_for("worker_notifications"))
    
    # Get notifications
    notifs = STORE.query_all(
        f"""SELECT id, type, title, message, job_id, match_score, is_read, created_at
            FROM {p}notifications
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 50""",
        (uid,)
    )
    
    unread_count = STORE.query_one(
        f"SELECT COUNT(*) as cnt FROM {p}notifications WHERE user_id=%s AND is_read=0",
        (uid,)
    )["cnt"]
    
    return render_template(
        "worker/notifications.html",
        notifications=notifs,
        unread_count=unread_count
    )

@app.route("/worker/settings", methods=["GET", "POST"])
@worker_required
def worker_settings():
    """Worker notification and email preferences."""
    uid = session.get("user_id")
    p = STORE.prefix
    
    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid request.", "error")
            return redirect(url_for("worker_settings"))
        
        # Update email preferences
        email_notifs = request.form.get("email_job_notifications") == "true"
        email_freq = request.form.get("email_notification_frequency", "instant")
        categories = request.form.get("categories", "").strip()
        
        if email_freq not in ['instant', 'daily', 'weekly']:
            email_freq = 'instant'
        
        try:
            STORE.execute(
                f"""UPDATE {p}users 
                   SET email_job_notifications=%s, 
                       email_notification_frequency=%s,
                       categories=%s
                   WHERE id=%s""",
                (email_notifs, email_freq, categories, uid)
            )
            audit_log(uid, f"Updated notification settings: freq={email_freq}, enabled={email_notifs}")
            flash("Notification settings updated.", "success")
        except Exception as e:
            LOG.error(f"Failed to update settings: {e}")
            flash("Failed to update settings.", "error")
        
        return redirect(url_for("worker_settings"))
    
    # Get current settings
    user = STORE.query_one(
        f"""SELECT email_job_notifications, email_notification_frequency, categories, skills
            FROM {p}users WHERE id=%s""",
        (uid,)
    )
    
    return render_template(
        "worker/settings.html",
        user=user,
        categories=JOB_CATEGORIES
    )


@app.route("/worker/profile")
@worker_required
def worker_profile():
    uid  = session["user_id"]
    _refresh_session(uid)  # Ensure session is fresh
    user = STORE.query_one(f"SELECT * FROM {STORE.t('users')} WHERE id=%s", (uid,))
    apps = STORE.query_all(
        f"SELECT a.*, j.title, j.category, j.slug FROM {STORE.t('applications')} a "
        f"JOIN {STORE.t('jobs')} j ON j.id=a.job_id WHERE a.user_id=%s ORDER BY a.applied_at DESC",
        (uid,),
    )
    skills = json.loads(user["skills"] or "[]") if user and user.get("skills") else []
    if user:
        user = dict(user)
        user["profile_pic_url"] = rewrite_pic_url(user.get("profile_pic_url"))
    return render_template("worker/profile.html", user=user, apps=apps, skills=skills)


# ═══════════════════════════════════════════════════════════════════════════════
# Employer Routes
# ═══════════════════════════════════════════════════════════════════════════════
def _employer_check_subscription(uid: int) -> bool:
    import datetime
    ep = STORE.query_one(f"SELECT * FROM {STORE.t('employer_profiles')} WHERE user_id=%s", (uid,))
    if not ep:
        return False
    if not ep["is_subscribed"]:
        return False
    exp = ep.get("subscription_expires_at")
    if exp and exp < datetime.datetime.utcnow():
        STORE.execute(f"UPDATE {STORE.t('employer_profiles')} SET is_subscribed=0 WHERE user_id=%s", (uid,))
        return False
    return True


def _get_employer_subscription_info(uid: int) -> dict:
    """Get detailed subscription info for employer (Option B model).
    
    Checks both:
    1. subscriptions table (paid subscriptions)
    2. employer_profiles table (free trial period)
    
    Returns:
    {
        'has_subscription': bool,
        'tier': str ('starter', 'basic', 'pro', or None),
        'expires_at': datetime or None,
        'jobs_posted_this_month': int,
        'job_limit': int or None (None = unlimited),
        'can_post': bool,
        'message': str (reason if can't post)
    }
    """
    import datetime
    
    # Tier definitions (job limit per month, can use AI)
    tier_config = {
        'starter': {'limit': None, 'ai': False},    # PAYG - no limit, no AI
        'basic': {'limit': 10, 'ai': False},        # $8/mo - 10 jobs/month
        'pro': {'limit': None, 'ai': True},         # $18/mo - unlimited + AI
    }
    
    # First check: Paid subscription in subscriptions table
    sub = STORE.query_one(
        f"SELECT * FROM {STORE.t('subscriptions')} WHERE employer_id=%s AND status='confirmed' ORDER BY created_at DESC LIMIT 1",
        (uid,)
    )
    
    tier = None
    expires_at = None
    
    if sub and sub.get('status') == 'confirmed':
        tier = sub.get('tier', 'basic')
        expires_at = sub.get('expires_at')
        
        # Check if subscription expired
        if expires_at and expires_at < datetime.datetime.utcnow():
            STORE.execute(
                f"UPDATE {STORE.t('employer_profiles')} SET is_subscribed=0 WHERE user_id=%s",
                (uid,)
            )
            return {
                'has_subscription': False,
                'tier': None,
                'expires_at': None,
                'jobs_posted_this_month': 0,
                'job_limit': 0,
                'can_post': False,
                'message': 'Subscription expired'
            }
    else:
        # Second check: Free trial in employer_profiles table
        ep = STORE.query_one(
            f"SELECT subscription_expires_at FROM {STORE.t('employer_profiles')} WHERE user_id=%s",
            (uid,)
        )
        
        if ep and ep.get('subscription_expires_at'):
            trial_expiry = ep.get('subscription_expires_at')
            if trial_expiry > datetime.datetime.utcnow():
                # Trial is still active - treat as free starter tier (unlimited jobs, no AI)
                tier = 'starter'
                expires_at = trial_expiry
            else:
                # Trial expired - clear it
                STORE.execute(
                    f"UPDATE {STORE.t('employer_profiles')} SET is_subscribed=0 WHERE user_id=%s",
                    (uid,)
                )
                return {
                    'has_subscription': False,
                    'tier': None,
                    'expires_at': None,
                    'jobs_posted_this_month': 0,
                    'job_limit': 0,
                    'can_post': False,
                    'message': 'Free trial expired. Subscribe to post more jobs.'
                }
        else:
            # No subscription and no trial
            return {
                'has_subscription': False,
                'tier': None,
                'expires_at': None,
                'jobs_posted_this_month': 0,
                'job_limit': 0,
                'can_post': False,
                'message': 'No active subscription'
            }
    
    # Get jobs posted this month
    month_start = datetime.datetime(datetime.datetime.utcnow().year, datetime.datetime.utcnow().month, 1)
    jobs_this_month = STORE.query_one(
        f"SELECT COUNT(*) as cnt FROM {STORE.t('jobs')} WHERE employer_id=%s AND created_at >= %s",
        (uid, month_start)
    )
    jobs_this_month = jobs_this_month['cnt'] if jobs_this_month else 0
    
    config = tier_config.get(tier, tier_config['basic'])
    job_limit = config['limit']
    can_post = job_limit is None or jobs_this_month < job_limit
    message = "" if can_post else f"Job limit reached ({jobs_this_month}/{job_limit} this month)"
    
    return {
        'has_subscription': True,
        'tier': tier,
        'expires_at': expires_at,
        'jobs_posted_this_month': jobs_this_month,
        'job_limit': job_limit,
        'can_post': can_post,
        'message': message,
        'can_use_ai': config['ai']
    }


def _enhance_job_description_with_ai(title: str, budget_usd: float, category: str, existing_description: str = "") -> str | None:
    """Enhance job description using Gemini API (Pro tier feature).
    
    Takes an existing or partial description and enhances it with:
    - Better structure
    - More professional language
    - Key requirements clarity
    
    Returns enhanced description or None on failure.
    """
    if not SETTINGS.gemini_api_key:
        LOG.warning("AI enhancement: GEMINI_API_KEY not set")
        return None
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=SETTINGS.gemini_api_key)
        model = genai.GenerativeModel(SETTINGS.gemini_model)
        
        prompt = (
            f"You are a professional job posting expert. Enhance this job description to be more compelling.\n\n"
            f"Job Title: {title}\n"
            f"Category: {category}\n"
            f"Budget: ${budget_usd}\n\n"
            f"Current Description:\n{existing_description}\n\n"
            f"Please enhance the description with:\n"
            f"1. Clear Overview (1-2 sentences)\n"
            f"2. Project Scope (3-4 key points)\n"
            f"3. Responsibilities (3-4 tasks)\n"
            f"4. Requirements (3-4 required skills)\n"
            f"5. Nice to Have (1-2 bonus items)\n\n"
            f"Keep it concise (250-350 words). Use bullet points. Make it professional and compelling.\n"
            f"Return ONLY the enhanced description, no markdown formatting."
        )
        
        resp = model.generate_content(prompt)
        enhanced = resp.text.strip() if resp.text else None
        return enhanced
        
    except Exception as e:
        LOG.error("AI enhancement failed: %s", e)
        return None


@app.route("/employer/dashboard")
@employer_required
def employer_dashboard():
    uid  = session["user_id"]
    user = STORE.query_one(f"SELECT * FROM {STORE.t('users')} WHERE id=%s", (uid,))
    ep   = STORE.query_one(f"SELECT * FROM {STORE.t('employer_profiles')} WHERE user_id=%s", (uid,))
    is_sub = _employer_check_subscription(uid)
    
    # Fetch current subscription tier
    current_sub = None
    if is_sub:
        current_sub = STORE.query_one(
            f"SELECT * FROM {STORE.t('subscriptions')} WHERE employer_id=%s AND status='confirmed' ORDER BY created_at DESC LIMIT 1",
            (uid,)
        )
    
    my_jobs = STORE.query_all(
        f"SELECT j.*, (SELECT COUNT(*) FROM {STORE.t('applications')} a WHERE a.job_id=j.id) as app_count "
        f"FROM {STORE.t('jobs')} j WHERE j.employer_id=%s ORDER BY j.created_at DESC LIMIT 10", (uid,))
    pending_pays = STORE.query_all(
        f"SELECT * FROM {STORE.t('employer_payments')} WHERE employer_id=%s AND status='pending'", (uid,))
    return render_template("employer/dashboard.html", user=user, ep=ep,
                           is_subscribed=is_sub, my_jobs=my_jobs, pending_pays=pending_pays, current_sub=current_sub)


@app.route("/employer/buy-single-job", methods=["POST"])
@employer_required
def employer_buy_single_job():
    """Buy a single job posting via PAYG (pay-as-you-go) mechanism."""
    if not verify_csrf():
        if request.is_json:
            return jsonify({"error": "Invalid request"}), 403
        flash("Invalid request.", "error")
        return redirect(url_for("employer_post_job"))
    
    uid = session.get("user_id")
    email = session.get("email", "")
    
    # Get job data from request
    if request.is_json:
        data = request.get_json() or {}
        title = data.get("title", "").strip()[:255]
        desc = data.get("description", "").strip()[:3000]
        category = data.get("category", "")
        jtype = data.get("job_type", "fixed")
        budget = float(data.get("budget_usd", 0) or 0)
        duration = data.get("duration", "").strip()[:80]
        use_ai = data.get("use_ai") == "true"
    else:
        title = request.form.get("title", "").strip()[:255]
        desc = request.form.get("description", "").strip()[:3000]
        category = request.form.get("category", "")
        jtype = request.form.get("job_type", "fixed")
        budget = float(request.form.get("budget_usd", 0) or 0)
        duration = request.form.get("duration", "").strip()[:80]
        use_ai = request.form.get("use_ai") == "true"
    
    # Validate job data
    if not title or category not in JOB_CATEGORIES or jtype not in JOB_TYPES or budget <= 0:
        if request.is_json:
            return jsonify({"error": "Invalid job data"}), 400
        flash("Please fill in all required fields correctly.", "error")
        return redirect(url_for("employer_post_job"))
    
    # Create payment record for single job ($2.99)
    ref = f"payg_{uid}_{secrets.token_hex(8)}"
    
    # Store job data in subscription record's metadata (as JSON string)
    job_data = json.dumps({
        "title": title,
        "description": desc,
        "category": category,
        "job_type": jtype,
        "budget_usd": budget,
        "duration": duration,
        "use_ai": use_ai,
    })
    
    STORE.execute(
        f"INSERT INTO {STORE.t('subscriptions')} (employer_id, reference, amount_usd, tier, status, metadata) VALUES (%s,%s,%s,%s,'pending',%s)",
        (uid, ref, EMPLOYER_PAYG_JOB_PRICE, "starter", job_data),
    )
    
    # Initialize Paystack payment
    cents = SETTINGS.usd_to_kes_cents(EMPLOYER_PAYG_JOB_PRICE)
    status, data = PAYSTACK.initialize(
        email=email, amount_cents=cents, reference=ref,
        callback_url=SETTINGS.paystack_callback_url, currency=SETTINGS.paystack_currency,
        metadata={"type": "payg_job", "employer_id": uid},
    )
    
    if status == 200 and data.get("status"):
        if request.is_json:
            return jsonify({"redirect_url": data["data"]["authorization_url"]})
        return redirect(data["data"]["authorization_url"])
    
    STORE.execute(
        f"UPDATE {STORE.t('subscriptions')} SET status='failed' WHERE reference=%s",
        (ref,),
    )
    
    if request.is_json:
        return jsonify({"error": data.get("message", "Payment init failed")}), 400
    flash("Payment initialization failed. Please try again.", "error")
    return redirect(url_for("employer_post_job"))


@app.route("/employer/subscribe", methods=["POST"])
@employer_required
def employer_subscribe():
    if not verify_csrf():
        return jsonify({"error": "Invalid request"}), 403
    uid   = session["user_id"]
    email = session.get("email", "")
    
    # Support both JSON (from pricing page) and form POST (from old code)
    if request.is_json:
        data = request.get_json()
        tier = data.get("tier", "").strip().lower()
        amount_usd = data.get("amount_usd", 0)
        provider = "paystack"  # Default for pricing page
        
        # Validate tier and amount (Option B pricing model)
        # starter: $2.99/job (pay-as-you-go, no subscription)
        # basic: $8/month with 10 jobs
        # pro: $18/month unlimited + AI features
        valid_tiers = {"starter": 2.99, "basic": 8, "pro": 18}
        if tier not in valid_tiers or valid_tiers[tier] != amount_usd:
            return jsonify({"error": "Invalid subscription tier or amount"}), 400
    else:
        # Old provider-based approach (backwards compatibility)
        tier = None
        provider = request.form.get("provider", "paystack")
        amount_usd = EMPLOYER_SUB_USD
    
    ref = f"sub_{uid}_{secrets.token_hex(8)}"

    STORE.execute(
        f"INSERT INTO {STORE.t('subscriptions')} (employer_id, reference, amount_usd, tier, status) VALUES (%s,%s,%s,%s,'pending')",
        (uid, ref, amount_usd, tier),
    )
    if provider == "paystack":
        cents = SETTINGS.usd_to_kes_cents(amount_usd)
        status, data = PAYSTACK.initialize(
            email=email, amount_cents=cents, reference=ref,
            callback_url=SETTINGS.paystack_callback_url, currency=SETTINGS.paystack_currency,
            metadata={"type": "subscription", "employer_id": uid, "tier": tier} if tier else {"type": "subscription", "employer_id": uid},
        )
        if status == 200 and data.get("status"):
            return jsonify({"redirect_url": data["data"]["authorization_url"]})
        STORE.execute(
            f"UPDATE {STORE.t('subscriptions')} SET status='failed' WHERE reference=%s AND status='pending'",
            (ref,),
        )
        return jsonify({"error": data.get("message", "Payment init failed")}), 400
    return jsonify({"error": "Use paystack for subscriptions"}), 400


@app.route("/employer/pricing")
def employer_pricing():
    """Employer subscription pricing page - Option B model."""
    uid = session.get("user_id")
    current_sub = None
    sub_info = None
    
    if uid:
        current_sub = STORE.query_one(
            f"SELECT * FROM {STORE.t('subscriptions')} WHERE employer_id=%s AND status='confirmed' ORDER BY created_at DESC LIMIT 1",
            (uid,)
        )
        sub_info = _get_employer_subscription_info(uid)
    
    # Option B pricing tiers
    tiers = [
        {
            "name": "Pay-As-You-Go",
            "price": 2.99,
            "period": "per job",
            "color": "#2196F3",
            "tier_id": "starter",
            "features": [
                "Post jobs individually",
                "No subscription required",
                "$2.99 per job posting",
                "Access to freelancer profiles",
                "Basic messaging",
                "Email support",
            ],
            "best_for": "One-time or occasional hiring",
        },
        {
            "name": "Basic",
            "price": 8,
            "period": "month",
            "color": "#FF9800",
            "tier_id": "basic",
            "featured": True,
            "features": [
                "Everything in Pay-As-You-Go",
                "Post up to 10 jobs per month",
                "Saves $2.91/job vs PAYG",
                "Advanced filtering & saved searches",
                "Contract template library",
                "Dispute resolution assistance",
                "Phone support (business hours)",
            ],
            "best_for": "Active hiring teams",
        },
        {
            "name": "Pro",
            "price": 18,
            "period": "month",
            "color": "#4CAF50",
            "tier_id": "pro",
            "features": [
                "Everything in Basic",
                "Unlimited job postings",
                "AI-powered job descriptions",
                "Dedicated account manager",
                "Custom payment terms",
                "Priority 24/7 email support",
            ],
            "best_for": "Growing recruitment operations",
        },
    ]
    
    return render_template("employer/pricing.html", 
                         tiers=tiers, 
                         current_sub=current_sub, 
                         sub_info=sub_info,
                         uid=uid)


@app.route("/employer/jobs/post", methods=["GET", "POST"])
@employer_required
@profile_required
def employer_post_job():
    uid = session["user_id"]
    
    # Check subscription using new tier system
    sub_info = _get_employer_subscription_info(uid)
    
    if not sub_info['can_post']:
        flash(f"Cannot post job: {sub_info['message']} Subscribe to post more jobs.", "warning")
        return redirect(url_for("employer_pricing"))
    
    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid request.", "error"); return redirect(url_for("employer_post_job"))
        title    = request.form.get("title", "").strip()[:255]
        desc     = request.form.get("description", "").strip()[:3000]
        category = request.form.get("category", "")
        jtype    = request.form.get("job_type", "fixed")
        budget   = float(request.form.get("budget_usd", 0) or 0)
        duration = request.form.get("duration", "").strip()[:80]
        use_ai   = request.form.get("use_ai") == "true"
        
        # Hourly job fields
        hourly_rate = None
        max_hours = None
        if jtype == "hourly":
            try:
                hourly_rate = float(request.form.get("hourly_rate", 0) or 0)
                max_hours = int(request.form.get("max_hours", 0) or 0)
                if hourly_rate <= 0 or max_hours <= 0:
                    flash("Hourly rate and max hours must be positive values.", "error")
                    return redirect(url_for("employer_post_job"))
                # Override budget_usd with calculated amount (hourly_rate * max_hours)
                budget = hourly_rate * max_hours
            except (ValueError, TypeError):
                flash("Invalid hourly rate or max hours value.", "error")
                return redirect(url_for("employer_post_job"))
        
        # Daily job fields
        daily_rate = None
        max_days = None
        if jtype == "daily":
            try:
                daily_rate = float(request.form.get("daily_rate", 0) or 0)
                max_days = int(request.form.get("max_days", 0) or 0)
                if daily_rate <= 0 or max_days <= 0:
                    flash("Daily rate and max days must be positive values.", "error")
                    return redirect(url_for("employer_post_job"))
                # Override budget_usd with calculated amount (daily_rate * max_days)
                budget = daily_rate * max_days
            except (ValueError, TypeError):
                flash("Invalid daily rate or max days value.", "error")
                return redirect(url_for("employer_post_job"))
        
        if not title or category not in JOB_CATEGORIES or jtype not in JOB_TYPES:
            flash("Please fill in all required fields correctly.", "error")
            return redirect(url_for("employer_post_job"))
        
        # AI enhancement available only for Pro tier
        if use_ai and sub_info['tier'] == 'pro':
            enhanced_desc = _enhance_job_description_with_ai(title, budget, category, desc)
            if enhanced_desc:
                desc = enhanced_desc
                LOG.info("Job description enhanced with AI for employer %d", uid)
            
        # Calculate connects_required using tiered calculator
        conn_req = _calculate_connects_required(budget)

        # Insert job with hourly fields if applicable
        if jtype == "hourly":
            job_id = STORE.execute(
                f"INSERT INTO {STORE.t('jobs')} (employer_id, title, description, category, job_type, budget_usd, duration, connects_required, hourly_rate, max_hours, is_robot, status) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,'open')",
                (uid, title, desc, category, jtype, budget, duration, conn_req, hourly_rate, max_hours),
            )
        # Insert job with daily fields if applicable
        elif jtype == "daily":
            job_id = STORE.execute(
                f"INSERT INTO {STORE.t('jobs')} (employer_id, title, description, category, job_type, budget_usd, duration, connects_required, daily_rate, max_days, is_robot, status) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,'open')",
                (uid, title, desc, category, jtype, budget, duration, conn_req, daily_rate, max_days),
            )
        else:
            job_id = STORE.execute(
                f"INSERT INTO {STORE.t('jobs')} (employer_id, title, description, category, job_type, budget_usd, duration, connects_required, is_robot, status) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,'open')",
                (uid, title, desc, category, jtype, budget, duration, conn_req),
            )
        
        # Generate and set slug for the job (includes job_id for uniqueness)
        if job_id:
            slug = _generate_job_slug(title, job_id)
            STORE.execute(f"UPDATE {STORE.t('jobs')} SET slug=%s WHERE id=%s", (slug, job_id))
        
        flash("Job posted successfully!", "success")
        return redirect(url_for("employer_applicants", job_id=job_id))
    
    # Show current subscription status on GET
    return render_template("employer/post_job.html", 
                         categories=JOB_CATEGORIES, 
                         job_types=JOB_TYPES,
                         sub_info=sub_info)


@app.route("/employer/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@employer_required
@profile_required
def employer_edit_job(job_id):
    uid = session["user_id"]
    job = STORE.query_one(f"SELECT * FROM {STORE.t('jobs')} WHERE id=%s AND employer_id=%s", (job_id, uid))
    if not job:
        flash("Job not found or you don't have permission to edit it.", "error")
        return redirect(url_for("employer_dashboard"))
    
    if job.get("is_robot"):
        flash("Cannot edit AI-generated jobs.", "warning")
        return redirect(url_for("employer_dashboard"))
    
    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid request.", "error")
            return redirect(url_for("employer_edit_job", job_id=job_id))
        
        title    = request.form.get("title", "").strip()[:255]
        desc     = request.form.get("description", "").strip()[:3000]
        category = request.form.get("category", "")
        jtype    = request.form.get("job_type", job["job_type"])
        duration = request.form.get("duration", "").strip()[:80]
        budget   = float(request.form.get("budget_usd", 0) or 0)
        
        # Hourly job fields
        hourly_rate = None
        max_hours = None
        if jtype == "hourly":
            try:
                hourly_rate = float(request.form.get("hourly_rate", 0) or 0)
                max_hours = int(request.form.get("max_hours", 0) or 0)
                if hourly_rate <= 0 or max_hours <= 0:
                    flash("Hourly rate and max hours must be positive values.", "error")
                    return redirect(url_for("employer_edit_job", job_id=job_id))
                budget = hourly_rate * max_hours
            except (ValueError, TypeError):
                flash("Invalid hourly rate or max hours value.", "error")
                return redirect(url_for("employer_edit_job", job_id=job_id))
        
        # Daily job fields
        daily_rate = None
        max_days = None
        if jtype == "daily":
            try:
                daily_rate = float(request.form.get("daily_rate", 0) or 0)
                max_days = int(request.form.get("max_days", 0) or 0)
                if daily_rate <= 0 or max_days <= 0:
                    flash("Daily rate and max days must be positive values.", "error")
                    return redirect(url_for("employer_edit_job", job_id=job_id))
                budget = daily_rate * max_days
            except (ValueError, TypeError):
                flash("Invalid daily rate or max days value.", "error")
                return redirect(url_for("employer_edit_job", job_id=job_id))
        
        if not title or category not in JOB_CATEGORIES or jtype not in JOB_TYPES:
            flash("Please fill in all required fields correctly.", "error")
            return redirect(url_for("employer_edit_job", job_id=job_id))
        
        # Calculate new connects_required based on updated budget
        conn_req = _calculate_connects_required(budget)
        
        # Update job
        if jtype == "hourly":
            STORE.execute(
                f"UPDATE {STORE.t('jobs')} SET title=%s, description=%s, category=%s, job_type=%s, duration=%s, budget_usd=%s, connects_required=%s, hourly_rate=%s, max_hours=%s WHERE id=%s",
                (title, desc, category, jtype, duration, budget, conn_req, hourly_rate, max_hours, job_id),
            )
        elif jtype == "daily":
            STORE.execute(
                f"UPDATE {STORE.t('jobs')} SET title=%s, description=%s, category=%s, job_type=%s, duration=%s, budget_usd=%s, connects_required=%s, daily_rate=%s, max_days=%s WHERE id=%s",
                (title, desc, category, jtype, duration, budget, conn_req, daily_rate, max_days, job_id),
            )
        else:
            STORE.execute(
                f"UPDATE {STORE.t('jobs')} SET title=%s, description=%s, category=%s, job_type=%s, duration=%s, budget_usd=%s, connects_required=%s WHERE id=%s",
                (title, desc, category, jtype, duration, budget, conn_req, job_id),
            )
        
        # Regenerate slug if title changed (updated_at will auto-update via TIMESTAMP)
        if title != job["title"]:
            slug = _generate_job_slug(title, job_id)
            STORE.execute(f"UPDATE {STORE.t('jobs')} SET slug=%s WHERE id=%s", (slug, job_id))
        
        audit_log(uid, "JOB_EDITED", "job", job_id, details=f"Edited: title, category, type, pricing")
        flash("Job updated successfully!", "success")
        return redirect(url_for("employer_view_job", job_id=job_id))
    
    # GET - show edit form pre-populated with current job data
    return render_template("employer/edit_job.html", job=job, categories=JOB_CATEGORIES, job_types=JOB_TYPES)


@app.route("/employer/jobs/<int:job_id>")
@employer_required
def employer_view_job(job_id):
    """Employer views details of their own job."""
    uid = session["user_id"]
    job = STORE.query_one(f"SELECT * FROM {STORE.t('jobs')} WHERE id=%s AND employer_id=%s", (job_id, uid))
    if not job:
        flash("Job not found.", "error")
        return redirect(url_for("employer_dashboard"))
    return render_template("employer/job_detail.html", job=job)


@app.route("/employer/jobs/<int:job_id>/applicants")
@employer_required
def employer_applicants(job_id):
    uid = session["user_id"]
    job = STORE.query_one(f"SELECT * FROM {STORE.t('jobs')} WHERE id=%s AND employer_id=%s", (job_id, uid))
    if not job:
        flash("Job not found.", "error"); return redirect(url_for("employer_dashboard"))
    applicants = STORE.query_all(
        f"SELECT a.*, u.id as user_id, u.full_name, u.country, u.specialty, u.connects_balance, u.profile_pic_url, u.skills, u.bio, u.portfolio_links "
        f"FROM {STORE.t('applications')} a JOIN {STORE.t('users')} u ON u.id=a.user_id "
        f"WHERE a.job_id=%s ORDER BY a.connects_spent DESC", (job_id,))
    for ap in applicants:
        ap["skills_list"] = json.loads(ap["skills"] or "[]") if ap.get("skills") else []
        ap["profile_pic_url"] = rewrite_pic_url(ap.get("profile_pic_url"))
        ap["application_links"] = _parse_link_blob(ap.get("attachment_links"))
        # Fetch worker stats
        ap["worker_stats"] = _get_worker_stats(ap.get("user_id"))
    return render_template("employer/applicants.html", job=job, applicants=applicants)


@app.route("/employer/applicants/<int:app_id>/accept", methods=["POST"])
@employer_required
def employer_accept_applicant(app_id):
    if not verify_csrf():
        flash("Invalid request.", "error"); return redirect(url_for("employer_dashboard"))
    uid = session["user_id"]
    application = STORE.query_one(
        f"SELECT a.*, j.employer_id, j.id as job_id FROM {STORE.t('applications')} a "
        f"JOIN {STORE.t('jobs')} j ON j.id=a.job_id WHERE a.id=%s", (app_id,))
    if not application or application["employer_id"] != uid:
        flash("Not found.", "error"); return redirect(url_for("employer_dashboard"))
    # Accept selected application and close the job.
    STORE.execute(f"UPDATE {STORE.t('applications')} SET status='accepted' WHERE id=%s", (app_id,))
    STORE.execute(f"UPDATE {STORE.t('jobs')} SET status='closed' WHERE id=%s", (application["job_id"],))
    
    # Mark all other pending applications as rejected and refund connects
    rejected_apps = STORE.query_all(
        f"SELECT user_id, connects_spent FROM {STORE.t('applications')} "
        f"WHERE job_id=%s AND id<>%s AND status='pending'",
        (application["job_id"], app_id),
    )
    for rejected_app in rejected_apps:
        # Refund 3 connects to rejected applicants
        STORE.execute(
            f"UPDATE {STORE.t('users')} SET connects_balance=connects_balance+%s WHERE id=%s",
            (APPLICATION_REFUND, rejected_app["user_id"]),
        )
        audit_log(rejected_app["user_id"], "APPLICATION_REJECTED_REFUND", "application", app_id, 
                  details=f"Job ID: {application['job_id']}, Refund: {APPLICATION_REFUND}")
        push_notif(rejected_app["user_id"], 
                   f"Your application for this job was not selected. {APPLICATION_REFUND} connects have been refunded.")
    
    # Update rejected applications status
    STORE.execute(
        f"UPDATE {STORE.t('applications')} SET status='rejected' "
        f"WHERE job_id=%s AND id<>%s AND status='pending'",
        (application["job_id"], app_id),
    )
    
    # Create contract from job acceptance (Phase 4 integration)
    contract_id = create_contract_from_job(application["job_id"], application["user_id"])
    if not contract_id:
        LOG.warning("Failed to create contract for job %s", application["job_id"])
    
    push_notif(
        application["user_id"],
        "Congratulations! Your application was accepted. Review the contract offer once the employer sends it.",
        url_for("contract_room", job_id=application["job_id"]),
    )
    send_email(
        STORE.query_one(f"SELECT email FROM {STORE.t('users')} WHERE id=%s", (application["user_id"],))["email"],
        "Application Accepted — TechBid",
        "<h2>Great news!</h2><p>Your application has been accepted. The employer will contact you shortly.</p>"
    )
    flash("Applicant hired! Contract room generated.", "success")
    return redirect(url_for("contract_room", job_id=application["job_id"]))


@app.route("/employer/payments")
@employer_required
def employer_payments():
    uid = session["user_id"]
    pays = STORE.query_all(
        f"SELECT ep.*, COALESCE(u.full_name, 'Unknown') as worker_name, COALESCE(j.title, 'Deleted Job') as job_title "
        f"FROM {STORE.t('employer_payments')} ep "
        f"LEFT JOIN {STORE.t('users')} u ON u.id=ep.worker_user_id "
        f"LEFT JOIN {STORE.t('jobs')} j ON j.id=ep.job_id "
        f"WHERE ep.employer_id=%s ORDER BY ep.created_at DESC", (uid,))
    return render_template("employer/payments.html", pays=pays)


# ═══════════════════════════════════════════════════════════════════════════════
# Contract Room / Messaging
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/cdn/profile_pics/<path:filename>")
def cdn_profile_pics(filename):
    """Proxy private GitHub repository images back securely to the browser."""
    if not SETTINGS.github_token or not SETTINGS.github_repo:
        flask_abort(404)
        
    url = f"https://raw.githubusercontent.com/{SETTINGS.github_repo}/{SETTINGS.github_branch}/profile_pics/{filename}"
    headers = {"Authorization": f"token {SETTINGS.github_token}"}
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            resp = make_response(r.content)
            resp.headers['Content-Type'] = r.headers.get('Content-Type', 'image/jpeg')
            resp.headers['Cache-Control'] = 'public, max-age=86400'
            return resp
        else:
            flask_abort(404)
    except Exception:
        flask_abort(404)

@app.route("/contract/<int:job_id>")
@login_required
def contract_room(job_id):
    uid = session["user_id"]
    job, accepted_app, contract, employer_id, worker_id = _contract_room_access(job_id, uid)
    if not job:
        flash("Contract not found.", "error"); return redirect(url_for("index"))
    if not accepted_app:
        flash("Contract has not been awarded yet.", "warning"); return redirect(url_for("index"))
    if uid != employer_id and uid != worker_id:
        flash("You do not have access to this contract room.", "error"); return redirect(url_for("index"))
    
    # Create a new session for this user in this contract room (no timestamp tracking)
    session_id = _create_session_for_user(uid, job_id)
        
    messages = STORE.query_all(
        f"SELECT m.*, u.full_name, u.role FROM {STORE.t('messages')} m "
        f"JOIN {STORE.t('users')} u ON u.id=m.sender_id "
        f"WHERE m.job_id=%s ORDER BY m.created_at ASC", (job_id,))
        
    other_party_id = worker_id if uid == employer_id else employer_id
    other_party = STORE.query_one(f"SELECT full_name, profile_pic_url, role FROM {STORE.t('users')} WHERE id=%s", (other_party_id,))
    if other_party:
        other_party = dict(other_party)
        other_party["profile_pic_url"] = rewrite_pic_url(other_party.get("profile_pic_url"))
        other_party["online_status"] = _get_user_online_status(other_party_id, job_id)
    
    # Phase 4: Check if contract needs escrow funding
    is_employer = uid == employer_id
    funded_statuses = {"funded", "in_progress", "submitted", "under_review", "approved", "completed", "disputed", "refunded"}
    contract_funded = bool(contract and contract.get("status") in funded_statuses)
    offer_can_be_sent = bool(
        contract and is_employer and (
            (contract.get("status") == "pending" and not contract.get("offer_sent_at"))
            or contract.get("status") == "offer_rejected"
        )
    )
    awaiting_employer_offer = bool(contract and contract.get("status") == "pending" and not contract.get("offer_sent_at"))
    awaiting_freelancer_response = bool(contract and contract.get("status") == "pending" and contract.get("offer_sent_at"))
    offer_rejected = bool(contract and contract.get("status") == "offer_rejected")
    offer_accepted = bool(contract and contract.get("status") == "offer_accepted")
    
    return render_template("contract_room.html", job=job, contract=contract, messages=messages, 
                         other_party=other_party, uid=uid, is_employer=is_employer, 
                         contract_funded=contract_funded,
                         offer_can_be_sent=offer_can_be_sent,
                         awaiting_employer_offer=awaiting_employer_offer,
                         awaiting_freelancer_response=awaiting_freelancer_response,
                         offer_rejected=offer_rejected,
                         offer_accepted=offer_accepted,
                         contract_state_token=_contract_state_token(contract))

@app.route("/contract/<int:job_id>/msg", methods=["POST"])
@login_required
def contract_room_msg(job_id):
    if not verify_csrf():
        if request.is_json:
            return jsonify({"error": "Invalid request"}), 403
        flash("Invalid request.", "error"); return redirect(url_for("contract_room", job_id=job_id))
    
    uid = session["user_id"]
    
    # Keep-alive: update session to mark user as active
    _keepalive_session(uid, job_id)
    
    message = request.form.get("message", "").strip()
    if not message:
        if request.is_json:
            return jsonify({"error": "Message cannot be empty"}), 400
        return redirect(url_for("contract_room", job_id=job_id))
        
    job = STORE.query_one(f"SELECT employer_id FROM {STORE.t('jobs')} WHERE id=%s", (job_id,))
    app = STORE.query_one(f"SELECT user_id FROM {STORE.t('applications')} WHERE job_id=%s AND status='accepted'", (job_id,))
    
    if not job or not app or (uid != job["employer_id"] and uid != app["user_id"]):
        if request.is_json:
            return jsonify({"error": "Unauthorized"}), 403
        return redirect(url_for("index"))
    
    # Queue message for background processing (immediate return to user)
    try:
        msg_id = int(time.time() * 1000000) % 9999999  # Temporary ID for immediate display
        MESSAGE_QUEUE.put((job_id, uid, message), timeout=1)
        
        # Return immediately with success
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": True, "message_id": msg_id})
        return redirect(url_for("contract_room", job_id=job_id))
    except queue.Full:
        LOG.error(f"Message queue full for job_id={job_id}")
        if request.is_json:
            return jsonify({"error": "Message queue overloaded, please try again"}), 503
        flash("System busy, please try again.", "error")
        return redirect(url_for("contract_room", job_id=job_id))
    except Exception as e:
        LOG.error(f"Error queuing message: {e}")
        if request.is_json:
            return jsonify({"error": "Failed to queue message"}), 500
        flash("Error sending message.", "error")
        return redirect(url_for("contract_room", job_id=job_id))


@app.route("/api/contract/<int:job_id>/live")
@login_required
def contract_room_live(job_id):
    uid = session["user_id"]
    job, accepted_app, contract, employer_id, worker_id = _contract_room_access(job_id, uid)
    if not job or not accepted_app:
        return jsonify({"error": "Contract not found"}), 404
    if uid != employer_id and uid != worker_id:
        return jsonify({"error": "Unauthorized"}), 403

    # Keep-alive: update session (they're actively polling)
    _keepalive_session(uid, job_id)

    after_id = max(0, request.args.get("after_id", type=int) or 0)
    messages = STORE.query_all(
        f"SELECT m.*, u.full_name FROM {STORE.t('messages')} m "
        f"JOIN {STORE.t('users')} u ON u.id=m.sender_id "
        f"WHERE m.job_id=%s AND m.id>%s ORDER BY m.created_at ASC",
        (job_id, after_id),
    )
    
    # Get other party's online status
    other_party_id = worker_id if uid == employer_id else employer_id
    other_party_online_status = _get_user_online_status(other_party_id, job_id)
    
    # Get comprehensive contract state for live updates
    is_employer = uid == employer_id
    funded_statuses = {"funded", "in_progress", "submitted", "under_review", "approved", "completed", "disputed", "refunded"}
    contract_funded = bool(contract and contract.get("status") in funded_statuses)
    offer_can_be_sent = bool(
        contract and is_employer and (
            (contract.get("status") == "pending" and not contract.get("offer_sent_at"))
            or contract.get("status") == "offer_rejected"
        )
    )
    awaiting_employer_offer = bool(contract and contract.get("status") == "pending" and not contract.get("offer_sent_at"))
    awaiting_freelancer_response = bool(contract and contract.get("status") == "pending" and contract.get("offer_sent_at"))
    offer_rejected = bool(contract and contract.get("status") == "offer_rejected")
    offer_accepted = bool(contract and contract.get("status") == "offer_accepted")
    
    # Get work submission if exists
    work_submission = STORE.query_one(
        f"SELECT * FROM {STORE.t('work_submissions')} WHERE contract_id=%s",
        (contract.get("id"),)
    ) if contract else None
    
    # Get dispute if exists
    dispute = STORE.query_one(
        f"SELECT * FROM {STORE.t('disputes')} WHERE contract_id=%s",
        (contract.get("id"),)
    ) if contract else None
    
    # Format contract data for live updates
    contract_data = {}
    if contract:
        contract_data = {
            "id": contract.get("id"),
            "status": contract.get("status"),
            "price_usd": float(contract.get("price_usd") or 0),
            "offer_sent_at": contract.get("offer_sent_at").isoformat() if contract.get("offer_sent_at") else None,
            "offer_accepted_at": contract.get("offer_accepted_at").isoformat() if contract.get("offer_accepted_at") else None,
            "review_deadline": contract.get("review_deadline").isoformat() if contract.get("review_deadline") else None,
            "status_display": {
                "is_pending_offer": awaiting_employer_offer,
                "is_awaiting_freelancer": awaiting_freelancer_response,
                "is_accepted": offer_accepted,
                "is_rejected": offer_rejected,
                "is_funded": contract_funded,
                "can_send_offer": offer_can_be_sent,
            },
            "work_submission": {
                "exists": work_submission is not None,
                "submitted_at": work_submission.get("submitted_at").isoformat() if work_submission and work_submission.get("submitted_at") else None,
            } if work_submission else None,
            "dispute": {
                "exists": dispute is not None,
                "status": dispute.get("status") if dispute else None,
            } if dispute else None,
        }
    
    return jsonify({
        "state_token": _contract_state_token(contract),
        "messages": [_serialize_contract_message(message) for message in messages],
        "other_party_online": other_party_online_status,
        "contract": contract_data,
        "job_id": job_id,
        "timestamp": datetime.utcnow().isoformat(),
    })


@app.route("/contract/<int:job_id>/heartbeat", methods=["POST"])
@login_required
def contract_heartbeat(job_id):
    """Keep-alive endpoint: session-based online status (no timestamp comparison)."""
    uid = session["user_id"]
    
    # Verify user has access to this contract
    job, accepted_app, contract, employer_id, worker_id = _contract_room_access(job_id, uid)
    if not job or not accepted_app or (uid != employer_id and uid != worker_id):
        return jsonify({"error": "Unauthorized"}), 403
    
    # Keep-alive: update session (marks user active via keep-alive, not timestamps)
    _keepalive_session(uid, job_id)
    
    # Return other user's current online status (based on active sessions)
    other_party_id = worker_id if uid == employer_id else employer_id
    other_party_status = _get_user_online_status(other_party_id, job_id)
    
    return jsonify({"ok": True, "other_party_online": other_party_status})


# ═══════════════════════════════════════════════════════════════════════════════
# Contract & Escrow System
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/contract/<int:contract_id>")
@login_required
def view_contract(contract_id):
    """View contract details with state-aware UI."""
    uid = session["user_id"]
    contract = STORE.query_one(
        f"SELECT c.*, u1.full_name as employer_name, u2.full_name as freelancer_name "
        f"FROM {STORE.t('contracts')} c "
        f"JOIN {STORE.t('users')} u1 ON u1.id=c.employer_id "
        f"JOIN {STORE.t('users')} u2 ON u2.id=c.freelancer_id "
        f"WHERE c.id=%s", (contract_id,)
    )
    if not contract or (uid != contract["employer_id"] and uid != contract["freelancer_id"]):
        flash("Contract not found or access denied.", "error")
        return redirect(url_for("index"))
    
    work_submission = STORE.query_one(
        f"SELECT * FROM {STORE.t('work_submissions')} WHERE contract_id=%s",
        (contract_id,)
    )
    if work_submission:
        work_submission["deliverables_links"] = _normalize_deliverables(
            json.loads(work_submission.get("deliverables_links") or "[]")
        )
    
    dispute = STORE.query_one(
        f"SELECT * FROM {STORE.t('disputes')} WHERE contract_id=%s",
        (contract_id,)
    )
    
    return render_template("contract/view.html", contract=contract, work_submission=work_submission, 
                          dispute=dispute, current_user_is_employer=(uid == contract["employer_id"]))


@app.route("/contract/<int:contract_id>/offer", methods=["POST"])
@employer_required
def contract_send_offer(contract_id):
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("employer_dashboard"))

    uid = session["user_id"]
    contract = STORE.query_one(
        f"SELECT * FROM {STORE.t('contracts')} WHERE id=%s AND employer_id=%s",
        (contract_id, uid),
    )
    if not contract:
        flash("Contract not found.", "error")
        return redirect(url_for("employer_dashboard"))

    if contract["status"] in {"funded", "in_progress", "submitted", "under_review", "approved", "completed", "disputed", "refunded"}:
        flash("This contract is already active and can no longer be renegotiated here.", "warning")
        return redirect(url_for("contract_room", job_id=contract["job_id"]))

    amount = request.form.get("amount_usd", type=float)
    offer_note = request.form.get("offer_note", "").strip()[:500]
    if amount is None or amount <= 0:
        flash("Enter a valid contract amount before sending the offer.", "error")
        return redirect(url_for("contract_room", job_id=contract["job_id"]))

    STORE.execute(
        f"UPDATE {STORE.t('contracts')} SET price_usd=%s, escrow_amount_usd=%s, status='pending', "
        f"offer_note=%s, offer_sent_at=NOW(), offer_responded_at=NULL, offer_accepted_at=NULL, offer_rejected_at=NULL, "
        f"funding_reference=NULL, funded_at=NULL WHERE id=%s",
        (amount, amount, offer_note, contract_id),
    )
    log_contract_event(contract_id, "offer_sent", uid, {"amount_usd": amount, "note": offer_note})
    audit_log(uid, "CONTRACT_OFFER_SENT", "contract", contract_id, details=f"Amount USD: {amount:.2f}")
    push_notif(
        contract["freelancer_id"],
        f"New contract offer for contract #{contract_id}: ${amount:.2f}",
        url_for("contract_room", job_id=contract["job_id"]),
    )
    flash("Offer sent. The freelancer must accept it before you can fund escrow.", "success")
    return redirect(url_for("contract_room", job_id=contract["job_id"]))


@app.route("/contract/<int:contract_id>/offer/respond", methods=["POST"])
@worker_required
def contract_respond_offer(contract_id):
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("worker_dashboard"))

    uid = session["user_id"]
    action = request.form.get("action", "").strip().lower()
    contract = STORE.query_one(
        f"SELECT * FROM {STORE.t('contracts')} WHERE id=%s AND freelancer_id=%s",
        (contract_id, uid),
    )
    if not contract:
        flash("Contract not found.", "error")
        return redirect(url_for("worker_dashboard"))

    if contract["status"] != "pending" or not contract.get("offer_sent_at"):
        flash("There is no active employer offer waiting for your response.", "warning")
        return redirect(url_for("contract_room", job_id=contract["job_id"]))

    if action == "accept":
        STORE.execute(
            f"UPDATE {STORE.t('contracts')} SET status='offer_accepted', offer_responded_at=NOW(), offer_accepted_at=NOW() WHERE id=%s",
            (contract_id,),
        )
        log_contract_event(contract_id, "offer_accepted", uid, {"amount_usd": float(contract["price_usd"])})
        audit_log(uid, "CONTRACT_OFFER_ACCEPTED", "contract", contract_id, details=f"Amount USD: {contract['price_usd']}")
        push_notif(
            contract["employer_id"],
            f"Your contract offer for contract #{contract_id} was accepted. Fund escrow to begin.",
            url_for("contract_room", job_id=contract["job_id"]),
        )
        flash("Offer accepted. The employer can now fund escrow.", "success")
    elif action == "reject":
        STORE.execute(
            f"UPDATE {STORE.t('contracts')} SET status='offer_rejected', offer_responded_at=NOW(), offer_rejected_at=NOW() WHERE id=%s",
            (contract_id,),
        )
        log_contract_event(contract_id, "offer_rejected", uid, {"amount_usd": float(contract["price_usd"])})
        audit_log(uid, "CONTRACT_OFFER_REJECTED", "contract", contract_id, details=f"Amount USD: {contract['price_usd']}")
        push_notif(
            contract["employer_id"],
            f"Your contract offer for contract #{contract_id} was declined. Send a revised amount to continue.",
            url_for("contract_room", job_id=contract["job_id"]),
        )
        flash("Offer declined. Continue negotiating in messages until the amount is right.", "info")
    else:
        flash("Unknown offer response.", "error")

    return redirect(url_for("contract_room", job_id=contract["job_id"]))


@app.route("/contract/<int:contract_id>/fund", methods=["POST"])
@employer_required
def contract_fund(contract_id):
    """Initialize an escrow payment for the agreed contract amount via selected provider."""
    # Handle both JSON and form POST
    provider = request.json.get("provider", "paystack") if request.is_json else request.form.get("provider", "paystack")
    is_json = request.is_json
    
    if not verify_csrf():
        if is_json:
            return jsonify({"error": "Invalid request"}), 403
        flash("Invalid request.", "error")
        return redirect(url_for("employer_dashboard"))
    
    uid = session["user_id"]
    contract = STORE.query_one(
        f"SELECT * FROM {STORE.t('contracts')} WHERE id=%s AND employer_id=%s",
        (contract_id, uid)
    )
    if not contract:
        if is_json:
            return jsonify({"error": "Contract not found."}), 404
        flash("Contract not found.", "error")
        return redirect(url_for("employer_dashboard"))
    
    if contract["status"] != "offer_accepted":
        msg = "The freelancer must accept the latest offer before escrow can be funded."
        if is_json:
            return jsonify({"error": msg}), 400
        flash(msg, "warning")
        return redirect(url_for("contract_room", job_id=contract["job_id"]))
    
    STORE.execute(
        f"UPDATE {STORE.t('payments')} SET status='failed' WHERE contract_id=%s AND payment_type='escrow' AND status='pending'",
        (contract_id,),
    )

    amount_usd = float(contract["escrow_amount_usd"] or contract["price_usd"] or 0)
    if amount_usd <= 0:
        msg = "The agreed contract amount is invalid. Send a fresh offer first."
        if is_json:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("contract_room", job_id=contract["job_id"]))

    email = session.get("email", "")
    if not email:
        row = STORE.query_one(f"SELECT email FROM {STORE.t('users')} WHERE id=%s", (uid,))
        email = str((row or {}).get("email") or "").strip()
    if not email:
        msg = "Your employer account is missing an email address for escrow checkout."
        if is_json:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("contract_room", job_id=contract["job_id"]))

    ref = f"escrow_{contract_id}_{uid}_{secrets.token_hex(8)}"
    amount_kes = SETTINGS.usd_to_kes_amount(amount_usd)
    STORE.execute(
        f"INSERT INTO {STORE.t('payments')} (user_id, provider, payment_type, contract_id, amount_usd, amount_kes, connects_awarded, status, reference) "
        f"VALUES (%s,%s,'escrow',%s,%s,%s,0,'pending',%s)",
        (uid, provider, contract_id, amount_usd, amount_kes, ref),
    )

    if provider == "paystack":
        cents = SETTINGS.usd_to_kes_cents(amount_usd)
        status, data = PAYSTACK.initialize(
            email=email,
            amount_cents=cents,
            reference=ref,
            callback_url=SETTINGS.paystack_callback_url,
            currency=SETTINGS.paystack_currency,
            metadata={"type": "escrow", "contract_id": contract_id, "job_id": contract["job_id"], "employer_id": uid},
        )
        if status == 200 and data.get("status") and data.get("data", {}).get("authorization_url"):
            audit_log(uid, "CONTRACT_ESCROW_CHECKOUT_STARTED", "contract", contract_id, details=f"Amount USD: {amount_usd:.2f}, Provider: {provider}")
            if is_json:
                return jsonify({"redirect_url": data["data"]["authorization_url"]})
            return redirect(data["data"]["authorization_url"])
        STORE.execute(f"UPDATE {STORE.t('payments')} SET status='failed' WHERE reference=%s AND status='pending'", (ref,))
        msg = data.get("message", "Unable to start escrow checkout right now.")
        if is_json:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("contract_room", job_id=contract["job_id"]))
    
    elif provider == "pesapal":
        token, err = PESAPAL.get_token()
        if err or not token:
            msg = "PesaPal authentication failed. Please try again."
            if is_json:
                return jsonify({"error": msg}), 500
            flash(msg, "error")
            return redirect(url_for("contract_room", job_id=contract["job_id"]))
        ipn_id, err2 = PESAPAL.register_ipn(token, SETTINGS.pesapal_ipn_url)
        if err2 or not ipn_id:
            msg = "PesaPal IPN registration failed. Please try again."
            if is_json:
                return jsonify({"error": msg}), 500
            flash(msg, "error")
            return redirect(url_for("contract_room", job_id=contract["job_id"]))
        mobile = STORE.query_one(f"SELECT mobile FROM {STORE.t('users')} WHERE id=%s", (uid,))
        phone = mobile["mobile"] if mobile else ""
        status, data = PESAPAL.submit_order(
            token=token, ipn_id=ipn_id, reference=ref, email=email,
            amount=amount_kes, callback_url=SETTINGS.pesapal_callback_url,
            currency=SETTINGS.pesapal_currency, phone=phone,
        )
        if status in (200, 201) and data.get("redirect_url"):
            audit_log(uid, "CONTRACT_ESCROW_CHECKOUT_STARTED", "contract", contract_id, details=f"Amount USD: {amount_usd:.2f}, Provider: {provider}")
            if is_json:
                return jsonify({"redirect_url": data["redirect_url"]})
            return redirect(data["redirect_url"])
        msg = data.get("message", "PesaPal order failed. Please try again.")
        if is_json:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("contract_room", job_id=contract["job_id"]))
    
    elif provider == "mpesa":
        # M-Pesa support: Route through Paystack (they support M-Pesa as payment method)
        # This ensures consistent payment flow
        cents = SETTINGS.usd_to_kes_cents(amount_usd)
        status, data = PAYSTACK.initialize(
            email=email,
            amount_cents=cents,
            reference=ref,
            callback_url=SETTINGS.paystack_callback_url,
            currency=SETTINGS.paystack_currency,
            metadata={"type": "escrow", "contract_id": contract_id, "job_id": contract["job_id"], "employer_id": uid},
        )
        if status == 200 and data.get("status") and data.get("data", {}).get("authorization_url"):
            audit_log(uid, "CONTRACT_ESCROW_CHECKOUT_STARTED", "contract", contract_id, details=f"Amount USD: {amount_usd:.2f}, Provider: {provider}")
            if is_json:
                return jsonify({"redirect_url": data["data"]["authorization_url"]})
            return redirect(data["data"]["authorization_url"])
        STORE.execute(f"UPDATE {STORE.t('payments')} SET status='failed' WHERE reference=%s AND status='pending'", (ref,))
        msg = data.get("message", "Unable to start escrow checkout right now.")
        if is_json:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("contract_room", job_id=contract["job_id"]))
    
    else:
        msg = "Unknown payment provider"
        if is_json:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("contract_room", job_id=contract["job_id"]))


@app.route("/contract/<int:contract_id>/submit-work", methods=["POST"])
@login_required
def contract_submit_work(contract_id):
    """Freelancer submits completed work with deliverables."""
    if not verify_csrf():
        return jsonify({"error": "Invalid request"}), 403
    
    uid = session["user_id"]
    contract = STORE.query_one(
        f"SELECT * FROM {STORE.t('contracts')} WHERE id=%s AND freelancer_id=%s",
        (contract_id, uid)
    )
    if not contract:
        return jsonify({"error": "Contract not found"}), 404
    
    # Allow transitions from funded or in_progress states
    if contract["status"] not in ["funded", "in_progress"]:
        return jsonify({"error": f"Contract is {contract['status']}, cannot submit work"}), 400
    
    # Get job to check job_type and hourly fields
    job = STORE.query_one(
        f"SELECT job_type, hourly_rate, max_hours, daily_rate, max_days FROM {STORE.t('jobs')} WHERE id=%s",
        (contract["job_id"],)
    )
    
    # Handle both JSON and form-encoded data (Phase 4 compatibility)
    if request.is_json:
        data = request.get_json()
        deliverables = data.get("deliverables_links", [])
        submitted_message = data.get("submitted_message", "Work completed").strip()
        hours_worked = data.get("hours_worked")
        days_worked = data.get("days_worked")
    else:
        deliverables = [{"title": t, "url": u} 
                       for t, u in zip(request.form.getlist("title[]"), request.form.getlist("url[]")) 
                       if u]
        submitted_message = request.form.get("submitted_message", "Work completed").strip()
        hours_worked = request.form.get("hours_worked")
        days_worked = request.form.get("days_worked")
    
    # Validate hours for hourly jobs
    if job and job["job_type"] == "hourly":
        if not hours_worked:
            return jsonify({"error": "Hours worked is required for hourly contracts"}), 400
        try:
            hours_worked = float(hours_worked)
            if hours_worked <= 0 or hours_worked > job.get("max_hours", 999999):
                return jsonify({"error": f"Hours must be between 0 and {job.get('max_hours')}"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid hours worked value"}), 400
        days_worked = None
    # Validate days for daily jobs
    elif job and job["job_type"] == "daily":
        if not days_worked:
            return jsonify({"error": "Days worked is required for daily contracts"}), 400
        try:
            days_worked = float(days_worked)
            if days_worked <= 0 or days_worked > job.get("max_days", 999999):
                return jsonify({"error": f"Days must be between 0 and {job.get('max_days')}"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid days worked value"}), 400
        hours_worked = None
    else:
        hours_worked = None
        days_worked = None
    
    deliverables = _normalize_deliverables(deliverables)
    if not deliverables:
        return jsonify({"error": "Must provide at least one deliverable link"}), 400
    
    # Insert work submission with hours/days if applicable
    if hours_worked:
        STORE.execute(
            f"INSERT INTO {STORE.t('work_submissions')} (contract_id, deliverables_links, submitted_message, hours_worked, submitted_at) "
            f"VALUES (%s,%s,%s,%s,NOW())",
            (contract_id, json.dumps(deliverables), submitted_message, hours_worked)
        )
    elif days_worked:
        STORE.execute(
            f"INSERT INTO {STORE.t('work_submissions')} (contract_id, deliverables_links, submitted_message, days_worked, submitted_at) "
            f"VALUES (%s,%s,%s,%s,NOW())",
            (contract_id, json.dumps(deliverables), submitted_message, days_worked)
        )
    else:
        STORE.execute(
            f"INSERT INTO {STORE.t('work_submissions')} (contract_id, deliverables_links, submitted_message, submitted_at) "
            f"VALUES (%s,%s,%s,NOW())",
            (contract_id, json.dumps(deliverables), submitted_message)
        )
    
    # Update contract status to under_review and set review window
    # For hourly/daily jobs, also store hours_logged/days_logged
    review_days = contract.get("review_days", 7)
    if hours_worked:
        STORE.execute(
            f"UPDATE {STORE.t('contracts')} SET status='under_review', submitted_at=NOW(), "
            f"review_window_starts=NOW(), review_deadline=DATE_ADD(NOW(), INTERVAL %s DAY), "
            f"hours_logged=%s WHERE id=%s",
            (review_days, hours_worked, contract_id)
        )
    elif days_worked:
        STORE.execute(
            f"UPDATE {STORE.t('contracts')} SET status='under_review', submitted_at=NOW(), "
            f"review_window_starts=NOW(), review_deadline=DATE_ADD(NOW(), INTERVAL %s DAY), "
            f"days_logged=%s WHERE id=%s",
            (review_days, days_worked, contract_id)
        )
    else:
        STORE.execute(
            f"UPDATE {STORE.t('contracts')} SET status='under_review', submitted_at=NOW(), "
            f"review_window_starts=NOW(), review_deadline=DATE_ADD(NOW(), INTERVAL %s DAY) "
            f"WHERE id=%s",
            (review_days, contract_id)
        )
    
    log_contract_event(contract_id, "work_submitted", uid, {"links_count": len(deliverables), "hours": hours_worked, "days": days_worked})
    audit_log(uid, "WORK_SUBMITTED", "contract", contract_id, details=f"Links: {len(deliverables)}, Hours: {hours_worked or 'N/A'}, Days: {days_worked or 'N/A'}")
    
    push_notif(
        contract["employer_id"],
        f"Work submitted for contract #{contract_id}. You have {review_days} days to review.",
        url_for("contract_room", job_id=contract["job_id"]),
    )
    
    return jsonify({"success": True})


@app.route("/contract/<int:contract_id>/approve", methods=["POST"])
@login_required
def contract_approve(contract_id):
    """Employer approves work and releases funds."""
    if not verify_csrf():
        return jsonify({"error": "Invalid request"}), 403
    
    uid = session["user_id"]
    contract = STORE.query_one(
        f"SELECT * FROM {STORE.t('contracts')} WHERE id=%s AND employer_id=%s",
        (contract_id, uid)
    )
    if not contract:
        return jsonify({"error": "Contract not found"}), 404
    
    if contract["status"] != "under_review":
        return jsonify({"error": "Contract not under review"}), 400
    
    # Get job to determine payment calculation
    job = STORE.query_one(
        f"SELECT job_type, hourly_rate FROM {STORE.t('jobs')} WHERE id=%s",
        (contract["job_id"],)
    )
    
    # Calculate payment amount based on job type
    payment_amount, payment_desc = _calculate_payment_amount(contract, job)
    
    # Update status and mark completed, store calculated payment amount
    STORE.execute(
        f"UPDATE {STORE.t('contracts')} SET status='completed', completed_at=NOW() WHERE id=%s",
        (contract_id,)
    )
    
    # Award job completion bonus if not already granted
    if not contract.get("completion_reward_granted"):
        STORE.execute(
            f"UPDATE {STORE.t('users')} SET connects_balance=connects_balance+%s WHERE id=%s",
            (JOB_COMPLETION_REWARD, contract["freelancer_id"]),
        )
        STORE.execute(
            f"UPDATE {STORE.t('contracts')} SET completion_reward_granted=1 WHERE id=%s",
            (contract_id,)
        )
        audit_log(contract["freelancer_id"], "JOB_COMPLETION_REWARD", "user", contract["freelancer_id"],
                  details=f"Contract: {contract_id}, Reward: {JOB_COMPLETION_REWARD}")
        push_notif(
            contract["freelancer_id"],
            f"Bonus! You earned {JOB_COMPLETION_REWARD} connects for completing this job successfully.",
            url_for("contract_room", job_id=contract["job_id"]),
        )
    
    log_contract_event(contract_id, "work_approved", uid, {"payment_amount": payment_amount, "payment_type": payment_desc})
    audit_log(uid, "CONTRACT_COMPLETED", "contract", contract_id, details=f"Payment: ${payment_amount:.2f} ({payment_desc})")
    
    push_notif(
        contract["freelancer_id"],
        f"Work approved on contract #{contract_id}! Funds released (${payment_amount:.2f}).",
        url_for("contract_room", job_id=contract["job_id"]),
    )
    
    return jsonify({"success": True, "payment_amount": payment_amount})


@app.route("/contract/<int:contract_id>/dispute", methods=["POST"])
@login_required
def contract_open_dispute(contract_id):
    """Either party opens a dispute."""
    if not verify_csrf():
        return jsonify({"error": "Invalid request"}), 403
    
    uid = session["user_id"]
    contract = STORE.query_one(
        f"SELECT * FROM {STORE.t('contracts')} WHERE id=%s",
        (contract_id,)
    )
    if not contract or (uid != contract["employer_id"] and uid != contract["freelancer_id"]):
        return jsonify({"error": "Contract not found"}), 404
    
    if contract["status"] not in {"under_review", "submitted"}:
        return jsonify({"error": "Can only dispute during review period"}), 400
    
    reason = request.form.get("reason", "").strip()[:500]
    
    # Create dispute
    dispute_id = STORE.execute(
        f"INSERT INTO {STORE.t('disputes')} (contract_id, opened_by_id, reason) VALUES (%s,%s,%s)",
        (contract_id, uid, reason)
    )
    
    # Update contract status
    STORE.execute(
        f"UPDATE {STORE.t('contracts')} SET status='disputed' WHERE id=%s",
        (contract_id,)
    )
    
    log_contract_event(contract_id, "dispute_opened", uid)
    audit_log(uid, "DISPUTE_OPENED", "dispute", dispute_id, details=f"Reason: {reason}")
    
    other_id = contract["freelancer_id"] if uid == contract["employer_id"] else contract["employer_id"]
    push_notif(
        other_id,
        f"Dispute opened on contract #{contract_id}. Admin will review evidence.",
        url_for("contract_room", job_id=contract["job_id"]),
    )
    
    return jsonify({"success": True, "dispute_id": dispute_id})


# Phase 5: Admin Dispute Resolution Dashboard Routes
@app.route("/secure-admin-console-9x7k2m/disputes")
@admin_required
def admin_disputes():
    """List all disputes for admin review (Phase 5)."""
    status_filter = request.args.get("status", "all")
    
    # Count disputes by status
    all_disputes = STORE.query_all(
        f"SELECT COUNT(*) as cnt, status FROM {STORE.t('disputes')} GROUP BY status"
    )
    disputes_count = {"all": sum(d["cnt"] for d in all_disputes)}
    for d in all_disputes:
        disputes_count[d["status"]] = d["cnt"]
    
    # Fetch disputes based on filter
    if status_filter == "all":
        disputes = STORE.query_all(
            f"SELECT d.*, u.full_name as opened_by_name, u.role as opened_by_role, "
            f"j.title as job_title, c.price_usd as contract_price_usd "
            f"FROM {STORE.t('disputes')} d "
            f"JOIN {STORE.t('users')} u ON u.id=d.opened_by_id "
            f"JOIN {STORE.t('contracts')} c ON c.id=d.contract_id "
            f"JOIN {STORE.t('jobs')} j ON j.id=c.job_id "
            f"ORDER BY d.created_at DESC"
        )
    else:
        disputes = STORE.query_all(
            f"SELECT d.*, u.full_name as opened_by_name, u.role as opened_by_role, "
            f"j.title as job_title, c.price_usd as contract_price_usd "
            f"FROM {STORE.t('disputes')} d "
            f"JOIN {STORE.t('users')} u ON u.id=d.opened_by_id "
            f"JOIN {STORE.t('contracts')} c ON c.id=d.contract_id "
            f"JOIN {STORE.t('jobs')} j ON j.id=c.job_id "
            f"WHERE d.status=%s "
            f"ORDER BY d.created_at DESC",
            (status_filter,)
        )
    
    # Parse JSON evidence fields
    for dispute in disputes:
        if dispute.get("claimant_evidence"):
            try:
                dispute["claimant_evidence"] = json.loads(dispute["claimant_evidence"])
            except:
                dispute["claimant_evidence"] = []
        if dispute.get("respondent_evidence"):
            try:
                dispute["respondent_evidence"] = json.loads(dispute["respondent_evidence"])
            except:
                dispute["respondent_evidence"] = []
    
    return render_template("admin/disputes.html", 
                          disputes=disputes, 
                          status=status_filter,
                          disputes_count=disputes_count)


@app.route("/secure-admin-console-9x7k2m/disputes/<int:dispute_id>", methods=["GET"])
@admin_required
def admin_dispute_detail(dispute_id):
    """Get dispute detail for modal (Phase 5) - returns HTML fragment."""
    dispute = STORE.query_one(
        f"SELECT d.*, u.full_name as opened_by_name, u.role as opened_by_role, "
        f"j.title as job_title, c.price_usd as contract_price_usd "
        f"FROM {STORE.t('disputes')} d "
        f"JOIN {STORE.t('users')} u ON u.id=d.opened_by_id "
        f"JOIN {STORE.t('contracts')} c ON c.id=d.contract_id "
        f"JOIN {STORE.t('jobs')} j ON j.id=c.job_id "
        f"WHERE d.id=%s",
        (dispute_id,)
    )
    
    if not dispute:
        return "Dispute not found", 404
    
    # Parse JSON evidence fields
    if dispute.get("claimant_evidence"):
        try:
            dispute["claimant_evidence"] = json.loads(dispute["claimant_evidence"])
        except:
            dispute["claimant_evidence"] = []
    else:
        dispute["claimant_evidence"] = []
        
    if dispute.get("respondent_evidence"):
        try:
            dispute["respondent_evidence"] = json.loads(dispute["respondent_evidence"])
        except:
            dispute["respondent_evidence"] = []
    else:
        dispute["respondent_evidence"] = []
    
    return render_template("admin/dispute_detail.html", dispute=dispute)


# Admin Expired Contracts Management (Manual Release)
@app.route("/secure-admin-console-9x7k2m/contracts/expired")
@admin_required
def admin_expired_contracts():
    """Show all contracts with expired review windows awaiting admin action."""
    expired = STORE.query_all(
        f"SELECT c.*, e.full_name as employer_name, e.email as employer_email, "
        f"f.full_name as freelancer_name, f.email as freelancer_email, "
        f"j.title as job_title FROM {STORE.t('contracts')} c "
        f"JOIN {STORE.t('users')} e ON e.id=c.employer_id "
        f"JOIN {STORE.t('users')} f ON f.id=c.freelancer_id "
        f"JOIN {STORE.t('jobs')} j ON j.id=c.job_id "
        f"WHERE c.status='under_review' AND c.review_deadline < NOW() "
        f"ORDER BY c.review_deadline ASC"
    )
    
    total_at_risk = sum(c["price_usd"] for c in expired)
    
    return render_template("admin/expired_contracts.html", 
                          expired_contracts=expired,
                          total_at_risk=total_at_risk,
                          count=len(expired),
                          now=datetime.utcnow())


@app.route("/secure-admin-console-9x7k2m/contracts/<int:contract_id>/release-to-freelancer", methods=["POST"])
@admin_required
def admin_release_to_freelancer(contract_id):
    """Admin manually releases funds to freelancer when review window expires."""
    if not verify_csrf():
        return jsonify({"error": "Invalid request"}), 403
    
    contract = STORE.query_one(
        f"SELECT * FROM {STORE.t('contracts')} WHERE id=%s",
        (contract_id,)
    )
    
    if not contract:
        return jsonify({"error": "Contract not found"}), 404
    
    if contract["status"] != "under_review":
        return jsonify({"error": f"Contract is {contract['status']}, must be under_review"}), 400
    
    if contract["review_deadline"] > datetime.utcnow():
        return jsonify({"error": "Review window has not expired yet"}), 400
    
    # Get job to determine payment calculation
    job = STORE.query_one(
        f"SELECT job_type, hourly_rate FROM {STORE.t('jobs')} WHERE id=%s",
        (contract["job_id"],)
    )
    
    # Calculate payment amount based on job type
    payment_amount, payment_desc = _calculate_payment_amount(contract, job)
    
    # Release to freelancer
    STORE.execute(
        f"UPDATE {STORE.t('contracts')} SET status='approved' WHERE id=%s",
        (contract_id,)
    )
    
    log_contract_event(contract_id, "manually_released_expired", session["user_id"], 
                      {"reason": "Admin manual release after review window expired", "payment_amount": payment_amount})
    
    # Notify both parties
    push_notif(
        contract["freelancer_id"],
        f"Contract #{contract_id} approved! Funds (${payment_amount:.2f}) have been released to you.",
        url_for("contract_room", job_id=contract["job_id"]),
    )
    push_notif(
        contract["employer_id"],
        f"Contract #{contract_id} auto-approved by admin. Review window expired with no action. Funds released to freelancer.",
        url_for("contract_room", job_id=contract["job_id"]),
    )
    
    # Send email notification to admin
    admin_email = os.getenv("SMTP_FROM_EMAIL")
    if admin_email and os.getenv("SMTP_HOST"):
        try:
            freelancer_info = STORE.query_one(f"SELECT full_name, email FROM {STORE.t('users')} WHERE id=%s", (contract["freelancer_id"],))
            employer_info = STORE.query_one(f"SELECT full_name, email FROM {STORE.t('users')} WHERE id=%s", (contract["employer_id"],))
            job_info = STORE.query_one(f"SELECT title FROM {STORE.t('jobs')} WHERE id=%s", (contract["job_id"],))
            
            email_body = f"""
<h2>Contract Funds Released - Expired Review Window</h2>

<p>You manually released funds for an expired contract:</p>

<table style="width:100%;border-collapse:collapse;margin:20px 0;">
    <tr style="background:#f5f5f5;">
        <td style="padding:8px;border:1px solid #ddd;"><strong>Contract ID</strong></td>
        <td style="padding:8px;border:1px solid #ddd;">{contract_id}</td>
    </tr>
    <tr>
        <td style="padding:8px;border:1px solid #ddd;"><strong>Job</strong></td>
        <td style="padding:8px;border:1px solid #ddd;">{job_info['title'] if job_info else 'Unknown'}</td>
    </tr>
    <tr style="background:#f5f5f5;">
        <td style="padding:8px;border:1px solid #ddd;"><strong>Amount Released</strong></td>
        <td style="padding:8px;border:1px solid #ddd;"><strong style="color:#2e7d32;font-size:1.2rem;">${contract['price_usd']:.2f}</strong></td>
    </tr>
    <tr>
        <td style="padding:8px;border:1px solid #ddd;"><strong>Freelancer (Recipient)</strong></td>
        <td style="padding:8px;border:1px solid #ddd;">{freelancer_info['full_name'] if freelancer_info else 'Unknown'}<br><small>{freelancer_info['email'] if freelancer_info else ''}</small></td>
    </tr>
    <tr style="background:#f5f5f5;">
        <td style="padding:8px;border:1px solid #ddd;"><strong>Employer (Defaulted)</strong></td>
        <td style="padding:8px;border:1px solid #ddd;">{employer_info['full_name'] if employer_info else 'Unknown'}<br><small>{employer_info['email'] if employer_info else ''}</small></td>
    </tr>
    <tr>
        <td style="padding:8px;border:1px solid #ddd;"><strong>Review Deadline Was</strong></td>
        <td style="padding:8px;border:1px solid #ddd;">{contract['review_deadline'].strftime('%Y-%m-%d %I:%M %p') if contract['review_deadline'] else 'N/A'}</td>
    </tr>
</table>

<p style="color:#666;font-size:0.9rem;margin-top:20px;">
    The employer did not take action within the review period. Funds have been released to the freelancer.
</p>
"""
            send_email(admin_email, 
                      f"Contract Release - Expired Review Window (Contract #{contract_id})",
                      email_body)
        except Exception as e:
            LOG.error("Failed to send admin email: %s", e)
    
    audit_log(session["user_id"], "RELEASED_EXPIRED_CONTRACT", "contract", contract_id, 
             details=f"Manually released {contract['price_usd']} to freelancer {contract['freelancer_id']}")
    
    return jsonify({"success": True})


@app.route("/secure-admin-console-9x7k2m/disputes/<int:dispute_id>/resolve", methods=["POST"])
@admin_required
def admin_resolve_dispute(dispute_id):
    """Admin resolves a dispute and releases funds accordingly."""
    if not verify_csrf():
        return jsonify({"error": "Invalid request"}), 403
    
    uid = session["user_id"]
    
    dispute = STORE.query_one(
        f"SELECT * FROM {STORE.t('disputes')} WHERE id=%s",
        (dispute_id,)
    )
    if not dispute:
        return jsonify({"error": "Dispute not found"}), 404
    
    decision = request.form.get("decision", "").strip()  # freelancer_full, employer_full, split
    percentage = int(request.form.get("percentage", 50))  # % to freelancer
    reason = request.form.get("reason", "").strip()[:500]
    
    if decision not in {"freelancer_full", "employer_full", "split"}:
        return jsonify({"error": "Invalid decision"}), 400
    
    if decision == "freelancer_full":
        percentage = 100
    elif decision == "employer_full":
        percentage = 0
    
    # Update dispute
    STORE.execute(
        f"UPDATE {STORE.t('disputes')} SET status='resolved', admin_decision=%s, decision_percentage=%s, decided_by_id=%s, decided_at=NOW(), reason=%s WHERE id=%s",
        (decision, percentage, uid, reason, dispute_id)
    )
    
    # Update contract
    contract = STORE.query_one(
        f"SELECT * FROM {STORE.t('contracts')} WHERE id=%s",
        (dispute["contract_id"],)
    )
    
    if percentage == 100:
        new_status = "completed"
    elif percentage == 0:
        new_status = "refunded"
    else:
        new_status = "completed"  # Partial split still completed, funds distributed
    
    STORE.execute(
        f"UPDATE {STORE.t('contracts')} SET status=%s, completed_at=NOW() WHERE id=%s",
        (new_status, dispute["contract_id"])
    )
    
    log_contract_event(dispute["contract_id"], "dispute_resolved", uid, 
                      {"decision": decision, "percentage": percentage})
    audit_log(uid, "DISPUTE_RESOLVED", "dispute", dispute_id, details=f"Decision: {decision} ({percentage}%)")
    
    # Notify both parties
    push_notif(
        contract["freelancer_id"],
        f"Dispute resolved on contract #{dispute['contract_id']}. Decision: {decision} ({percentage}% to freelancer)",
        url_for("contract_room", job_id=contract["job_id"]),
    )
    push_notif(
        contract["employer_id"],
        f"Dispute resolved on contract #{dispute['contract_id']}. Decision: {decision} ({100-percentage}% to employer)",
        url_for("contract_room", job_id=contract["job_id"]),
    )
    
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════════════════════════════
# Payment Webhooks & Callbacks
# ═══════════════════════════════════════════════════════════════════════════════
def _credit_connects_or_subscription(reference: str) -> tuple[bool, int | None]:
    """Shared post-payment logic for both Paystack and PesaPal. 
    
    Returns: (success, job_id_for_payg)
        - success: True if processed
        - job_id_for_payg: Job ID if PAYG purchase, None otherwise
    """
    # Check for duplicate processing (idempotency)
    pay = STORE.query_one(f"SELECT * FROM {STORE.t('payments')} WHERE reference=%s", (reference,))
    if pay:
        if pay["status"] == "confirmed":
            LOG.info("Payment %s already processed, skipping duplicate webhook", reference)
            audit_log(pay["user_id"], "PAYMENT_DUPLICATE", "payment", pay["id"], details=f"Reference: {reference}")
            return True, None  # Already processed
        
        if pay["status"] != "pending":
            LOG.warning("Payment %s has status %s, not processing", reference, pay["status"])
            return False, None
        
        # Process pending payment atomically
        result = STORE.execute(
            f"UPDATE {STORE.t('payments')} SET status='confirmed', confirmed_at=NOW() WHERE id=%s AND status='pending'",
            (pay["id"],),
        )
        if result > 0:  # Successfully updated (no race condition)
            STORE.execute(
                f"UPDATE {STORE.t('users')} SET connects_balance=connects_balance+%s WHERE id=%s",
                (pay["connects_awarded"], pay["user_id"]),
            )
            audit_log(pay["user_id"], "PAYMENT_CONFIRMED", "payment", pay["id"], 
                     old_value="0", new_value=str(pay["connects_awarded"]), 
                     details=f"Reference: {reference}, Amount USD: {pay['amount_usd']}, Connects: {pay['connects_awarded']}")
            push_notif(
                pay["user_id"],
                f"Payment confirmed! {pay['connects_awarded']} connects added to your account.",
                url_for("worker_buy_connects"),
            )
            user = STORE.query_one(f"SELECT email,full_name FROM {STORE.t('users')} WHERE id=%s", (pay["user_id"],))
            if user:
                send_email(user["email"], "Connects Added — TechBid",
                    f"<p>Hi {user['full_name']}, your payment was confirmed and "
                    f"<b>{pay['connects_awarded']} connects</b> have been added to your account.</p>")
            return True, None
        else:
            LOG.warning("Failed to update payment %s (possible race condition)", reference)
            audit_log(pay["user_id"], "PAYMENT_RACE_CONDITION", "payment", pay["id"], details=f"Reference: {reference}")
            return False, None

    # Check if it's a subscription or PAYG payment
    sub = STORE.query_one(f"SELECT * FROM {STORE.t('subscriptions')} WHERE reference=%s", (reference,))
    if sub:
        if sub["status"] == "confirmed":
            LOG.info("Subscription %s already processed, skipping duplicate", reference)
            audit_log(sub["employer_id"], "SUBSCRIPTION_DUPLICATE", "subscription", sub["id"], details=f"Reference: {reference}")
            return True, None
        
        if sub["status"] != "pending":
            LOG.warning("Subscription %s has status %s, not processing", reference, sub["status"])
            return False, None
        
        import datetime
        
        # Handle PAYG job posting (tier='starter')
        if sub.get("tier") == "starter":
            try:
                job_data = json.loads(sub.get("metadata", "{}"))
                title = job_data.get("title", "").strip()[:255]
                desc = job_data.get("description", "").strip()[:3000]
                category = job_data.get("category", "")
                jtype = job_data.get("job_type", "fixed")
                budget = float(job_data.get("budget_usd", 0) or 0)
                duration = job_data.get("duration", "").strip()[:80]
                use_ai = job_data.get("use_ai", False)
                
                # Validate job data
                if not title or not category or budget <= 0:
                    LOG.error("Invalid PAYG job data in subscription %d", sub["id"])
                    return False, None
                
                # AI enhancement available for Pro tier (not for PAYG)
                final_desc = desc
                
                # Calculate connects_required
                conn_req = _calculate_connects_required(budget)
                
                # Create the job
                job_id = STORE.execute(
                    f"INSERT INTO {STORE.t('jobs')} (employer_id, title, description, category, job_type, budget_usd, duration, connects_required, is_robot, status) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,'open')",
                    (sub["employer_id"], title, final_desc, category, jtype, budget, duration, conn_req),
                )
                
                # Generate and set slug for the job (includes job_id for uniqueness)
                if job_id:
                    slug = _generate_job_slug(title, job_id)
                    STORE.execute(f"UPDATE {STORE.t('jobs')} SET slug=%s WHERE id=%s", (slug, job_id))
                
                # Mark subscription as confirmed (PAYG purchase complete)
                STORE.execute(
                    f"UPDATE {STORE.t('subscriptions')} SET status='confirmed', expires_at=NOW(), confirmed_at=NOW() WHERE id=%s",
                    (sub["id"],),
                )
                
                audit_log(sub["employer_id"], "PAYG_JOB_POSTED", "subscription", sub["id"],
                         details=f"Reference: {reference}, Job ID: {job_id}, Title: {title}, Budget: ${budget}")
                
                push_notif(sub["employer_id"], f"Job posted successfully! {title} is now live.", 
                          url_for("employer_applicants", job_id=job_id))
                
                LOG.info("PAYG job %d posted for employer %d after payment confirmation", job_id, sub["employer_id"])
                return True, job_id
                
            except Exception as e:
                LOG.error("Failed to post PAYG job: %s", e)
                audit_log(sub["employer_id"], "PAYG_JOB_FAILED", "subscription", sub["id"], 
                         details=f"Reference: {reference}, Error: {str(e)}")
                return False, None
        
        # Handle regular subscription (monthly plans)
        expires = datetime.datetime.utcnow() + datetime.timedelta(days=30)
        result = STORE.execute(
            f"UPDATE {STORE.t('subscriptions')} SET status='confirmed', expires_at=%s, confirmed_at=NOW() WHERE id=%s AND status='pending'",
            (expires, sub["id"]),
        )
        if result > 0:
            STORE.execute(
                f"UPDATE {STORE.t('employer_profiles')} SET is_subscribed=1, subscription_expires_at=%s WHERE user_id=%s",
                (expires, sub["employer_id"]),
            )
            audit_log(sub["employer_id"], "SUBSCRIPTION_CONFIRMED", "subscription", sub["id"],
                     details=f"Reference: {reference}, Amount USD: {sub['amount_usd']}, Expires: {expires}")
            push_notif(sub["employer_id"], "Your employer subscription is now active!", url_for("employer_dashboard"))
            return True, None
        else:
            LOG.warning("Failed to update subscription %s (possible race condition)", reference)
            audit_log(sub["employer_id"], "SUBSCRIPTION_RACE_CONDITION", "subscription", sub["id"], details=f"Reference: {reference}")
            return False, None
    
    LOG.warning("No pending payment or subscription found for reference %s", reference)
    audit_log(None, "PAYMENT_NOT_FOUND", "payment", None, details=f"Reference: {reference}")
    return False, None


def _process_payment_reference(reference: str) -> bool:
    pay = STORE.query_one(f"SELECT * FROM {STORE.t('payments')} WHERE reference=%s", (reference,))
    if pay:
        if pay["status"] == "confirmed":
            return True
        if pay["status"] != "pending":
            return False

        result = STORE.execute(
            f"UPDATE {STORE.t('payments')} SET status='confirmed', confirmed_at=NOW() WHERE id=%s AND status='pending'",
            (pay["id"],),
        )
        if result <= 0:
            return False

        payment_type = str(pay.get("payment_type") or "connects")
        if payment_type == "escrow":
            contract = STORE.query_one(
                f"SELECT * FROM {STORE.t('contracts')} WHERE id=%s",
                (pay.get("contract_id"),),
            )
            if not contract:
                audit_log(pay["user_id"], "ESCROW_CONTRACT_MISSING", "payment", pay["id"], details=f"Reference: {reference}")
                return False

            STORE.execute(
                f"UPDATE {STORE.t('contracts')} SET status='funded', funded_at=NOW(), funding_reference=%s WHERE id=%s AND status='offer_accepted'",
                (reference, contract["id"]),
            )
            log_contract_event(contract["id"], "escrow_funded", pay["user_id"], {"reference": reference, "amount_usd": float(pay["amount_usd"])})
            audit_log(pay["user_id"], "CONTRACT_FUNDED", "contract", contract["id"], details=f"Reference: {reference}, Amount USD: {pay['amount_usd']}")
            push_notif(
                contract["freelancer_id"],
                f"Escrow funded for contract #{contract['id']}. You can now start work!",
                url_for("contract_room", job_id=contract["job_id"]),
            )
            push_notif(
                contract["employer_id"],
                f"Escrow payment confirmed for contract #{contract['id']}. Work can begin.",
                url_for("contract_room", job_id=contract["job_id"]),
            )
            return True

        STORE.execute(
            f"UPDATE {STORE.t('users')} SET connects_balance=connects_balance+%s WHERE id=%s",
            (pay["connects_awarded"], pay["user_id"]),
        )
        audit_log(pay["user_id"], "PAYMENT_CONFIRMED", "payment", pay["id"], details=f"Reference: {reference}, Amount USD: {pay['amount_usd']}, Connects: {pay['connects_awarded']}")
        push_notif(
            pay["user_id"],
            f"Payment confirmed! {pay['connects_awarded']} connects added to your account.",
            url_for("worker_buy_connects"),
        )
        user = STORE.query_one(f"SELECT email,full_name FROM {STORE.t('users')} WHERE id=%s", (pay["user_id"],))
        if user:
            send_email(
                user["email"],
                "Connects Added - TechBid",
                f"<p>Hi {user['full_name']}, your payment was confirmed and <b>{pay['connects_awarded']} connects</b> have been added to your account.</p>",
            )
        return True

    sub = STORE.query_one(f"SELECT id FROM {STORE.t('subscriptions')} WHERE reference=%s", (reference,))
    if sub:
        success, _ = _credit_connects_or_subscription(reference)
        return success

    audit_log(None, "PAYMENT_NOT_FOUND", "payment", None, details=f"Reference: {reference}")
    return False


@app.route("/billing/paystack/callback")
def paystack_callback():
    ref = request.args.get("reference") or request.args.get("trxref")
    if ref:
        status, data = PAYSTACK.verify(ref)
        if status == 200 and data.get("data", {}).get("status") == "success":
            processed = _process_payment_reference(ref)
            if processed:
                flash("Payment successful! Your account has been updated.", "success")
            else:
                flash("Payment was received but processing is pending. Please refresh shortly.", "warning")
        else:
            STORE.execute(
                f"UPDATE {STORE.t('payments')} SET status='failed' WHERE reference=%s AND status='pending'",
                (ref,),
            )
            payment = STORE.query_one(
                f"SELECT payment_type FROM {STORE.t('payments')} WHERE reference=%s",
                (ref,),
            )
            if payment and payment.get("payment_type") == "escrow":
                flash("Payment was not completed. You can try funding escrow again when you're ready.", "warning")
            else:
                flash("Payment was not completed. Please try again when you're ready.", "warning")
    return redirect(_payment_redirect_target(ref) if ref else url_for("index"))


@app.route("/api/payments/paystack/webhook", methods=["POST"])
def paystack_webhook():
    raw = request.get_data()
    sig = request.headers.get("X-Paystack-Signature")
    if not PAYSTACK.valid_sig(raw, sig):
        return "Forbidden", 403
    try:
        event = json.loads(raw)
    except Exception:
        return "Bad Request", 400
    if event.get("event") == "charge.success":
        ref = event.get("data", {}).get("reference")
        if ref:
            _process_payment_reference(ref)
    return "OK", 200


@app.route("/billing/pesapal/callback")
def pesapal_callback():
    ref = request.args.get("OrderMerchantReference") or request.args.get("reference")
    tracking_id = request.args.get("OrderTrackingId")
    job_id = None
    if ref and tracking_id:
        token, _ = PESAPAL.get_token()
        if token:
            status, data = PESAPAL.get_tx_status(token, tracking_id)
            if status == 200 and data.get("payment_status_description", "").lower() == "completed":
                success, payg_job_id = _credit_connects_or_subscription(ref)
                if success:
                    job_id = payg_job_id
                    flash("Payment successful! Your account has been updated.", "success")
                else:
                    flash("Payment received but processing failed. Please contact support.", "error")
            else:
                flash("Payment pending or failed. Contact support if debited.", "warning")
    
    # Redirect to appropriate page
    if job_id:
        return redirect(url_for("employer_applicants", job_id=job_id))
    return redirect(url_for("worker_buy_connects") if session.get("role") == "worker" else url_for("employer_dashboard"))


@app.route("/api/payments/pesapal/ipn")
def pesapal_ipn():
    ref = request.args.get("OrderMerchantReference")
    tracking_id = request.args.get("OrderTrackingId")
    if ref and tracking_id:
        token, _ = PESAPAL.get_token()
        if token:
            status, data = PESAPAL.get_tx_status(token, tracking_id)
            if status == 200 and data.get("payment_status_description", "").lower() == "completed":
                _, _ = _credit_connects_or_subscription(ref)
    return jsonify({"orderNotificationType": "IPNCHANGE", "orderTrackingId": tracking_id,
                    "orderMerchantReference": ref, "status": 200})


# ═══════════════════════════════════════════════════════════════════════════════
# Contact Page
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid request.", "error"); return redirect(url_for("contact"))
        name    = request.form.get("name", "").strip()
        email   = request.form.get("email", "").strip()
        message = request.form.get("message", "").strip()
        if name and email and message:
            send_email(SETTINGS.smtp_from_email or SETTINGS.smtp_user,
                f"Contact Form: {name} <{email}>",
                f"<p><b>From:</b> {name} ({email})</p><p>{message}</p>")
            flash("Thank you! We'll get back to you soon.", "success")
        else:
            flash("Please fill in all fields.", "error")
        return redirect(url_for("contact"))
    return render_template("contact.html")


# ═══════════════════════════════════════════════════════════════════════════════
# Public pages (footer)
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/about")
def about():
    team_dir = ROOT / "team_profiles"
    team_files: list[str] = []
    if team_dir.exists():
        for p in sorted(team_dir.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                team_files.append(p.name)
    # Keep About page team section intentionally concise.
    team_files = team_files[:4]

    role_cycle = [
        "Founder & Product Lead",
        "Head of Engineering",
        "Design Lead",
        "Trust & Safety",
        "Community & Support",
    ]
    fixed_roles = {
        "brian shaw": "Design Lead",
        "jason brand": "Head of Engineering",
    }
    team = []
    for i, fname in enumerate(team_files):
        stem = Path(fname).stem
        display_name = stem.replace("_", " ").strip()
        # Clean common stock-image naming a bit
        if display_name.lower().startswith("istockphoto"):
            display_name = "Team Member"
        role = fixed_roles.get(display_name.lower(), role_cycle[i % len(role_cycle)])
        team.append(
            {
                "name": display_name,
                "role": role,
                "photo_url": url_for("team_profile_asset", filename=fname),
            }
        )
    return render_template("pages/about.html", team=team)


@app.route("/team-profiles/<path:filename>")
def team_profile_asset(filename: str):
    # Serve team profile images stored in project root `team_profiles/`.
    safe_dir = ROOT / "team_profiles"
    if not safe_dir.exists():
        flask_abort(404)
    ext = Path(filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        flask_abort(404)
    resp = make_response(send_from_directory(str(safe_dir), filename))
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/favicon.ico")
def favicon():
    """Serve favicon or return empty response to suppress 404."""
    favicon_path = ROOT / "static" / "favicon.ico"
    if favicon_path.exists():
        resp = make_response(send_from_directory(str(ROOT / "static"), "favicon.ico"))
        resp.headers["Cache-Control"] = "public, max-age=31536000"
        return resp
    # Return 204 No Content to suppress 404 error
    return "", 204


@app.route("/feedback", methods=["GET", "POST"])
def feedback():
    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid request.", "error")
            return redirect(url_for("feedback"))
        name = request.form.get("name", "").strip()[:120]
        email = request.form.get("email", "").strip()[:255]
        message = request.form.get("message", "").strip()[:4000]
        if not message:
            flash("Please share your feedback message.", "warning")
            return redirect(url_for("feedback"))
        send_email(
            SETTINGS.smtp_from_email or SETTINGS.smtp_user,
            "TechBid Feedback",
            f"<p><b>Name:</b> {name or '—'}</p><p><b>Email:</b> {email or '—'}</p><p>{message}</p>",
        )
        flash("Thanks — your feedback was received.", "success")
        return redirect(url_for("feedback"))
    return render_template("pages/feedback.html")


@app.route("/trust-safety")
def trust_safety():
    return render_template("pages/trust_safety.html")


@app.route("/help")
def help_support():
    return render_template("pages/help_support.html")


@app.route("/terms")
def terms():
    return render_template("pages/terms.html")


@app.route("/privacy")
def privacy():
    return render_template("pages/privacy.html")


@app.route("/cookie-policy")
def cookie_policy():
    return render_template("pages/cookie_policy.html")


@app.route("/accessibility")
def accessibility():
    return render_template("pages/accessibility.html")


@app.route("/enterprise")
def enterprise():
    return render_template("pages/enterprise.html")


@app.route("/release-notes")
def release_notes():
    return render_template("pages/release_notes.html")


@app.route("/ca-notice")
def ca_notice():
    return render_template("pages/ca_notice.html")


@app.route("/privacy-choices")
def privacy_choices():
    return render_template("pages/privacy_choices.html")


# ═══════════════════════════════════════════════════════════════════════════════
# Admin Routes (Secured with unique URL)
# ═══════════════════════════════════════════════════════════════════════════════

# Redirect old admin URLs to new secure ones
@app.route("/admin", strict_slashes=False)
@app.route("/admin/login", strict_slashes=False)
def redirect_old_admin():
    """Redirect old admin URLs to new secure location."""
    return redirect(url_for("admin_dashboard"), code=301)

@app.route("/admin/<path:path>")
def redirect_old_admin_path(path):
    """Redirect any other old admin paths to login."""
    return redirect(url_for("admin_login"), code=301)

@app.route("/secure-admin-console-9x7k2m/login", methods=["GET", "POST"])
def admin_login():
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        if not verify_csrf(): flash("Invalid request.", "error"); return redirect(url_for("admin_login"))
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == SETTINGS.admin_username and p == SETTINGS.admin_password:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Invalid admin credentials.", "error")
    return render_template("admin/login.html")


@app.route("/secure-admin-console-9x7k2m/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("index"))


@app.route("/secure-admin-console-9x7k2m")
@admin_required
def admin_dashboard():
    users_count   = (STORE.query_one(f"SELECT COUNT(*) as c FROM {STORE.t('users')}") or {}).get("c", 0)
    workers_count = (STORE.query_one(f"SELECT COUNT(*) as c FROM {STORE.t('users')} WHERE role='worker'") or {}).get("c", 0)
    emp_count     = (STORE.query_one(f"SELECT COUNT(*) as c FROM {STORE.t('users')} WHERE role='employer'") or {}).get("c", 0)
    
    # Use cached job counts for better performance
    job_counts = get_job_counts()
    jobs_count = job_counts["total"]
    robot_count = job_counts["robot"]
    
    apps_count    = (STORE.query_one(f"SELECT COUNT(*) as c FROM {STORE.t('applications')}") or {}).get("c", 0)
    revenue_row   = STORE.query_one(f"SELECT COALESCE(SUM(amount_usd),0) as r FROM {STORE.t('payments')} WHERE status='confirmed'")
    revenue_usd   = float(revenue_row["r"]) if revenue_row else 0.0
    pending_disb  = (STORE.query_one(f"SELECT COUNT(*) as c FROM {STORE.t('employer_payments')} WHERE status='pending'") or {}).get("c", 0)
    recent_pays   = STORE.query_all(
        f"SELECT p.*, u.email FROM {STORE.t('payments')} p JOIN {STORE.t('users')} u ON u.id=p.user_id "
        f"ORDER BY p.created_at DESC LIMIT 10")
    stats = {
        "users_count": users_count,
        "workers_count": workers_count,
        "emp_count": emp_count,
        "jobs_count": jobs_count,
        "robot_count": robot_count,
        "apps_count": apps_count,
        "revenue_usd": revenue_usd,
        "pending_disb": pending_disb
    }
    return render_template("admin/dashboard.html",
                           users_count=stats.get("users_count", 0),
                           workers_count=stats.get("workers_count", 0),
                           emp_count=stats.get("emp_count", 0),
                           jobs_count=stats.get("jobs_count", 0),
                           robot_count=stats.get("robot_count", 0),
                           apps_count=stats.get("apps_count", 0),
                           revenue_usd=float(stats.get("revenue_usd") or 0),
                           pending_disb=stats.get("pending_disb", 0),
                           recent_pays=recent_pays)


@app.route("/secure-admin-console-9x7k2m/users")
@admin_required
def admin_users():
    users = STORE.query_all(
        f"SELECT u.*, ep.is_subscribed, ep.subscription_expires_at FROM {STORE.t('users')} u "
        f"LEFT JOIN {STORE.t('employer_profiles')} ep ON ep.user_id=u.id "
        f"ORDER BY u.created_at DESC LIMIT 200")
    return render_template("admin/users.html", users=users)


@app.route("/secure-admin-console-9x7k2m/gift-connects", methods=["POST"])
@admin_required
def admin_gift_connects():
    if not verify_csrf(): flash("Invalid request.", "error"); return redirect(url_for("admin_users"))
    email = request.form.get("email", "").strip()
    amount = request.form.get("amount", type=int)
    
    if not email or not amount or amount <= 0:
        flash("Invalid email or amount.", "error"); return redirect(url_for("admin_users"))
        
    user = STORE.query_one(f"SELECT id FROM {STORE.t('users')} WHERE email=%s", (email,))
    if not user:
        flash("No user found with that email addressing.", "error"); return redirect(url_for("admin_users"))
        
    STORE.execute(f"UPDATE {STORE.t('users')} SET connects_balance=connects_balance+%s WHERE id=%s", (amount, user["id"]))
    flash(f"Successfully gifted {amount} connects to {email}!", "success")
    return redirect(url_for("admin_users"))


@app.route("/secure-admin-console-9x7k2m/gift-subscription", methods=["POST"])
@admin_required
def admin_gift_subscription():
    if not verify_csrf(): flash("Invalid request.", "error"); return redirect(url_for("admin_users"))
    email = request.form.get("email", "").strip()
    tier = request.form.get("tier", "").strip().lower()
    days = request.form.get("days", type=int) or 30  # Default 30 days if not specified
    
    # Validate tier
    valid_tiers = ['basic', 'pro']
    if not email or tier not in valid_tiers or days <= 0:
        flash("Invalid email, tier, or duration.", "error"); return redirect(url_for("admin_users"))
    
    # Find user by email (can be either worker or employer role, or both)
    user = STORE.query_one(
        f"SELECT u.id FROM {STORE.t('users')} u LEFT JOIN {STORE.t('employer_profiles')} ep ON ep.user_id=u.id WHERE u.email=%s AND (u.role='employer' OR ep.id IS NOT NULL)",
        (email,)
    )
    
    if not user:
        flash("No employer or dual-role user found with that email.", "error"); return redirect(url_for("admin_users"))
    
    # Ensure employer_profiles entry exists
    existing_ep = STORE.query_one(
        f"SELECT id FROM {STORE.t('employer_profiles')} WHERE user_id=%s",
        (user["id"],)
    )
    if not existing_ep:
        STORE.execute(
            f"INSERT INTO {STORE.t('employer_profiles')} (user_id, is_subscribed, subscription_expires_at) VALUES (%s, 0, NULL)",
            (user["id"],)
        )
    
    # Create subscription record with specified tier
    expires_at = datetime.utcnow() + timedelta(days=days)
    reference = f"admin-gift-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    subscription_id = STORE.execute(
        f"INSERT INTO {STORE.t('subscriptions')} (employer_id, reference, amount_usd, tier, status) VALUES (%s,%s,%s,%s,'confirmed')",
        (user["id"], reference, 0, tier),
    )
    
    # Update subscription with expires_at
    STORE.execute(
        f"UPDATE {STORE.t('subscriptions')} SET expires_at=%s WHERE reference=%s",
        (expires_at, reference),
    )
    
    # Update employer_profiles
    STORE.execute(
        f"UPDATE {STORE.t('employer_profiles')} SET is_subscribed=1, subscription_expires_at=%s WHERE user_id=%s",
        (expires_at, user["id"]),
    )
    flash(f"Successfully gifted {days}-day {tier.upper()} subscription to {email}!", "success")
    return redirect(url_for("admin_users"))


@app.route("/secure-admin-console-9x7k2m/gift-connects-random", methods=["POST"])
@admin_required
def admin_gift_connects_random():
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("admin_payments"))
    amount = request.form.get("amount", type=int)
    if not amount or amount <= 0:
        flash("Invalid connects amount.", "error")
        return redirect(url_for("admin_payments"))
    worker = STORE.query_one(
        f"SELECT id, email FROM {STORE.t('users')} WHERE role='worker' ORDER BY RAND() LIMIT 1",
        (),
    )
    if not worker:
        flash("No worker account found to gift connects.", "error")
        return redirect(url_for("admin_payments"))
    STORE.execute(
        f"UPDATE {STORE.t('users')} SET connects_balance=connects_balance+%s WHERE id=%s",
        (amount, worker["id"]),
    )
    flash(f"Gifted {amount} connects to random worker: {worker['email']}", "success")
    return redirect(url_for("admin_payments"))


@app.route("/secure-admin-console-9x7k2m/gift-subscription-random", methods=["POST"])
@admin_required
def admin_gift_subscription_random():
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("admin_payments"))
    tier = request.form.get("tier", "").strip().lower()
    days = request.form.get("days", type=int) or 30
    
    # Validate tier
    valid_tiers = ['basic', 'pro']
    if tier not in valid_tiers or days <= 0:
        flash("Invalid tier or subscription days.", "error")
        return redirect(url_for("admin_payments"))
    
    # Find random employer (role='employer' OR has employer_profiles entry)
    employer = STORE.query_one(
        f"SELECT u.id, u.email FROM {STORE.t('users')} u LEFT JOIN {STORE.t('employer_profiles')} ep ON ep.user_id=u.id WHERE u.role='employer' OR ep.id IS NOT NULL ORDER BY RAND() LIMIT 1",
        (),
    )
    if not employer:
        flash("No employer account found to gift subscription.", "error")
        return redirect(url_for("admin_payments"))
    
    # Ensure employer_profiles entry exists
    existing_ep = STORE.query_one(
        f"SELECT id FROM {STORE.t('employer_profiles')} WHERE user_id=%s",
        (employer["id"],)
    )
    if not existing_ep:
        STORE.execute(
            f"INSERT INTO {STORE.t('employer_profiles')} (user_id, is_subscribed, subscription_expires_at) VALUES (%s, 0, NULL)",
            (employer["id"],)
        )
    
    # Create subscription record
    expires_at = datetime.utcnow() + timedelta(days=days)
    reference = f"admin-gift-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    STORE.execute(
        f"INSERT INTO {STORE.t('subscriptions')} (employer_id, reference, amount_usd, tier, status) VALUES (%s,%s,%s,%s,'confirmed')",
        (employer["id"], reference, 0, tier),
    )
    
    # Update subscription with expires_at
    STORE.execute(
        f"UPDATE {STORE.t('subscriptions')} SET expires_at=%s WHERE reference=%s",
        (expires_at, reference),
    )
    
    # Update employer_profiles
    STORE.execute(
        f"UPDATE {STORE.t('employer_profiles')} SET is_subscribed=1, subscription_expires_at=%s WHERE user_id=%s",
        (expires_at, employer["id"]),
    )
    flash(f"Gifted {days}-day {tier.upper()} subscription to random employer: {employer['email']}", "success")
    return redirect(url_for("admin_payments"))


@app.route("/secure-admin-console-9x7k2m/robot-names", methods=["GET", "POST"])
@admin_required
def admin_robot_names():
    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid request.", "error")
            return redirect(url_for("admin_robot_names"))
        bulk_text = request.form.get("names_bulk", "").strip()
        if not bulk_text:
            flash("Please provide at least one name.", "warning")
            return redirect(url_for("admin_robot_names"))
        added = 0
        for raw in bulk_text.splitlines():
            name = raw.strip()[:120]
            if not name:
                continue
            try:
                STORE.execute(
                    f"INSERT INTO {STORE.t('robot_name_pool')} (display_name, is_active) VALUES (%s,1)",
                    (name,),
                )
                added += 1
            except Exception:
                # Ignore duplicates gracefully.
                continue
        flash(f"Added {added} name(s) to simulated applicant pool.", "success")
        return redirect(url_for("admin_robot_names"))

    names = STORE.query_all(
        f"SELECT * FROM {STORE.t('robot_name_pool')} ORDER BY is_active DESC, display_name ASC",
        (),
    )
    return render_template("admin/robot_names.html", names=names)


@app.route("/secure-admin-console-9x7k2m/robot-names/<int:name_id>/toggle", methods=["POST"])
@admin_required
def admin_robot_name_toggle(name_id):
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("admin_robot_names"))
    row = STORE.query_one(
        f"SELECT id, is_active FROM {STORE.t('robot_name_pool')} WHERE id=%s",
        (name_id,),
    )
    if not row:
        flash("Name entry not found.", "error")
        return redirect(url_for("admin_robot_names"))
    new_state = 0 if int(row.get("is_active") or 0) else 1
    STORE.execute(
        f"UPDATE {STORE.t('robot_name_pool')} SET is_active=%s WHERE id=%s",
        (new_state, name_id),
    )
    flash("Name status updated.", "success")
    return redirect(url_for("admin_robot_names"))


@app.route("/secure-admin-console-9x7k2m/robot-names/<int:name_id>/delete", methods=["POST"])
@admin_required
def admin_robot_name_delete(name_id):
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("admin_robot_names"))
    STORE.execute(
        f"DELETE FROM {STORE.t('robot_name_pool')} WHERE id=%s",
        (name_id,),
    )
    flash("Name removed from pool.", "success")
    return redirect(url_for("admin_robot_names"))


@app.route("/secure-admin-console-9x7k2m/jobs")
@admin_required
def admin_jobs():
    jobs = STORE.query_all(
        f"SELECT j.*, rw.robot_name, rw.connects_shown FROM {STORE.t('jobs')} j "
        f"LEFT JOIN {STORE.t('robot_winners')} rw ON rw.job_id=j.id "
        f"ORDER BY j.created_at DESC LIMIT 200")
    ai_jobs_today = (STORE.query_one(
        f"SELECT COUNT(*) as c FROM {STORE.t('jobs')} WHERE is_robot=1 AND created_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)",
        (),
    ) or {}).get("c", 0)
    latest_run = STORE.query_one(
        f"SELECT * FROM {STORE.t('ai_generation_runs')} ORDER BY created_at DESC LIMIT 1",
        (),
    )
    return render_template(
        "admin/jobs.html",
        jobs=jobs,
        ai_jobs_today=ai_jobs_today,
        ai_daily_limit=max(1, min(SETTINGS.ai_jobs_per_run, 10)),
        ai_last_run=latest_run,
        ai_cooldown_seconds=AI_MIN_INTERVAL_SECONDS,
    )


@app.route("/secure-admin-console-9x7k2m/robot-applications")
@admin_required
def admin_robot_applications():
    """View all applications for robot/AI jobs."""
    apps = STORE.query_all(
        f"SELECT a.id, a.user_id, a.job_id, a.connects_spent, a.cover_letter, a.attachment_links, a.status, a.applied_at, "
        f"j.title as job_title, j.is_robot, j.employer_id, "
        f"u.full_name, u.email, u.profile_pic_url "
        f"FROM {STORE.t('applications')} a "
        f"JOIN {STORE.t('jobs')} j ON j.id=a.job_id "
        f"JOIN {STORE.t('users')} u ON u.id=a.user_id "
        f"WHERE j.is_robot=1 "
        f"ORDER BY a.applied_at DESC LIMIT 500",
        ()
    )
    return render_template("admin/robot_applications.html", applications=apps)


@app.route("/secure-admin-console-9x7k2m/jobs/create-robot", methods=["GET", "POST"])
@admin_required
def admin_create_robot_job():
    if request.method == "POST":
        if not verify_csrf(): flash("Invalid request.", "error"); return redirect(url_for("admin_create_robot_job"))
        import random
        title    = request.form.get("title", "").strip()[:255]
        desc     = request.form.get("description", "").strip()[:3000]
        category = request.form.get("category", "")
        jtype    = request.form.get("job_type", "fixed")
        budget   = float(request.form.get("budget_usd", 500) or 500)
        duration = request.form.get("duration", "1 month").strip()[:80]
        connects = int(request.form.get("connects_required", 20) or 20)
        robot_name  = request.form.get("robot_name", "").strip() or _pick_robot_name(STORE)
        ai_employer_name = request.form.get("employer_name", "").strip() or _pick_employer_name(STORE)
        requested_robot_conn = request.form.get("robot_connects", type=int)
        auto_conn, winner_completed_jobs, winner_success_rate, winner_style = _build_robot_winner_profile()
        robot_conn = int(requested_robot_conn or auto_conn)
        if not title or category not in JOB_CATEGORIES or jtype not in JOB_TYPES:
            flash("Fill all fields correctly.", "error"); return redirect(url_for("admin_create_robot_job"))
        job_id = STORE.execute(
            f"INSERT INTO {STORE.t('jobs')} (employer_id, title, description, category, job_type, budget_usd, "
            f"duration, connects_required, is_robot, ai_employer_name, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s,'open')",
            (None, title, desc, category, jtype, budget, duration, connects, ai_employer_name),
        )
        
        # Generate and set slug for the job (includes job_id for uniqueness)
        if job_id:
            slug = _generate_job_slug(title, job_id)
            STORE.execute(f"UPDATE {STORE.t('jobs')} SET slug=%s WHERE id=%s", (slug, job_id))
        
        awarded_at = datetime.utcnow() + timedelta(hours=random.randint(24, 36))
        STORE.execute(
            f"INSERT INTO {STORE.t('robot_winners')} "
            "(job_id, robot_name, connects_shown, completed_jobs, success_rate, selection_style, awarded_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (job_id, robot_name, robot_conn, winner_completed_jobs, winner_success_rate, winner_style, awarded_at),
        )
        flash("Robot job created!", "success"); return redirect(url_for("admin_jobs"))
    categories_text = "\n".join(JOB_CATEGORIES)
    robot_names_db = _get_simulated_name_pool(STORE)
    employer_names_db = _get_employer_name_pool(STORE)
    return render_template("admin/create_robot_job.html", categories=JOB_CATEGORIES, job_types=JOB_TYPES,
                           robot_names=robot_names_db, employer_names=employer_names_db, categories_text=categories_text)


@app.route("/secure-admin-console-9x7k2m/jobs/generate-ai", methods=["POST"])
@admin_required
def admin_generate_ai_jobs():
    if not verify_csrf():
        flash("Invalid request.", "error"); return redirect(url_for("admin_jobs"))
    
    # Check if Gemini is configured
    if not SETTINGS.gemini_api_key:
        flash("⚠️ Gemini API key not configured. Set GEMINI_API_KEY in .env", "error")
        return redirect(url_for("admin_jobs"))
    
    now_utc = datetime.utcnow()
    with AI_TRIGGER_LOCK:
        global AI_LAST_MANUAL_TRIGGER_AT
        if AI_LAST_MANUAL_TRIGGER_AT:
            elapsed = (now_utc - AI_LAST_MANUAL_TRIGGER_AT).total_seconds()
            if elapsed < AI_MIN_INTERVAL_SECONDS:
                wait_sec = int(AI_MIN_INTERVAL_SECONDS - elapsed)
                flash(f"Please wait {wait_sec}s before triggering AI generation again.", "warning")
                return redirect(url_for("admin_jobs"))
        run_id = STORE.execute(
            f"INSERT INTO {STORE.t('ai_generation_runs')} (triggered_by_user_id, status, message) VALUES (%s,'running',%s)",
            (session.get("user_id"), "Manual run started"),
        )
        AI_LAST_MANUAL_TRIGGER_AT = now_utc

    def _run_ai_generation_job(run_pk: int) -> None:
        try:
            created = _generate_ai_jobs(STORE)
            STORE.execute(
                f"UPDATE {STORE.t('ai_generation_runs')} "
                "SET status='success', jobs_created=%s, message=%s, finished_at=UTC_TIMESTAMP() WHERE id=%s",
                (created, f"Created {created} AI job(s)", run_pk),
            )
        except Exception as exc:
            STORE.execute(
                f"UPDATE {STORE.t('ai_generation_runs')} "
                "SET status='failed', message=%s, finished_at=UTC_TIMESTAMP() WHERE id=%s",
                (str(exc)[:250], run_pk),
            )

    # Run AI generation in background
    t = threading.Thread(target=_run_ai_generation_job, args=(run_id,), daemon=True)
    t.start()
    
    flash("🤖 AI generation started. Cooldown guardrail is active.", "info")
    return redirect(url_for("admin_jobs"))


@app.route("/secure-admin-console-9x7k2m/jobs/<int:job_id>/delete", methods=["POST"])
@admin_required
def admin_delete_job(job_id):
    if not verify_csrf():
        flash("Invalid request.", "error"); return redirect(url_for("admin_jobs"))
    STORE.execute(f"DELETE FROM {STORE.t('jobs')} WHERE id=%s", (job_id,))
    flash("Job deleted.", "success"); return redirect(url_for("admin_jobs"))


@app.route("/secure-admin-console-9x7k2m/jobs/bulk-delete", methods=["POST"])
@admin_required
def admin_bulk_delete_jobs():
    if not verify_csrf():
        flash("Invalid request.", "error"); return redirect(url_for("admin_jobs"))
    job_ids = request.form.getlist("job_ids")
    if not job_ids:
        flash("No jobs selected.", "warning"); return redirect(url_for("admin_jobs"))
    placeholders = ",".join(["%s"] * len(job_ids))
    STORE.execute(f"DELETE FROM {STORE.t('jobs')} WHERE id IN ({placeholders})", tuple(job_ids))
    flash(f"{len(job_ids)} job(s) deleted.", "success")
    return redirect(url_for("admin_jobs"))


@app.route("/secure-admin-console-9x7k2m/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_job(job_id):
    job = STORE.query_one(
        f"SELECT j.*, rw.robot_name, rw.connects_shown, rw.completed_jobs, rw.success_rate, rw.selection_style, rw.awarded_at "
        f"FROM {STORE.t('jobs')} j LEFT JOIN {STORE.t('robot_winners')} rw ON rw.job_id=j.id WHERE j.id=%s",
        (job_id,),
    )
    if not job:
        flash("Job not found.", "error"); return redirect(url_for("admin_jobs"))
    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid request.", "error"); return redirect(url_for("admin_edit_job", job_id=job_id))
        title    = request.form.get("title", "").strip()[:255]
        desc     = request.form.get("description", "").strip()[:3000]
        category = request.form.get("category", "")
        jtype    = request.form.get("job_type", "fixed")
        budget   = float(request.form.get("budget_usd", 500) or 500)
        duration = request.form.get("duration", "1 month").strip()[:80]
        connects = int(request.form.get("connects_required", 20) or 20)
        status   = request.form.get("status", "open")
        robot_name = request.form.get("robot_name", "").strip()
        robot_conn = int(request.form.get("robot_connects", 500) or 500)
        if not title or category not in JOB_CATEGORIES or jtype not in JOB_TYPES:
            flash("Fill all required fields correctly.", "error")
            return redirect(url_for("admin_edit_job", job_id=job_id))
        STORE.execute(
            f"UPDATE {STORE.t('jobs')} SET title=%s, description=%s, category=%s, job_type=%s, "
            f"budget_usd=%s, duration=%s, connects_required=%s, status=%s WHERE id=%s",
            (title, desc, category, jtype, budget, duration, connects, status, job_id)
        )
        if robot_name and job["is_robot"]:
            existing_winner = STORE.query_one(
                f"SELECT id, completed_jobs, success_rate, selection_style FROM {STORE.t('robot_winners')} WHERE job_id=%s",
                (job_id,),
            )
            if existing_winner:
                metric_conn, metric_completed_jobs, metric_success_rate, metric_style = _build_robot_winner_profile()
                completed_jobs = existing_winner.get("completed_jobs") or metric_completed_jobs
                success_rate = float(existing_winner.get("success_rate") or metric_success_rate)
                selection_style = existing_winner.get("selection_style") or metric_style
                STORE.execute(
                    f"UPDATE {STORE.t('robot_winners')} "
                    "SET robot_name=%s, connects_shown=%s, completed_jobs=%s, success_rate=%s, selection_style=%s WHERE job_id=%s",
                    (robot_name, robot_conn or metric_conn, completed_jobs, success_rate, selection_style, job_id),
                )
            else:
                new_conn, winner_completed_jobs, winner_success_rate, winner_style = _build_robot_winner_profile()
                STORE.execute(
                    f"INSERT INTO {STORE.t('robot_winners')} "
                    "(job_id, robot_name, connects_shown, completed_jobs, success_rate, selection_style) VALUES (%s,%s,%s,%s,%s,%s)",
                    (job_id, robot_name, robot_conn or new_conn, winner_completed_jobs, winner_success_rate, winner_style),
                )
        flash("Job updated successfully.", "success")
        return redirect(url_for("admin_jobs"))
    return render_template("admin/edit_job.html", job=job, categories=JOB_CATEGORIES, job_types=JOB_TYPES)


@app.route("/secure-admin-console-9x7k2m/employer-names")
@admin_required
def admin_employer_names():
    """Manage employer names pool."""
    names = STORE.query_all(
        f"SELECT * FROM {STORE.t('employer_name_pool')} ORDER BY created_at DESC"
    )
    return render_template("admin/employer_names.html", names=names)


@app.route("/secure-admin-console-9x7k2m/employer-names/add", methods=["POST"])
@admin_required
def admin_add_employer_name():
    """Add a new employer name."""
    if not verify_csrf():
        flash("Invalid request.", "error")
    else:
        name = request.form.get("name", "").strip()[:120]
        if not name:
            flash("Name cannot be empty.", "error")
        else:
            try:
                STORE.execute(
                    f"INSERT INTO {STORE.t('employer_name_pool')} (display_name, is_active) VALUES (%s, 1)",
                    (name,),
                )
                flash(f"Employer name '{name}' added successfully.", "success")
            except Exception as exc:
                if "Duplicate" in str(exc):
                    flash(f"Employer name '{name}' already exists.", "error")
                else:
                    flash(f"Error adding employer name: {exc}", "error")
    return redirect(url_for("admin_employer_names"))


@app.route("/secure-admin-console-9x7k2m/employer-names/<int:name_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_employer_name(name_id):
    """Toggle employer name active status."""
    if not verify_csrf():
        flash("Invalid request.", "error")
    else:
        name = STORE.query_one(
            f"SELECT * FROM {STORE.t('employer_name_pool')} WHERE id=%s", (name_id,)
        )
        if name:
            new_status = 1 if not name.get("is_active") else 0
            STORE.execute(
                f"UPDATE {STORE.t('employer_name_pool')} SET is_active=%s WHERE id=%s",
                (new_status, name_id),
            )
            status_text = "enabled" if new_status else "disabled"
            flash(f"Employer name {status_text}.", "success")
        else:
            flash("Employer name not found.", "error")
    return redirect(url_for("admin_employer_names"))


@app.route("/secure-admin-console-9x7k2m/employer-names/<int:name_id>/delete", methods=["POST"])
@admin_required
def admin_delete_employer_name(name_id):
    """Delete an employer name."""
    if not verify_csrf():
        flash("Invalid request.", "error")
    else:
        STORE.execute(
            f"DELETE FROM {STORE.t('employer_name_pool')} WHERE id=%s", (name_id,)
        )
        flash("Employer name deleted.", "success")
    return redirect(url_for("admin_employer_names"))


@app.route("/secure-admin-console-9x7k2m/job-templates")
@admin_required
def admin_job_templates():
    """Manage job description templates."""
    templates = STORE.query_all(
        f"SELECT * FROM {STORE.t('job_templates')} ORDER BY category, created_at DESC"
    )
    return render_template("admin/job_templates.html", templates=templates, categories=JOB_CATEGORIES, job_types=JOB_TYPES)


@app.route("/secure-admin-console-9x7k2m/job-templates/add", methods=["POST"])
@admin_required
def admin_add_job_template():
    """Add a new job template."""
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("admin_job_templates"))
    
    title    = request.form.get("title", "").strip()[:255]
    desc     = request.form.get("description", "").strip()[:3000]
    category = request.form.get("category", "")
    jtype    = request.form.get("job_type", "fixed")
    budget   = float(request.form.get("budget_usd", 500) or 500)
    duration = request.form.get("duration", "").strip()[:80]
    connects = int(request.form.get("connects_required", 20) or 20)
    
    if not title or not desc or category not in JOB_CATEGORIES or jtype not in JOB_TYPES:
        flash("Please fill in all required fields correctly.", "error")
        return redirect(url_for("admin_job_templates"))
    
    try:
        STORE.execute(
            f"INSERT INTO {STORE.t('job_templates')} (title, description, category, job_type, budget_usd, duration, connects_required) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (title, desc, category, jtype, budget, duration, connects),
        )
        flash(f"Job template '{title}' added successfully.", "success")
    except Exception as exc:
        flash(f"Error adding template: {str(exc)[:100]}", "error")
    
    return redirect(url_for("admin_job_templates"))


@app.route("/secure-admin-console-9x7k2m/job-templates/<int:template_id>/edit", methods=["POST"])
@admin_required
def admin_edit_job_template(template_id):
    """Edit a job template."""
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("admin_job_templates"))
    
    template = STORE.query_one(
        f"SELECT * FROM {STORE.t('job_templates')} WHERE id=%s", (template_id,)
    )
    if not template:
        flash("Template not found.", "error")
        return redirect(url_for("admin_job_templates"))
    
    title    = request.form.get("title", "").strip()[:255]
    desc     = request.form.get("description", "").strip()[:3000]
    category = request.form.get("category", "")
    jtype    = request.form.get("job_type", "fixed")
    budget   = float(request.form.get("budget_usd", 500) or 500)
    duration = request.form.get("duration", "").strip()[:80]
    connects = int(request.form.get("connects_required", 20) or 20)
    
    if not title or not desc or category not in JOB_CATEGORIES or jtype not in JOB_TYPES:
        flash("Please fill in all required fields correctly.", "error")
        return redirect(url_for("admin_job_templates"))
    
    try:
        STORE.execute(
            f"UPDATE {STORE.t('job_templates')} SET title=%s, description=%s, category=%s, job_type=%s, budget_usd=%s, duration=%s, connects_required=%s WHERE id=%s",
            (title, desc, category, jtype, budget, duration, connects, template_id),
        )
        flash(f"Template updated successfully.", "success")
    except Exception as exc:
        flash(f"Error updating template: {str(exc)[:100]}", "error")
    
    return redirect(url_for("admin_job_templates"))


@app.route("/secure-admin-console-9x7k2m/job-templates/<int:template_id>/delete", methods=["POST"])
@admin_required
def admin_delete_job_template(template_id):
    """Delete a job template."""
    if not verify_csrf():
        flash("Invalid request.", "error")
    else:
        STORE.execute(
            f"DELETE FROM {STORE.t('job_templates')} WHERE id=%s", (template_id,)
        )
        flash("Template deleted.", "success")
    return redirect(url_for("admin_job_templates"))


@app.route("/secure-admin-console-9x7k2m/job-templates/<int:template_id>/create-job", methods=["POST"])
@admin_required
def admin_create_job_from_template(template_id):
    """Create a robot job from a template."""
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("admin_job_templates"))
    
    template = STORE.query_one(
        f"SELECT * FROM {STORE.t('job_templates')} WHERE id=%s", (template_id,)
    )
    if not template:
        flash("Template not found.", "error")
        return redirect(url_for("admin_job_templates"))
    
    try:
        robot_name = _pick_robot_name(STORE)
        ai_employer_name = _pick_employer_name(STORE)
        auto_conn, winner_completed_jobs, winner_success_rate, winner_style = _build_robot_winner_profile()
        
        job_id = STORE.execute(
            f"INSERT INTO {STORE.t('jobs')} (employer_id, title, description, category, job_type, budget_usd, "
            f"duration, connects_required, is_robot, ai_employer_name, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s,'open')",
            (None, template['title'], template['description'], template['category'], template['job_type'], 
             template['budget_usd'], template['duration'], template['connects_required'], ai_employer_name),
        )
        
        # Generate and set slug
        if job_id:
            slug = _generate_job_slug(template['title'], job_id)
            STORE.execute(f"UPDATE {STORE.t('jobs')} SET slug=%s WHERE id=%s", (slug, job_id))
        
        # Create robot winner profile
        awarded_at = datetime.utcnow() + timedelta(hours=random.randint(24, 36))
        STORE.execute(
            f"INSERT INTO {STORE.t('robot_winners')} "
            "(job_id, robot_name, connects_shown, completed_jobs, success_rate, selection_style, awarded_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (job_id, robot_name, auto_conn, winner_completed_jobs, winner_success_rate, winner_style, awarded_at),
        )
        
        flash(f"Job created from template! Robot: {robot_name}, Job ID: {job_id}", "success")
    except Exception as exc:
        flash(f"Error creating job: {str(exc)[:100]}", "error")
    
    return redirect(url_for("admin_job_templates"))


@app.route("/secure-admin-console-9x7k2m/payments")
@admin_required
def admin_payments():
    pays = STORE.query_all(
        f"SELECT p.*, u.email, u.full_name FROM {STORE.t('payments')} p "
        f"JOIN {STORE.t('users')} u ON u.id=p.user_id ORDER BY p.created_at DESC LIMIT 200")
    emp_pays = STORE.query_all(
        f"SELECT ep.*, eu.email as employer_email, wu.full_name as worker_name, wu.email as worker_email, j.title "
        f"FROM {STORE.t('employer_payments')} ep "
        f"JOIN {STORE.t('users')} eu ON eu.id=ep.employer_id "
        f"JOIN {STORE.t('users')} wu ON wu.id=ep.worker_user_id "
        f"JOIN {STORE.t('jobs')} j ON j.id=ep.job_id "
        f"ORDER BY ep.created_at DESC LIMIT 200")
    return render_template("admin/payments.html", pays=pays, emp_pays=emp_pays)


@app.route("/secure-admin-console-9x7k2m/payments/<int:pay_id>/disburse", methods=["POST"])
@admin_required
def admin_disburse(pay_id):
    if not verify_csrf():
        flash("Invalid request.", "error"); return redirect(url_for("admin_payments"))
    note = request.form.get("admin_note", "").strip()
    STORE.execute(
        f"UPDATE {STORE.t('employer_payments')} SET status='disbursed', admin_note=%s WHERE id=%s",
        (note, pay_id),
    )
    ep = STORE.query_one(f"SELECT * FROM {STORE.t('employer_payments')} WHERE id=%s", (pay_id,))
    if ep:
        push_notif(ep["worker_user_id"], "Payment disbursed to you by admin. Check your account.", url_for("worker_dashboard"))
        push_notif(ep["employer_id"], "Your payment has been processed and disbursed to the worker.", url_for("employer_payments"))
    flash("Payment marked as disbursed.", "success")
    return redirect(url_for("admin_payments"))


# ═══════════════════════════════════════════════════════════════════════════════
# SEO Routes
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/robots.txt")
def robots_txt():
    """Generate robots.txt for search engine crawling."""
    return """User-agent: *
Allow: /
Allow: /jobs
Allow: /worker/jobs
Allow: /employer/pricing
Allow: /about
Allow: /help-support
Disallow: /admin
Disallow: /login
Disallow: /register
Disallow: /profile
Disallow: /dashboard
Disallow: /employer/dashboard
Disallow: /worker/dashboard
Disallow: /contract
Sitemap: """ + url_for("sitemap_xml", _external=True, _scheme='https'), 200, {"Content-Type": "text/plain"}


@app.route("/sitemap.xml")
def sitemap_xml():
    """Generate sitemap index pointing to separate sitemaps."""
    from datetime import datetime
    
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    
    # Link to sub-sitemaps (just the type names, route adds "sitemap-" prefix and ".xml" suffix)
    sitemaps = [
        "static",
        "jobs",
    ]
    
    for sitemap_type in sitemaps:
        xml += f'  <sitemap>\n'
        xml += f'    <loc>{url_for("sitemap_by_type", sitemap_type=sitemap_type, _external=True, _scheme="https")}</loc>\n'
        xml += f'    <lastmod>{datetime.utcnow().isoformat()}Z</lastmod>\n'
        xml += f'  </sitemap>\n'
    
    xml += '</sitemapindex>'
    return Response(xml, mimetype="application/xml")


@app.route("/sitemap-<sitemap_type>.xml")
def sitemap_by_type(sitemap_type):
    """Generate individual sitemaps for different content types."""
    from datetime import datetime
    
    urls = []
    
    if sitemap_type == "static":
        # STATIC PAGES with accurate priorities and update frequencies
        static_pages = [
            ("index", 1.0, "weekly", None),                       # Homepage - changes with new jobs
            ("worker_jobs", 0.95, "daily", None),                 # Job listings - highest priority
            ("help_support", 0.7, "monthly", None),               # Help/Support
            ("about", 0.6, "yearly", None),                       # About
            ("contact", 0.6, "yearly", None),                     # Contact
            ("employer_pricing", 0.8, "monthly", None),           # Pricing page
            ("terms", 0.4, "yearly", None),                       # Legal
            ("privacy", 0.4, "yearly", None),                     # Legal
        ]
        
        for endpoint, priority, changefreq, lastmod in static_pages:
            try:
                urls.append({
                    "loc": url_for(endpoint, _external=True, _scheme='https'),
                    "priority": priority,
                    "changefreq": changefreq,
                    "lastmod": lastmod or datetime.utcnow().isoformat() + "Z",
                })
            except Exception as e:
                LOG.debug(f"Sitemap static page error ({endpoint}): {e}")
    
    elif sitemap_type == "jobs":
        # ALL ACTIVE JOBS (real + robot) - both are valuable SEO content
        try:
            jobs = STORE.query_all(
                f"SELECT id, slug, created_at, COALESCE(updated_at, created_at) as lastmod_col, status FROM {STORE.t('jobs')} "
                f"WHERE status='open' "
                f"ORDER BY created_at DESC LIMIT 50000",
                ()
            )
            for job in jobs:
                try:
                    # Use lastmod_col (which handles both updated_at and created_at)
                    lastmod = job.get("lastmod_col")
                    lastmod_str = lastmod.isoformat() + "Z" if isinstance(lastmod, datetime) else datetime.utcnow().isoformat() + "Z"
                    
                    # Use slug if available, otherwise ID
                    slug_or_id = job.get("slug") or job["id"]
                    urls.append({
                        "loc": url_for("worker_job_detail", slug_or_id=slug_or_id, _external=True, _scheme='https'),
                        "priority": 0.85,  # High priority for specific job listings
                        "changefreq": "weekly",  # Individual jobs don't change daily
                        "lastmod": lastmod_str,
                    })
                except Exception as e:
                    LOG.debug(f"Sitemap job error (ID {job.get('id')}): {e}")
        except Exception as e:
            LOG.debug(f"Sitemap jobs query error: {e}")
    
    # Generate XML for this sitemap
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    
    if not urls:
        LOG.warning(f"Sitemap '{sitemap_type}' has no URLs to include")
    
    for url in urls:
        xml += '  <url>\n'
        xml += f'    <loc>{url["loc"]}</loc>\n'
        xml += f'    <lastmod>{url["lastmod"]}</lastmod>\n'
        xml += f'    <changefreq>{url["changefreq"]}</changefreq>\n'
        xml += f'    <priority>{url["priority"]:.1f}</priority>\n'
        xml += '  </url>\n'
    
    xml += '</urlset>'
    return Response(xml, mimetype="application/xml")


# ═══════════════════════════════════════════════════════════════════════════════
# Verification Files (Google Search Console, etc.)
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/<filename>")
def serve_verification_file(filename):
    """Serve Google Search Console verification files from static folder."""
    # Only serve google verification files (google*.html)
    if filename.startswith("google") and filename.endswith(".html"):
        try:
            return send_from_directory(ROOT / "static", filename)
        except Exception:
            pass
    return flask_abort(404)


# ═══════════════════════════════════════════════════════════════════════════════
# Error Handlers
# ═══════════════════════════════════════════════════════════════════════════════
@app.errorhandler(404)
def not_found(e):
    return render_template("errors/404.html"), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("errors/500.html"), 500

@app.errorhandler(413)
def too_large(e):
    flash("File too large. Maximum upload size is 5MB.", "error")
    return redirect(request.referrer or url_for("index"))


# ═══════════════════════════════════════════════════════════════════════════════
# Admin Job Reports
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/secure-admin-console-9x7k2m/job-reports")
@admin_required
def admin_job_reports():
    """View all job reports (fake job submissions)."""
    status_filter = request.args.get("status", "").strip()
    
    query = f"""
        SELECT 
            r.id, r.job_id, r.reporter_id, r.reason, r.details, r.status, 
            r.admin_notes, r.created_at, r.reviewed_at, r.reviewed_by_id,
            j.title as job_title, 
            u.email as reporter_email
        FROM {STORE.t('job_reports')} r
        JOIN {STORE.t('jobs')} j ON j.id = r.job_id
        JOIN {STORE.t('users')} u ON u.id = r.reporter_id
    """
    
    params = []
    if status_filter:
        query += f" WHERE r.status=%s"
        params.append(status_filter)
    
    query += " ORDER BY r.created_at DESC"
    
    reports = STORE.query_all(query, tuple(params))
    return render_template("admin/job_reports.html", reports=reports)


@app.route("/secure-admin-console-9x7k2m/job-reports/mark-reviewed", methods=["POST"])
@admin_required
def admin_mark_report_reviewed():
    """Mark a report as reviewed."""
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("admin_job_reports"))
    
    report_id = request.form.get("report_id", type=int)
    if not report_id:
        flash("Invalid report ID.", "error")
        return redirect(url_for("admin_job_reports"))
    
    uid = session.get("user_id")
    STORE.execute(
        f"UPDATE {STORE.t('job_reports')} "
        f"SET status='reviewed', reviewed_at=NOW(), reviewed_by_id=%s "
        f"WHERE id=%s",
        (uid, report_id)
    )
    
    audit_log(uid, "REPORT_MARKED_REVIEWED", "job_report", report_id)
    flash("Report marked as reviewed.", "success")
    return redirect(url_for("admin_job_reports"))


@app.route("/secure-admin-console-9x7k2m/job-reports/dismiss", methods=["POST"])
@admin_required
def admin_dismiss_report():
    """Dismiss a report as not actionable."""
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("admin_job_reports"))
    
    report_id = request.form.get("report_id", type=int)
    if not report_id:
        flash("Invalid report ID.", "error")
        return redirect(url_for("admin_job_reports"))
    
    uid = session.get("user_id")
    STORE.execute(
        f"UPDATE {STORE.t('job_reports')} "
        f"SET status='dismissed', reviewed_at=NOW(), reviewed_by_id=%s "
        f"WHERE id=%s",
        (uid, report_id)
    )
    
    audit_log(uid, "REPORT_DISMISSED", "job_report", report_id)
    flash("Report dismissed.", "success")
    return redirect(url_for("admin_job_reports"))


@app.route("/secure-admin-console-9x7k2m/job-reports/take-action", methods=["POST"])
@admin_required
def admin_take_action_on_report():
    """Take action on a report: delete job and mark report as action_taken."""
    if not verify_csrf():
        flash("Invalid request.", "error")
        return redirect(url_for("admin_job_reports"))
    
    report_id = request.form.get("report_id", type=int)
    job_id = request.form.get("job_id", type=int)
    
    if not report_id or not job_id:
        flash("Invalid request.", "error")
        return redirect(url_for("admin_job_reports"))
    
    uid = session.get("user_id")
    
    # Delete the job (cascades to applications, contracts, messages, etc.)
    STORE.execute(f"DELETE FROM {STORE.t('jobs')} WHERE id=%s", (job_id,))
    
    # Mark report as action_taken
    STORE.execute(
        f"UPDATE {STORE.t('job_reports')} "
        f"SET status='action_taken', reviewed_at=NOW(), reviewed_by_id=%s, "
        f"admin_notes=CONCAT(COALESCE(admin_notes, ''), 'Job deleted by admin action.\\n') "
        f"WHERE id=%s",
        (uid, report_id)
    )
    
    audit_log(uid, "FAKE_JOB_DELETED", "job", job_id, details=f"Report ID: {report_id}")
    LOG.info(f"Admin {uid} deleted job {job_id} based on fraud report {report_id}")
    flash("Job has been deleted and report marked as action taken.", "success")
    return redirect(url_for("admin_job_reports"))


# ═══════════════════════════════════════════════════════════════════════════════
# WebSocket Events (Flask-SocketIO)
# ═══════════════════════════════════════════════════════════════════════════════

@socketio.on('connect')
def handle_connect():
    """Handle new WebSocket connection."""
    LOG.info(f"Client connected: {request.sid}")
    emit('connection_response', {'data': 'Connected to server'})

@socketio.on('join_contract')
def on_join_contract(data):
    """Join a contract room for real-time messaging."""
    job_id = data.get('job_id')
    user_id = data.get('user_id')
    
    if not job_id or not user_id:
        emit('error', {'message': 'Missing job_id or user_id'})
        return
    
    # Verify user has access to this contract
    job = STORE.query_one(f"SELECT employer_id FROM {STORE.t('jobs')} WHERE id=%s", (job_id,))
    app = STORE.query_one(f"SELECT user_id FROM {STORE.t('applications')} WHERE job_id=%s AND status='accepted'", (job_id,))
    
    if not job or not app or (user_id != job["employer_id"] and user_id != app["user_id"]):
        emit('error', {'message': 'Unauthorized access to contract'})
        return
    
    # Create unique room name for this contract
    room = f"contract_{job_id}"
    join_room(room)
    
    # Keep-alive: update session to mark user as active
    _keepalive_session(user_id, job_id)
    
    LOG.info(f"User {user_id} joined contract room: {room}")
    emit('joined_contract', {'room': room, 'job_id': job_id}, room=room)

@socketio.on('send_message')
def handle_send_message(data):
    """Handle incoming message from client."""
    job_id = data.get('job_id')
    message_text = data.get('message')
    user_id = data.get('user_id')
    
    if not job_id or not message_text or not user_id:
        emit('error', {'message': 'Invalid message data'})
        return
    
    # Verify user has access to this contract
    job = STORE.query_one(f"SELECT employer_id FROM {STORE.t('jobs')} WHERE id=%s", (job_id,))
    app = STORE.query_one(f"SELECT user_id FROM {STORE.t('applications')} WHERE job_id=%s AND status='accepted'", (job_id,))
    
    if not job or not app or (user_id != job["employer_id"] and user_id != app["user_id"]):
        emit('error', {'message': 'Unauthorized'})
        return
    
    try:
        # Keep-alive: update session to mark user as active
        _keepalive_session(user_id, job_id)
        
        # Queue message for background processing (same as HTTP endpoint)
        MESSAGE_QUEUE.put((job_id, user_id, message_text), timeout=1)
        
        # Generate temporary ID for immediate display
        msg_id = int(time.time() * 1000000) % 9999999
        
        # Get sender info
        user = STORE.query_one(
            f"SELECT full_name FROM {STORE.t('users')} WHERE id=%s",
            (user_id,)
        )
        sender_name = user.get('full_name', 'Unknown') if user else 'Unknown'
        
        # Broadcast message only to OTHER users in the contract room
        # (sender already has it from optimistic update)
        room = f"contract_{job_id}"
        emit('new_message', {
            'id': msg_id,
            'sender_id': user_id,
            'sender_name': sender_name,
            'message': message_text,
            'created_at_label': datetime.now().strftime('%I:%M %p')
        }, room=room, skip_sid=request.sid)
        
        LOG.info(f"Message sent in contract {job_id}: {message_text[:50]}")
    
    except queue.Full:
        LOG.error(f"Message queue full for job_id={job_id}")
        emit('error', {'message': 'System busy, please try again'})
    except Exception as e:
        LOG.error(f"Error queueing message: {e}")
        emit('error', {'message': 'Failed to send message'})

@socketio.on('heartbeat')
def handle_heartbeat(data):
    """Handle user heartbeat to mark as online."""
    job_id = data.get('job_id')
    user_id = data.get('user_id')
    
    if job_id and user_id:
        # Update online status
        CONTRACT_ROOM_SESSIONS.setdefault(job_id, {})[user_id] = {
            'session_id': request.sid,
            'last_keepalive': time.time()
        }
        
        room = f"contract_{job_id}"
        emit('user_online', {'user_id': user_id}, room=room)

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnect."""
    LOG.info(f"Client disconnected: {request.sid}")

# ═══════════════════════════════════════════════════════════════════════════════
# HTTP Caching Headers
# ═══════════════════════════════════════════════════════════════════════════════
@app.after_request
def set_cache_headers(response):
    """Set appropriate Cache-Control headers based on content type."""
    if request.path.startswith('/static/'):
        # Cache static files (CSS, JS, images) for 1 year
        response.cache_control.max_age = 31536000
        response.cache_control.public = True
    elif 'application/json' in response.content_type:
        # Don't cache API responses
        response.cache_control.max_age = 0
        response.cache_control.no_cache = True
        response.cache_control.no_store = True
    elif 'text/html' in response.content_type:
        # Cache HTML pages for 5 minutes
        response.cache_control.max_age = 300
        response.cache_control.must_revalidate = True
    
    # Allow unload event for compatibility with older tracking libraries
    # (suppressess browser Permissions Policy violations for deprecated unload event)
    response.headers['Permissions-Policy'] = 'unload=(self)'
    
    return response

# ═══════════════════════════════════════════════════════════════════════════════
# App Startup
# ═══════════════════════════════════════════════════════════════════════════════
def _startup() -> None:
    STORE.ensure_schema()
    # Start background email worker
    t_email = threading.Thread(target=_email_worker, daemon=True)
    t_email.start()
    LOG.info("TechBid Marketplace started (AI generation: manual only, once/day max, cooldown enforced).")


_startup()

if __name__ == "__main__":
    # Start message queue worker
    _start_message_worker()
    
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")
    LOG.info("Starting on http://%s:%d", "127.0.0.1", port)
    socketio.run(app, host=host, port=port, debug=False)
