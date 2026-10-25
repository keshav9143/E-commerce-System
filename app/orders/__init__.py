"""app/orders/__init__.py — Orders blueprint (order history, status — Phase 3)."""
from flask import Blueprint

orders_bp = Blueprint("orders", __name__, template_folder="../../templates/orders")
