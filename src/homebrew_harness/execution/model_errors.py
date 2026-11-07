"""Classify actionable provider errors without exposing response bodies."""


def quota_exhausted(error: BaseException) -> bool:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if getattr(error, "status_code", None) == 402:
            return True
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            detail = body.get("error", body)
            if isinstance(detail, dict) and detail.get("code") == "insufficient_quota":
                return True
        if getattr(error, "code", None) == "insufficient_quota":
            return True
        error = error.__cause__
    return False
