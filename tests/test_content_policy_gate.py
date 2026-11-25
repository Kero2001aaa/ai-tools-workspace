"""Policy binding and denials using synthetic verdicts, not moderation proof."""
import unittest
from dataclasses import replace
from agent.adult.content_policy_gate import ContentPolicyGate,PolicyVerdict,PolicyReceipt
from agent.adult.payload_store import PayloadCipher


class ContentPolicyGateTests(unittest.TestCase):
    def setUp(self):
        self.cipher=PayloadCipher(b'k'*32);self.now=1000.0
        self.calls=[]
        def backend(task_type,stage,content):
            self.calls.append((task_type,stage,content))
            return PolicyVerdict(True,'allowed')
        self.gate=ContentPolicyGate(backend,self.cipher,'policy-v1',clock=lambda:self.now)
        self.args=('task-1','text.analyze','input',b'private exact content')

    def test_exact_content_stage_task_operation_and_gate_bound(self):
        receipt=self.gate.review(*self.args)
        self.assertTrue(self.gate.verify(receipt,*self.args))
        for args in (('task-2',*self.args[1:]),(self.args[0],'text.generate',*self.args[2:]),
                     (*self.args[:2],'output',self.args[3]),(*self.args[:3],b'changed')):
            self.assertFalse(self.gate.verify(receipt,*args))
        other=ContentPolicyGate(lambda *args:PolicyVerdict(True,'allowed'),self.cipher,'policy-v1',clock=lambda:self.now)
        self.assertFalse(other.verify(receipt,*self.args))
        self.assertEqual(1,len(self.calls))
        self.assertNotIn('private exact content',repr(receipt))
        self.assertNotIn(receipt.signature,repr(receipt))

    def test_tamper_expiry_future_and_clock_rollback_fail_closed(self):
        receipt=self.gate.review(*self.args,ttl_seconds=10)
        for bad in (replace(receipt,signature='0'*64),replace(receipt,policy_version='policy-v2'),
                    replace(receipt,expires_at=2000.0),replace(receipt,issued_at=float('nan')),
                    replace(receipt,content_fingerprint='not-a-fingerprint'),{}):
            self.assertFalse(self.gate.verify(bad,*self.args))
        self.now=1010.0;self.assertFalse(self.gate.verify(receipt,*self.args))
        self.now=999.0;self.assertFalse(self.gate.verify(receipt,*self.args))

    def test_backend_error_malformed_and_denied_never_issue_receipts(self):
        def broken(*args):raise RuntimeError('sensitive backend trace')
        for backend in (broken,lambda *args:True,lambda *args:{'allowed':True},
                        lambda *args:PolicyVerdict(False,'prohibited_content')):
            gate=ContentPolicyGate(backend,self.cipher,'v1')
            with self.assertRaises(ValueError) as error:gate.review(*self.args)
            self.assertNotIn('sensitive',str(error.exception))

    def test_nonbypassable_minors_uncertainty_and_sexual_real_person_images(self):
        for flags in ({'involves_minors':True},{'uncertain':True},{'sexual':True,'real_person':True}):
            gate=ContentPolicyGate(lambda *args:PolicyVerdict(True,'allowed',**flags),self.cipher,'v1')
            for operation in ('image.generate','image.edit'):
                with self.assertRaises(ValueError):gate.review('task-1',operation,'input',b'content')
                with self.assertRaises(ValueError):gate.review('task-1',operation,'output',b'content')
        # This is not a real-person consent exception or a keyword moderator.
        self.assertTrue(self.gate.verify(self.gate.review(*self.args),*self.args))

    def test_invalid_context_ttl_and_exact_verdict_types(self):
        for value in (True,0,301,1.0,'1'):
            with self.assertRaises(ValueError):self.gate.review(*self.args,ttl_seconds=value)
        for args in (('../task',*self.args[1:]),(self.args[0],'video.generate',*self.args[2:]),
                     (*self.args[:2],'unknown',self.args[3]),(*self.args[:3],b'')):
            with self.assertRaises(ValueError):self.gate.review(*args)
            self.assertFalse(self.gate.verify(None,*args))
        with self.assertRaises(ValueError):PolicyVerdict(1,'allowed')
        with self.assertRaises(ValueError):PolicyVerdict(True,'prohibited_content')
        with self.assertRaises(ValueError):PolicyVerdict(False,'private provider exception')

    def test_invalid_clock_never_issues_or_verifies(self):
        receipt=self.gate.review(*self.args)
        self.now=float('nan')
        self.assertFalse(self.gate.verify(receipt,*self.args))
        with self.assertRaises(ValueError):self.gate.review(*self.args)
