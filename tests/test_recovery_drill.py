"""Runs the real operator CLIs and resumed workers in separate processes."""
import importlib.util
from pathlib import Path
import unittest


class RecoveryDrillTests(unittest.TestCase):
    def test_clean_process_plan_memory_recovery(self):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'recovery_drill.py'
        spec = importlib.util.spec_from_file_location('recovery_drill', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.main()
        self.assertTrue(result['offline'])
        self.assertTrue(result['source_plan_unchanged'])
        self.assertFalse(result['cross_database_atomic_snapshot'])
        self.assertEqual(1, result['clean_process']['dispatches'])
        self.assertTrue(result['clean_process']['private_namespace_excluded'])
