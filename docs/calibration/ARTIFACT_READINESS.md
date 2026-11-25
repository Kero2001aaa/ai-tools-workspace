# Retained ciphertext readiness audit

Internal trusted-operator helper audit_retained(existing_store, limit=100). It
reuses the encrypted store's safe file reader and the current configured limits.
No HTTP/MCP registration, no provider calls, no decryption or key export. Report
contains counts and stable codes only, no IDs, paths, text or ciphertext. It does
not initialize a store, modify data, clean expiry, extend TTL or recover anything.

Audits at most100rows,64MiBof ciphertext and five seconds between records.
The complete flag is false when truncated. File outcomes in counts are exclusive per inspected row. Separate key_checks
contains mutually exclusive key observations for eligible nonexpired records;
do not add the two dimensions. Missing key plus matching ciphertext digest means
bytes match stored digest, not that plaintext can be authenticated or recovered.
Missing, unreadable or malformed index returns stable index_unavailable without
raw paths or exception text. Hash matching is not AEAD authentication.
Expired rows are counted without reading their file. Transient-only payloads are
not persisted and cannot be audited/recovered after process loss. Missing,
corrupt and unsafe file entries are counted; existing scoped reads remain the
only authority path for payload access.

This does not solve encrypted artifact backup. Copying retained payload bytes
into an unmanaged backup may preserve them beyond configured expiry, while
copying the encryption key would create another secret-bearing asset. Neither
backup retention nor key recovery was inferred. A full disaster recovery feature
needs those decisions and a quiescent multi-store consistency boundary first.
Roadmap12.2is not complete. Audit only synthetic or already-authorized local
stores; this feature grants no new access to another namespace or account.
