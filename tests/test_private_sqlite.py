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
