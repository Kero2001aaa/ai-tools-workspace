"""Actual CLI prepares bounded logical context without model/source execution."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from tests import test_code_report_retrieval as fixtures
from agent.saved_code_context import saved_code_context


class SavedContextCLITests(unittest.TestCase):
    setUp=fixtures.SavedCodeReportTests.setUp

    def command(self, database):
        return [sys.executable,'-m','agent.code_report_cli','--db',str(database),
            '--plan-id',self.plan['id'],'--step-id',self.step,'--supervisor-context']

    def test_fresh_process_prepares_after_source_deletion_no_state_change(self):
        before=self.store.get_plan(self.plan['id'])
        self.source.unlink()
        process=subprocess.run(self.command(self.store.db_path),cwd=Path(__file__).resolve().parents[1],
            text=True,capture_output=True,timeout=10)
        self.assertEqual(0,process.returncode,process.stderr)
        self.assertTrue(process.stdout.isascii())
        self.assertNotIn(chr(27),process.stdout)
        self.assertEqual(json.loads(saved_code_context(self.store.db_path,self.plan['id'],self.step)),json.loads(process.stdout))
        logical=json.dumps(json.loads(process.stdout),ensure_ascii=False,separators=(',',':'))
        self.assertLessEqual(len(logical),4000)
        self.assertEqual(before,self.store.get_plan(self.plan['id']))
        self.assertEqual(1,self.store.queue.get(self.first['task_id'])['attempts'])

    def test_missing_database_fixed_error_no_creation(self):
        missing=self.root/'missing.sqlite3'
        process=subprocess.run(self.command(missing),cwd=Path(__file__).resolve().parents[1],
            text=True,capture_output=True,timeout=10)
        self.assertEqual(1,process.returncode)
        self.assertEqual('',process.stdout)
        self.assertEqual('saved_code_report_unavailable\n',process.stderr)
        self.assertFalse(missing.exists())
