"""app/api/__init__.py — REST API blueprint (metrics JSON endpoints — Phase 5)."""
from flask import Blueprint, jsonify

api_bp = Blueprint("api", __name__)


@api_bp.route("/health")
def health():
    """Liveness probe — returns 200 OK when the app is running."""
    return jsonify({"status": "ok", "service": "CartOS"})
