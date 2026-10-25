"""
app/config.py — Configuration classes for CartOS.

Three environments are supported:
  development  Local development with DEBUG mode on.
  testing      Automated tests; uses SQLite so no MySQL server is needed.
  production   Deployment; DEBUG off, real secret key required.

The active environment is chosen by the FLASK_CONFIG environment variable
(defaults to 'development').

Design note on worker settings
-------------------------------
ENGINE_* settings are read here so that the WorkerManager (Phase 4) can
retrieve them from app.config.  They have no effect in Phase 2 — the engine
is not started during app creation.
"""

import os


class Config:
    """Base configuration shared by all environments."""

    # ── Security ──────────────────────────────────────────────────────────────
    # In production, SECRET_KEY must be set in the environment; there is
    # no acceptable fallback.
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev-insecure-key-replace-me")

    # ── Database ──────────────────────────────────────────────────────────────
    # Build DATABASE_URL from individual components if the full URL is absent.
    @staticmethod
    def _build_db_url() -> str:
        url = os.environ.get("DATABASE_URL")
        if url:
            return url
        host = os.environ.get("MYSQL_HOST", "localhost")
        port = os.environ.get("MYSQL_PORT", "3306")
        user = os.environ.get("MYSQL_USER", "cartos_user")
        password = os.environ.get("MYSQL_PASSWORD", "")
        db = os.environ.get("MYSQL_DB", "cartos_db")
        return f"mysql+pymysql://{user}:{password}@{host}:{port}/{db}"

    SQLALCHEMY_DATABASE_URI: str = _build_db_url.__func__()  # type: ignore[attr-defined]
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        # Recycle connections before MySQL's default 8-hour wait_timeout.
        "pool_recycle": 280,
        # Issue a lightweight SELECT 1 before using a pooled connection to
        # detect stale connections after a MySQL restart.
        "pool_pre_ping": True,
    }

    # ── Flask-WTF CSRF ────────────────────────────────────────────────────────
    WTF_CSRF_ENABLED: bool = True

    # ── Engine / Worker settings (read by WorkerManager in Phase 4) ───────────
    ENGINE_WORKER_COUNT: int = int(os.environ.get("ENGINE_WORKER_COUNT", 2))
    ENGINE_HEARTBEAT_TIMEOUT: int = int(os.environ.get("ENGINE_HEARTBEAT_TIMEOUT", 60))
    ENGINE_TASK_TIMEOUT: int = int(os.environ.get("ENGINE_TASK_TIMEOUT", 300))

    # ── Pagination ────────────────────────────────────────────────────────────
    PRODUCTS_PER_PAGE: int = 12
    ORDERS_PER_PAGE: int = 20


class DevelopmentConfig(Config):
    """Local development — verbose errors, no HTTPS requirement."""
    DEBUG: bool = True


class TestingConfig(Config):
    """
    Automated tests — isolated SQLite database, CSRF disabled.

    SQLite is used so that unit tests run without a MySQL server.
    Phase 3 concurrent-checkout tests use a separate MySQL test database
    because they require InnoDB row-level locking and SKIP LOCKED.
    """
    TESTING: bool = True
    DEBUG: bool = True
    # In-memory SQLite; each test session starts with a clean schema.
    SQLALCHEMY_DATABASE_URI: str = "sqlite:///:memory:"
    # Disable CSRF so test client can POST forms without a token.
    WTF_CSRF_ENABLED: bool = False
    # Faster bcrypt rounds for tests (default 12 rounds is intentionally slow).
    BCRYPT_LOG_ROUNDS: int = 4


class ProductionConfig(Config):
    """
    Production deployment.

    Before deploying, ensure:
      - SECRET_KEY is set to a cryptographically random 32+ byte value.
      - DATABASE_URL points to your production MySQL server.
      - DEBUG is False (enforced here).
    """
    DEBUG: bool = False

    def __init__(self) -> None:
        if self.SECRET_KEY == "dev-insecure-key-replace-me":
            raise RuntimeError(
                "SECRET_KEY must be set to a secure random value in production. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )


# ── Config registry ───────────────────────────────────────────────────────────
# create_app() selects the right class using the FLASK_CONFIG env variable.
config: dict = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
