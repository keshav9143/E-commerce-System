"""
app/auth/routes.py — Registration, login, and logout views.

Authorization rules
───────────────────
- Logged-in users are redirected away from /auth/login and /auth/register.
- The 'next' URL after login is validated to be a relative path to prevent
  open-redirect attacks.
- Passwords are never logged.

Security: timing-safe login
────────────────────────────
Even when the email is not found, we call bcrypt.check_password_hash() against
a dummy hash.  This prevents timing attacks that could reveal whether an email
address is registered (the attacker would otherwise notice that invalid emails
return faster than valid-email/wrong-password responses).
"""

import logging
from urllib.parse import urlparse

from flask import (
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from app.extensions import bcrypt, db
from app.models import SchedulingConfig, User, UserRole
from app.auth import auth_bp
from app.auth.forms import LoginForm, RegistrationForm

logger = logging.getLogger(__name__)

# A dummy hash used for constant-time comparison when the email is not found.
# It must be a valid bcrypt hash so that check_password_hash() doesn't
# short-circuit before performing the full bcrypt computation.
_DUMMY_HASH = "$2b$12$KIXmb/.lKGb14vXSqZ3lnOenrKj1R2JN0GJl2KnzFSXs.5NQMR6Eu"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_next_url(next_url: str | None) -> str:
    """
    Return a safe redirect target after login.

    Flask-Login appends ?next=<url> when redirecting to the login page.
    We validate that the target is a relative URL (same host) to prevent
    open-redirect attacks.  Absolute URLs (e.g. http://evil.com), protocol-relative
    URLs (//evil.com), and Windows-style paths (\\\\evil.com) are discarded.
    """
    if not next_url:
        return url_for("shop.catalogue")
    if next_url.startswith("//") or "\\" in next_url or not next_url.startswith("/"):
        return url_for("shop.catalogue")
    parsed = urlparse(next_url)
    # A relative URL has no scheme or netloc.
    if parsed.scheme or parsed.netloc:
        return url_for("shop.catalogue")
    return next_url


def _ensure_scheduling_config_exists() -> None:
    """
    Create the singleton SchedulingConfig row if it does not yet exist.

    Called after the first successful registration so that the admin
    dashboard always has a config row to read.
    """
    if not SchedulingConfig.query.first():
        db.session.add(SchedulingConfig())
        db.session.commit()
        logger.info("SchedulingConfig singleton created.")


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """
    GET  — render the registration form.
    POST — validate input, create user, redirect to login.

    The first registered user is automatically granted the ADMIN role so that
    the admin dashboard is accessible without a manual DB update.
    """
    if current_user.is_authenticated:
        return redirect(url_for("shop.catalogue"))

    form = RegistrationForm()
    if form.validate_on_submit():
        is_first_user = User.query.count() == 0
        role = UserRole.ADMIN.value if is_first_user else UserRole.CUSTOMER.value

        user = User(
            username=form.username.data.strip(),
            email=form.email.data.strip().lower(),
            role=role,
        )
        user.set_password(form.password.data, bcrypt)

        db.session.add(user)
        db.session.commit()

        # Ensure the scheduler config singleton exists.
        _ensure_scheduling_config_exists()

        logger.info(
            "New user registered: id=%s username=%r role=%s",
            user.id, user.username, user.role,
        )
        flash(
            f"Account created! You are registered as {'an administrator' if is_first_user else 'a customer'}. "
            "Please log in.",
            "success",
        )
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html", form=form, title="Create account")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """
    GET  — render the login form.
    POST — validate credentials, create session, redirect.
    """
    if current_user.is_authenticated:
        return redirect(url_for("shop.catalogue"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.strip().lower()).first()

        if user and user.check_password(form.password.data, bcrypt):
            if not user.is_active:
                flash("Your account has been deactivated. Please contact support.", "danger")
                return render_template("auth/login.html", form=form, title="Log in")

            login_user(user, remember=form.remember_me.data)
            logger.info("User logged in: id=%s username=%r", user.id, user.username)
            flash(f"Welcome back, {user.username}!", "success")
            return redirect(_safe_next_url(request.args.get("next")))
        else:
            # Perform a constant-time dummy check to prevent timing attacks.
            bcrypt.check_password_hash(_DUMMY_HASH, form.password.data)
            logger.warning(
                "Failed login attempt for email=%r from IP=%s",
                form.email.data, request.remote_addr,
            )
            flash("Invalid email address or password.", "danger")

    return render_template("auth/login.html", form=form, title="Log in")


@auth_bp.route("/logout")
@login_required
def logout():
    """Clear the session and redirect to the login page."""
    username = current_user.username
    logout_user()
    logger.info("User logged out: username=%r", username)
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
