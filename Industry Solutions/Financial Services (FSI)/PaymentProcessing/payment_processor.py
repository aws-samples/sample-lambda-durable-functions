# Copyright (c) 2025 Amazon Web Services, Inc. or its affiliates.
# All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Stripe card payment workflow using Lambda Durable Functions.
Enhanced with Powertools structured logging, X-Ray tracing, and EMF metrics.
"""
import json
import os
from datetime import datetime, timezone

import stripe
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit

from aws_durable_execution_sdk_python import (
    DurableContext, StepContext, durable_execution, durable_step,
)
from aws_durable_execution_sdk_python.config import CallbackConfig, Duration
from aws_durable_execution_sdk_python.exceptions import CallbackError

PAYMENT_TIMEOUT_MINUTES = 5
# Test-only: this payment method simulates a stuck webhook. The durable
# function will suspend indefinitely and hit ExecutionTimeout instead of
# the callback timeout, surfacing a TIMED_OUT terminal state.
SIMULATE_TIMEOUT_PM = "pm_card_simulate_timeout"

logger = Logger(service="payment-processor")
tracer = Tracer(service="payment-processor")
metrics = Metrics(namespace="DurablePayments", service="payment-processor")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")


@durable_step
@tracer.capture_method
def validate_payment_request(step_context: StepContext, event: dict) -> dict:
    amount = event.get("amount")
    currency = event.get("currency", "usd")
    customer_id = event.get("customer_id")
    payment_method_id = event.get("payment_method_id")

    if not amount or amount <= 0:
        metrics.add_metric(name="ValidationFailure", unit=MetricUnit.Count, value=1)
        raise ValueError(f"Invalid amount: {amount}")
    if not customer_id:
        metrics.add_metric(name="ValidationFailure", unit=MetricUnit.Count, value=1)
        raise ValueError("Missing customer_id")
    if not payment_method_id:
        metrics.add_metric(name="ValidationFailure", unit=MetricUnit.Count, value=1)
        raise ValueError("Missing payment_method_id")

    logger.info("Payment validated", customer_id=customer_id, amount=amount,
                currency=currency, payment_method_id=payment_method_id)
    return {
        "customer_id": customer_id, "amount": amount, "currency": currency.lower(),
        "payment_method_id": payment_method_id,
        "description": event.get("description", "Card payment"),
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }


@durable_step
@tracer.capture_method
def create_stripe_payment_intent(step_context: StepContext, payment: dict, callback_id: str) -> dict:
    tracer.put_annotation("callback_id", callback_id)
    tracer.put_annotation("customer_id", payment["customer_id"])

    try:
        intent = stripe.PaymentIntent.create(
            amount=payment["amount"], currency=payment["currency"],
            payment_method=payment["payment_method_id"], confirm=True,
            description=payment["description"],
            metadata={"callback_id": callback_id},
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            return_url="https://example.com/return",
        )
    except stripe.error.CardError as exc:
        # A hard decline at confirm time (e.g. pm_card_chargeDeclined) raises
        # synchronously. This is a deterministic business outcome, not a
        # transient error — return a structured decline instead of letting the
        # step retry and fail the whole execution.
        err = getattr(exc, "error", None)
        decline_code = (getattr(err, "decline_code", None)
                        or getattr(exc, "code", None) or "generic_decline")
        pi = getattr(err, "payment_intent", None)
        pi_id = pi["id"] if pi and "id" in pi else "pi_declined"
        logger.warning("Card declined at creation", decline_code=decline_code,
                       error_message=str(exc.user_message or exc))
        metrics.add_metric(name="PaymentDeclinedAtCreate", unit=MetricUnit.Count, value=1)
        return {"declined": True, "decline_code": decline_code,
                "error_message": exc.user_message or "Your card was declined.",
                "payment_intent_id": pi_id,
                "created_at": datetime.now(timezone.utc).isoformat()}

    logger.info("PaymentIntent created", payment_intent_id=intent.id,
                status=intent.status, amount=intent.amount, callback_id=callback_id)
    metrics.add_metric(name="PaymentIntentCreated", unit=MetricUnit.Count, value=1)

    result = {"payment_intent_id": intent.id, "status": intent.status,
              "amount": intent.amount, "currency": intent.currency,
              "created_at": datetime.now(timezone.utc).isoformat()}

    if intent.status == "requires_action":
        result["client_secret"] = intent.client_secret
        metrics.add_metric(name="ThreeDSecureRequired", unit=MetricUnit.Count, value=1)

    return result


def _safe_annotate(key, value):
    try:
        tracer.put_annotation(key, value)
    except Exception:
        pass


@durable_execution
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict, context: DurableContext) -> dict:
    # 1 — Validate
    payment = context.step(validate_payment_request(event), name="validate-payment")
    logger.append_keys(customer_id=payment["customer_id"])
    _safe_annotate("customer_id", payment["customer_id"])

    # 2 — Create durable callback. For the simulated-timeout test path we set
    # the callback timeout well beyond ExecutionTimeout so the execution itself
    # times out (TIMED_OUT) rather than the callback raising a handled timeout.
    is_timeout_sim = payment["payment_method_id"] == SIMULATE_TIMEOUT_PM
    callback_timeout = (
        Duration.from_hours(1) if is_timeout_sim
        else Duration.from_minutes(PAYMENT_TIMEOUT_MINUTES)
    )
    callback = context.create_callback(
        name="stripe-payment-result",
        config=CallbackConfig(timeout=callback_timeout),
    )
    logger.info("Callback created", callback_id=callback.callback_id,
                simulate_timeout=is_timeout_sim)
    _safe_annotate("callback_id", callback.callback_id)

    # 3 — Create PaymentIntent via Stripe (skipped for timeout simulation)
    if is_timeout_sim:
        logger.info("Timeout simulation — skipping PaymentIntent, suspending indefinitely")
        intent = {"payment_intent_id": f"pi_sim_{callback.callback_id[:12]}"}
    else:
        intent = context.step(
            create_stripe_payment_intent(payment, callback.callback_id),
            name="create-payment-intent",
        )
    logger.append_keys(payment_intent_id=intent["payment_intent_id"])
    _safe_annotate("payment_intent_id", intent["payment_intent_id"])

    # 3a — Synchronous decline. A hard decline (e.g. pm_card_chargeDeclined)
    # fails at PaymentIntent.create with confirm=True, so no webhook will ever
    # arrive. Return the decline immediately instead of suspending forever.
    if intent.get("declined"):
        now = datetime.now(timezone.utc).isoformat()
        logger.error("Payment declined at creation",
                     decline_code=intent.get("decline_code"))
        metrics.add_metric(name="PaymentFailed", unit=MetricUnit.Count, value=1)
        return {"payment_intent_id": intent["payment_intent_id"], "status": "failed",
                "decline_code": intent.get("decline_code", "generic_decline"),
                "error_message": intent.get("error_message", "Payment was declined"),
                "completed_at": now}

    # 4 — Suspend and wait for Stripe webhook.
    # callback.result() suspends the execution at zero compute cost and resumes
    # when the webhook handler sends SendDurableExecutionCallbackSuccess. If the
    # callback timeout elapses with no response, result() raises CallbackError
    # (it does NOT return None — None is only returned for an empty success
    # payload), so the timeout path is handled via except.
    logger.info("Suspending — waiting for Stripe webhook callback")
    try:
        result = callback.result()
    except CallbackError as exc:
        now = datetime.now(timezone.utc).isoformat()
        logger.warning("Payment timed out",
                       payment_intent_id=intent["payment_intent_id"], reason=str(exc))
        metrics.add_metric(name="PaymentTimeout", unit=MetricUnit.Count, value=1)
        return {"payment_intent_id": intent["payment_intent_id"], "status": "timeout",
                "message": f"No confirmation within {PAYMENT_TIMEOUT_MINUTES} minutes",
                "completed_at": now}

    now = datetime.now(timezone.utc).isoformat()

    # The webhook sends a JSON string (passed through verbatim by the SDK), so
    # decode it before inspecting the event type.
    if isinstance(result, (str, bytes)):
        result = json.loads(result)

    event_type = result.get("type", "unknown")

    # 5b — Success
    if event_type == "payment_intent.succeeded":
        logger.info("Payment succeeded", charge_id=result.get("charge_id"),
                     card_brand=result.get("card_brand"), amount=payment["amount"])
        metrics.add_metric(name="PaymentSucceeded", unit=MetricUnit.Count, value=1)
        metrics.add_metric(name="PaymentAmount", unit=MetricUnit.Count, value=payment["amount"])
        return {"payment_intent_id": intent["payment_intent_id"], "status": "succeeded",
                "charge_id": result.get("charge_id"), "amount": payment["amount"],
                "currency": payment["currency"],
                "card": f"{result.get('card_brand','unknown')} ****{result.get('card_last4','****')}",
                "completed_at": now}

    # 5c — Failure
    decline_code = result.get("decline_code", "generic_decline")
    logger.error("Payment failed", decline_code=decline_code,
                 error_message=result.get("error_message"))
    metrics.add_metric(name="PaymentFailed", unit=MetricUnit.Count, value=1)
    _safe_annotate("decline_code", decline_code)
    return {"payment_intent_id": intent["payment_intent_id"], "status": "failed",
            "decline_code": decline_code,
            "error_message": result.get("error_message", "Payment was declined"),
            "completed_at": now}
