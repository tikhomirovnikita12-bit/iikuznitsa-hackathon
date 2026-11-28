"""Shared time limit for a reply on the mechanic screen."""

from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic


# Leave ten seconds of the browser's 65-second timeout for returning the result.
REPLY_BUDGET_SECONDS = 55
_deadline = ContextVar("mechanic_reply_deadline", default=None)


@contextmanager
def reply_budget():
    token = _deadline.set(monotonic() + REPLY_BUDGET_SECONDS)
    try:
        yield
    finally:
        _deadline.reset(token)


def timeout_within_reply(limit: float) -> float:
    deadline = _deadline.get()
    if deadline is None:
        return limit
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("Истекло время подготовки ответа.")
    return min(limit, remaining)
