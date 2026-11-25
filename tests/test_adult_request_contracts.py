"""Pure strict request validation, not an endpoint, consent or provider test."""
import base64
import unittest
from agent.adult.adult_request_contracts import validate_request, request_schemas, REQUEST_MODELS
from tests.test_adult_image_executor import png_bytes

ID='12345678-1234-4321-8123-123456789012'


class RequestContractTests(unittest.TestCase):
    def valid(self):
        return {
            'adult_submit_text_task': {'task_type':'text.analyze','text':'safe content'},
            'adult_submit_image_task': {'task_type':'image.generate','prompt':'red square'},
            'adult_run_task': {'task_id':ID}, 'adult_get_task': {'task_id':ID},
            'adult_list_tasks': {}, 'adult_get_attempts': {'task_id':ID},
            'adult_resume_task': {'task_id':ID,'decision':'continue'},
            'adult_cancel_task': {'task_id':ID},
            'adult_get_artifact_chunk': {'artifact_id':'a'*32}}

    def test_exact_nine_schemas_forbid_extra_fields_without_registration(self):
        self.assertEqual(set(self.valid()),set(REQUEST_MODELS))
        for name,args in self.valid().items():
            validate_request(name,args)
            self.assertFalse(request_schemas()[name]['additionalProperties'])
            with self.assertRaises(ValueError):validate_request(name,{**args,'principal':'admin'})
        schemas=request_schemas();schemas.clear()
        self.assertEqual(9,len(request_schemas()))

    def test_strict_numbers_booleans_and_limits(self):
        for value in (True,1.0,'1',0,301):
            with self.assertRaises(ValueError):validate_request('adult_run_task',{'task_id':ID,'max_seconds':value})
        for value in (True,1.0,'1',0,21):
            with self.assertRaises(ValueError):validate_request('adult_list_tasks',{'limit':value})
        for value in (1,'true',None):
            with self.assertRaises(ValueError):validate_request('adult_submit_text_task',{'task_type':'text.analyze','text':'safe','retain_payload':value})
        for field,value in [('offset',-1),('offset',True),('max_bytes',262145),('max_bytes',0)]:
            with self.assertRaises(ValueError):validate_request('adult_get_artifact_chunk',{'artifact_id':'a'*32,field:value})

    def test_ids_types_and_unapproved_fields_rejected(self):
        for name in ('adult_run_task','adult_get_task','adult_cancel_task'):
            for value in ('../../file','a'*32,'',None,123):
                with self.assertRaises(ValueError):validate_request(name,{'task_id':value})
        for args in ({'task_type':'image.edit','text':'safe'}, {'task_type':'text.analyze','text':' '},
                     {'task_type':'text.analyze','text':'x'*20001}):
            with self.assertRaises(ValueError):validate_request('adult_submit_text_task',args)

    def test_image_shape_decoding_actual_mime_and_prompt(self):
        encoded=base64.b64encode(png_bytes()).decode()
        for task in ('image.analyze','image.edit'):
            validate_request('adult_submit_image_task',{'task_type':task,'prompt':'' if task=='image.analyze' else 'blue',
                             'image_base64':encoded,'image_mime_type':'image/png'})
        # An optional bounded analysis prompt validates like generate/edit prompts.
        validate_request('adult_submit_image_task',{'task_type':'image.analyze','prompt':'read the sign',
                         'image_base64':encoded,'image_mime_type':'image/png'})
        for args in ({'task_type':'image.analyze','prompt':''},
                     {'task_type':'image.edit','prompt':'','image_base64':encoded},
                     {'task_type':'image.generate','prompt':'red','image_base64':encoded},
                     {'task_type':'image.analyze','prompt':'','image_base64':encoded,'image_mime_type':'image/jpeg'},
                     {'task_type':'image.analyze','prompt':'','image_base64':'%%%'}):
            with self.assertRaises(ValueError):validate_request('adult_submit_image_task',args)

    def test_replacement_conflicts_cancel_and_mime_without_image(self):
        for fields in ({'replacement_text':'text','replacement_image_base64':'AA=='},
                       {'decision':'cancel','replacement_text':'text'},
                       {'replacement_image_mime_type':'image/png'},
                       {'replacement_image_base64':'%%%'}):
            with self.assertRaises(ValueError):validate_request('adult_resume_task',{'task_id':ID,'decision':'continue',**fields})
        validate_request('adult_resume_task',{'task_id':ID,'decision':'continue','replacement_text':'original'})

    def test_errors_and_repr_never_echo_sensitive_input(self):
        marker='private-input-sentinel'
        with self.assertRaises(ValueError) as error:
            validate_request('adult_submit_text_task',{'task_type':'invalid','text':marker})
        self.assertEqual('invalid_adult_request',str(error.exception))
        valid=validate_request('adult_submit_text_task',{'task_type':'text.analyze','text':marker})
        self.assertNotIn(marker,repr(valid))
        for name,args in [('unknown',{}),('adult_run_task',[]),(None,{})]:
            with self.assertRaises(ValueError):validate_request(name,args)
