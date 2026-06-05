# Copyright (c) 2025 Amazon Web Services, Inc. or its affiliates.
# All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Thin API Lambda behind API Gateway.
Enhanced with Powertools structured logging, X-Ray tracing, and EMF metrics.
"""
import json
import os
from urllib.parse import unquote

import boto3
import stripe
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit

logger = Logger(service="payment-api")
tracer = Tracer(service="payment-api")
metrics = Metrics(namespace="DurablePayments", service="payment-api")

lambda_client = boto3.client("lambda")
PROCESSOR_FUNCTION_ARN = os.environ["PROCESSOR_FUNCTION_ARN"]
STRIPE_PK = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict, context) -> dict:
    http_method = event.get("httpMethod", "")
    path = event.get("path", "")
    logger.info("Request received", http_method=http_method, path=path)

    if http_method == "POST" and path == "/pay":
        return _start_payment(event)
    if http_method == "POST" and path.startswith("/pay/cancel/"):
        # The execution ARN contains ':' and '/' which arrive URL-encoded
        # through API Gateway's {proxy+}; decode before calling the Lambda API.
        return _cancel_execution(unquote(path.split("/pay/cancel/", 1)[1]))
    if http_method == "GET" and path.startswith("/pay/latest-intent"):
        return _get_latest_intent(event)
    if http_method == "GET" and path.startswith("/pay/"):
        return _get_status(unquote(path.split("/pay/", 1)[1]))
    if http_method == "GET" and path == "/checkout":
        return _serve_checkout_with_event(event)
    return _response(404, {"error": "Not found"})


@tracer.capture_method
def _start_payment(event: dict) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Invalid JSON body"})

    required = ["customer_id", "amount", "payment_method_id"]
    missing = [f for f in required if f not in body]
    if missing:
        return _response(400, {"error": f"Missing fields: {missing}"})

    logger.info("Starting payment", customer_id=body["customer_id"],
                amount=body["amount"], payment_method_id=body["payment_method_id"])
    tracer.put_annotation("customer_id", body["customer_id"])
    metrics.add_metric(name="PaymentRequested", unit=MetricUnit.Count, value=1)

    try:
        response = lambda_client.invoke(
            FunctionName=PROCESSOR_FUNCTION_ARN,
            InvocationType="Event", Payload=json.dumps(body),
        )
        durable_arn = response.get("DurableExecutionArn")
        logger.info("Durable invoke success",
                    status_code=response.get("StatusCode"),
                    execution_arn=durable_arn)
    except Exception as exc:
        logger.exception("Invoke failed")
        metrics.add_metric(name="InvokeFailure", unit=MetricUnit.Count, value=1)
        return _response(500, {"error": f"Failed to start: {exc}"})

    return _response(202, {"message": "Payment processing started",
                           "execution_arn": durable_arn,
                           "payment_method_id": body["payment_method_id"],
                           "amount": body["amount"], "currency": body.get("currency", "usd")})


@tracer.capture_method
def _cancel_execution(execution_arn: str) -> dict:
    logger.info("Cancelling durable execution", execution_arn=execution_arn)
    tracer.put_annotation("execution_arn", execution_arn)
    try:
        lambda_client.stop_durable_execution(
            DurableExecutionArn=execution_arn,
            Error={
                "ErrorType": "CustomerCancelled",
                "ErrorMessage": "Customer cancelled from checkout page",
            },
        )
        metrics.add_metric(name="PaymentCancelled", unit=MetricUnit.Count, value=1)
        return _response(200, {"execution_arn": execution_arn, "status": "STOPPING"})
    except lambda_client.exceptions.ResourceNotFoundException:
        return _response(404, {"error": "Execution not found"})
    except Exception as exc:
        logger.exception("Cancel failed")
        return _response(500, {"error": str(exc)})


@tracer.capture_method
def _get_latest_intent(event: dict) -> dict:
    try:
        intents = stripe.PaymentIntent.list(limit=1)
        if intents.data:
            pi = intents.data[0]
            result = {"payment_intent_id": pi.id, "status": pi.status}
            if pi.status == "requires_action":
                result["client_secret"] = pi.client_secret
            return _response(200, result)
        return _response(404, {"error": "No intents found"})
    except Exception as exc:
        logger.exception("Latest intent error")
        return _response(500, {"error": str(exc)})


@tracer.capture_method
def _get_status(execution_arn: str) -> dict:
    logger.info("Checking execution status", execution_arn=execution_arn)
    try:
        result = lambda_client.get_durable_execution(DurableExecutionArn=execution_arn)
        response_body = {"execution_arn": result["DurableExecutionArn"], "status": result["Status"]}
        if result["Status"] == "SUCCEEDED":
            response_body["result"] = json.loads(result.get("Result", "{}"))
        elif result["Status"] in ("FAILED", "TIMED_OUT", "STOPPED"):
            response_body["error"] = result.get("Error", {})
        return _response(200, response_body)
    except lambda_client.exceptions.ResourceNotFoundException:
        return _response(404, {"error": "Execution not found"})
    except Exception as exc:
        logger.exception("Status check error")
        return _response(500, {"error": str(exc)})


def _serve_checkout_with_event(event: dict) -> dict:
    headers = event.get("headers", {})
    host = headers.get("Host", "")
    stage = event.get("requestContext", {}).get("stage", "dev")
    api_url = f"https://{host}/{stage}"
    html = CHECKOUT_HTML.replace("{{STRIPE_PK}}", STRIPE_PK).replace("{{API_URL}}", api_url)
    return {"statusCode": 200, "headers": {"Content-Type": "text/html",
            "Access-Control-Allow-Origin": "*"}, "body": html}


def _response(status_code: int, body: dict) -> dict:
    return {"statusCode": status_code,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(body)}


CHECKOUT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Payment Demo</title>
<script src="https://js.stripe.com/v3/"></script>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 480px;
         margin: 40px auto; padding: 0 20px; background: #f6f9fc; }
  .card { background: #fff; border-radius: 8px; padding: 24px;
          box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
  h2 { margin-top: 0; }
  label { display: block; margin: 12px 0 4px; font-size: 14px;
          font-weight: 500; color: #333; }
  select, input { width: 100%; padding: 8px 12px; border: 1px solid #ccc;
                  border-radius: 4px; font-size: 14px; box-sizing: border-box; }
  button { width: 100%; padding: 12px; margin-top: 16px; background: #5469d4;
           color: #fff; border: none; border-radius: 4px; font-size: 16px;
           cursor: pointer; }
  button:disabled { opacity: 0.6; cursor: not-allowed; }
  #status { margin-top: 16px; padding: 12px; border-radius: 4px;
            font-size: 14px; display: none; white-space: pre-line; }
  .success { background: #d4edda; color: #155724; }
  .error { background: #f8d7da; color: #721c24; }
  .pending { background: #fff3cd; color: #856404; }
  .info { background: #d1ecf1; color: #0c5460; }
  #timeline { margin-top: 16px; font-size: 13px; color: #555; }
  #timeline div { padding: 4px 0; border-left: 2px solid #5469d4;
                  padding-left: 12px; margin-left: 8px; }
</style>
</head>
<body>
<div class="card">
  <h2>Payment Demo</h2>
  <p>Lambda Durable Functions + Stripe</p>
  <label for="amount">Amount (cents)</label>
  <input id="amount" type="number" value="2500" min="50">
  <label for="pm">Test Card</label>
  <select id="pm">
    <option value="pm_card_visa">Visa (instant success)</option>
    <option value="pm_card_threeDSecure2Required" selected>3D Secure Required (real-world)</option>
    <option value="pm_card_chargeDeclined">Declined</option>
    <option value="pm_card_simulate_timeout">Simulate timeout (no webhook)</option>
  </select>
  <button id="payBtn" onclick="startPayment()">Pay Now</button>
  <button id="cancelBtn" onclick="cancelPayment()" style="display:none; background:#b14c4c;">Cancel Payment</button>
  <div id="status"></div>
  <div id="timeline"></div>
</div>
<script>
const stripe = Stripe("{{STRIPE_PK}}");
const API = "{{API_URL}}";
let currentExecutionArn = null;
function log(msg) {
  const t = document.getElementById("timeline");
  const d = document.createElement("div");
  d.textContent = new Date().toLocaleTimeString() + " — " + msg;
  t.appendChild(d);
}
function setStatus(cls, msg) {
  const s = document.getElementById("status");
  s.style.display = "block"; s.className = cls; s.textContent = msg;
}
function showCancel(show) {
  document.getElementById("cancelBtn").style.display = show ? "block" : "none";
}
async function cancelPayment() {
  if (!currentExecutionArn) return;
  log("POST /pay/cancel — stopping durable execution");
  try {
    await fetch(API + "/pay/cancel/" + encodeURIComponent(currentExecutionArn), {method: "POST"});
    setStatus("info", "Payment cancelled. Durable execution stopped.");
  } catch (err) { log("Cancel error: " + err.message); }
  showCancel(false);
  document.getElementById("payBtn").disabled = false;
}
async function startPayment() {
  const btn = document.getElementById("payBtn");
  document.getElementById("timeline").innerHTML = "";
  btn.disabled = true; showCancel(false); currentExecutionArn = null;
  const amount = parseInt(document.getElementById("amount").value);
  const pm = document.getElementById("pm").value;
  setStatus("pending", "Starting payment...");
  log("POST /pay — invoking durable Lambda async");
  try {
    const res = await fetch(API + "/pay", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({customer_id: "cus_demo", amount, currency: "usd",
                            payment_method_id: pm, description: "Demo payment"})
    });
    const data = await res.json();
    if (res.status !== 202) { setStatus("error", data.error || "Failed"); btn.disabled = false; return; }
    if (data.execution_arn) { currentExecutionArn = data.execution_arn; }
    if (pm === "pm_card_simulate_timeout") {
      log("Timeout simulation — durable function will suspend and hit ExecutionTimeout");
      setStatus("pending", "Simulating a stuck webhook. Wait for ExecutionTimeout to fire, or click Cancel to stop early.");
      showCancel(true);
      return;
    }
    log("Durable Lambda started — creating PaymentIntent...");
    setStatus("pending", "Durable Lambda creating Stripe PaymentIntent...");
    await new Promise(r => setTimeout(r, 6000));
    log("Fetching PaymentIntent from Stripe...");
    const piRes = await fetch(API + "/pay/latest-intent");
    if (!piRes.ok) { setStatus("error", "Could not retrieve PaymentIntent"); btn.disabled = false; return; }
    const piData = await piRes.json();
    log("PaymentIntent: " + piData.payment_intent_id + " (status: " + piData.status + ")");
    if (piData.status === "requires_action" && piData.client_secret) {
      setStatus("info", "3D Secure authentication required...");
      log("Launching 3D Secure modal via Stripe.js...");
      const result = await stripe.confirmCardPayment(piData.client_secret);
      if (result.error) { log("3D Secure failed: " + result.error.message); setStatus("error", "Authentication failed: " + result.error.message); btn.disabled = false; return; }
      log("3D Secure completed — payment confirmed");
      log("Stripe firing webhook to wake up durable Lambda...");
      setStatus("success", "Payment succeeded!\\nPaymentIntent: " + result.paymentIntent.id + "\\nAmount: $" + (amount / 100).toFixed(2) + "\\n\\nDurable Lambda resumed via webhook callback.");
    } else if (piData.status === "succeeded") {
      log("Payment succeeded instantly (no 3DS needed)");
      log("Stripe firing webhook to wake up durable Lambda...");
      setStatus("success", "Payment succeeded!\\nPaymentIntent: " + piData.payment_intent_id + "\\nAmount: $" + (amount / 100).toFixed(2) + "\\n\\nDurable Lambda resumed via webhook callback.");
    } else if (piData.status === "requires_payment_method") {
      log("Card was declined"); setStatus("error", "Payment declined\\nPaymentIntent: " + piData.payment_intent_id);
    } else {
      log("Unexpected status: " + piData.status); setStatus("pending", "Status: " + piData.status + "\\nWaiting for webhook...");
    }
  } catch (err) { setStatus("error", "Error: " + err.message); log("Error: " + err.message); }
  btn.disabled = false;
}
</script>
</body>
</html>"""
