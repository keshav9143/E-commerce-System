"""
app/__init__.py — Flask application factory.

Usage:
    from app import create_app
    app = create_app()                    # development (default)
    app = create_app('testing')           # unit tests
    app = create_app('production')        # deployment

Design decision — no auto-start of the worker engine
──────────────────────────────────────────────────────
The background task engine (WorkerManager + worker threads) is NOT started
here.  Starting it automatically would cause:
  - Flask development server: two engine instances (werkzeug reloader forks).
  - pytest: worker threads spawned on every test session import.
  - flask db migrate / flask db upgrade: unnecessary threads during migrations.

The engine is started explicitly via the CLI:
    flask engine start --workers 3

This command is registered below as a Click command group but implemented
in app/engine/worker_manager.py (Phase 4).
"""

import logging
import os

import click
from flask import Flask, render_template

from app.config import config
from app.extensions import bcrypt, db, login_manager, migrate


def create_app(config_name: str | None = None) -> Flask:
    """
    Create and configure the Flask application.

    Parameters
    ----------
    config_name:
        One of 'development', 'testing', 'production', or 'default'.
        Falls back to the FLASK_CONFIG environment variable, then 'default'.
    """
    if config_name is None:
        config_name = os.environ.get("FLASK_CONFIG", "default")

    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config[config_name])

    # ── Initialise extensions ─────────────────────────────────────────────────
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    bcrypt.init_app(app)

    # ── Register blueprints ───────────────────────────────────────────────────
    _register_blueprints(app)

    # ── Register template filters ─────────────────────────────────────────────
    _register_template_filters(app)

    # ── Register error handlers ───────────────────────────────────────────────
    _register_error_handlers(app)

    # ── Register CLI commands ─────────────────────────────────────────────────
    _register_cli(app)

    # ── Configure logging ─────────────────────────────────────────────────────
    _configure_logging(app)

    return app


# ─────────────────────────────────────────────────────────────────────────────
# Blueprint registration
# ─────────────────────────────────────────────────────────────────────────────

def _register_blueprints(app: Flask) -> None:
    from app.auth import auth_bp
    app.register_blueprint(auth_bp, url_prefix="/auth")

    # Stub blueprints — routes will be added in later phases.
    from app.shop import shop_bp
    app.register_blueprint(shop_bp, url_prefix="/shop")

    from app.orders import orders_bp
    app.register_blueprint(orders_bp, url_prefix="/orders")

    from app.admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix="/admin")

    from app.api import api_bp
    app.register_blueprint(api_bp, url_prefix="/api/v1")

    # Root route — home page.
    from flask import redirect, url_for

    @app.route("/")
    def index():
        return redirect(url_for("shop.catalogue"))


# ─────────────────────────────────────────────────────────────────────────────
# Template filters
# ─────────────────────────────────────────────────────────────────────────────

def _register_template_filters(app: Flask) -> None:
    """Register custom Jinja2 template filters."""
    from app.currency import format_inr

    # Available in templates as {{ amount|inr }} or {{ amount|currency }}
    app.jinja_env.filters["inr"] = format_inr
    app.jinja_env.filters["currency"] = format_inr


# ─────────────────────────────────────────────────────────────────────────────
# Error handlers
# ─────────────────────────────────────────────────────────────────────────────

def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def internal_error(e):
        # Roll back any open transaction so the DB session is usable again.
        db.session.rollback()
        return render_template("errors/500.html"), 500


# ─────────────────────────────────────────────────────────────────────────────
# CLI commands
# ─────────────────────────────────────────────────────────────────────────────

def _register_cli(app: Flask) -> None:
    """Register custom Flask CLI commands."""

    @app.cli.group()
    def engine():
        """Commands for the CartOS task-processing engine."""

    @engine.command("start")
    @click.option(
        "--workers",
        default=None,
        type=int,
        help="Number of worker threads (overrides scheduling_config.worker_count).",
    )
    def engine_start(workers: int | None):
        """
        Start the background task-processing engine.

        This command blocks until interrupted with Ctrl+C, then shuts down
        workers cleanly.  Run it in a separate terminal alongside `flask run`.

        Example:
            flask engine start
            flask engine start --workers 4
        """
        # Import deferred to Phase 4; raises ImportError with a clear message
        # until the engine module is implemented.
        try:
            from app.engine.worker_manager import WorkerManager
        except ImportError:
            click.echo(
                "Engine not yet implemented (Phase 4).  "
                "The engine module is at app/engine/worker_manager.py."
            )
            return

        import time

        manager = WorkerManager(worker_count_override=workers)
        click.echo(f"Starting engine with {manager.worker_count} workers …")
        manager.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            click.echo("\nShutting down engine …")
            manager.stop()
            click.echo("Engine stopped.")

    @engine.command("status")
    def engine_status():
        """Print current worker and task queue status."""
        from app.models import Worker, FulfillmentTask, WorkerStatus, TaskStatus

        with app.app_context():
            workers = Worker.query.all()
            click.echo(f"Workers: {len(workers)}")
            for w in workers:
                click.echo(f"  {w.name}: {w.status}")

            for status in [TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.FAILED]:
                count = FulfillmentTask.query.filter_by(status=status.value).count()
                click.echo(f"  Tasks {status.value}: {count}")


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def _configure_logging(app: Flask) -> None:
    """Set up structured logging to stdout."""
    level = logging.DEBUG if app.config.get("DEBUG") else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    app.logger.setLevel(level)
