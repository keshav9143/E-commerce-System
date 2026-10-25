"""app/shop/__init__.py — Shop blueprint (products, cart, checkout)."""
from flask import Blueprint

shop_bp = Blueprint("shop", __name__, template_folder="../../templates/shop")

# Import routes after creating the blueprint to bind view functions
from app.shop import routes  # noqa: F401, E402
