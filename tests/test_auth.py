"""
tests/test_auth.py — Unit tests for registration, login, and logout.

Each test uses the `client` and `db` fixtures from conftest.py.
The database is SQLite in-memory; each test is wrapped in a rollback
so tests are independent.

Test categories
───────────────
Registration:
  - Valid registration creates a user and redirects to login.
  - First registered user gets the admin role.
  - Duplicate username is rejected.
  - Duplicate email is rejected.
  - Short password is rejected.
  - Mismatched confirm-password is rejected.
  - Whitespace-only username is rejected.

Login:
  - Valid credentials create a session.
  - Wrong password is rejected with a generic error (no info leak).
  - Unregistered email is rejected with the same generic error.
  - Deactivated account is rejected.

Logout:
  - Logged-in user can log out.
  - Unauthenticated request to /auth/logout is redirected to login.

Security:
  - Password is stored as a bcrypt hash (not plain text).
  - Open-redirect in ?next= is blocked.
"""

import pytest
from flask import session

from app.models import User, UserRole
from tests.conftest import login_user, register_user


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistration:

    def test_valid_registration_redirects_to_login(self, client, db):
        """A valid POST to /auth/register should redirect to the login page."""
        response = register_user(client)
        assert response.status_code == 200
        # After follow_redirects=True, we should be on the login page.
        assert b"Log in" in response.data or b"log in" in response.data.lower()

    def test_valid_registration_creates_user_in_db(self, client, db):
        """The user record should exist in the database after registration."""
        register_user(client, username="bob", email="bob@example.com")
        user = User.query.filter_by(username="bob").first()
        assert user is not None
        assert user.email == "bob@example.com"

    def test_first_user_gets_admin_role(self, client, db):
        """The very first registered user should be granted the admin role."""
        register_user(client, username="first", email="first@example.com")
        user = User.query.filter_by(username="first").first()
        assert user is not None
        assert user.role == UserRole.ADMIN.value

    def test_second_user_gets_customer_role(self, client, db):
        """All users after the first should be customers."""
        register_user(client, username="admin_user", email="admin@example.com")
        register_user(client, username="customer_user", email="customer@example.com")
        user = User.query.filter_by(username="customer_user").first()
        assert user is not None
        assert user.role == UserRole.CUSTOMER.value

    def test_password_is_hashed_not_plain(self, client, db):
        """The stored password_hash must not equal the plain-text password."""
        register_user(client, username="charlie", email="charlie@example.com",
                      password="MySecret99!")
        user = User.query.filter_by(username="charlie").first()
        assert user is not None
        assert user.password_hash != "MySecret99!"
        assert user.password_hash.startswith("$2b$")  # bcrypt prefix

    def test_duplicate_username_rejected(self, client, db):
        """Submitting the same username twice must show a validation error."""
        register_user(client, username="dup", email="dup1@example.com")
        response = register_user(client, username="dup", email="dup2@example.com")
        assert b"already taken" in response.data
        # Only one user with that username should exist.
        assert User.query.filter_by(username="dup").count() == 1

    def test_duplicate_email_rejected(self, client, db):
        """Submitting the same email address twice must show a validation error."""
        register_user(client, username="user1", email="shared@example.com")
        response = register_user(client, username="user2", email="shared@example.com")
        assert b"already exists" in response.data
        assert User.query.filter_by(email="shared@example.com").count() == 1

    def test_short_password_rejected(self, client, db):
        """A password shorter than 8 characters must fail validation."""
        response = register_user(
            client, username="shortpw", email="shortpw@example.com", password="abc"
        )
        # The form should re-render with an error, not redirect to login.
        assert b"at least 8" in response.data or b"8 characters" in response.data
        assert User.query.filter_by(username="shortpw").first() is None

    def test_mismatched_passwords_rejected(self, client, db):
        """confirm_password ≠ password must fail validation."""
        response = client.post(
            "/auth/register",
            data={
                "username": "mismatch",
                "email": "mismatch@example.com",
                "password": "Password1!",
                "confirm_password": "Different1!",
            },
            follow_redirects=True,
        )
        assert b"do not match" in response.data
        assert User.query.filter_by(username="mismatch").first() is None

    def test_empty_username_rejected(self, client, db):
        """An empty username must fail the DataRequired validator."""
        response = client.post(
            "/auth/register",
            data={
                "username": "",
                "email": "empty@example.com",
                "password": "Password1!",
                "confirm_password": "Password1!",
            },
            follow_redirects=True,
        )
        assert b"required" in response.data.lower()
        assert User.query.filter_by(email="empty@example.com").first() is None

    def test_invalid_email_rejected(self, client, db):
        """A malformed email address must fail the Email() validator."""
        response = client.post(
            "/auth/register",
            data={
                "username": "bademail",
                "email": "not-an-email",
                "password": "Password1!",
                "confirm_password": "Password1!",
            },
            follow_redirects=True,
        )
        assert b"valid email" in response.data.lower()
        assert User.query.filter_by(username="bademail").first() is None


