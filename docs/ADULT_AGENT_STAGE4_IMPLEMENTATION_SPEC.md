# Adult Agent — Stage 4 Encrypted Payload and Artifact Storage

## Status and scope

This document is the implementation contract for Stage 4. It adds local encrypted payload retention and artifact retrieval without adding model/provider calls, an executor, an AttemptTracker, or Adult MCP registration.

The implementation lives in `agent/adult/payload_store.py` and exposes:

- `PayloadCipher`: AES-256-GCM authenticated encryption, HKDF-separated encryption/fingerprint keys, and keyed input fingerprints.
- `TransientPayloadCache`: bounded process-memory storage for non-retained input and output fallback.
- `AdultArtifactStore`: encrypted file storage, a private SQLite metadata index, retention, deletion, and bounded artifact chunk reads.

The metadata index is `ADULT_PAYLOAD_DIR/metadata.sqlite3`. It is a separate SQLite database from `.data/adult.sqlite3`; this avoids changing or weakening the Stage 3 TaskStore schema and keeps payload metadata ownership inside the payload-store boundary. The index stores opaque IDs, task association, MIME type, byte count, timestamps, ciphertext digest, key identifier/version, and schema version. It never stores plaintext payload bytes or the master key.

## Cryptographic contract

- The local master key is exactly 32 random bytes encoded as URL-safe Base64.
- AES-256-GCM encrypts payload bytes with a fresh 96-bit random nonce per encryption and a full 128-bit authentication tag.
- HKDF-SHA256 derives independent 32-byte subkeys using distinct context labels for payload encryption and input fingerprinting.
- Associated authenticated data binds the payload to task ID, payload ID, payload schema version, and key version.
- The encrypted envelope records format/schema version, key version, a non-secret key identifier, nonce, ciphertext, and authentication tag.
- The metadata index records the key identifier and versions, never the key bytes.
- Authentication is verified before plaintext is returned. Wrong keys, modified ciphertext, changed task IDs, changed payload IDs, and unsupported versions fail closed.
- Key rotation is not automated in Stage 4. Keep the configured key stable while encrypted records exist; losing it makes those records unrecoverable.

## Retention and lifecycle

- Input retention defaults to ephemeral. Ephemeral bytes exist only in the process cache and are not serialized to SQLite or files.
- `retain_encrypted=true` is accepted only when a valid local master key is configured. TaskStore validates the key before writing the task row.
- Encrypted retained input is deleted on explicit task-input cleanup or expiry. Default input retention is 24 hours.
- Output artifacts are encrypted and retained for 24 hours when a key is configured.
- Without a key, output is available only in the bounded process cache; it is never written as plaintext to simulate persistence. The default cache is limited to 32 entries, 40 MiB total, and 10 minutes.
- Input and output size defaults are 15 MiB and 20 MiB. Artifact reads are chunked to at most 256 KiB.
- Missing ephemeral input after process restart raises `PayloadUnavailable`; the later task supervisor must map this condition to `needs_input` and request re-upload.

## Filesystem and SQLite controls

- The configured payload root and its existing path components must not be symbolic links.
- The payload directory is mode 0700 and files/index are mode 0600 on POSIX systems.
- Payload IDs are generated opaque 128-bit random identifiers; caller-provided paths and filenames are never accepted.
- Payload files are written under random temporary names, flushed, fsynced, and atomically renamed. Failed writes remove their temporary files.
- File reads reject symlinks and non-regular files. IDs are strictly validated before any path is constructed.
- SQLite uses parameterized SQL, a bounded busy timeout, foreign keys, DELETE journaling, and FULL synchronous mode.
- The metadata index has its own schema ledger and integrity check. It contains no raw content, prompt, image, provider response, credential, or absolute path.
- `.data/` is already ignored by Git. No runtime database or payload file is added to version control.

## Configuration

The following names belong in the local `.env` only; `.env.example` contains empty/safe defaults:

| Variable | Default | Meaning |
|---|---:|---|
| `ADULT_PAYLOAD_DIR` | `.data/adult_payloads` | Private encrypted payload root |
| `ADULT_PAYLOAD_ENCRYPTION_KEY` | empty | URL-safe Base64 of 32 random bytes; empty disables persistence |
| `ADULT_INPUT_RETENTION_HOURS` | 24 | Retained input TTL, 1–720 hours |
| `ADULT_OUTPUT_RETENTION_HOURS` | 24 | Encrypted output TTL, 1–720 hours |
| `ADULT_MAX_IMAGE_INPUT_BYTES` | 15728640 | Input byte limit |
| `ADULT_MAX_IMAGE_OUTPUT_BYTES` | 20971520 | Output byte limit |

Generate a key locally (do not commit the result and do not paste it into chat):

```bash
.venv/bin/python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

Store the generated value in the local `.env` as `ADULT_PAYLOAD_ENCRYPTION_KEY=...`. Back it up securely outside the repository. Never commit the value.

## Acceptance tests

`tests/test_adult_payload_store.py` covers encryption round trips, random nonces, distinct derived keys, task/payload associated-data binding, tampering, wrong and missing keys, encrypted-at-rest verification, plaintext exclusion from SQLite, path traversal, symlink roots, permissions, atomic partial-write cleanup, size/chunk bounds, cache bounds/expiry, encrypted retention expiry, task-input deletion, memory-only output fallback, and restart behavior.

The Stage 4 gate is not passed until the focused payload-store tests, Stage 3 and Stage 2 tests, the full repository unit suite, dependency security checks, and CI are all green. No live database migration is required or permitted in this stage.
