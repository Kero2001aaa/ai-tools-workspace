"""Strict response models against real offline applications, no endpoint."""
import base64
import hashlib
import unittest
from agent.adult.adult_response_contracts import validate_response,response_schemas,ErrorResponse
from agent.adult.artifact_result_access import ArtifactResultAccess
from tests import test_artifact_result_access as fixtures


class ResponseContractTests(unittest.TestCase):
    setUp=fixtures.ArtifactResultTests.setUp
    agent=fixtures.ArtifactResultTests.agent
    enable_text=fixtures.ArtifactResultTests.enable_text
    completed=fixtures.ArtifactResultTests.completed

    def test_actual_task_list_execution_attempt_and_result_validate(self):
        app=self.agent();self.enable_text()
        staged=app.submit_text_task('text.analyze','private source')
        validate_response('task',staged)
        validate_response('tasks',app.list_tasks())
        result=app.run_task(staged['task_id']);validate_response('execution',result)
        validate_response('attempts',{'attempts':app.get_attempts(staged['task_id'])})
        artifact=result['task']['latest_result_artifact_id']
        access=ArtifactResultAccess(self.tasks,self.artifacts)
        validate_response('artifact',access.read(artifact))
        validate_response('artifact',access.read(artifact,1,3))

    def test_every_object_schema_forbids_unknown_fields(self):
        def visit(value):
            if isinstance(value,dict):
                if value.get('type')=='object':self.assertFalse(value.get('additionalProperties',True))
                for child in value.values():visit(child)
            elif isinstance(value,list):
                for child in value:visit(child)
        visit(response_schemas())
        schemas=response_schemas();schemas.clear();self.assertEqual(6,len(response_schemas()))

    def test_unknown_payload_secret_and_path_fields_rejected_not_dropped(self):
        app,task,artifact,access=self.completed();safe=app.get_task(task)
        for field in ('raw_text','api_key','payload_id','input_fingerprint','path','namespace'):
            with self.assertRaises(ValueError) as error:
                validate_response('task',{**safe,field:'sensitive-marker'})
            self.assertEqual('invalid_adult_response',str(error.exception))
        for field,value in (('task_id','/tmp/task'),('selected_model','https://user:pass@host/model'),
                            ('last_error_code','private prompt\n/home/file'),('created_at','yesterday')):
            with self.assertRaises(ValueError):validate_response('task',{**safe,field:value})

    def test_chunk_base64_size_offset_completion_and_whole_hash(self):
        app,task,artifact,access=self.completed();safe=access.read(artifact)
        for field,value in (('data_base64','%%%'),('decoded_bytes',True),('decoded_bytes',262145),
                            ('next_offset',0),('total_bytes',-1),('offset',1),
                            ('complete',False),('mime_type','text/html'),('sha256','0'*64)):
            with self.assertRaises(ValueError):validate_response('artifact',{**safe,field:value})
        partial={**safe,'data_base64':'','decoded_bytes':0,'offset':1,'next_offset':1,'complete':False}
        with self.assertRaises(ValueError):validate_response('artifact',partial)

    def test_counters_exact_booleans_and_bounds(self):
        app,task,artifact,access=self.completed();safe=app.get_task(task)
        for field,value in (('attempt_count',True),('attempt_count',21),('provider_calls','1'),
                            ('user_action_required',1)):
            with self.assertRaises(ValueError):validate_response('task',{**safe,field:value})
        for value in ({'tasks':[safe]*21,'has_more':False,'next_cursor':None},
                      {'tasks':[safe,safe],'has_more':False,'next_cursor':None},
                      {'tasks':[],'has_more':True,'next_cursor':None}):
            with self.assertRaises(ValueError):validate_response('tasks',value)

    def test_attempt_order_and_cleanup_consistency(self):
        app,task,artifact,access=self.completed();attempt=app.get_attempts(task)[0]
        with self.assertRaises(ValueError):validate_response('attempts',{'attempts':[attempt,attempt]})
        with self.assertRaises(ValueError):validate_response('execution',{'task':app.get_task(task),'error_code':None,
                                                 'input_cleanup':{'status':'cleanup_failed'}})
        validate_response('execution',{'task':app.get_task(task),'error_code':None,
                   'input_cleanup':{'status':'cleanup_failed','error_code':'input_cleanup_unavailable'}})
        with self.assertRaises(ValueError):ErrorResponse(status='error',error_code='private-provider-error')
        for kind,value in (('unknown',{}),('task',[]),(None,{})):
            with self.assertRaises(ValueError):validate_response(kind,value)
