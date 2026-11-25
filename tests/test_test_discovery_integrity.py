"""Prevent silently shadowed test methods from inflating coverage confidence."""
import ast
from pathlib import Path
import unittest


def duplicate_test_names(source):
    tree=ast.parse(source)
    duplicates=[]
    for node in ast.walk(tree):
        if not isinstance(node,ast.ClassDef):
            continue
        seen=set()
        for child in node.body:
            if isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef)) and child.name.startswith('test_'):
                if child.name in seen:duplicates.append((node.name,child.name))
                seen.add(child.name)
    return duplicates


class TestDiscoveryIntegrity(unittest.TestCase):
    def test_no_shadowed_test_method_in_suite(self):
        duplicates=[]
        for path in Path(__file__).parent.glob('test_*.py'):
            duplicates.extend((path.name,*item) for item in duplicate_test_names(path.read_text()))
        self.assertEqual([],duplicates)

    def test_detector_finds_sync_and_async_shadow(self):
        source='class Example:\n def test_x(self):pass\n async def test_x(self):pass\n'
        self.assertEqual([('Example','test_x')],duplicate_test_names(source))
