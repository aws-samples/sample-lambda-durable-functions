# Copyright (c) 2025 Amazon Web Services, Inc. or its affiliates.
# All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Local unit tests for the Stripe payment durable workflow.

These tests exercise the durable function's branching logic without deploying
to AWS or calling the real Stripe API. The ``DurableFunctionTestRunner`` from
``aws-durable-execution-sdk-python-testing`` runs the handler locally and lets
us drive the callback (the point where the function suspends waiting for the
Stripe webhook).

Stripe network calls inside the ``create-payment-intent`` step are patched so
the workflow runs deterministically and offline.

Run with:
    pytest -q test_payment_processor.py
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aws_durable_execution_sdk_python_testing import DurableFunctionTestRunner
from aws_durable_execution_sdk_python_testing.model import ErrorObject

import payment_processor


def _fake_intent(status: str = "requires_action") -> SimpleNamespace:
    """Build a stand-in for a Stripe PaymentIntent object."""
    return SimpleNamespace(
        id="pi_test_12345",
        status=status,
        amount=2500,
        currency="usd",
        client_secret="pi_test_12345_secret_abc",
    )


@pytest.fixture
def runner():
    r = DurableFunctionTestRunner(payment_processor.handler, poll_interval=0.05)
    yield r
    r.close()


def _result_dict(test_result):
    """The runner returns the handler output as a JSON string; decode it."""
    raw = test_result.result
    return json.loads(raw) if isinstance(raw, (str, bytes)) else raw


def _event(**overrides) -> str:
    base = {
        "customer_id": "cus_test",
        "amount": 2500,
        "currency": "usd",
        "payment_method_id": "pm_card_visa",
        "description": "unit-test payment",
    }
    base.update(overrides)
    return json.dumps(base)


def test_successful_payment_resumes_and_returns_succeeded(runner):
    """Happy path: webhook reports success, function returns 'succeeded'."""
    with patch.object(payment_processor.stripe.PaymentIntent, "create",
                      return_value=_fake_intent()):
        arn = runner.run_async(input=_event())
        callback_id = runner.wait_for_callback(arn, name="stripe-payment-result")
        runner.send_callback_success(callback_id, result=json.dumps({
            "type": "payment_intent.succeeded",
            "charge_id": "ch_test_999",
            "card_brand": "visa",
            "card_last4": "4242",
        }).encode("utf-8"))
        result = _result_dict(runner.wait_for_result(arn))

    assert result["status"] == "succeeded"
    assert result["charge_id"] == "ch_test_999"
    assert result["card"] == "visa ****4242"
    assert result["payment_intent_id"] == "pi_test_12345"


def test_declined_payment_returns_failed(runner):
    """Webhook reports payment_failed -> function returns 'failed'."""
    with patch.object(payment_processor.stripe.PaymentIntent, "create",
                      return_value=_fake_intent(status="requires_payment_method")):
        arn = runner.run_async(input=_event(payment_method_id="pm_card_chargeDeclined"))
        callback_id = runner.wait_for_callback(arn, name="stripe-payment-result")
        runner.send_callback_success(callback_id, result=json.dumps({
            "type": "payment_intent.payment_failed",
            "decline_code": "card_declined",
            "error_message": "Your card was declined.",
        }).encode("utf-8"))
        result = _result_dict(runner.wait_for_result(arn))

    assert result["status"] == "failed"
    assert result["decline_code"] == "card_declined"
    assert result["error_message"] == "Your card was declined."


def test_callback_timeout_returns_timeout(runner):
    """A failed/timed-out callback raises CallbackError internally; the handler
    must catch it and return a clean 'timeout' result (regression test for the
    bug where the code checked ``if result is None``)."""
    with patch.object(payment_processor.stripe.PaymentIntent, "create",
                      return_value=_fake_intent()):
        arn = runner.run_async(input=_event())
        callback_id = runner.wait_for_callback(arn, name="stripe-payment-result")
        runner.send_callback_failure(callback_id, error=ErrorObject(
            message="callback timed out",
            type="CallbackTimeout",
            data=None,
            stack_trace=None,
        ))
        result = _result_dict(runner.wait_for_result(arn))

    assert result["status"] == "timeout"
    assert "5 minutes" in result["message"]


def test_synchronous_card_decline_returns_failed(runner):
    """A hard decline raises CardError at PaymentIntent.create (confirm=True).
    The step must catch it and the handler must return a clean 'failed' result
    immediately, without suspending on a webhook that will never arrive."""
    card_error = payment_processor.stripe.error.CardError(
        message="Your card was declined.", param=None, code="card_declined")
    with patch.object(payment_processor.stripe.PaymentIntent, "create",
                      side_effect=card_error):
        result = _result_dict(runner.run(input=_event(
            payment_method_id="pm_card_chargeDeclined")))

    assert result["status"] == "failed"
    assert result["decline_code"]
    assert "declined" in result["error_message"].lower()


def test_validation_rejects_zero_amount(runner):
    """Invalid amount must fail validation before any Stripe call."""
    result = runner.run(input=_event(amount=0))
    assert result.error is not None


def test_validation_rejects_missing_payment_method(runner):
    """Missing payment_method_id must fail validation."""
    event = json.loads(_event())
    del event["payment_method_id"]
    result = runner.run(input=json.dumps(event))
    assert result.error is not None
