"""Local caller-approved code excerpts, descriptor-relative non-following reads.

Trusted local library, no model/HTTP/MCP registration. The caller supplies a
trusted root and explicit reviewed paths; this is not a process sandbox or DLP.
"""
import hashlib
import os
from pathlib import PurePosixPath
import stat

MAX_BYTES = 65536
SUFFIXES = frozenset({'.py', '.js', '.ts', '.tsx', '.jsx', '.md', '.txt', '.json', '.toml', '.yaml', '.yml', '.css', '.html'})


def _relative(path):
    if not isinstance(path, str) or not 1 <= len(path) <= 240 or '\\' in path:
        raise ValueError('code_source_invalid')
    parts = path.split('/')
    if any(not part or part.startswith('.') or any(ord(char) < 32 or ord(char) == 127 for char in part) for part in parts):
        raise ValueError('code_source_invalid')
    if len(parts) > 12 or PurePosixPath(path).suffix.lower() not in SUFFIXES:
        raise ValueError('code_source_invalid')
    return parts


def read_code_excerpt(root, approved_files, path, start_line=1, max_lines=100):
    """Read one exact allowlisted file, cap 64KiB input and 200 output lines.

    Hash refers to the full returned byte snapshot, not immutable disk content or
    authorship. Concurrent ordinary changes are refused; hostile same-stat writes
    are not excluded. Caller approval is essential: file extension is not consent.
    """
    if not isinstance(approved_files, (list, tuple)) or not 1 <= len(approved_files) <= 20:
        raise ValueError('code_source_invalid')
    for approved in approved_files:
        _relative(approved)
    parts = _relative(path)
    if path not in approved_files:
        raise ValueError('code_source_not_approved')
    if type(start_line) is not int or not 1 <= start_line <= 65537 or type(max_lines) is not int or not 1 <= max_lines <= 200:
        raise ValueError('code_range_invalid')
    if not all(hasattr(os, flag) for flag in ('O_NOFOLLOW', 'O_DIRECTORY', 'O_NONBLOCK')) or os.open not in os.supports_dir_fd:
        raise ValueError('code_platform_unsupported')
    descriptors = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            raise ValueError('code_source_unavailable')
        data = bytearray()
        while len(data) <= MAX_BYTES:
            chunk = os.read(descriptor, min(8192, MAX_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if len(data) > MAX_BYTES or len(data) != after.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('code_source_unavailable')
        text = data.decode('utf-8')
        if any(ord(char) < 32 and char not in '\r\n\t' for char in text) or '\x7f' in text:
            raise ValueError('code_source_unavailable')
    except (OSError, UnicodeError, TypeError):
        raise ValueError('code_source_unavailable') from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    lines = text.splitlines(keepends=True)
    selected = ''.join(lines[start_line - 1:start_line - 1 + max_lines])
    if start_line > max(1, len(lines)):
        raise ValueError('code_range_invalid')
    return {'kind': 'caller_approved_code_excerpt', 'path': path, 'text': selected,
        'start_line': start_line, 'end_line': min(len(lines), start_line + max_lines - 1),
        'total_lines': len(lines), 'file_bytes': len(data),
        'file_sha256': hashlib.sha256(data).hexdigest(),
        'excerpt_sha256': hashlib.sha256(selected.encode('utf-8')).hexdigest(),
        'truncated': start_line != 1 or len(lines) > max_lines,
        'trust': 'untrusted_source_data', 'approval': 'explicit_local_caller_allowlist'}
