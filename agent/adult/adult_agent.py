"""Combined internal Adult application, no provider factory or public transport."""
from agent.adult.bounded_text_application import BoundedTextApplication
from agent.adult.bounded_image_application import BoundedImageApplication
from agent.adult.adult_image_validator import validate_image

_TEXT = frozenset({'text.analyze', 'text.generate', 'text.rewrite'})
_IMAGE = frozenset({'image.analyze', 'image.generate', 'image.edit'})


class AdultAgent:
    """Route bounded operations over fixed injected text and image applications.

    Internal composition only. Real approval authentication, semantic content
    policy and transport registration are intentionally not supplied here.
    """
    def __init__(self, text: BoundedTextApplication, image: BoundedImageApplication):
        if not isinstance(text, BoundedTextApplication) or not isinstance(image, BoundedImageApplication):
            raise TypeError('bounded_applications_required')
        if text.tasks is not image.tasks or text.artifacts is not image.artifacts:
            raise ValueError('shared_application_stores_required')
        if text.executor.fingerprinter.key_id != image.executor.fingerprinter.key_id:
            raise ValueError('matching_application_keys_required')
        if text.budget != image.budget:
            raise ValueError('matching_application_budgets_required')
        self.text = text
        self.image = image
        self.tasks = text.tasks

    def _application(self, task_id):
        task = self.tasks.get_task(task_id)
        if task is None:
            raise ValueError('task_unavailable')
        if task.task_type in _TEXT:
            return self.text
        if task.task_type in _IMAGE:
            return self.image
        raise ValueError('task_type_unavailable')

    def submit_text_task(self, task_type, text, instruction='', *, retain_payload=False):
        if type(task_type) is not str or task_type not in _TEXT:
            raise ValueError('text_task_unavailable')
        return self.text.submit_text_task(task_type, text, instruction, retain_payload=retain_payload)

    def submit_image_task(self, task_type, prompt='', image_bytes=None, mime_type=None, *, retain_payload=False):
        if type(task_type) is not str or task_type not in _IMAGE:
            raise ValueError('image_task_unavailable')
        if type(prompt) is not str:
            raise ValueError('invalid_image_prompt')
        if image_bytes is not None:
            # Never trust declared MIME, verify actual decoding before staging.
            metadata = validate_image(image_bytes, output=False)
            if mime_type is not None and mime_type != metadata.mime_type:
                raise ValueError('image_mime_mismatch')
        elif mime_type is not None:
            raise ValueError('unexpected_image_mime')
        # An optional analysis prompt is supported by the envelope below and
        # stays bound to the task fingerprint; an empty prompt stays promptless.
        return self.image.submit_image_task(task_type, image=image_bytes,
            prompt=prompt if prompt else None, retain_payload=retain_payload)

    def get_task(self, task_id):
        return self._application(task_id).get_task(task_id)

    def list_tasks(self, status=None, limit=20, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('invalid_page_limit')
        page = self.tasks.list_tasks(status, limit, cursor)
        return {'tasks': [self.get_task(task.task_id) for task in page.items],
                'next_cursor': page.next_cursor, 'has_more': page.has_more}

    def get_attempts(self, task_id, limit=20):
        return self._application(task_id).get_attempts(task_id, limit)

    def run_task(self, task_id, *, max_seconds=240, max_attempts=5):
        BoundedTextApplication._bounds(max_attempts, max_seconds)
        return self._application(task_id).run_task(task_id, max_attempts=max_attempts,
                                                  max_seconds=max_seconds)

    def resume_task(self, task_id, decision, replacement_payload=None, *, max_seconds=240, max_attempts=5):
        BoundedTextApplication._bounds(max_attempts, max_seconds)
        if replacement_payload is not None:
            # A generic replacement would silently change fingerprints or
            # lose instruction/MIME context. Use explicit exact-restore methods.
            raise ValueError('use_explicit_exact_input_restore')
        return self._application(task_id).resume_task(task_id, decision,
                                    max_attempts=max_attempts, max_seconds=max_seconds)

    def restore_text_input(self, task_id, text, instruction='', *, max_seconds=240, max_attempts=5):
        BoundedTextApplication._bounds(max_attempts, max_seconds)
        if self._application(task_id) is not self.text:
            raise ValueError('text_task_unavailable')
        return self.text.restore_input_and_resume(task_id, text, instruction,
                                    max_attempts=max_attempts, max_seconds=max_seconds)

    def restore_image_input(self, task_id, *, image_bytes=None, prompt=None, max_seconds=240, max_attempts=5):
        BoundedTextApplication._bounds(max_attempts, max_seconds)
        if self._application(task_id) is not self.image:
            raise ValueError('image_task_unavailable')
        return self.image.restore_input_and_run(task_id, image=image_bytes, prompt=prompt,
                                    max_attempts=max_attempts, max_seconds=max_seconds)

    def cancel_task(self, task_id):
        return self._application(task_id).cancel_task(task_id)

    def get_result_chunk(self, task_id, offset=0, max_bytes=262144):
        return self._application(task_id).get_result_chunk(task_id, offset, max_bytes)
