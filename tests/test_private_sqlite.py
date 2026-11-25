"""Real subprocess umask and concurrent owner-only DB creation checks."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from agent.private_sqlite import prepare_database
from agent.task_queue import TaskQueue
from agent.plan_store import PlanStore


@unittest.skipUnless(os.name=='posix','POSIX mode checks')
class PrivateSQLiteTests(unittest.TestCase):
    def test_real_subprocess_permissive_umask_never_changes_new_db_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ('queue','plan'):
                for mask in ('0','18'):
                    path=Path(directory)/(kind+mask+'.db')
                    program='import os;from agent.task_queue import TaskQueue;from agent.plan_store import PlanStore;os.umask('+mask+');store='+('TaskQueue' if kind=='queue' else 'PlanStore')+'('+repr(str(path))+');print(oct(os.stat(store.db_path).st_mode & 0o777))'
                    result=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True,timeout=5)
                    self.assertEqual(0,result.returncode,result.stderr);self.assertEqual('0o600',result.stdout.strip())

    def test_existing_regular_mode_and_data_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'existing.db';db=sqlite3.connect(path);db.execute('CREATE TABLE marker(value)');db.execute("INSERT INTO marker VALUES('preserved')");db.commit();db.close();path.chmod(0o640)
            prepare_database(path);self.assertEqual(0o640,path.stat().st_mode & 0o777)
            with sqlite3.connect(path) as db:self.assertEqual('preserved',db.execute('SELECT value FROM marker').fetchone()[0])

    def test_symlink_fifo_directory_refused_without_target_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'target';target.write_bytes(b'keep');link=root/'link';link.symlink_to(target)
            fifo=root/'fifo';os.mkfifo(fifo)
            for path in (link,fifo,root):
                with self.assertRaises(ValueError):prepare_database(path)
            self.assertEqual(b'keep',target.read_bytes())
            dangling=root/'dangling';dangling.symlink_to(root/'absent')
            with self.assertRaises(ValueError):prepare_database(dangling)
            self.assertFalse((root/'absent').exists())

    def test_concurrent_creation_no_overwrite_and_normal_plan_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'new.db'
            with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(prepare_database,[path]*16))
            self.assertEqual(0o600,path.stat().st_mode & 0o777)
            store=PlanStore(path);plan=store.create_plan('private',[{'task_type':'workspace_status'}]);store.run_next_step(plan['id']);self.assertEqual('completed',store.get_plan(plan['id'])['status'])

    def test_live_native_sqlite_wal_sidecars_inherit_private_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'wal.db';prepare_database(path)
            with sqlite3.connect(path) as db:
                self.assertEqual('wal',db.execute('PRAGMA journal_mode=WAL').fetchone()[0])
                db.execute('CREATE TABLE note(value)');db.execute("INSERT INTO note VALUES('synthetic')");db.commit()
                for suffix in ('-wal','-shm'):
                    sidecar=Path(str(path)+suffix);self.assertTrue(sidecar.exists());self.assertEqual(0o600,sidecar.stat().st_mode & 0o777)

    def test_exact_sqlite_memory_name_creates_no_literal_disk_file(self):
        with tempfile.TemporaryDirectory() as directory:
            program="from agent.task_queue import TaskQueue;TaskQueue(':memory:')"
            result=subprocess.run([sys.executable,'-c',program],cwd=directory,env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])},capture_output=True,text=True,timeout=5)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertEqual([],list(Path(directory).iterdir()))

    def test_uri_style_actual_store_entrypoints_refuse_before_any_file_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            program="""import os
from pathlib import Path
from agent.task_queue import TaskQueue
from agent.plan_store import PlanStore
os.umask(0o022)
for factory in (TaskQueue,PlanStore):
 for name in ('file:target.db','file:target.db?mode=rwc','file::memory:'):
  try: factory(name)
  except ValueError: pass
  else: raise AssertionError('URI name accepted')
assert list(Path('.').iterdir())==[]
"""
            result=subprocess.run([sys.executable,'-c',program],cwd=directory,env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])},capture_output=True,text=True,timeout=5)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertEqual([],list(Path(directory).iterdir()))
