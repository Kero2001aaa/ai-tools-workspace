"""Completed source quote survives cancel/restore without source or dispatch."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent.code_task import configured_context
from agent.code_report_cli import read_saved_report
from agent.plan_store import PlanStore
from agent.plan_recovery import copy_database


class CancelSavedReportJourneyTests(unittest.TestCase):
    def test_completed_quote_survives_cancel_and_restore_remaining_work_never_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'sample.py';source.write_text('def sample():\n    return 1\n')
            with patch.dict(os.environ,{'WORKSPACE_CODE_CONTEXT':json.dumps({'root':str(root),'approved_files':['sample.py']})}):
                payload={'question':'Which function is supplied?','selections':[{'path':'sample.py','start_line':1,'max_lines':1}],'config_sha256':configured_context()[1]}
                store=PlanStore(root/'plans.db');plan=store.create_plan('quote then cancel',[{'task_type':'local_code_report','payload':payload},{'task_type':'workspace_status'}])
                outcome=store.run_next_step(plan['id']);self.assertEqual('step_succeeded',outcome['status'])
            before=read_saved_report(store.db_path,plan['id'],plan['steps'][0]['id'])
            source.unlink();current=store.get_plan(plan['id']);store.cancel_plan(plan['id'],current['updated_at'])
            copy_database(store.db_path,root/'backup.db');copy_database(root/'backup.db',root/'restored.db')
            with patch.dict(os.environ,{'WORKSPACE_CODE_CONTEXT':''}),patch('agent.task_worker.execute_task',side_effect=AssertionError('cancelled work cannot execute')):
                restarted=PlanStore(root/'restored.db');self.assertEqual('stop',restarted.supervise(plan['id'])['decision'])
                after=read_saved_report(root/'restored.db',plan['id'],plan['steps'][0]['id'])
            self.assertEqual(before,after);self.assertEqual('def sample():\n',after['citations'][0]['quote'])
            self.assertEqual(['succeeded','cancelled'],[step['status'] for step in restarted.get_plan(plan['id'])['steps']])
            tasks=restarted.queue.list_tasks();self.assertEqual(1,len(tasks));self.assertEqual(('succeeded',1),(tasks[0]['status'],tasks[0]['attempts']))
