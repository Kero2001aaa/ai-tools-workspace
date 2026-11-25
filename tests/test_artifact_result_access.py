"""Accepted artifact ownership, integrity and bounded chunks, no provider."""
import base64
import hashlib
import unittest
from unittest.mock import patch
from tests import test_adult_agent as fixtures
from agent.adult.artifact_result_access import ArtifactResultAccess


class ArtifactResultTests(unittest.TestCase):
    setUp=fixtures.AdultAgentTests.setUp
    agent=fixtures.AdultAgentTests.agent
    enable_text=fixtures.AdultAgentTests.enable_text

    def completed(self):
        app=self.agent();self.enable_text()
        task=app.submit_text_task('text.analyze','private source')['task_id']
        app.run_task(task)
        artifact=self.tasks.get_task(task).latest_result_artifact_id
        return app,task,artifact,ArtifactResultAccess(self.tasks,self.artifacts)

    def test_accepted_result_by_id_reports_full_hash_and_bounded_chunk(self):
        app,task,artifact,access=self.completed()
        whole=base64.b64decode(app.get_result_chunk(task)['data_base64'])
        result=access.read(artifact,2,7)
        self.assertEqual(whole[2:9],base64.b64decode(result['data_base64']))
        self.assertEqual(7,result['decoded_bytes'])
        self.assertEqual(hashlib.sha256(whole).hexdigest(),result['sha256'])
        self.assertEqual(len(whole),result['total_bytes'])
        self.assertFalse(result['complete'])
        self.assertNotIn('private source',str(result))
        self.assertNotIn('task_id',result)

    def test_uncommitted_output_input_and_missing_ids_refused(self):
        app,task,artifact,access=self.completed()
        orphan=self.artifacts.save_output(task,'a'*32,b'orphan output','text/plain')
        source=self.artifacts.save_input(task,b'private source','text/plain',retain_encrypted=False)
        for reference in (orphan.artifact_id,source.payload_id,'b'*32,'../../file'):
            with self.assertRaises(ValueError):access.read(reference)

    def test_owner_crosscheck_and_incomplete_task_refused(self):
        app,task,artifact,access=self.completed()
        original=self.artifacts.read_chunk
        with patch.object(self.artifacts,'read_chunk',wraps=original) as read:
            access.read(artifact)
            self.assertTrue(all(call.kwargs.get('task_id')==task for call in read.call_args_list))
        # Task DB result mapping, not an artifact metadata guess, is required.
        with self.tasks._transaction() as db:
            db.execute('UPDATE adult_tasks SET latest_result_artifact_id=NULL WHERE task_id=?',(task,))
        with self.assertRaises(ValueError):access.read(artifact)

    def test_invalid_bounds_and_expired_unavailable_storage_never_leak_partial(self):
        app,task,artifact,access=self.completed()
        for offset,size in ((-1,1),(True,1),(0,True),(0,262145),(0,0)):
            with self.assertRaises(ValueError):access.read(artifact,offset,size)
        with patch.object(self.artifacts,'read_chunk',side_effect=ValueError('unavailable')):
            with self.assertRaises(ValueError):access.read(artifact)

    def test_read_has_no_task_state_mutation(self):
        app,task,artifact,access=self.completed()
        before=(self.root/'tasks.sqlite3').read_bytes()
        access.read(artifact)
        self.assertEqual(before,(self.root/'tasks.sqlite3').read_bytes())

    def test_encrypted_tamper_and_expiry_refused(self):
        import datetime
        from agent.adult.payload_store import PayloadStoreError
        app,task,artifact,access=self.completed()
        # Swap only accepted bytes for encrypted storage to exercise real index,
        # owner, ciphertext-integrity and retention checks without any adapter.
        encrypted=self.artifacts.save_output(task,'c'*32,b'encrypted accepted output','text/plain')
        with self.tasks._transaction() as db:
            db.execute('UPDATE adult_tasks SET latest_result_artifact_id=? WHERE task_id=?',(encrypted.artifact_id,task))
        self.assertEqual(hashlib.sha256(b'encrypted accepted output').hexdigest(),access.read(encrypted.artifact_id)['sha256'])
        path=self.artifacts._payload_path(encrypted.artifact_id)
        original=path.read_bytes()
        path.write_bytes(original[:-1]+bytes([original[-1]^1]))
        with self.assertRaises(PayloadStoreError):access.read(encrypted.artifact_id)
        path.write_bytes(original)
        with self.artifacts._connection() as db:
            db.execute("UPDATE adult_payload_records SET expires_at=? WHERE payload_id=?",('2000-01-01T00:00:00+00:00',encrypted.artifact_id))
        with self.assertRaises(PayloadStoreError):access.read(encrypted.artifact_id)

    def test_digest_covers_entire_artifact_even_empty_last_chunk(self):
        app,task,artifact,access=self.completed()
        content=b'content'*40000
        output=self.artifacts.save_output(task,'d'*32,content,'text/plain')
        with self.tasks._transaction() as db:
            db.execute('UPDATE adult_tasks SET latest_result_artifact_id=? WHERE task_id=?',(output.artifact_id,task))
        result=access.read(output.artifact_id,len(content),1)
        self.assertEqual('',result['data_base64'])
        self.assertEqual(0,result['decoded_bytes'])
        self.assertTrue(result['complete'])
        self.assertEqual(hashlib.sha256(content).hexdigest(),result['sha256'])
        self.assertEqual(len(content),result['total_bytes'])
        with self.assertRaises(ValueError):access.read(output.artifact_id,len(content)+1)

    def test_legacy_store_digest_opt_in_and_exact_boolean(self):
        app,task,artifact,access=self.completed()
        old=self.artifacts.read_chunk(artifact,0,4,task_id=task)
        self.assertIsNone(old.sha256)
        with self.assertRaises(ValueError):
            self.artifacts.read_chunk(artifact,0,4,task_id=task,include_digest=1)
        with self.assertRaises(Exception):
            self.artifacts.read_chunk(artifact,0,4,task_id='another-task',include_digest=True)
