"""Encrypted payload and artifact storage for Adult Agent Stage 4.

Sensitive bytes are encrypted with AES-256-GCM before reaching disk or kept in
a bounded process-local cache. SQLite contains metadata only.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


PAYLOAD_SCHEMA_VERSION = 1
PAYLOAD_KEY_VERSION = 1
_PAYLOAD_MAGIC = b"AAP"
_HEADER_SIZE = 49
_TAG_SIZE = 16
_DEFAULT_ROOT = Path(".data") / "adult_payloads"
_DEFAULT_MAX_INPUT_BYTES = 15 * 1024 * 1024
_DEFAULT_MAX_OUTPUT_BYTES = 20 * 1024 * 1024
_DEFAULT_CHUNK_BYTES = 262_144
_MAX_RETENTION_HOURS = 24 * 30
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PAYLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MIME_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]{1,63}/[A-Za-z0-9!#$&^_.+-]{1,63}$")
_KDF_SALT = b"adult-agent/payload-kdf/v1"
_SCHEMA_NAME = "adult_payload_index_v1"


class PayloadStoreError(Exception):
    """Base class for stable, non-sensitive payload-store errors."""


class PayloadKeyUnavailable(PayloadStoreError):
    error_code = "payload_key_unavailable"


class PayloadKeyInvalid(PayloadStoreError):
    error_code = "payload_key_invalid"


class PayloadKeyMismatch(PayloadStoreError):
    error_code = "payload_key_mismatch"


class PayloadAuthenticationError(PayloadStoreError):
    error_code = "payload_authentication_failed"


class PayloadNotFound(PayloadStoreError):
    error_code = "payload_not_found"


class PayloadUnavailable(PayloadNotFound):
    error_code = "payload_needs_input"


class PayloadExpired(PayloadUnavailable):
    error_code = "payload_expired"


class PayloadStorageError(PayloadStoreError):
    error_code = "payload_storage_error"


class PayloadTooLarge(PayloadStoreError):
    error_code = "payload_too_large"


class InvalidPayloadReference(PayloadStoreError):
    error_code = "invalid_payload_reference"


class TransientCacheFull(PayloadStoreError):
    error_code = "transient_cache_full"


@dataclass(frozen=True)
class EncryptedPayload:
    ciphertext: bytes
    key_id: str
    key_version: int
    schema_version: int


@dataclass(frozen=True)
class PayloadRef:
    payload_id: str
    task_id: str
    retained: bool
    expires_at: str | None


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    task_id: str
    mime_type: str
    size_bytes: int
    persisted: bool
    expires_at: str | None


@dataclass(frozen=True)
class ArtifactChunk:
    artifact_id: str
    offset: int
    data: bytes
    total_bytes: int
    mime_type: str
    next_offset: int
    eof: bool


def _validate_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise InvalidPayloadReference("Task reference is invalid.")


def _validate_payload_id(payload_id: str) -> None:
    if not isinstance(payload_id, str) or not _PAYLOAD_ID_RE.fullmatch(payload_id):
        raise InvalidPayloadReference("Payload reference is invalid.")


def _validate_mime_type(mime_type: str) -> None:
    if not isinstance(mime_type, str) or not _MIME_RE.fullmatch(mime_type):
        raise InvalidPayloadReference("MIME type is invalid.")


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PayloadStorageError("Storage clock must return a timezone-aware datetime.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except (TypeError, ValueError) as exc:
        raise PayloadStorageError("Stored retention metadata is invalid.") from exc
    if parsed.utcoffset() is None:
        raise PayloadStorageError("Stored retention metadata is invalid.")
    return parsed.astimezone(timezone.utc)


def _retention_hours(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_RETENTION_HOURS:
        raise ValueError(f"{name} must be between 1 and {_MAX_RETENTION_HOURS} hours.")
    return value


def _env_integer(environ, name: str, default: int, minimum: int, maximum: int) -> int:
    raw = environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the supported range.")
    return value


class PayloadCipher:
    """AES-256-GCM with HKDF-separated encryption and fingerprint keys.

    ADULT_PAYLOAD_ENCRYPTION_KEY is URL-safe Base64 encoding of exactly 32
    cryptographically random bytes. The master key is not retained on the
    instance; only independently derived subkeys are kept in memory.
    """

    def __init__(self, master_key: bytes | str) -> None:
        if isinstance(master_key, str):
            try:
                raw = base64.b64decode(master_key.encode("ascii"), altchars=b"-_", validate=True)
            except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
                raise PayloadKeyInvalid("Encryption key must be URL-safe Base64.") from exc
        elif isinstance(master_key, bytes):
            raw = master_key
        else:
            raise PayloadKeyInvalid("Encryption key has an unsupported type.")
        if len(raw) != 32:
            raise PayloadKeyInvalid("Encryption key must decode to exactly 32 bytes.")

        self.key_id = hashlib.sha256(b"adult-agent-key-id-v1:" + raw).hexdigest()[:32]
        self.key_version = PAYLOAD_KEY_VERSION
        self._encryption_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=_KDF_SALT,
            info=b"adult-agent/payload-encryption/v1",
        ).derive(raw)
        self._fingerprint_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=_KDF_SALT,
            info=b"adult-agent/input-fingerprint/v1",
        ).derive(raw)

    @classmethod
    def from_environment(cls, environ=None) -> "PayloadCipher":
        source = os.environ if environ is None else environ
        encoded = source.get("ADULT_PAYLOAD_ENCRYPTION_KEY", "")
        if not encoded:
            raise PayloadKeyUnavailable("A local encryption key is not configured.")
        return cls(encoded.strip())

    def fingerprint(self, data: bytes) -> str:
        if not isinstance(data, bytes):
            raise TypeError("Fingerprint input must be bytes.")
        digest = hmac.new(self._fingerprint_key, data, hashlib.sha256).hexdigest()
        return "hmac-sha256:" + digest

    @staticmethod
    def _associated_data(task_id: str, payload_id: str, schema_version: int, key_version: int) -> bytes:
        _validate_task_id(task_id)
        _validate_payload_id(payload_id)
        return (
            f"adult-agent-payload|{schema_version}|{key_version}|{task_id}|{payload_id}"
        ).encode("utf-8")

    def encrypt(self, task_id: str, payload_id: str, plaintext: bytes) -> EncryptedPayload:
        if not isinstance(plaintext, bytes):
            raise TypeError("Payload must be bytes.")
        aad = self._associated_data(task_id, payload_id, PAYLOAD_SCHEMA_VERSION, self.key_version)
        nonce = secrets.token_bytes(12)
        body = AESGCM(self._encryption_key).encrypt(nonce, plaintext, aad)
        header = (
            _PAYLOAD_MAGIC + bytes((PAYLOAD_SCHEMA_VERSION, self.key_version))
            + self.key_id.encode("ascii") + nonce
        )
        return EncryptedPayload(header + body, self.key_id, self.key_version, PAYLOAD_SCHEMA_VERSION)

    def decrypt(self, task_id: str, payload_id: str, ciphertext: bytes) -> bytes:
        _validate_task_id(task_id)
        _validate_payload_id(payload_id)
        if not isinstance(ciphertext, bytes) or len(ciphertext) < _HEADER_SIZE + _TAG_SIZE:
            raise PayloadAuthenticationError("Encrypted payload is invalid.")
        if ciphertext[:3] != _PAYLOAD_MAGIC:
            raise PayloadAuthenticationError("Encrypted payload format is unsupported.")
        schema_version, key_version = ciphertext[3], ciphertext[4]
        try:
            key_id = ciphertext[5:37].decode("ascii")
        except UnicodeDecodeError as exc:
            raise PayloadAuthenticationError("Encrypted payload header is invalid.") from exc
        if schema_version != PAYLOAD_SCHEMA_VERSION or key_version != self.key_version:
            raise PayloadAuthenticationError("Encrypted payload version is unsupported.")
        if not hmac.compare_digest(key_id, self.key_id):
            raise PayloadKeyMismatch("Configured encryption key does not match the payload.")
        nonce = ciphertext[37:_HEADER_SIZE]
        aad = self._associated_data(task_id, payload_id, schema_version, key_version)
        try:
            return AESGCM(self._encryption_key).decrypt(nonce, ciphertext[_HEADER_SIZE:], aad)
        except InvalidTag as exc:
            raise PayloadAuthenticationError("Encrypted payload authentication failed.") from exc


class TransientPayloadCache:
    """Bounded process-local byte cache. It has no persistence or serialization API."""

    def __init__(
        self, *, max_entries: int = 32, max_bytes: int = 40 * 1024 * 1024,
        ttl_seconds: int = 600, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for name, value in (("max_entries", max_entries), ("max_bytes", max_bytes), ("ttl_seconds", ttl_seconds)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        self._max_entries, self._max_bytes, self._ttl_seconds = max_entries, max_bytes, ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str], tuple[bytes, float, str]] = OrderedDict()
        self._total_bytes = 0
        self._lock = threading.RLock()

    def _remove(self, key: tuple[str, str]) -> bool:
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        self._total_bytes -= len(entry[0])
        return True

    def _prune_locked(self) -> int:
        now = self._clock()
        expired = [key for key, (_, deadline, _) in self._entries.items() if deadline <= now]
        for key in expired:
            self._remove(key)
        return len(expired)

    def _put_reference(self, task_id: str, payload_ref: str, payload: bytes, *, kind: str = "input") -> None:
        _validate_task_id(task_id)
        _validate_payload_id(payload_ref)
        if kind not in {"input", "output"}:
            raise ValueError("Transient payload kind is invalid.")
        if not isinstance(payload, bytes):
            raise TypeError("Transient payload must be bytes.")
        if len(payload) > self._max_bytes:
            raise TransientCacheFull("Payload exceeds the transient cache capacity.")
        key = (task_id, payload_ref)
        with self._lock:
            self._prune_locked()
            if key in self._entries:
                raise InvalidPayloadReference("Transient reference already exists.")
            while len(self._entries) >= self._max_entries or self._total_bytes + len(payload) > self._max_bytes:
                if not self._entries:
                    raise TransientCacheFull("Transient payload cache is full.")
                self._remove(next(iter(self._entries)))
            self._entries[key] = (bytes(payload), self._clock() + self._ttl_seconds, kind)
            self._total_bytes += len(payload)

    def put(self, task_id: str, payload: bytes) -> str:
        reference = uuid.uuid4().hex
        self._put_reference(task_id, reference, payload, kind="input")
        return reference

    def get(self, task_id: str, payload_ref: str) -> bytes | None:
        _validate_task_id(task_id)
        _validate_payload_id(payload_ref)
        key = (task_id, payload_ref)
        with self._lock:
            self._prune_locked()
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return bytes(entry[0])

    def get_input(self, task_id: str, payload_ref: str) -> bytes | None:
        """Return only task-owned transient input payloads, never output artifacts."""
        _validate_task_id(task_id)
        _validate_payload_id(payload_ref)
        key = (task_id, payload_ref)
        with self._lock:
            self._prune_locked()
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry[2] != "input":
                raise PayloadNotFound("Payload reference does not belong to this task.")
            self._entries.move_to_end(key)
            return bytes(entry[0])

    def discard_ref(self, task_id: str, payload_ref: str) -> bool:
        _validate_task_id(task_id)
        _validate_payload_id(payload_ref)
        with self._lock:
            return self._remove((task_id, payload_ref))

    def discard(self, task_id: str) -> None:
        _validate_task_id(task_id)
        with self._lock:
            keys = [key for key in self._entries if key[0] == task_id]
            for key in keys:
                self._remove(key)

    def discard_task_kind(self, task_id: str, kind: str) -> int:
        _validate_task_id(task_id)
        if kind not in {"input", "output"}:
            raise ValueError("Transient payload kind is invalid.")
        with self._lock:
            selected = [
                key for key, (_, _, entry_kind) in self._entries.items()
                if key[0] == task_id and entry_kind == kind
            ]
            for key in selected:
                self._remove(key)
            return len(selected)

    def prune(self) -> int:
        with self._lock:
            return self._prune_locked()


class AdultArtifactStore:
    """Encrypted file store with a private SQLite metadata index."""

    def __init__(
        self, root: str | Path | None = None, *, cipher: PayloadCipher | None = None,
        cache: TransientPayloadCache | None = None,
        max_input_bytes: int = _DEFAULT_MAX_INPUT_BYTES,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        input_retention_hours: int = 24, output_retention_hours: int = 24,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if cipher is not None and not isinstance(cipher, PayloadCipher):
            raise TypeError("cipher must be a PayloadCipher or None.")
        for name, value in (("max_input_bytes", max_input_bytes), ("max_output_bytes", max_output_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        self.input_retention_hours = _retention_hours(input_retention_hours, "input_retention_hours")
        self.output_retention_hours = _retention_hours(output_retention_hours, "output_retention_hours")
        self.max_input_bytes, self.max_output_bytes = max_input_bytes, max_output_bytes
        self.cipher = cipher
        self.cache = cache if cache is not None else TransientPayloadCache()
        self._clock = clock
        self._lock = threading.RLock()
        self._transient_artifacts: OrderedDict[str, tuple[str, str, int, str]] = OrderedDict()

        configured_root = root if root is not None else os.environ.get("ADULT_PAYLOAD_DIR", "")
        candidate = Path(configured_root).expanduser() if configured_root else _DEFAULT_ROOT
        absolute = Path(os.path.abspath(os.fspath(candidate)))
        for component in (absolute, *absolute.parents):
            if component.is_symlink():
                raise PayloadStorageError("Payload storage path cannot contain a symbolic link.")
        try:
            absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
            if absolute.is_symlink() or not absolute.is_dir():
                raise PayloadStorageError("Payload storage root is not a safe directory.")
            self.root = absolute.resolve(strict=True)
            if os.name == "posix":
                os.chmod(self.root, 0o700)
                if stat.S_IMODE(self.root.stat().st_mode) & 0o077:
                    raise PayloadStorageError("Payload storage directory permissions are too broad.")
        except PayloadStoreError:
            raise
        except OSError as exc:
            raise PayloadStorageError("Payload storage directory could not be secured.") from exc

        self._index_path = self.root / "metadata.sqlite3"
        if self._index_path.is_symlink():
            raise PayloadStorageError("Payload metadata index cannot be a symbolic link.")
        self._initialize_index()

    @classmethod
    def from_environment(cls, environ=None) -> "AdultArtifactStore":
        source = os.environ if environ is None else environ
        encoded = source.get("ADULT_PAYLOAD_ENCRYPTION_KEY", "")
        cipher = PayloadCipher(encoded.strip()) if encoded else None
        return cls(
            root=source.get("ADULT_PAYLOAD_DIR") or _DEFAULT_ROOT,
            cipher=cipher,
            input_retention_hours=_env_integer(source, "ADULT_INPUT_RETENTION_HOURS", 24, 1, _MAX_RETENTION_HOURS),
            output_retention_hours=_env_integer(source, "ADULT_OUTPUT_RETENTION_HOURS", 24, 1, _MAX_RETENTION_HOURS),
            max_input_bytes=_env_integer(source, "ADULT_MAX_IMAGE_INPUT_BYTES", _DEFAULT_MAX_INPUT_BYTES, 1, 100 * 1024 * 1024),
            max_output_bytes=_env_integer(source, "ADULT_MAX_IMAGE_OUTPUT_BYTES", _DEFAULT_MAX_OUTPUT_BYTES, 1, 100 * 1024 * 1024),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise PayloadStorageError("Storage clock must return a timezone-aware datetime.")
        return value.astimezone(timezone.utc)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self._index_path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_index(self) -> None:
        expected_tables = {"adult_payload_schema", "adult_payload_records"}
        expected_record_columns = {
            "payload_id", "task_id", "kind", "mime_type", "plaintext_size",
            "key_id", "key_version", "schema_version", "source_attempt_id",
            "created_at", "expires_at", "ciphertext_sha256",
        }
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS adult_payload_schema (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        applied_at TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS adult_payload_records (
                        payload_id TEXT PRIMARY KEY CHECK(length(payload_id)=32),
                        task_id TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('input','output')),
                        mime_type TEXT NOT NULL,
                        plaintext_size INTEGER NOT NULL CHECK(plaintext_size >= 0),
                        key_id TEXT NOT NULL CHECK(length(key_id)=32),
                        key_version INTEGER NOT NULL CHECK(key_version=1),
                        schema_version INTEGER NOT NULL CHECK(schema_version=1),
                        source_attempt_id TEXT,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        ciphertext_sha256 TEXT NOT NULL CHECK(length(ciphertext_sha256)=64)
                    )"""
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_adult_payload_task_kind ON adult_payload_records(task_id,kind)"
                )
                rows = connection.execute(
                    "SELECT version,name FROM adult_payload_schema ORDER BY version"
                ).fetchall()
                if not rows:
                    connection.execute(
                        "INSERT INTO adult_payload_schema(version,name,applied_at) VALUES (1,?,?)",
                        (_SCHEMA_NAME, _utc_text(self._now())),
                    )
                elif len(rows) != 1 or (rows[0]["version"], rows[0]["name"]) != (1, _SCHEMA_NAME):
                    raise PayloadStorageError("Payload metadata schema version is unsupported.")
                tables = {
                    row["name"] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if tables != expected_tables:
                    raise PayloadStorageError("Payload metadata schema contains unexpected tables.")
                actual_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(adult_payload_records)")
                }
                if actual_columns != expected_record_columns:
                    raise PayloadStorageError("Payload metadata schema columns are inconsistent.")
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise PayloadStorageError("Payload metadata database failed integrity validation.")
                connection.commit()
            if os.name == "posix":
                os.chmod(self._index_path, 0o600)
                if stat.S_IMODE(self._index_path.stat().st_mode) & 0o077:
                    raise PayloadStorageError("Payload metadata file permissions are too broad.")
        except PayloadStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise PayloadStorageError("Payload metadata index could not be initialized safely.") from exc

    def _payload_path(self, payload_id: str) -> Path:
        _validate_payload_id(payload_id)
        candidate = self.root / (payload_id + ".aap")
        if candidate.parent != self.root:
            raise InvalidPayloadReference("Payload reference is invalid.")
        return candidate

    def _atomic_write(self, destination: Path, content: bytes) -> None:
        temporary: str | None = None
        committed = False
        try:
            fd, temporary = tempfile.mkstemp(prefix=".adult-payload-", suffix=".partial", dir=self.root)
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
            committed = True
            if os.name == "posix":
                directory_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            if committed:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
            raise PayloadStorageError("Encrypted payload could not be committed atomically.") from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _read_encrypted_file(self, payload_id: str, max_plaintext_size: int) -> bytes:
        path = self._payload_path(payload_id)
        maximum = max_plaintext_size + _HEADER_SIZE + _TAG_SIZE
        try:
            if path.is_symlink():
                raise PayloadStorageError("Payload file cannot be a symbolic link.")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                details = os.fstat(handle.fileno())
                if not stat.S_ISREG(details.st_mode):
                    raise PayloadStorageError("Payload storage entry is not a regular file.")
                if os.name == "posix" and stat.S_IMODE(details.st_mode) & 0o077:
                    raise PayloadStorageError("Payload file permissions are too broad.")
                if details.st_size > maximum:
                    raise PayloadTooLarge("Encrypted payload file exceeds its configured limit.")
                content = handle.read(maximum + 1)
            if len(content) != details.st_size or len(content) > maximum:
                raise PayloadStorageError("Encrypted payload file is incomplete or oversized.")
            return content
        except PayloadStoreError:
            raise
        except FileNotFoundError as exc:
            raise PayloadUnavailable("Required payload is no longer available.") from exc
        except OSError as exc:
            raise PayloadStorageError("Encrypted payload could not be read safely.") from exc

    def _insert_record(
        self, payload_id: str, task_id: str, kind: str, mime_type: str,
        plaintext_size: int, key_id: str, source_attempt_id: str | None,
        created_at: str, expires_at: str, ciphertext: bytes,
    ) -> None:
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO adult_payload_records(
                        payload_id,task_id,kind,mime_type,plaintext_size,key_id,
                        key_version,schema_version,source_attempt_id,created_at,
                        expires_at,ciphertext_sha256
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        payload_id, task_id, kind, mime_type, plaintext_size,
                        key_id, PAYLOAD_KEY_VERSION, PAYLOAD_SCHEMA_VERSION,
                        source_attempt_id, created_at, expires_at,
                        hashlib.sha256(ciphertext).hexdigest(),
                    ),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise PayloadStorageError("Payload metadata could not be committed.") from exc

    def _get_record(self, payload_id: str) -> dict[str, object] | None:
        _validate_payload_id(payload_id)
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM adult_payload_records WHERE payload_id=?", (payload_id,)
                ).fetchone()
                return dict(row) if row is not None else None
        except sqlite3.Error as exc:
            raise PayloadStorageError("Payload metadata could not be read.") from exc

    def _remove_records(self, payload_ids: list[str]) -> None:
        if not payload_ids:
            return
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "DELETE FROM adult_payload_records WHERE payload_id=?",
                    ((item,) for item in payload_ids),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise PayloadStorageError("Payload metadata could not be deleted.") from exc

    def _verify_record_key(self, record: dict[str, object]) -> None:
        if self.cipher is None:
            raise PayloadKeyUnavailable("A local encryption key is required to read this payload.")
        if not hmac.compare_digest(str(record["key_id"]), self.cipher.key_id):
            raise PayloadKeyMismatch("Configured encryption key does not match the payload metadata.")
        if record["key_version"] != PAYLOAD_KEY_VERSION or record["schema_version"] != PAYLOAD_SCHEMA_VERSION:
            raise PayloadAuthenticationError("Stored payload version is unsupported.")

    def _persist_encrypted(
        self, *, payload_id: str, task_id: str, kind: str, mime_type: str,
        data: bytes, source_attempt_id: str | None, retention_hours: int,
    ) -> str:
        if self.cipher is None:
            raise PayloadKeyUnavailable("A local encryption key is required for persistent payloads.")
        encrypted = self.cipher.encrypt(task_id, payload_id, data)
        destination = self._payload_path(payload_id)
        self._atomic_write(destination, encrypted.ciphertext)
        now = self._now()
        created_at = _utc_text(now)
        expires_at = _utc_text(now + timedelta(hours=retention_hours))
        try:
            self._insert_record(
                payload_id, task_id, kind, mime_type, len(data), encrypted.key_id,
                source_attempt_id, created_at, expires_at, encrypted.ciphertext,
            )
        except Exception:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return expires_at

    def save_input(
        self, task_id: str, data: bytes, mime_type: str, *, retain_encrypted: bool
    ) -> PayloadRef:
        _validate_task_id(task_id)
        _validate_mime_type(mime_type)
        if not isinstance(data, bytes):
            raise TypeError("Input payload must be bytes.")
        if len(data) > self.max_input_bytes:
            raise PayloadTooLarge("Input payload exceeds its configured size limit.")
        if not isinstance(retain_encrypted, bool):
            raise TypeError("retain_encrypted must be boolean.")
        with self._lock:
            if not retain_encrypted:
                payload_id = self.cache.put(task_id, data)
                return PayloadRef(payload_id, task_id, False, None)
            if self.cipher is None:
                raise PayloadKeyUnavailable("Encrypted input retention requires a configured local key.")
            payload_id = uuid.uuid4().hex
            expiry = self._persist_encrypted(
                payload_id=payload_id, task_id=task_id, kind="input",
                mime_type=mime_type, data=data, source_attempt_id=None,
                retention_hours=self.input_retention_hours,
            )
            return PayloadRef(payload_id, task_id, True, expiry)

    def restore_transient_input(self, task_id: str, payload_id: str, data: bytes) -> None:
        """Restore an absent original input reference, never persist or overwrite.

        The trusted application verifies the task/type/keyed fingerprint first.
        A byte-identical cached value makes a response-loss retry idempotent.
        """
        _validate_task_id(task_id)
        _validate_payload_id(payload_id)
        if not isinstance(data, bytes):
            raise TypeError("Input payload must be bytes.")
        if len(data) > self.max_input_bytes:
            raise PayloadTooLarge("Input payload exceeds its configured size limit.")
        with self._lock:
            if self._get_record(payload_id) is not None:
                raise InvalidPayloadReference("Persistent input cannot be restored as transient.")
            existing = self.cache.get_input(task_id, payload_id)
            if existing is not None:
                if not hmac.compare_digest(existing, data):
                    raise InvalidPayloadReference("Transient input reference already exists.")
                return
            self.cache._put_reference(task_id, payload_id, data, kind="input")

    def load_input(self, payload_id: str, task_id: str) -> bytes:
        _validate_task_id(task_id)
        _validate_payload_id(payload_id)
        with self._lock:
            record = self._get_record(payload_id)
            if record is None:
                transient = self.cache.get_input(task_id, payload_id)
                if transient is None:
                    raise PayloadUnavailable("Required input is unavailable; new input is required.")
                return transient
            if record["task_id"] != task_id or record["kind"] != "input":
                raise PayloadNotFound("Payload reference does not belong to this task.")
            if _parse_utc(str(record["expires_at"])) <= self._now():
                raise PayloadExpired("Required input has expired; new input is required.")
            self._verify_record_key(record)
            ciphertext = self._read_encrypted_file(payload_id, self.max_input_bytes)
            if not hmac.compare_digest(
                hashlib.sha256(ciphertext).hexdigest(), str(record["ciphertext_sha256"])
            ):
                raise PayloadAuthenticationError("Encrypted payload integrity check failed.")
            plaintext = self.cipher.decrypt(task_id, payload_id, ciphertext)
            if len(plaintext) != record["plaintext_size"]:
                raise PayloadAuthenticationError("Decrypted payload size does not match its metadata.")
            return plaintext

    def save_output(
        self, task_id: str, attempt_id: str, data: bytes, mime_type: str
    ) -> ArtifactRef:
        _validate_task_id(task_id)
        _validate_task_id(attempt_id)
        _validate_mime_type(mime_type)
        if not isinstance(data, bytes):
            raise TypeError("Output artifact must be bytes.")
        if len(data) > self.max_output_bytes:
            raise PayloadTooLarge("Output artifact exceeds its configured size limit.")
        with self._lock:
            artifact_id = uuid.uuid4().hex
            if self.cipher is None:
                self.cache._put_reference(task_id, artifact_id, data, kind="output")
                while len(self._transient_artifacts) >= self.cache._max_entries:
                    self._transient_artifacts.popitem(last=False)
                self._transient_artifacts[artifact_id] = (task_id, mime_type, len(data), attempt_id)
                return ArtifactRef(artifact_id, task_id, mime_type, len(data), False, None)
            expiry = self._persist_encrypted(
                payload_id=artifact_id, task_id=task_id, kind="output",
                mime_type=mime_type, data=data, source_attempt_id=attempt_id,
                retention_hours=self.output_retention_hours,
            )
            return ArtifactRef(artifact_id, task_id, mime_type, len(data), True, expiry)

    def read_chunk(
        self, artifact_id: str, offset: int, max_bytes: int = _DEFAULT_CHUNK_BYTES,
        *, task_id: str | None = None,
    ) -> ArtifactChunk:
        _validate_payload_id(artifact_id)
        if task_id is not None:
            _validate_task_id(task_id)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer.")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= _DEFAULT_CHUNK_BYTES:
            raise ValueError(f"max_bytes must be between 1 and {_DEFAULT_CHUNK_BYTES}.")
        with self._lock:
            record = self._get_record(artifact_id)
            if record is None:
                transient_info = self._transient_artifacts.get(artifact_id)
                if transient_info is None:
                    raise PayloadNotFound("Artifact is unavailable.")
                self._transient_artifacts.move_to_end(artifact_id)
                stored_task_id, mime_type, expected_size, _attempt_id = transient_info
                if task_id is not None and stored_task_id != task_id:
                    raise PayloadNotFound("Artifact does not belong to this task.")
                data = self.cache.get(stored_task_id, artifact_id)
                if data is None:
                    self._transient_artifacts.pop(artifact_id, None)
                    raise PayloadUnavailable("Transient artifact is no longer available.")
                if len(data) != expected_size:
                    raise PayloadAuthenticationError("Transient artifact metadata is inconsistent.")
            else:
                if record["kind"] != "output":
                    raise PayloadNotFound("Artifact is unavailable.")
                if task_id is not None and record["task_id"] != task_id:
                    raise PayloadNotFound("Artifact does not belong to this task.")
                if _parse_utc(str(record["expires_at"])) <= self._now():
                    raise PayloadExpired("Artifact retention has expired.")
                self._verify_record_key(record)
                ciphertext = self._read_encrypted_file(artifact_id, self.max_output_bytes)
                if not hmac.compare_digest(
                    hashlib.sha256(ciphertext).hexdigest(), str(record["ciphertext_sha256"])
                ):
                    raise PayloadAuthenticationError("Encrypted artifact integrity check failed.")
                data = self.cipher.decrypt(str(record["task_id"]), artifact_id, ciphertext)
                if len(data) != record["plaintext_size"]:
                    raise PayloadAuthenticationError("Decrypted artifact size does not match its metadata.")
                mime_type = str(record["mime_type"])
            if offset > len(data):
                raise ValueError("offset exceeds artifact size.")
            chunk = data[offset:offset + max_bytes]
            next_offset = offset + len(chunk)
            return ArtifactChunk(
                artifact_id, offset, chunk, len(data), mime_type,
                next_offset, next_offset >= len(data),
            )

    def discard_output(self, artifact_id: str, task_id: str, attempt_id: str) -> bool:
        """Discard one uncommitted output owned by the exact task and attempt.

        Returns False when the artifact is already absent. Ownership mismatches
        fail closed; callers never provide or influence a filesystem path.
        """
        _validate_payload_id(artifact_id)
        _validate_task_id(task_id)
        _validate_task_id(attempt_id)
        with self._lock:
            record = self._get_record(artifact_id)
            if record is not None:
                if (
                    record["task_id"] != task_id
                    or record["kind"] != "output"
                    or record["source_attempt_id"] != attempt_id
                ):
                    raise PayloadNotFound("Artifact does not belong to this task attempt.")
                try:
                    self._payload_path(artifact_id).unlink(missing_ok=True)
                except OSError as exc:
                    raise PayloadStorageError("Uncommitted artifact could not be discarded safely.") from exc
                self._remove_records([artifact_id])
                return True

            transient_info = self._transient_artifacts.get(artifact_id)
            if transient_info is None:
                return False
            stored_task_id, _mime_type, _size, stored_attempt_id = transient_info
            if stored_task_id != task_id or stored_attempt_id != attempt_id:
                raise PayloadNotFound("Artifact does not belong to this task attempt.")
            self.cache.discard_ref(task_id, artifact_id)
            self._transient_artifacts.pop(artifact_id, None)
            return True

    def discard_input(self, payload_id: str, task_id: str) -> bool:
        """Remove one uncommitted input, never an output or another task's data."""
        _validate_payload_id(payload_id)
        _validate_task_id(task_id)
        with self._lock:
            record = self._get_record(payload_id)
            if record is not None:
                if record["task_id"] != task_id or record["kind"] != "input":
                    raise PayloadNotFound("Input does not belong to this task.")
                try:
                    self._payload_path(payload_id).unlink(missing_ok=True)
                except OSError as exc:
                    raise PayloadStorageError("Uncommitted input could not be discarded safely.") from exc
                self._remove_records([payload_id])
                return True
            if self.cache.get_input(task_id, payload_id) is None:
                return False
            return self.cache.discard_ref(task_id, payload_id)

    def delete_task_inputs(self, task_id: str) -> int:
        """Delete retained and transient inputs after task completion or cancellation."""
        _validate_task_id(task_id)
        with self._lock:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT payload_id FROM adult_payload_records WHERE task_id=? AND kind='input'",
                    (task_id,),
                ).fetchall()
            persistent_ids = [str(row["payload_id"]) for row in rows]
            for payload_id in persistent_ids:
                try:
                    self._payload_path(payload_id).unlink(missing_ok=True)
                except OSError as exc:
                    raise PayloadStorageError("Retained input could not be deleted safely.") from exc
            self._remove_records(persistent_ids)
            removed_ephemeral = self.cache.discard_task_kind(task_id, "input")
            return len(persistent_ids) + removed_ephemeral

    def delete_expired(self) -> int:
        with self._lock:
            now = _utc_text(self._now())
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT payload_id FROM adult_payload_records WHERE expires_at<=?", (now,)
                ).fetchall()
            expired_ids = [str(row["payload_id"]) for row in rows]
            for payload_id in expired_ids:
                try:
                    self._payload_path(payload_id).unlink(missing_ok=True)
                except OSError as exc:
                    raise PayloadStorageError("Expired payload could not be deleted safely.") from exc
            self._remove_records(expired_ids)
            return len(expired_ids) + self.cache.prune()
