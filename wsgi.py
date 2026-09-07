"""WSGI entry point for production servers.

Run locally with `python run.py`; in production gunicorn imports `app` from here
(see gunicorn.conf.py and the Procfile).
"""

from app import create_app

app = create_app()
