"""Shared bounded operational views, no content/fingerprints/provider bodies."""

SNAPSHOT_FIELDS=(
    'task_id','task_type','status','created_at','updated_at','deadline_at',
    'attempt_count','failed_attempts_since_approval','provider_calls','approvals_granted',
    'max_total_attempts','max_wall_seconds','max_provider_calls','max_cost_microusd',
    'spent_cost_microusd','current_step_id','selected_adapter','selected_provider',
    'selected_model','selected_tool','latest_result_artifact_id','last_error_code',
    'user_action_required','cancel_requested')
ATTEMPT_FIELDS=(
    'attempt_id','attempt_no','adapter_id','adapter_version','provider_id','model_id',
    'model_version','tool_id','tool_version','status','failure_class','retryable',
    'elapsed_ms','error_code','cost_microusd')
USAGE_KEYS=frozenset({'input_tokens','output_tokens','total_tokens','image_tokens','provider_units'})


def task_snapshot(task):
    return {name:getattr(task,name) for name in SNAPSHOT_FIELDS}


def attempt_summary(attempt):
    result={name:getattr(attempt,name) for name in ATTEMPT_FIELDS}
    # Revalidate stored counters, never pass through arbitrary usage labels.
    usage=attempt.usage
    if type(usage) is not dict or any(key not in USAGE_KEYS or type(value) is not int
                                   or not 0<=value<=10**12 for key,value in usage.items()):
        raise ValueError('attempt_usage_unavailable')
    result['usage']=dict(usage)
    return result
