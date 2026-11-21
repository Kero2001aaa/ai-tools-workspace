"""Tests for Adult capability persistence, evidence and v2-to-v3 migration."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.capability_registry import (
    CapabilityConflict, CapabilityEvidenceError, CapabilityNotEligible,
    CapabilityRegistryError, CapabilityStateConflict, ModelCapabilityRegistry,
)
from agent.adult.models import (
    AdultTaskRequest, CapabilityEvidence, CapabilityQuery, CostPolicy,
    ModelCapabilitySpec, TaskBudget, RouteCandidate,
)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-11-21T12:00:00.000000Z"
FUTURE = "2026-12-21T12:00:00.000000Z"
COST_FUTURE = "2026-11-28T12:00:00.000000Z"
INPUT_FP = "hmac-sha256:0123456789abcdef0123456789abcdef"
CONFIG_FP = "hmac-sha256:abcdef0123456789abcdef0123456789"


def text_spec(*, adapter_id="mock-adapter", provider_id="mock-provider",