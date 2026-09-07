"""Gunicorn settings for the Railway deployment.

Read with `gunicorn --config gunicorn.conf.py wsgi:app`.
"""

import os

# Railway assigns the port at runtime and routes its edge to it.
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# Threads, not the default sync worker. Generating an exam blocks on the model API for
# minutes at a time: a sync worker could answer nothing else for the duration, and the
# arbiter would kill it once `timeout` elapsed. A gthread worker keeps its accept loop -
# and the heartbeat the arbiter watches - running while request threads sit in I/O, so
# long requests and the SSE stream survive and other requests are still served.
worker_class = "gthread"
workers = int(os.environ.get("WEB_CONCURRENCY", 2))
threads = int(os.environ.get("GUNICORN_THREADS", 8))

# Only trips if the accept loop itself wedges; a slow request no longer counts against it.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 120))
# Longer than the platform proxy's idle timeout, so the edge never reuses a connection
# gunicorn has just closed - that race is what shows up as a sporadic 502.
keepalive = 65
# Let an in-flight exam generation finish rather than cutting it off mid-deploy.
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", 60))

# The only ingress is Railway's proxy, so its X-Forwarded-* headers are the real client.
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "*")

# Railway collects the container's stdout/stderr.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sus "%(a)s"'

# Build the app once in the master and fork it. Beyond saving memory on a small container,
# this runs the schema sync in app/__init__.py exactly once instead of racing N workers
# through the same CREATE TABLE / ALTER TABLE.
preload_app = True


def post_fork(server, worker):
    """Give every worker its own database connections.

    The schema sync opens a pool before the fork, so without this each worker would inherit
    and interleave on the same sockets.
    """
    from app.extensions import db
    from wsgi import app

    with app.app_context():
        db.engine.dispose()

    worker.log.info("worker %s: database pool reset after fork", worker.pid)
