"""Private creation preserves explicit memory migration and scope semantics."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from agent.memory_store import MemoryStore,UnsupportedLegacySchema


@unittest.skipUnless(os.name=='posix','POSIX mode checks')
class PrivateMemoryDBTests(unittest.TestCase):
    def test_actual_subprocess_umask_new_database_private_and_scope_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            for mask in ('0','18'):
                path=Path(directory)/(mask+'.db')
                program="""import os
from pathlib import Path
from agent.memory_store import MemoryStore
os.umask(MASK)
store=MemoryStore(PATH)
store.remember('publicscopefixture')
store.adult_scope().remember('privatescopefixture',memory_type='note')
assert not store.recall('privatescopefixture')
assert store.recall('publicscopefixture')
assert Path(PATH).stat().st_mode & 0o777 == 0o600
""".replace('MASK',mask).replace('PATH',repr(str(path)))
                result=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True,timeout=5)
                self.assertEqual(0,result.returncode,result.stderr)

    def test_existing_current_mode_and_records_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'memory.db';MemoryStore(path).remember('retained');path.chmod(0o640)
            self.assertEqual('retained',MemoryStore(path).recall('retained')[0]['content'])
            self.assertEqual(0o640,path.stat().st_mode & 0o777)

    def test_existing_empty_and_symlink_still_refuse_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'empty.db';path.touch();path.chmod(0o640)
            with self.assertRaises(UnsupportedLegacySchema):MemoryStore(path)
            self.assertEqual(0,path.stat().st_size);self.assertEqual(0o640,path.stat().st_mode & 0o777)
            link=root/'link';link.symlink_to(path)
            with self.assertRaises(ValueError):MemoryStore(link)
            self.assertEqual(0,path.stat().st_size)

    def test_exact_memory_name_actual_store_refuses_before_disk_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            program="""import os
from pathlib import Path
from agent.memory_store import MemoryStore
os.umask(0o022)
try: MemoryStore(':memory:')
except ValueError: pass
else: raise AssertionError('MemoryStore memory name accepted')
assert list(Path('.').iterdir())==[]
"""
            result=subprocess.run([sys.executable,'-c',program],cwd=directory,env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])},capture_output=True,text=True,timeout=5)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertEqual([],list(Path(directory).iterdir()))
