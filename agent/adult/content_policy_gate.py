"""Fail-closed exact-content policy boundary, no built-in semantic moderator.

Only an injected private backend evaluates content. Decisions are not accepted
from tool arguments, task options, adapter/model output or owner attestations.
This module is not wired into execution or a public endpoint.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import secrets
import time
from agent.adult.models import TASK_TYPES, _identifier
from agent.adult.payload_store import PayloadCipher


@dataclass(frozen=True)
class PolicyVerdict:
    allowed: bool
    reason: str
    # Fixed facts from a trusted moderation backend, never owner attestations.
    # Sexual-image generation/edit involving a real person or an unknown
    # identity/age/consent cannot be allowed by this boundary at all.
    sexual: bool = False
    involves_minors: bool = False
    real_person: bool = False
    uncertain: bool = False

    def __post_init__(self):
        if any(type(getattr(self,key)) is not bool for key in
               ('allowed','sexual','involves_minors','real_person','uncertain')):
            raise ValueError('invalid_policy_verdict')
        if self.reason not in {'allowed','prohibited_content','uncertain_content','backend_unavailable'}:
            raise ValueError('invalid_policy_verdict')
        if self.allowed != (self.reason=='allowed'):
            raise ValueError('invalid_policy_verdict')


@dataclass(frozen=True)
class PolicyReceipt:
    task_id: str
    task_type: str
    stage: str
    policy_version: str
    content_fingerprint: str = field(repr=False)
    issued_at: float
    expires_at: float
    signature: str = field(repr=False)


class ContentPolicyGate:
    """Issue only backend-approved, stage/content/task-bound private receipts.

    An unavailable or malformed backend is a denial. A receipt alone does not
    execute anything and does not replace a backend's semantic certification.
    The key is random per process. Restart invalidates prior receipts, so a
    fresh review is required. No secret/content/receipt is persisted or logged.
    """
    __slots__=('_backend','_cipher','_version','_clock','_key')

    def __init__(self, backend, cipher: PayloadCipher, policy_version: str, *, clock=time.time):
        if not callable(backend) or not isinstance(cipher,PayloadCipher) or not callable(clock):
            raise ValueError('policy_backend_required')
        _identifier('policy_version',policy_version)
        self._backend,self._cipher,self._version,self._clock=backend,cipher,policy_version,clock
        self._key=secrets.token_bytes(32)

    def __repr__(self):
        return 'ContentPolicyGate(private_backend)'

    @staticmethod
    def _context(task_id,task_type,stage,content):
        _identifier('task_id',task_id)
        if type(task_type) is not str or task_type not in TASK_TYPES:
            raise ValueError('invalid_policy_context')
        if type(stage) is not str or stage not in {'input','output'}:
            raise ValueError('invalid_policy_context')
        if type(content) is not bytes or not content or len(content)>20*1024*1024:
            raise ValueError('invalid_policy_content')

    def _now(self):
        value=self._clock()
        if type(value) not in (int,float) or not math.isfinite(value):
            raise ValueError('policy_clock_unavailable')
        return float(value)

    @staticmethod
    def _body(receipt):
        return json.dumps([receipt.task_id,receipt.task_type,receipt.stage,
            receipt.policy_version,receipt.content_fingerprint,receipt.issued_at,
            receipt.expires_at],separators=(',',':'),allow_nan=False).encode('utf-8')

    def review(self,task_id,task_type,stage,content,*,ttl_seconds=60):
        self._context(task_id,task_type,stage,content)
        if type(ttl_seconds) is not int or not 1<=ttl_seconds<=300:
            raise ValueError('invalid_policy_ttl')
        try:
            verdict=self._backend(task_type,stage,content)
        except Exception:
            raise ValueError('content_policy_unavailable') from None
        if type(verdict) is not PolicyVerdict:
            raise ValueError('content_policy_unavailable')
        # No claim that a backend flag proves a third party's consent. This MVP
        # refuses all sexual real-person image generation/editing, even if a
        # backend otherwise returns allow. Minors and uncertainty fail closed.
        if (not verdict.allowed or verdict.uncertain or verdict.involves_minors
                or (verdict.sexual and verdict.real_person and task_type in {'image.generate','image.edit'})):
            raise ValueError('content_policy_rejected')
        now=self._now()
        receipt=PolicyReceipt(task_id,task_type,stage,self._version,
            self._cipher.fingerprint(content),now,now+ttl_seconds,'')
        from dataclasses import replace
        return replace(receipt,signature=hmac.new(self._key,self._body(receipt),hashlib.sha256).hexdigest())

    def verify(self,receipt,task_id,task_type,stage,content):
        """Only exact matching unexpired decisions authorize a later gate check."""
        try:
            self._context(task_id,task_type,stage,content)
            if type(receipt) is not PolicyReceipt:
                return False
            now=self._now()
            if (type(receipt.issued_at) is not float or type(receipt.expires_at) is not float
                    or not math.isfinite(receipt.issued_at) or not math.isfinite(receipt.expires_at)
                    or not receipt.issued_at<=now<receipt.expires_at
                    or not 0<receipt.expires_at-receipt.issued_at<=300
                    or (receipt.task_id,receipt.task_type,receipt.stage,receipt.policy_version)
                       !=(task_id,task_type,stage,self._version)
                    or not hmac.compare_digest(receipt.content_fingerprint,self._cipher.fingerprint(content))):
                return False
            signature=hmac.new(self._key,self._body(receipt),hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature,receipt.signature)
        except Exception:
            return False
