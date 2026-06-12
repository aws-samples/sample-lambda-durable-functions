# Copyright (c) 2025 Amazon Web Services, Inc. or its affiliates.
# All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Stripe webhook handler.
Enhanced with Powertools structured logging, X-Ray tracing, and EMF metrics.
"""
import json
import os

import boto3
import stripe
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit

logger = Logger(service="stripe-webhook")
tracer = Tracer(service="stripe-webhook")
metrics = Metrics(namespace="DurablePayments", service="stripe-webhook")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
lambda_client = boto3.client("lambda")


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict, context) -> dict:
    body = event.get("body", "")
    sig_header = (event.get("headers", {}).get("Stripe-Signature", "")
                  or event.get("headers", {}).get("stripe-signature", ""))

    logger.info("Webhook received", has_signature=bool(sig_header))
    metrics.add_metric(name="WebhookReceived", unit=MetricUnit.Count, value=1)

    try:
        stripe_event = stripe.Webhook.construct_event(body, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        logger.warning("Webhook signature verification failed")
        metrics.add_metric(name="WebhookSignatureFailure", unit=MetricUnit.Count, value=1)
        return _response(400, {"error": "Invalid signature"})
    except Exception as exc:
        logger.exception("Webhook parse error")
        return _response(400, {"error": str(exc)})

    event_type = stripe_event["type"]
    intent = stripe_event["data"]["object"].to_dict()
    callback_id = intent.get("metadata", {}).get("callback_id")
    payment_intent_id = intent.get("id")

    logger.append_keys(event_type=event_type, payment_intent_id=payment_intent_id)
    tracer.put_annotation("event_type", event_type)
    tracer.put_annotation("payment_intent_id", payment_intent_id or "unknown")

    if not callback_id:
        logger.info("No callback_id in metadata — skipping")
        return _response(200, {"received": True})

    logger.append_keys(callback_id=callback_id)
    tracer.put_annotation("callback_id", callback_id)
    logger.info("Processing webhook event")

    if event_type == "payment_intent.succeeded":
        charge_id, card_brand, card_last4 = _extract_charge_details(intent)
        callback_result = json.dumps({
            "type": "payment_intent.succeeded",
            "charge_id": charge_id,
            "card_brand": card_brand,
            "card_last4": card_last4,
        })
        _send_callback_success(callback_id, callback_result)
        metrics.add_metric(name="WebhookSucceeded", unit=MetricUnit.Count, value=1)

    elif event_type == "payment_intent.payment_failed":
        error = intent.get("last_payment_error", {})
        decline_code = error.get("decline_code", "generic_decline")
        callback_result = json.dumps({
            "type": "payment_intent.payment_failed",
            "decline_code": decline_code,
            "error_message": error.get("message", "Payment failed"),
        })
        _send_callback_success(callback_id, callback_result)
        metrics.add_metric(name="WebhookPaymentFailed", unit=MetricUnit.Count, value=1)
        tracer.put_annotation("decline_code", decline_code)

    else:
        logger.info("Unhandled event type — skipping callback")

    return _response(200, {"received": True})


def _to_plain_dict(obj) -> dict:
    """Normalize a Stripe object (or dict) into a plain nested dict.

    Stripe objects behave inconsistently with ``.get()`` and item access
    depending on nesting, so we serialize to JSON and parse back to get a
    predictable plain ``dict`` for safe traversal.
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    try:
        return json.loads(str(obj))
    except (TypeError, ValueError):
        return {}


@tracer.capture_method
def _extract_charge_details(intent: dict) -> tuple:
    """Pull charge id and card brand/last4 from a succeeded PaymentIntent.

    Recent Stripe API versions removed the expanded ``charges`` list from the
    PaymentIntent object, exposing only ``latest_charge`` as an id. When the
    embedded charge data is absent, retrieve the charge so card details are
    still populated. Falls back to safe defaults if the lookup fails.
    """
    intent = _to_plain_dict(intent)
    charge_id = intent.get("latest_charge")

    # Older/expanded payloads may still embed the charge object directly.
    charge_list = (intent.get("charges") or {}).get("data") or []
    charge_data = _to_plain_dict(charge_list[0]) if charge_list else {}
    card = (charge_data.get("payment_method_details") or {}).get("card") or {}

    if not card and charge_id:
        try:
            charge = _to_plain_dict(stripe.Charge.retrieve(charge_id))
            card = (charge.get("payment_method_details") or {}).get("card") or {}
        except stripe.error.StripeError:
            logger.warning("Could not retrieve charge for card details",
                           charge_id=charge_id)

    return (charge_id or charge_data.get("id"),
            card.get("brand") or "unknown",
            card.get("last4") or "****")


@tracer.capture_method
def _send_callback_success(callback_id: str, result: str) -> None:
    lambda_client.send_durable_execution_callback_success(
        CallbackId=callback_id, Result=result.encode("utf-8"),
    )
    logger.info("Callback notified successfully")


def _response(status_code: int, body: dict) -> dict:
    return {"statusCode": status_code,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}
