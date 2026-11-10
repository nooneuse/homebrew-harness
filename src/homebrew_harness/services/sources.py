"""Platform-bound Source factory and generic transient read retry policy."""

import time
from homebrew_harness.platforms.registry import RepositorySource as RepositorySource


def retry_after(error, attempts):
    headers = getattr(error, "headers", {})
    try:
        seconds = float(headers.get("retry-after", 0))
        reset = float(headers.get("x-ratelimit-reset", 0)) - time.time()
    except (TypeError, ValueError):
        seconds = reset = 0
    if getattr(error, "status", None) in {403, 429}:
        return min(86400, max(1, seconds, reset, 30))
    return min(3600, 2 ** min(attempts, 10))
