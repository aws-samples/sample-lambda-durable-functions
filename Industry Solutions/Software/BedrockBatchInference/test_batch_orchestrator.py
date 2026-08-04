# Copyright (c) 2025 Amazon Web Services, Inc. or its affiliates.
# All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Tests for Bedrock Batch Inference durable function.
"""
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from aws_durable_execution_sdk_python_testing import InvocationStatus


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("BUCKET", "test-bucket")
    monkeypatch.setenv("INPUT_PREFIX", "inputs/")
    monkeypatch.setenv("MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")
    monkeypatch.setenv("BEDROCK_ROLE_ARN", "arn:aws:iam::123456789012:role/test")
    monkeypatch.setenv("CALLBACK_TABLE", "test-callbacks")
    monkeypatch.setenv("PROMPT_TEMPLATE", "Summarize:\n\n{content}")
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "bedrock-batch")
    monkeypatch.setenv("LOG_LEVEL", "INFO")


@pytest.fixture
def mock_aws():
    """Mock all AWS service calls."""
    with patch("batch_orchestrator.s3") as mock_s3, \
         patch("batch_orchestrator.bedrock") as mock_bedrock, \
         patch("batch_orchestrator.dynamodb") as mock_dynamodb:

        # list_objects_v2 returns 100 .txt files
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {"Contents": [{"Key": f"inputs/doc-{i:03d}.txt"} for i in range(100)]}
        ]
        mock_s3.get_paginator.return_value = mock_paginator

        # get_object for reading file content
        mock_s3.get_object.return_value = {
            "Body": MagicMock(
                read=MagicMock(return_value=b"This is a sample document about cloud computing.")
            )
        }

        # list_objects_v2 for reading output (non-paginated call)
        mock_s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "jobs/abc/output/results.jsonl.out"}]
        }

        mock_bedrock.create_model_invocation_job.return_value = {
            "jobArn": "arn:aws:bedrock:us-east-1:123456789012:model-invocation-job/test-123"
        }
        mock_bedrock.get_model_invocation_job.return_value = {
            "outputDataConfig": {
                "s3OutputDataConfig": {"s3Uri": "s3://test-bucket/jobs/abc/output/"}
            }
        }

        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        yield {
            "s3": mock_s3,
            "bedrock": mock_bedrock,
            "dynamodb": mock_dynamodb,
            "table": mock_table,
        }


class TestBatchOrchestrator:
    """Test the durable function orchestrator."""

    @pytest.mark.durable_execution(
        handler="batch_orchestrator.handler",
        lambda_function_name="test-orchestrator",
    )
    def test_happy_path(self, durable_runner, mock_aws):
        """Full flow: list files → build input → submit → callback → read output."""
        with durable_runner:
            execution_future = durable_runner.run_async(input={}, timeout=30)

            time.sleep(0.2)
            callback_op = durable_runner.get_operation("wait-for-bedrock")
            callback_op.send_callback_success(json.dumps({
                "batchJobArn": "arn:aws:bedrock:us-east-1:123456789012:model-invocation-job/test-123",
                "status": "Completed",
            }))

            result = execution_future.result(timeout=10)

        assert result.status is InvocationStatus.SUCCEEDED
        assert result.result["status"] == "Completed"
        assert result.result["filesProcessed"] == 100

        # Verify batch input was written to S3
        mock_aws["s3"].put_object.assert_called_once()
        call_kwargs = mock_aws["s3"].put_object.call_args[1]
        assert call_kwargs["Key"].startswith("jobs/")
        assert call_kwargs["Key"].endswith("/input.jsonl")

    @pytest.mark.durable_execution(
        handler="batch_orchestrator.handler",
        lambda_function_name="test-orchestrator",
    )
    def test_job_failure_handled_gracefully(self, durable_runner, mock_aws):
        """Bedrock job fails — function returns error status, doesn't crash."""
        with durable_runner:
            execution_future = durable_runner.run_async(input={}, timeout=30)

            time.sleep(0.2)
            callback_op = durable_runner.get_operation("wait-for-bedrock")
            callback_op.send_callback_failure("JobFailed", "Model error")

            result = execution_future.result(timeout=10)

        assert result.status is InvocationStatus.SUCCEEDED
        assert result.result["status"] == "failed"

    @pytest.mark.durable_execution(
        handler="batch_orchestrator.handler",
        lambda_function_name="test-orchestrator",
    )
    def test_too_few_files_raises(self, durable_runner, mock_aws):
        """Fewer than 100 files raises ValueError."""
        # Override to return only 50 files
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {"Contents": [{"Key": f"inputs/doc-{i:03d}.txt"} for i in range(50)]}
        ]
        mock_aws["s3"].get_paginator.return_value = mock_paginator

        with durable_runner:
            result = durable_runner.run(input={}, timeout=10)

        assert result.status is InvocationStatus.FAILED
        assert "100 files" in str(result.error)


class TestCallbackRelay:
    """Test the EventBridge callback relay."""

    def test_relay_sends_callback(self):
        with patch("callback_relay.table") as mock_table, \
             patch("callback_relay.lambda_client") as mock_lambda:
            mock_table.get_item.return_value = {
                "Item": {"jobArn": "arn:aws:bedrock:...:job/abc", "callbackId": "cb-123"}
            }

            from callback_relay import handler
            result = handler(
                {"detail": {"batchJobArn": "arn:aws:bedrock:...:job/abc", "status": "Completed"}},
                None,
            )

            assert result["sent"] is True
            mock_lambda.send_durable_execution_callback_success.assert_called_once()

    def test_relay_ignores_unknown_jobs(self):
        with patch("callback_relay.table") as mock_table:
            mock_table.get_item.return_value = {}

            from callback_relay import handler
            result = handler(
                {"detail": {"batchJobArn": "arn:aws:bedrock:...:job/unknown", "status": "Completed"}},
                None,
            )

            assert result["sent"] is False
            assert result["reason"] == "not_tracked"
