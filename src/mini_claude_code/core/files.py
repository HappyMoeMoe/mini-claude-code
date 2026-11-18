"""Read ordinary workspace files without waiting for FIFO/device producers."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat


@contextmanager
def open_regular_read(path: Path, *, binary=False):
    # Reject special files before opening, then check the actual descriptor too:
    # a path can be replaced between stat and open. O_NONBLOCK prevents a
    # replacement FIFO from waiting for a writer; O_NOFOLLOW rejects new links.
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Only regular files are supported')
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Only regular files are supported')
        stream = os.fdopen(fd, 'rb' if binary else 'r',
                           **({} if binary else {'encoding': 'utf-8', 'newline': ''}))
        fd = None  # The stream now owns the descriptor.
        with stream:
            yield stream
    finally:
        if fd is not None:
            os.close(fd)
