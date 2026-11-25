"""User-invocable zero-network local journey, explicitly not AI."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from agent.adult.text_stats_cli import run_stats

class TextStatsCliTests(unittest.TestCase):
    def test_real_journey_counts_with_network_disabled(self):
        with patch('socket.socket',side_effect=AssertionError('network forbidden')):
            result=run_stats('one two\nthree')
        self.assertEqual('completed',result['status'])
        self.assertEqual(1,result['attempts']);self.assertEqual('step_completed',result['checkpoint'])
        self.assertEqual({'mode':'local_text_statistics_not_ai','characters':13,'words_whitespace':3,'lines':2,'utf8_bytes':13},result['statistics'])
    def command(self,text):
        return subprocess.run([sys.executable,'-m','agent.adult.text_stats_cli'],input=text,capture_output=True,cwd=Path(__file__).resolve().parents[1],timeout=20)
    def test_subprocess_stdin_to_safe_output(self):
        result=self.command(b'Private source words.')
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual('local_text_statistics_not_ai',json.loads(result.stdout)['statistics']['mode'])
        self.assertNotIn(b'Private source',result.stdout+result.stderr)
    def test_empty_oversize_and_invalid_utf8_fail_without_source_echo(self):
        for data in (b'',b'x'*(80*1024+1),b'\xff'):
            with self.subTest(size=len(data)):
                result=self.command(data);self.assertEqual(1,result.returncode)
                self.assertEqual(b'',result.stdout)
                self.assertEqual('local_text_stats_failed',json.loads(result.stderr)['error_code'])
