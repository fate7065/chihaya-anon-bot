"""One nonblocking runner per local workflow directory; crash releases the OS lock."""
import os

from ..domain import ValidationError


class WorkflowBusy(ValidationError):
    pass


class RunnerLock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        handle = open(self.path, 'a+b')
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise WorkflowBusy('workflow runner busy or lock unavailable') from None
        self.handle = handle
        return self

    def __exit__(self, *args):
        # Closing releases the lock even when a run is interrupted.
        self.handle.close()

