"""Owner-only creation for new trusted-local SQLite files, not a sandbox.

Parent directories and concurrent filesystem writers remain trusted. Existing
regular files keep their permissions. No process-global umask is changed.
"""
import os
from pathlib import Path
import stat


def prepare_database(path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('Database path must be a regular file, not a symlink') from None
    else:
        os.close(descriptor)
    return path
