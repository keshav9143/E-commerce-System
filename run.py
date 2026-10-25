"""
run.py — CartOS application entry point.

Usage (development):
    flask run          # uses FLASK_APP=run.py from .env
    python run.py      # equivalent shorthand

To start the background task engine (Phase 4):
    flask engine start --workers 3

Never start the engine automatically from this file; the worker pool
must be started explicitly so that tests and migrations can import
the app without spawning threads.
"""

from dotenv import load_dotenv

# Load .env before importing the app so that os.environ is populated
# when config.py reads environment variables.
load_dotenv()

from app import create_app  # noqa: E402 — import after load_dotenv

app = create_app()

if __name__ == "__main__":
    # python run.py — useful for quick manual testing only.
    # For production, use a proper WSGI server (gunicorn, waitress, etc.).
    app.run(host="0.0.0.0", port=5000, debug=app.config.get("DEBUG", False))
