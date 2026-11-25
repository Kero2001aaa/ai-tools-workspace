"""Accepted-result artifact access by opaque ID, no path or arbitrary payload API."""
import base64
import re
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.payload_store import AdultArtifactStore


class ArtifactResultAccess:
    def __init__(self, tasks: AdultTaskStore, artifacts: AdultArtifactStore):
        if not isinstance(tasks, AdultTaskStore) or not isinstance(artifacts, AdultArtifactStore):
            raise TypeError('real_result_stores_required')
        self.tasks, self.artifacts = tasks, artifacts

    def read(self, artifact_id, offset=0, max_bytes=262144):
        if type(artifact_id) is not str or re.fullmatch(r'[0-9a-f]{32}',artifact_id) is None:
            raise ValueError('artifact_unavailable')
        if type(offset) is not int or offset < 0:
            raise ValueError('invalid_chunk_offset')
        if type(max_bytes) is not int or not 1 <= max_bytes <= 262144:
            raise ValueError('invalid_chunk_size')
        # Task result ownership, not possession of a storage identifier, grants
        # this single-owner internal read. Uncommitted/cancelled outputs and
        # input references are never sufficient. No caller-supplied owner/path.
        with self.tasks._connect() as db:
            rows = db.execute('SELECT task_id FROM adult_tasks WHERE status=? AND latest_result_artifact_id=?',
                              ('completed',artifact_id)).fetchall()
        if len(rows) != 1:
            raise ValueError('artifact_unavailable')
        task_id = rows[0]['task_id']
        # One authenticated storage snapshot computes the whole-content hash
        # before releasing bytes. No whole-file copy or repeat decryption loop.
        chunk = self.artifacts.read_chunk(artifact_id, offset, max_bytes,
                                         task_id=task_id, include_digest=True)
        return {'artifact_id': artifact_id, 'data_base64':base64.b64encode(chunk.data).decode('ascii'),
                'decoded_bytes':len(chunk.data), 'total_bytes':chunk.total_bytes,
                'offset':chunk.offset, 'next_offset':chunk.next_offset,
                'mime_type':chunk.mime_type, 'sha256':chunk.sha256, 'complete':chunk.eof}