# ─────────────────────────────────────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────────────────────────────────────

class TestLogin:

    def test_valid_login_creates_session(self, client, db):
        """Correct credentials must log the user in (session is set)."""
        register_user(client, username="logintest", email="logintest@example.com",
                      password="Password1!")
        response = login_user(client, email="logintest@example.com", password="Password1!")
        assert response.status_code == 200
        # After login, the welcome message or username should appear.
        assert b"logintest" in response.data or b"Welcome" in response.data

    def test_wrong_password_rejected(self, client, db):
        """Wrong password must show a generic error without revealing whether
        the email address is registered."""
        register_user(client, username="pwtest", email="pwtest@example.com",
                      password="Password1!")
        response = login_user(client, email="pwtest@example.com", password="WrongPass!")
        assert b"Invalid email" in response.data or b"Invalid" in response.data

    def test_unregistered_email_rejected(self, client, db):
        """An email that was never registered must show the same generic error."""
        response = login_user(client, email="ghost@example.com", password="Password1!")
        assert b"Invalid" in response.data

    def test_deactivated_account_rejected(self, client, db, app):
        """A deactivated user must not be able to log in."""
        register_user(client, username="deactivated", email="deactivated@example.com",
                      password="Password1!")
        with app.app_context():
            from app.extensions import db as _db
            user = User.query.filter_by(username="deactivated").first()
            user.is_active = False
            _db.session.commit()

        response = login_user(client, email="deactivated@example.com", password="Password1!")
        assert b"deactivated" in response.data.lower()

    def test_already_authenticated_redirected(self, client, db):
        """A logged-in user visiting /auth/login is redirected to the shop."""
        register_user(client, username="alreadyin", email="alreadyin@example.com",
                      password="Password1!")
        login_user(client, email="alreadyin@example.com", password="Password1!")
        response = client.get("/auth/login", follow_redirects=False)
        # Should redirect (302) to the shop catalogue.
        assert response.status_code == 302


# ─────────────────────────────────────────────────────────────────────────────
# Logout
# ─────────────────────────────────────────────────────────────────────────────

class TestLogout:

    def test_logout_clears_session(self, client, db):
        """After logout, accessing a protected route redirects to login."""
        register_user(client, username="logouttest", email="logout@example.com",
                      password="Password1!")
        login_user(client, email="logout@example.com", password="Password1!")

        logout_response = client.get("/auth/logout", follow_redirects=True)
        response_text = logout_response.data
        assert b"logged out" in response_text.lower() or b"Log in" in response_text

    def test_unauthenticated_logout_redirects_to_login(self, client, db):
        """A request to /auth/logout without a session redirects to login."""
        response = client.get("/auth/logout", follow_redirects=False)
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]


# ─────────────────────────────────────────────────────────────────────────────
# Security
# ─────────────────────────────────────────────────────────────────────────────

class TestSecurity:

    def test_open_redirect_blocked(self, client, db):
        """
        ?next=http://evil.com, //evil.com, and \\evil.com must not redirect off-site after login.
        The _safe_next_url() helper should discard absolute and malicious URLs.
        """
        register_user(client, username="redirect_test", email="redirect@example.com",
                      password="Password1!")
        for malicious_target in ["http://evil.com", "//evil.com", "\\\\evil.com", "javascript:alert(1)"]:
            response = client.post(
                f"/auth/login?next={malicious_target}",
                data={
                    "email": "redirect@example.com",
                    "password": "Password1!",
                    "remember_me": False,
                },
                follow_redirects=False,
            )
            # Should redirect (302) but NOT to the malicious target.
            assert response.status_code == 302
            location = response.headers.get("Location", "")
            assert "evil.com" not in location
            assert "javascript:" not in location
            assert location.startswith("/shop")

    def test_health_endpoint(self, client, db):
        """The /api/v1/health endpoint must return 200 with JSON."""
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "ok"
