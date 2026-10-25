"""
app/extensions.py — Shared Flask extension singletons.

Each extension is created here without an app object.  The actual
initialization (binding to the app) happens inside create_app() via
the extension's init_app() pattern.  This avoids circular imports between
the app factory, models, and blueprints.

Import these objects wherever you need them:

    from app.extensions import db, login_manager, bcrypt, migrate
"""

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_bcrypt import Bcrypt

# ── SQLAlchemy ORM ─────────────────────────────────────────────────────────────
db = SQLAlchemy()

# ── Alembic-based schema migrations ───────────────────────────────────────────
migrate = Migrate()

# ── Session-based authentication ──────────────────────────────────────────────
login_manager = LoginManager()
# The view function name Flask-Login redirects to when @login_required fails.
login_manager.login_view = "auth.login"                    # type: ignore[assignment]
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "warning"

# ── Password hashing ──────────────────────────────────────────────────────────
bcrypt = Bcrypt()
