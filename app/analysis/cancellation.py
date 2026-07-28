import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


class AnalysisCancelled(Exception):
    """Raised when the caller no longer needs an in-flight analysis."""


@dataclass(slots=True)
class AnalysisCancellation:
    _event: threading.Event = field(default_factory=threading.Event)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise AnalysisCancelled("Patent analysis was cancelled.")


_current_cancellation: ContextVar[AnalysisCancellation | None] = ContextVar(
    "patent_analysis_cancellation",
    default=None,
)


@contextmanager
def cancellation_scope(cancellation: AnalysisCancellation | None):
    token = _current_cancellation.set(cancellation)
    try:
        yield
    finally:
        _current_cancellation.reset(token)


def current_cancellation() -> AnalysisCancellation | None:
    return _current_cancellation.get()


def raise_if_cancelled() -> None:
    cancellation = current_cancellation()
    if cancellation:
        cancellation.raise_if_cancelled()
