"""Linux process locks, automatically released by the operating system."""

import errno
import fcntl
import os

from ..core.errors import StorageError


class SessionLock:
    def __init__(self, path):
        self.fd = None
        try:
            self.fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.close()
            if exc.errno in {errno.EAGAIN, errno.EACCES}:
                raise StorageError("会话正由其他进程使用。") from exc
            raise StorageError("无法锁定会话。") from exc

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
