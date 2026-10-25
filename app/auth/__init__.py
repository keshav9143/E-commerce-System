"""app/auth/__init__.py — Authentication blueprint."""
from flask import Blueprint

auth_bp = Blueprint("auth", __name__, template_folder="../../templates/auth")

# Import routes AFTER creating the blueprint to avoid circular imports.
# This registers all @auth_bp.route() decorators with the blueprint object.
from app.auth import routes  # noqa: F401, E402
