import sys
import os
from urllib.parse import parse_qs, urlencode

# Set VERCEL environment flag
os.environ.setdefault("VERCEL", "1")

# Ensure backend modules can be imported in Vercel serverless environment
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
BACKEND_DIR = os.path.join(ROOT_DIR, "sudharsarthi", "backend")

for p in [BACKEND_DIR, ROOT_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

# Import backend Flask application
import app as backend_module
flask_app = backend_module.app

class VercelWSGIHandler:
    """
    WSGI wrapper ensuring clean request dispatching on Vercel Serverless Functions.
    Accurately maps root UI, assets, and /api/* subpaths into Flask routes.
    """
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        qs = environ.get("QUERY_STRING", "")
        params = parse_qs(qs, keep_blank_values=True)

        if "__path__" in params:
            subpath = params.pop("__path__")[0]
            new_qs = urlencode([(k, v) for k, vs in params.items() for v in vs])
            environ["QUERY_STRING"] = new_qs
            clean = subpath.lstrip("/")
            if not clean:
                environ["PATH_INFO"] = "/api"
            elif clean.startswith("uploads/"):
                environ["PATH_INFO"] = "/" + clean
            elif clean.startswith("assets/"):
                environ["PATH_INFO"] = "/" + clean
            elif clean in ("index.html", "favicon.ico", "favicon.svg"):
                environ["PATH_INFO"] = "/" + clean
            else:
                environ["PATH_INFO"] = "/api/" + clean
        else:
            path = environ.get("PATH_INFO", "")
            if not path or path in ("/api/index.py", "/api/index", "/index.py", "/index"):
                environ["PATH_INFO"] = "/api"

        return self.application(environ, start_response)

app = VercelWSGIHandler(flask_app)
