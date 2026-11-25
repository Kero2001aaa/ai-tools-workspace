"""Explicit fixed-free text composition, no route certification or auto execution.

This library is not registered as a model tool or gateway endpoint. External
provider availability remains a separate verified precondition for a live run.
"""
from __future__ import annotations

from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.adult_evaluator import AdultEvaluator
from agent.adult.adult_policies import AdultPolicies
from agent.adult.adult_router import AdultModelRouter
from agent.adult.adult_supervisor import AdultSupervisor
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.adult_text_executor import AdultTextExecutor
from agent.adult.adult_text_input import encode_text_input
from agent.adult.adult_text_intake import AdultTextIntake
from agent.adult.adult_text_pipeline import AdultTextPipeline
from agent.adult.capability_registry import ModelCapabilityRegistry
from agent.adult.integrations.omniroute_chat import AdultOmniRouteConfig
from agent.adult.integrations.omniroute_text import OmniRouteTextAdapter
from agent.adult.models import CapabilityQuery, CostPolicy, TaskBudget
from agent.adult.payload_store import AdultArtifactStore, PayloadCipher


class FixedFreeTextService:
    """Compose existing stores and guarded adapter, never broaden model policy."""
    def __init__(self, tasks: AdultTaskStore, artifacts: AdultArtifactStore,
                 cipher: PayloadCipher, config: AdultOmniRouteConfig):
        if not isinstance(tasks, AdultTaskStore) or not isinstance(artifacts, AdultArtifactStore):
            raise TypeError('Real task and artifact stores required.')
        if not isinstance(cipher, PayloadCipher) or artifacts.cipher is None or artifacts.cipher.key_id != cipher.key_id:
            raise ValueError('matching_encryption_key_required')
        if (not isinstance(config, AdultOmniRouteConfig)
                or config.base_url.rstrip('/') != 'https://openrouter.ai/api/v1'):
            raise ValueError('fixed_free_endpoint_required')
        self.tasks, self.artifacts, self.cipher, self.config = tasks, artifacts, cipher, config
        self.registry = ModelCapabilityRegistry(tasks)
        self.tracker = AdultAttemptTracker(tasks, route_validator=self.registry.current_text_route_in_transaction)

    def _fingerprint(self, task_type):
        return self.cipher.fingerprint(b'fixed-free-text-service-v1:' + task_type.encode('ascii'))

    @staticmethod
    def _permitted(route):
        return (route.cost_class == 'free_verified' and route.provider_id == 'openrouter'
                and route.model_id == 'openrouter/free'
                and route.task_type in {'text.analyze', 'text.generate', 'text.rewrite'})

    def _executor(self, task_type, input_fingerprint, budget):
        config = self._fingerprint(task_type)
        query = CapabilityQuery(task_type, ('text',), 'text', input_fingerprint, config)
        policy = CostPolicy(budget.max_provider_calls, 0, False)
        eligible = self.registry.list_eligible(query, policy)
        permitted = [route for route in eligible if self._permitted(route)]
        # A route-bound adapter has one exact model/operation identity per key.
        keys = [(route.adapter_id, route.adapter_version) for route in permitted]
        if len(keys) != len(set(keys)):
            raise ValueError('ambiguous_adapter_contract')
        adapters = {key: OmniRouteTextAdapter(self.config, route, self.artifacts)
                    for key, route in zip(keys, permitted)}
        router = AdultModelRouter(self.registry, approach_key_builder=self.tracker.approach_keys)
        # Filter again after the registry's live evidence check at each dispatch.
        choose = router.choose_routes
        router.choose_routes = lambda query, cost, excluded: [
            route for route in choose(query, cost, excluded)
            if self._permitted(route) and (route.adapter_id, route.adapter_version) in adapters
        ]
        policies = AdultPolicies()
        pipeline = AdultTextPipeline(self.artifacts, self.tasks, router, self.tracker,
            policies, AdultEvaluator(policies), adapters)
        return AdultTextExecutor(self.tasks, AdultTextIntake(self.tasks, self.artifacts, self.cipher),
                                 pipeline, self.cipher)

    def submit(self, task_type: str, text: str, instruction: str = '', *, budget: TaskBudget):
        if not isinstance(budget, TaskBudget) or budget.max_provider_calls < 1:
            raise ValueError('explicit_provider_call_budget_required')
        data = encode_text_input(task_type, text, instruction)
        executor = self._executor(task_type, self.cipher.fingerprint(data), budget)
        # Existing stage validates the environment key before creating the task.
        # Submission makes no adapter call, even with an available route.
        return executor.stage_encrypted(task_type, data, budget)

    def run(self, task_id: str, *, max_attempts=1, max_seconds=120):
        # Provider availability/rate-limit recovery is external, never a loop.
        if isinstance(max_attempts, bool) or max_attempts != 1:
            raise ValueError('fixed_free_single_attempt_only')
        task = self.tasks.get_task(task_id)
        if task is None or task.task_type not in {'text.analyze', 'text.generate', 'text.rewrite'}:
            raise ValueError('text_task_unavailable')
        request = self.tasks.get_task_request(task_id)
        if task.status not in {'completed', 'failed', 'cancelled'}:
            if request.payload_id is None:
                raise ValueError('retained_input_required')
            data = self.artifacts.load_input(request.payload_id, task_id)
            if self.cipher.fingerprint(data) != request.input_fingerprint:
                raise ValueError('input_key_mismatch')
        budget = TaskBudget(max_total_attempts=task.max_total_attempts,
            max_wall_seconds=task.max_wall_seconds, max_provider_calls=task.max_provider_calls)
        executor = self._executor(task.task_type, request.input_fingerprint, budget)
        return AdultSupervisor(executor).run_until_done(task_id, self._fingerprint(task.task_type),
            max_attempts=max_attempts, max_seconds=max_seconds)
