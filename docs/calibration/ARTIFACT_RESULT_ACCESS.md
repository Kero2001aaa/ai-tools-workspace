# Accepted artifact result reads

Internal `ArtifactResultAccess.read` resolves a single completed task's accepted
result from an opaque artifact ID, never a filesystem path or an input/pending
output. It returns at most 262144 decoded bytes, exact MIME and size, offsets,
end-of-file flag, base64, and whole-content SHA256. The store computes the digest
from the same authenticated full plaintext snapshot used for slicing, not from
the chunk and not by repeatedly decrypting the file. The optional digest field
keeps existing read callers compatible and stays absent unless explicitly asked.

Ownership is rechecked by the artifact store using the resolved task ID. Missing,
expired, tampered, input-only and uncommitted outputs fail closed. Reads do not
change task state. This is a single-owner internal library boundary, not an
HTTP authentication mechanism or permission to expose a multi-user endpoint.
It is not registered as a model tool. Public output schemas remain separate.
