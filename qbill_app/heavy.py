"""Concurrency gate for heavy endpoints (QGrab Release.13 anti-freeze
lesson: heavy work must never be able to starve the whole server).

At most MAX_HEAVY heavy operations run at once; the rest wait briefly and
then get an honest 503 + Retry-After instead of piling up until the site
jams. PDF generation (Phase 1, later step) will sit behind this gate.
"""
import threading
from functools import wraps

from flask import jsonify

MAX_HEAVY = 3
WAIT_SECONDS = 20

_semaphore = threading.BoundedSemaphore(MAX_HEAVY)


def heavy_endpoint(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _semaphore.acquire(timeout=WAIT_SECONDS):
            resp = jsonify(
                ok=False,
                error="Server is busy with heavy work — please retry in a few seconds.",
            )
            resp.status_code = 503
            resp.headers["Retry-After"] = str(WAIT_SECONDS)
            return resp
        try:
            return fn(*args, **kwargs)
        finally:
            _semaphore.release()

    return wrapper
