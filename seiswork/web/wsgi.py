"""
Gunicorn entry point for production deployments (nginx + gunicorn, replacing
the Werkzeug dev server that `seiswork gui` runs).

    gunicorn --workers 1 --threads 8 --worker-class gthread \
             -b 127.0.0.1:5000 seiswork.web.wsgi:app

IMPORTANT — must stay a SINGLE worker process: this app keeps job/session
state in plain in-process memory (`_jobs`, `_LIVE_SESSION`, etc.), not a
shared store like Redis. Multiple gunicorn *worker processes* (`--workers >1`)
would each get their own copy of that state, so a request handled by one
worker wouldn't see jobs started on another — pipeline runs would silently
"disappear". Use `--threads` for concurrency instead (threads share one
process's memory, matching the dev server's own `threaded=True`).

Behind a reverse proxy, also set SEISWORK_TRUST_PROXY=1 in the environment
so `request.remote_addr` reflects the real client IP via X-Forwarded-For,
not the proxy's own loopback hop (see app.py's ProxyFix setup) — several
endpoints gate privileged actions on "is this request from localhost".
"""
from seiswork.web.app import app, _bootstrap

_bootstrap()

__all__ = ["app"]
