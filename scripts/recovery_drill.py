"""Offline clean-process recovery drill using only temporary synthetic data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from agent.memory_store import MemoryStore
from agent.plan_store import PlanStore


def main():
    with tempfile.TemporaryDirectory(prefix='workspace-recovery-drill-') as directory:
        root = Path(directory)
        source_plan = root / 'plans.sqlite3'
        source_memory = root / 'memory.sqlite3'
        restored_plan = root / 'restored-plans.sqlite3'
        restored_memory = root / 'restored-memory.sqlite3'
        memories = MemoryStore(source_memory)
        memories.remember('Synthetic general recovery note')
        memories.adult_scope().remember('adultrecoveryuniquemarker', memory_type='note')
        plans = PlanStore(source_plan)
        plan = plans.create_plan('Integrated recovery drill', [
            {'task_type': 'workspace_status'},
            {'task_type': 'memory_recent', 'payload': {'limit': 5}},
        ])
        first = plans.run_next_step(plan['id'])
        original_first = plans.get_plan(plan['id'])['steps'][0]
        receipts = []
        for module, source, destination in (
            ('agent.plan_recovery', source_plan, restored_plan),
            ('agent.memory_recovery', source_memory, restored_memory),
        ):
            result = subprocess.run([sys.executable, '-m', module, 'restore', str(source), str(destination)],
                                    capture_output=True, text=True, check=True)
            receipts.append(json.loads(result.stdout))
        env = {**os.environ, 'WORKSPACE_PLAN_DB': str(restored_plan),
               'WORKSPACE_MEMORY_DB': str(restored_memory), 'DRILL_PLAN_ID': plan['id'],
               'DRILL_FIRST_TASK': first['task_id']}
        child = '''import json,os
from unittest.mock import patch
from agent.plan_store import PlanStore
from agent.memory_store import MemoryStore
from agent.task_worker import execute_task
s=PlanStore();p=os.environ['DRILL_PLAN_ID'];before=s.get_plan(p)['steps'][0]
with patch('agent.task_worker.execute_task',wraps=execute_task) as worker:
    outcome=s.run_until_done(p);s.run_until_done(p)
    assert worker.call_count==1
result=s.get_plan(p)['steps'][1]['result']['memories']
assert [r['content'] for r in result]==['Synthetic general recovery note']
assert MemoryStore().recall('adultrecoveryuniquemarker')==[]
assert s.get_plan(p)['steps'][0]==before
assert s.queue.get(os.environ['DRILL_FIRST_TASK'])['status']=='succeeded'
print(json.dumps({'status':outcome['status'],'dispatches':worker.call_count,
 'completed_output_unchanged':True,'execution_identity_preserved':True,
 'restored_memory_worker_used':True,'private_namespace_excluded':True}))
'''
        result = subprocess.run([sys.executable, '-c', child], env=env, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Synthetic recovery drill child failed')
        assert plans.get_plan(plan['id'])['steps'][0] == original_first
        assert plans.get_plan(plan['id'])['status'] == 'checkpointed'
        return {'offline': True, 'temporary_synthetic_data_only': True,
                'cross_database_atomic_snapshot': False, 'source_plan_unchanged': True,
                'receipts': receipts, 'clean_process': json.loads(result.stdout)}


if __name__ == '__main__':
    print(json.dumps(main(), indent=2, sort_keys=True))
