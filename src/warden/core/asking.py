"""Who is on the other end of the request being served right now.

The store writes down what happened; until there were named tokens there was
nobody to write down as having asked for it. Threading a name through every
call from the API to the registry to the store would have put an argument
nobody reads into a dozen signatures, so it travels the way request-scoped
facts are meant to: beside the call rather than inside it.

Empty means nobody was named - a person at the machine running `warden release`
is not a caller and has no token to be called after.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_who: ContextVar[str] = ContextVar("warden_asking", default="")


def who() -> str:
    """The name of the token this request arrived with, or nothing."""
    return _who.get()


@contextmanager
def named(name: str) -> Iterator[None]:
    """Answer `who()` with this name for as long as the block runs."""
    token = _who.set(name)
    try:
        yield
    finally:
        _who.reset(token)


def set_to(name: str) -> None:
    """The same, for a dependency that has no block to wrap.

    FastAPI copies the context into the threadpool it runs a sync endpoint in,
    so a name set here is still there when the store writes the row.
    """
    _who.set(name)
