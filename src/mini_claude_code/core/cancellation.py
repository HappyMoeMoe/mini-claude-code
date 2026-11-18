"""Per-turn cooperative cancellation, independent of persistent session data."""

from threading import Event


class Cancelled(Exception):
    pass


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def check(self) -> None:
        if self.cancelled:
            raise Cancelled("当前执行轮已取消。")

    def wait(self, seconds: float) -> None:
        if self._event.wait(seconds):
            self.check()
