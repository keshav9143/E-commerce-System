"""
tests/conftest.py — pytest fixtures shared across all test modules.

Database strategy
─────────────────
Unit tests (auth, models, forms) use an in-memory SQLite database.
This avoids requiring a running MySQL server and keeps tests fast.
Limitations compared to MySQL:
  - CHECK constraints are not enforced by SQLite < 3.25 (we check in Python).
  - ENUM columns become VARCHAR in SQLite (constraint is in application code).
  - SELECT … FOR UPDATE SKIP LOCKED is not supported.

Phase 3 concurrent-checkout tests use a real MySQL database (see
tests/test_checkout_concurrent.py) because they test InnoDB-specific behaviour.
The MySQL test URI is read from CARTOS_TEST_MYSQL_URL environment variable;
those tests are skipped automatically if the variable is not set.
"""

import pytest

from app import create_app
from app.extensions import db as _db


@pytest.fixture(scope="session")
def app():
    """
    Create a Flask application configured for testing.

    scope='session' means one app is created for the entire test run.
    The database schema is created once and shared; individual tests roll
    back their changes using the `db` fixture below.
    """
    _app = create_app("testing")
    with _app.app_context():
        _db.create_all()
        yield _app
        _db.drop_all()


@pytest.fixture(scope="function")
def db(app):
    """
    Provide a clean database session for each test function.

    After each test, we truncate all tables and remove the session
    so that tests remain isolated even when application routes commit
    transactions.
    """
    with app.app_context():
        yield _db
        _db.session.remove()
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()


@pytest.fixture(scope="function")
def client(app):
    """Flask test client.  CSRF is disabled in TestingConfig."""
    return app.test_client()


@pytest.fixture(scope="function")
def runner(app):
    """Flask CLI runner for testing Click commands."""
    return app.test_cli_runner()


# ─────────────────────────────────────────────────────────────────────────────
# Convenience helpers
# ─────────────────────────────────────────────────────────────────────────────

def register_user(client, username="alice", email="alice@example.com", password="Password1!"):
    """POST to /auth/register and return the response."""
    return client.post(
        "/auth/register",
        data={
            "username": username,
            "email": email,
            "password": password,
            "confirm_password": password,
        },
        follow_redirects=True,
    )


def login_user(client, email="alice@example.com", password="Password1!"):
    """POST to /auth/login and return the response."""
    return client.post(
        "/auth/login",
        data={"email": email, "password": password, "remember_me": False},
        follow_redirects=True,
    )
