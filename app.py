"""
SUDHARSARTHI Root Application Entrypoint
Enables Render, Railway, Heroku, and local environments to run cleanly with:
  gunicorn app:app
or
  python app.py
"""
import os
import sys
import importlib.util

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(BASE_DIR, "sudharsarthi", "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

backend_app_path = os.path.join(BACKEND_DIR, "app.py")
spec = importlib.util.spec_from_file_location("sudharsarthi_backend_app", backend_app_path)
backend_mod = importlib.util.module_from_spec(spec)
sys.modules["sudharsarthi_backend_app"] = backend_mod
spec.loader.exec_module(backend_mod)

app = backend_mod.app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "0.0.0.0")
    debug = os.environ.get("FLASK_DEBUG") == "1"
    print(f"Starting SUDHARSARTHI server on {host}:{port} (PORT={port})")
    app.run(host=host, port=port, debug=debug)
