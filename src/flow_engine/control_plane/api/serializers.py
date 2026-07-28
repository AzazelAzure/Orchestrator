"""Request/response serializers for OpenAPI."""

from rest_framework import serializers


class CommandRequestSerializer(serializers.Serializer):
    payload = serializers.DictField(required=False, default=dict)
    target_id = serializers.CharField(required=False, allow_null=True)
    idempotency_key = serializers.CharField(required=False, allow_null=True)
    expected_revision = serializers.IntegerField(required=False, allow_null=True)


class OperationResponseSerializer(serializers.Serializer):
    operation_id = serializers.CharField()
    command_type = serializers.CharField(required=False)
    status = serializers.CharField()
    from_cache = serializers.BooleanField(required=False)
    error_code = serializers.CharField(required=False, allow_null=True)
    error = serializers.CharField(required=False, allow_null=True)
    result = serializers.DictField(required=False, allow_null=True)
    anomalies = serializers.ListField(required=False)


class RuntimePreviewSerializer(CommandRequestSerializer):
    work_item_id = serializers.CharField()
    provider = serializers.CharField()


class RuntimeRunSerializer(CommandRequestSerializer):
    work_item_id = serializers.CharField()
    provider = serializers.CharField()
    delivery_mode = serializers.ChoiceField(
        choices=["inline", "async"], required=False, default="inline"
    )


class RuntimeShowSerializer(serializers.Serializer):
    run_id = serializers.CharField()


class HeartbeatSerializer(serializers.Serializer):
    attempt_id = serializers.CharField()


class ResultSubmitSerializer(serializers.Serializer):
    attempt_id = serializers.CharField()
    outcome = serializers.ChoiceField(choices=["complete", "failed", "outcome_unknown"])
    evidence = serializers.DictField(required=False)
    anomalies = serializers.ListField(required=False, default=list)


class McpInvokeSerializer(serializers.Serializer):
    tool = serializers.CharField()
    arguments = serializers.DictField(required=False, default=dict)
    expected_snapshot_digest = serializers.CharField(required=False, allow_null=True)
    department = serializers.ChoiceField(
        choices=["admin-ops", "qa", "tech"], required=False, allow_null=True
    )
    loadout_id = serializers.CharField(required=False, allow_null=True)


class McpToolResultSerializer(serializers.Serializer):
    status = serializers.CharField()
    lane_id = serializers.CharField(required=False)
    tool = serializers.CharField(required=False)
    snapshot_digest = serializers.CharField(required=False)
    result = serializers.DictField(required=False)
    error_code = serializers.CharField(required=False, allow_null=True)
    error = serializers.CharField(required=False, allow_null=True)
    initiating_principal_id = serializers.CharField(required=False)
    mcp_service_principal = serializers.DictField(required=False)


class ScriptExecuteSerializer(serializers.Serializer):
    """Public execute schema — no caller-controlled test hooks or path overrides."""

    script_id = serializers.CharField()
    input = serializers.DictField(required=False, default=dict)
    idempotency_key = serializers.CharField(required=False, allow_null=True)
    expected_executable_digest = serializers.CharField(required=False, allow_null=True)
    expected_image_digest = serializers.CharField(required=False, allow_null=True)

    _BANNED_HOOKS = frozenset(
        {
            "workspace_root",
            "simulate_network",
            "force_timeout",
            "inject_env",
            "override_argv",
            "override_cwd",
            "cwd",
            "argv",
            "env",
        }
    )

    def validate(self, attrs: dict) -> dict:
        initial = getattr(self, "initial_data", None) or {}
        for key in self._BANNED_HOOKS:
            if key in initial:
                raise serializers.ValidationError(
                    {key: "caller-controlled path/test hook is denied"}
                )
        return attrs


class ScriptCancelSerializer(serializers.Serializer):
    execution_id = serializers.CharField()


class RuntimeRunControlSerializer(serializers.Serializer):
    run_id = serializers.CharField()


class ScheduleTickSerializer(serializers.Serializer):
    schedule_id = serializers.CharField()
    planned_time = serializers.CharField()
    provider_call_budget = serializers.IntegerField(required=False, default=0)
    idempotency_key = serializers.CharField(required=False, allow_null=True)

    def validate(self, attrs: dict) -> dict:
        initial = getattr(self, "initial_data", None) or {}
        for key in ScriptExecuteSerializer._BANNED_HOOKS:
            if key in initial:
                raise serializers.ValidationError(
                    {key: "caller-controlled path/test hook is denied"}
                )
        return attrs


class ScheduleCompleteSerializer(serializers.Serializer):
    run_id = serializers.CharField()
    effects = serializers.ListField(required=False, default=list)
    script_results = serializers.ListField(required=False, default=list)
    script_ids = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    provider_calls = serializers.IntegerField(required=False, default=0)
    attempt_remediation = serializers.BooleanField(required=False, default=False)
    remediation = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs: dict) -> dict:
        initial = getattr(self, "initial_data", None) or {}
        for key in ScriptExecuteSerializer._BANNED_HOOKS:
            if key in initial:
                raise serializers.ValidationError(
                    {key: "caller-controlled path/test hook is denied"}
                )
        return attrs
