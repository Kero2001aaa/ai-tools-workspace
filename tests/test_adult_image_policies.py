"""Deterministic image input/output policy tests."""
from __future__ import annotations

import unittest

from agent.adult.adult_image_policies import AdultImagePolicies
from agent.adult.models import ValidatedImage


class AdultImagePoliciesTests(unittest.TestCase):
    def setUp(self):
        self.policies = AdultImagePolicies()
        self.image = ValidatedImage(
            mime_type="image/png",
            byte_count=1024,
            width=100,
            height=100,
            sha256="a" * 64,
        )
        self.output_route = {"output_mime_types": ("image/png",), "max_bytes": 20 * 1024 * 1024, "max_width": 8192, "max_height": 8192}

    def validate_output_policy(self, **route):
        policy = {"output_mime_types": ("image/png", "image/jpeg", "image/webp"), "max_bytes": 20 * 1024 * 1024, "max_width": 8192, "max_height": 8192}
        policy.update(route)
        return self.policies.validate_image_output("image.generate", "image/png", self.image, **policy)

    def test_allowed_image_input_tasks_and_default_supported_mimes(self):
        for task_type in ("image.analyze", "image.edit"):
            with self.subTest(task_type=task_type):
                decision = self.policies.validate_image_input(task_type, self.image)
                self.assertTrue(decision.allowed)
                self.assertEqual((), decision.reason_codes)

    def test_image_input_rejects_wrong_task_and_non_metadata(self):
        self.assertFalse(self.policies.validate_image_input("image.generate", self.image).allowed)
        self.assertFalse(self.policies.validate_image_input("image.analyze", object()).allowed)

    def test_image_input_enforces_route_mime_bytes_and_dimensions(self):
        decisions = (
            self.policies.validate_image_input(
                "image.analyze", self.image, accepted_mime_types=("image/jpeg",)
            ),
            self.policies.validate_image_input("image.analyze", self.image, max_bytes=100),
            self.policies.validate_image_input("image.analyze", self.image, max_width=50),
            self.policies.validate_image_input("image.analyze", self.image, max_height=50),
        )
        for decision in decisions:
            with self.subTest(reasons=decision.reason_codes):
                self.assertFalse(decision.allowed)

    def test_image_input_malformed_or_looser_route_constraints_fail_closed(self):
        for kwargs in (
            {"accepted_mime_types": ("image/png", "image/png")},
            {"accepted_mime_types": ("image/*",)},
            {"accepted_mime_types": ()},
            {"max_bytes": True},
            {"max_width": 0},
            {"max_height": "8192"},
        ):
            with self.subTest(kwargs=kwargs):
                decision = self.policies.validate_image_input("image.edit", self.image, **kwargs)
                self.assertFalse(decision.allowed)

    def test_image_output_requires_operation_declared_and_detected_mime_agreement(self):
        self.assertTrue(
            self.validate_output_policy().allowed
        )
        decisions = (
            self.policies.validate_image_output("image.analyze", "image/png", self.image, output_mime_types=("image/png",), max_bytes=1000, max_width=10, max_height=10),
            self.policies.validate_image_output("image.generate", "image/jpeg", self.image, output_mime_types=("image/png",), max_bytes=1000, max_width=10, max_height=10),
            self.policies.validate_image_output(
                "image.edit", "image/png", self.image,
                **{"output_mime_types": ("image/webp",), "max_bytes": 1000, "max_width": 10, "max_height": 10},
            ),
        )
        for decision in decisions:
            self.assertFalse(decision.allowed)

    def test_image_output_enforces_bytes_and_route_dimensions(self):
        decisions = (
            self.validate_output_policy(max_bytes=100),
            self.validate_output_policy(max_width=50),
            self.validate_output_policy(max_height=50),
        )
        for decision in decisions:
            self.assertFalse(decision.allowed)


    def test_image_output_requires_all_four_explicit_route_constraints(self):
        complete = {"output_mime_types": ("image/png",), "max_bytes": 1000, "max_width": 10, "max_height": 10}
        for missing in tuple(complete):
            with self.subTest(missing=missing):
                route = dict(complete)
                del route[missing]
                decision = self.policies.validate_image_output(
                    "image.generate", "image/png", self.image, **route
                )
                self.assertFalse(decision.allowed)
                self.assertIn("route_policy_missing", decision.reason_codes)

    def test_image_output_rejects_invalid_or_looser_route_limits(self):
        for kwargs in ({"max_bytes": True}, {"max_bytes": 20 * 1024 * 1024 + 1}, {"max_width": 8193}, {"max_height": 0}, {"output_mime_types": ()}):
            with self.subTest(kwargs=kwargs):
                decision = self.validate_output_policy(**kwargs)
                self.assertFalse(decision.allowed)
                self.assertTrue({"route_limit_invalid", "route_mime_policy_invalid"}.intersection(decision.reason_codes))

    def test_analysis_output_is_bounded_utf8_and_rejects_controls(self):
        self.assertTrue(self.policies.validate_analysis_output("image.analyze", "clear result").allowed)
        for text in ("", "x" * 20_001, "private\x00content", "\ud800"):
            with self.subTest(length=len(text)):
                self.assertFalse(
                    self.policies.validate_analysis_output("image.analyze", text).allowed
                )
        self.assertFalse(
            self.policies.validate_analysis_output("image.edit", "clear result").allowed
        )


if __name__ == "__main__":
    unittest.main()
