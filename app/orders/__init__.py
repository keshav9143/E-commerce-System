"""app/orders/__init__.py — Orders blueprint (checkout, order history — Phase 3)."""
from flask import Blueprint

orders_bp = Blueprint("orders", __name__, template_folder="../../templates/orders")

# Import routes so that they are registered on the blueprint.
from app.orders import routes  # noqa: E402, F401
