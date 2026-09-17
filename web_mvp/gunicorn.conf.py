"""Gunicorn config for zero-downtime deploys."""

import os

bind = os.environ.get("AUTOSELF_BIND", "127.0.0.1:5410")
worker_class = "gthread"
workers = int(os.environ.get("AUTOSELF_WORKERS", "1"))
threads = int(os.environ.get("AUTOSELF_THREADS", "8"))
timeout = int(os.environ.get("AUTOSELF_TIMEOUT", "3600"))
graceful_timeout = int(os.environ.get("AUTOSELF_GRACEFUL_TIMEOUT", "30"))
keepalive = 5
accesslog = None
errorlog = "-"
capture_output = True
preload_app = False


def on_starting(server):  # noqa: ARG001
    # Schedulers must start inside the worker after app sets publish_fn.
    # Starting them in the master process leaves _publish_fn=None and silent misses.
    return
