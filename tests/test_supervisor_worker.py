"""Tests for bounded supervisor orchestration."""
import unittest
from unittest.mock import patch

from agent.supervisor_worker import _parse_plan, supervise_request


class SupervisorWorkerTests(unittest.TestCase):
    def test_accepts_one_or_two_distinct_analysis_roles(self):
        self.assertEqual(
            [{"specialist": "research", "task": "Find evidence"}],
            _parse_plan('{"assignments":[{"specialist":"research","task":"Find evidence"}]}'),
        )
        self.assertEqual(
            2,
            len(_parse_plan('{"assignments":[{"specialist":"research","task":"A"},{"specialist":"coding","task":"B"}]}')),
        )

    def test_rejects_invalid_or_unsafe_plans(self):
        invalid = [
            "not json",
            '{"assignments":[]}',
            '{"assignments":[{"specialist":"review","task":"Review"}]}',
            '{"assignments":[{"specialist":"research","task":"A"},{"specialist":"research","task":"B"}]}',
            '{"assignments":[{"specialist":"shell","task":"run command"}]}',
            '{"assignments":[{"specialist":"coding","task":"x","extra":"y"}]}',
            '{"assignments":[{"specialist":"coding","task":"' + ("x" * 1001) + '"}]}',
            '{"assignments":[{"specialist":"coding","task":"A"}],"extra":true}',
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                _parse_plan(raw)

    def test_offline_mode_fails_closed(self):
        with patch("agent.supervisor_worker.load_chat_model", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                supervise_request("Review this design")

    def test_workflow_plans_dispatches_and_reviews_with_bounded_calls(self):
        class Response:
            content = '{"assignments":[{"specialist":"research","task":"Research risks"},{"specialist":"coding","task":"Review implementation"}]}'

        class FakeModel:
            def invoke(self, messages):
                return Response()

        with patch("agent.supervisor_worker.load_chat_model", return_value=FakeModel()), patch(
            "agent.supervisor_worker.run_specialist",
            side_effect=lambda role, task, context, model: {
                "specialist": role, "analysis": f"{role}-analysis", "truncated": False
            },
        ) as worker:
            result = supervise_request("Assess this implementation")

        self.assertEqual(3, worker.call_count)
        self.assertEqual(["research", "coding", "review"], [x["specialist"] for x in result["specialist_results"]])
        self.assertEqual("completed", result["status"])
        self.assertEqual(4, result["model_calls"])
        self.assertEqual(4, result["max_model_calls"])
        self.assertIn("Final review", result["synthesis"])
        self.assertIn("research-analysis", worker.call_args_list[2].args[2])
        self.assertEqual("review", worker.call_args_list[2].args[0])

    def test_workflow_stops_on_first_specialist_failure_without_retry(self):
        class Response:
            content = '{"assignments":[{"specialist":"research","task":"Research"}]}'
        class FakeModel:
            def invoke(self, messages):
                return Response()
        with patch("agent.supervisor_worker.load_chat_model", return_value=FakeModel()), patch(
            "agent.supervisor_worker.run_specialist", side_effect=RuntimeError("worker failed")
        ) as worker:
            with self.assertRaisesRegex(RuntimeError, "worker failed"):
                supervise_request("Do research")
        self.assertEqual(1, worker.call_count)

    def test_rejects_invalid_request_and_context_bounds(self):
        for request, context in [("", ""), ("x" * 4001, ""), ("ok", "x" * 4001)]:
            with self.subTest(length=len(request)), self.assertRaises(ValueError):
                supervise_request(request, context)


if __name__ == "__main__":
    unittest.main()
