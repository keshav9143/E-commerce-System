"""app/admin/__init__.py — Admin blueprint (dashboard, inventory, config — Phase 5)."""
from flask import Blueprint

admin_bp = Blueprint("admin", __name__, template_folder="../../templates/admin")
