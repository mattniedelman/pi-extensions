"""Minimal Linux inotify sensor for private legacy-state fixtures."""
import ctypes
import os
import struct
from pathlib import Path

IN_ACCESS = 0x00000001
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000

ACCESS_MASK = IN_ACCESS
WRITE_MASK = IN_MODIFY | IN_CLOSE_WRITE | IN_ATTRIB
DELETE_MASK = IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED
WATCH_MASK = ACCESS_MASK | WRITE_MASK | DELETE_MASK


class _Inotify:
    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)

    def init(self):
        fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify setup failed")
        return fd

    def add_watch(self, fd, path):
        wd = self.libc.inotify_add_watch(fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            raise OSError(ctypes.get_errno(), "inotify watch failed")
        return wd

    def read(self, fd):
        return os.read(fd, 65536)

    def close(self, fd):
        os.close(fd)


class LegacyStateSensor:
    """Count access, write, and delete events per watched fixture inode."""

    def __init__(self, paths, calibration_path, *, api=None):
        self.api = api or _Inotify()
        self.fd = self.api.init()
        self.closed = False
        self.finalized = False
        self.watches = {}
        try:
            self.calibration_path = Path(calibration_path)
            watched = [self.calibration_path, *(Path(path) for path in paths)]
            self.totals = {path: {"access": 0, "write": 0, "delete": 0} for path in watched}
            for path in watched:
                wd = self.api.add_watch(self.fd, path)
                if wd in self.watches:
                    raise RuntimeError("duplicate inotify watch")
                self.watches[wd] = path
        except BaseException:
            self.close()
            raise

    def calibrate(self, *, read_calibration=True):
        if read_calibration:
            self.calibration_path.read_bytes()
        counts = self.drain()
        if counts[self.calibration_path]["access"] <= 0:
            raise RuntimeError("calibration access missing")
        return counts

    def drain(self):
        if self.closed:
            raise RuntimeError("sensor is closed")
        while True:
            try:
                data = self.api.read(self.fd)
            except BlockingIOError:
                break
            if not data:
                raise RuntimeError("inotify final drain failed")
            offset = 0
            while offset < len(data):
                if len(data) - offset < 16:
                    raise RuntimeError("truncated inotify event")
                wd, mask, _cookie, size = struct.unpack_from("iIII", data, offset)
                event_size = 16 + size
                if event_size > len(data) - offset:
                    raise RuntimeError("truncated inotify event")
                offset += event_size
                if mask & IN_Q_OVERFLOW:
                    raise RuntimeError("inotify queue overflow")
                if wd not in self.watches:
                    raise RuntimeError("unknown inotify watch")
                unknown = mask & ~WATCH_MASK
                if unknown:
                    raise RuntimeError("unknown inotify event")
                path = self.watches[wd]
                if mask & ACCESS_MASK:
                    self.totals[path]["access"] += 1
                if mask & WRITE_MASK:
                    self.totals[path]["write"] += 1
                if mask & DELETE_MASK:
                    self.totals[path]["delete"] += 1
        return self._counts()

    def final_drain(self):
        counts = self.drain()
        self.finalized = True
        return counts

    def final_counts(self):
        if not self.finalized:
            raise RuntimeError("final drain required")
        return self._counts()

    def _counts(self):
        return {path: dict(categories) for path, categories in self.totals.items()}

    def close(self):
        if not self.closed:
            self.closed = True
            self.api.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
