import sys
import os

# Set VERCEL environment flag
os.environ.setdefault("VERCEL", "1")

# Ensure backend modules can be imported in Vercel serverless environment
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
BACKEND_DIR = os.path.join(ROOT_DIR, "sudharsarthi", "backend")

for p in [ROOT_DIR, BACKEND_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

# Import backend Flask application
import app as backend_module
flask_app = backend_module.app

class VercelWSGIHandler:
    """
    WSGI wrapper ensuring clean request dispatching on Vercel Serverless Functions.
    Restores original request paths if rewritten to /api/index.py and logs invocations.
    """
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        x_matched = environ.get("HTTP_X_MATCHED_PATH") or environ.get("REQUEST_URI") or environ.get("RAW_URI") or ""

        # If Vercel rewrote the path directly to the script name
        if path in ("/api/index.py", "/api/index", "/index.py", "/index", ""):
            if x_matched:
                clean_path = x_matched.split("?")[0]
                environ["PATH_INFO"] = clean_path

        return self.application(environ, start_response)

app = VercelWSGIHandler(flask_app)
