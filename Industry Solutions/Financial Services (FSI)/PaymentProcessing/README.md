# Card payment processing with Lambda durable functions

This example demonstrates the callback pattern in Lambda durable functions using a card payment workflow with the Stripe test API, including 3D Secure authentication.

> **Disclaimer:** This is sample code for demonstration and educational purposes. It is not intended for production use as-is and has not undergone production hardening. Before deploying to production, complete your own security review and testing, and add the controls described in [Security and compliance](#security-and-compliance).

## The problem

Traditional Lambda functions have a 15-minute maximum runtime and you pay for every millisecond, even when idle. Payment processing often requires waiting for:

- 3D Secure customer authentication (seconds to minutes)
- Bank confirmations
- Webhook callbacks from payment providers

With a standard Lambda function, you either pay for compute while waiting, or build a complex state machine with [AWS Step Functions](https://aws.amazon.com/step-functions/), [Amazon SQS](https://aws.amazon.com/sqs/), and [Amazon DynamoDB](https://aws.amazon.com/dynamodb/) to manage the asynchronous flow.

## The solution

Lambda durable functions can **suspend mid-execution** at zero compute cost, wait for an external event through a **callback**, and **resume exactly where they left off**. This example builds a complete card payment workflow that:

1. Creates a real Stripe PaymentIntent
2. Suspends waiting for a webhook callback (up to 5 minutes)
3. Resumes when Stripe confirms success or failure
4. Times out gracefully if no response arrives

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         Your AWS account                             │
│                                                                      │
│  ┌──────────┐    ┌──────────────┐    ┌───────────────────────────┐   │
│  │ API      │    │ API function │    │ Durable function          │   │
│  │ Gateway  │───>│ (payment-api)│───>│ (payment-processor)       │   │
│  │          │    │              │    │                           │   │
│  │ POST /pay│    │ Invokes      │    │ 1. Validate payment       │   │
│  │ GET /pay/│    │ async        │    │ 2. Create PaymentIntent   │   │
│  │ GET /chk │    │              │    │ 3. Create callback (5min) │   │
│  │          │    │              │    │ 4. ██ SUSPEND ██          │   │
│  │          │    │              │    │    (zero compute cost)    │   │
│  │          │    │              │    │ 5. ── RESUME ──           │   │
│  │          │    │              │    │ 6. Return result          │   │
│  │          │    │              │    └───────────────────────────┘   │
│  │          │    │              │                 ▲                  │
│  │          │    └──────────────┘                 │                  │
│  │          │                              callback result           │
│  │          │    ┌──────────────────┐             │                  │
│  │ POST     │    │ Webhook function │─────────────┘                  │
│  │ /stripe/ │───>│ (webhook-handler)│                                │
│  │ webhook  │    │                  │  send_durable_execution_       │
│  └──────────┘    └──────────────────┘  callback_success()            │
│                          ▲                                           │
└──────────────────────────│───────────────────────────────────────────┘
                           │
                   Stripe fires webhook
                   (payment_intent.succeeded
                    or payment_intent.payment_failed)
                           │
                  ┌────────┴────────┐
                  │  Stripe servers │
                  └─────────────────┘
```

## Components

| File | Description |
|------|-------------|
| `payment_processor.py` | Durable function — creates the PaymentIntent, suspends on the callback, resumes on the webhook |
| `api_handler.py` | API function — starts payments, serves the checkout page, returns PaymentIntent status |
| `webhook_handler.py` | Receives Stripe webhook events and resumes the durable execution |
| `template.yaml` | AWS SAM template with all three functions, [Amazon API Gateway](https://aws.amazon.com/api-gateway/), and the [Amazon CloudWatch](https://aws.amazon.com/cloudwatch/) dashboard |

## Prerequisites

- AWS SAM CLI >= 1.157.1 (must support `DurableConfig`)
- A [Stripe account](https://dashboard.stripe.com/register) (free, test mode)
- Your Stripe test keys:
  - **Secret key** (`sk_test_...`) — Dashboard → Developers → API keys
  - **Publishable key** (`pk_test_...`) — same page
  - **Webhook signing secret** (`whsec_...`) — created after registering the webhook endpoint

## Deployment

### 1. Build and deploy

```bash
cd "Industry Solutions/Financial Services (FSI)/PaymentProcessing"

PIP_INDEX_URL=https://pypi.org/simple/ sam build

sam deploy --guided \
  --parameter-overrides \
    StripeSecretKey=sk_test_YOUR_KEY \
    StripePublishableKey=pk_test_YOUR_KEY \
    StripeWebhookSecret=whsec_placeholder
```

### 2. Register the Stripe webhook

Copy the `StripeWebhookUrl` from the stack outputs, then:

1. Go to [Stripe Dashboard → Developers → Webhooks](https://dashboard.stripe.com/test/webhooks)
2. Choose **Add endpoint**
3. Paste the webhook URL
4. Select events: `payment_intent.succeeded` and `payment_intent.payment_failed`
5. Create the endpoint and copy the **Signing secret** (`whsec_...`)

### 3. Redeploy with the real webhook secret

```bash
sam deploy --parameter-overrides \
  StripeSecretKey=sk_test_YOUR_KEY \
  StripePublishableKey=pk_test_YOUR_KEY \
  StripeWebhookSecret=whsec_YOUR_REAL_SECRET \
  --no-confirm-changeset
```

### 4. Publish the durable function version

`AutoPublishAlias: live` in the template publishes a new version and moves the
`live` alias on each deploy, so this is normally automatic. If you ever need to
move the alias manually, note the function name is scoped to the stack name
(`<stack-name>-payment-processor`, e.g. `payment-processing-payment-processor`):

```bash
FUNCTION=payment-processing-payment-processor

VERSION=$(aws lambda publish-version \
  --function-name $FUNCTION \
  --region us-west-2 --query 'Version' --output text)

aws lambda update-alias \
  --function-name $FUNCTION \
  --name live --function-version $VERSION \
  --region us-west-2
```

## Demo walkthrough

### Test 1: Instant success (Visa)

```bash
curl -X POST https://YOUR_API_ENDPOINT/dev/pay \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "cus_demo",
    "amount": 2500,
    "currency": "usd",
    "payment_method_id": "pm_card_visa",
    "description": "Demo payment"
  }'
```

This returns 202 immediately. Behind the scenes:
- The durable function creates a PaymentIntent, Stripe charges instantly, and the status becomes `succeeded`
- Stripe fires the `payment_intent.succeeded` webhook, and the webhook handler sends the callback
- The durable function resumes and returns success

### Test 2: Card decline

```bash
curl -X POST https://YOUR_API_ENDPOINT/dev/pay \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "cus_demo",
    "amount": 1500,
    "currency": "usd",
    "payment_method_id": "pm_card_chargeDeclined",
    "description": "Decline demo"
  }'
```

### Test 3: 3D Secure (checkout page)

Open the following in a browser:

```
https://YOUR_API_ENDPOINT/dev/checkout
```

Choose **3D Secure Required**, and then choose **Pay Now**. For details, see the 3D Secure walkthrough later in this document.

### Observability

Durable functions have a different operational surface than standard Lambda: callbacks can time out, whole executions can time out, and running workflows can be cancelled. Each is a distinct terminal state and each needs its own signal. This example layers four kinds of telemetry on top of the built-in metrics.

#### 1. Built-in durable execution metrics (`AWS/Lambda`)

Lambda automatically emits CloudWatch metrics for durable executions — no code required. The dashboard surfaces:

- **Execution state**: `DurableExecutionStarted`, `DurableExecutionSucceeded`, `DurableExecutionFailed`, `DurableExecutionTimedOut`, `DurableExecutionStopped` — the five terminal/lifecycle states, each answering a different operational question.
- **Capacity**: `ApproximateRunningDurableExecutions` and its utilization percentage against your account quota.
- **Duration**: `DurableExecutionDuration` measures **total wall-clock time including the callback wait**. A payment that computes in 2s but waits 30s for the webhook reports ~32s — distinct from the standard `Duration` metric, which only counts active compute.
- **Cost drivers**: `DurableExecutionOperations` and `DurableExecutionStorageWrittenBytes`.

See [Monitoring durable functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-monitoring.html) for the full list.

#### 2. Custom business metrics — the callback funnel (EMF)

Built-in metrics tell you *whether* an execution succeeded; custom metrics tell you *where in the business flow* it broke. Using Powertools Metrics (Embedded Metric Format) under the `DurablePayments` namespace, each stage emits a counter:

```
PaymentRequested → PaymentIntentCreated → WebhookReceived → WebhookSucceeded → PaymentSucceeded
```

Any drop-off pinpoints the problem:
- `PaymentIntentCreated` > `WebhookReceived` → Stripe isn't delivering webhooks.
- `WebhookReceived` > `WebhookSucceeded` → signature verification is failing.
- `PaymentIntentCreated` with no matching `PaymentSucceeded`/`PaymentFailed` → the callback timed out.

The full set of custom metrics emitted by this example:

| Metric | Emitted by | Meaning |
|---|---|---|
| `PaymentRequested` | payment-api | A payment request was accepted |
| `PaymentIntentCreated` | payment-processor | Stripe PaymentIntent created |
| `ThreeDSecureRequired` | payment-processor | Intent requires 3DS authentication |
| `PaymentSucceeded` / `PaymentFailed` | payment-processor | Terminal business outcome |
| `PaymentTimeout` | payment-processor | Callback never arrived within the timeout |
| `PaymentDeclinedAtCreate` | payment-processor | Hard decline at PaymentIntent creation |
| `ValidationFailure` | payment-processor | Request failed validation |
| `WebhookReceived` / `WebhookSucceeded` | stripe-webhook | Webhook delivery + successful callback |
| `WebhookSignatureFailure` | stripe-webhook | Signature verification failed |
| `WebhookPaymentFailed` | stripe-webhook | Webhook reported a failed payment |
| `PaymentCancelled` / `InvokeFailure` | payment-api | Execution stopped / async invoke failed |

#### 3. Alarms for callback-specific failure modes

These failure modes don't exist for standard Lambda. The template defines an alarm for each:

| Alarm | Metric | What it catches |
|---|---|---|
| `DurableExecutionFailureAlarm` | `DurableExecutionFailed` | Code errors, Stripe API failures, unhandled exceptions |
| `DurableExecutionTimedOutAlarm` | `DurableExecutionTimedOut` | Whole-execution timeout (exceeds `ExecutionTimeout`) |
| `PaymentTimeoutAlarm` | `PaymentTimeout` | Per-callback timeout: webhook misconfig, Stripe outage, network |
| `WebhookSignatureFailureAlarm` | `WebhookSignatureFailure` | Wrong webhook secret, replay attempts, endpoint misconfig |
| `WebhookErrorAlarm` | `Errors` (webhook fn) | Webhook function error spikes |

> **Two distinct timeouts.** `PaymentTimeout` is the *per-callback* bound (the function returns a clean `timeout` result). `DurableExecutionTimedOut` is the *outer* bound set by `DurableConfig.ExecutionTimeout`, which caps total wall-clock time. The execution timeout must be larger than the longest callback timeout, or the whole execution terminates before the callback timeout can fire gracefully — this example uses 600s execution vs a 300s payment callback.

#### 4. Structured logging with correlation keys

Using Powertools Logger, correlation keys are appended progressively as they become known, so every subsequent log line carries them. The durable handler appends `customer_id` then `payment_intent_id`; the webhook handler appends `event_type`, `payment_intent_id`, and `callback_id`. Because all three functions log the same `payment_intent_id`, you can reconstruct a single payment's full lifecycle — including the suspension gap — across all three log groups in CloudWatch Logs Insights:

```
fields @timestamp, service, message, customer_id, payment_intent_id, callback_id
| filter payment_intent_id = "pi_3TJafD04vzZc6RmP0RrCWhix"
| sort @timestamp asc
```

#### 5. AWS X-Ray tracing across the suspension boundary

`Tracing: Active` (Globals) and `TracingEnabled: true` (API) propagate traces from the initial request through the durable execution and the separate webhook path. [AWS X-Ray](https://aws.amazon.com/xray/) traces, combined with Powertools Tracer annotations (`payment_intent_id`, `callback_id`, `customer_id`, `event_type`, `decline_code`), let you filter the Service Map and traces by business context.

> **Durable-function caveat:** the main durable handler runs inside a `FacadeSegment` X-Ray context that does **not** support `put_annotation()`. This example wraps annotation calls in the handler with a safe helper (`_safe_annotate`) that swallows that limitation, while using standard `tracer.put_annotation()` inside `@durable_step` functions where the context supports mutation.

#### 6. Durable executions tab (Lambda console)
The **Durable executions** tab on the payment processor function shows each execution's step-by-step timeline — which steps succeeded, where it suspended on the `stripe-payment-result` callback, and whether the callback was received, timed out, or the execution was stopped. This is the fastest way to see *exactly* where a single execution is in its lifecycle without querying logs.

#### Unified dashboard

The CloudWatch dashboard (URL in stack outputs) combines all of the above into one view:

| Widget | Shows |
|---|---|
| Durable Execution State | started / succeeded / failed / timed-out / stopped |
| Payment Outcomes (EMF Custom Metrics) | succeeded / failed / timeout business outcomes |
| End-to-End Flow Metrics | the callback funnel (requested → intent → webhook → succeeded) |
| Running Executions & Quota Utilization | concurrency against account quota |
| Durable Execution Duration (ms) | wall-clock time including wait |
| Resource Usage (Cost Drivers) | operations and storage bytes |
| Error Breakdown (Debugging) | signature failures, validation failures, invoke failures |
| API & Webhook Invocations / Errors + Latency | standard Lambda metrics for the API and webhook functions |

#### Debugging a stuck webhook end-to-end

When `PaymentTimeoutAlarm` fires:
1. **Dashboard** — "End-to-End Flow Metrics" shows `PaymentIntentCreated` > `WebhookReceived`, so the webhook never arrived.
2. **Logs** — find the `payment_intent_id` of the timed-out payment (filter `message = "Payment timed out"`).
3. **Cross-reference** — search that `payment_intent_id` in the webhook handler log group. No results → Stripe never delivered it; `WebhookSignatureFailure` → wrong webhook secret.
4. **X-Ray** — filter by the `payment_intent_id` annotation; the missing webhook span confirms non-delivery.
5. **Durable executions tab** — shows `validate-payment` and `create-payment-intent` succeeded, with the `stripe-payment-result` callback in a timed-out state.

You reach root cause without adding debug statements or redeploying.

### Stripe Dashboard

- [Payments](https://dashboard.stripe.com/test/payments) — see PaymentIntents created
- [Developers → Webhooks](https://dashboard.stripe.com/test/webhooks) — see webhook deliveries
- [Developers → Events](https://dashboard.stripe.com/test/events) — see all events

### Lambda console

Open the durable function (`<stack-name>-payment-processor`, for example `payment-processing-payment-processor`), and then choose the **Durable executions** tab to see each execution's status (`RUNNING`, `SUCCEEDED`, `FAILED`, `TIMED_OUT`, `STOPPED`) with input and output.

## Stripe test payment methods

| Payment Method | Behavior |
|---|---|
| `pm_card_visa` | Succeeds instantly |
| `pm_card_mastercard` | Succeeds instantly |
| `pm_card_threeDSecure2Required` | Requires 3D Secure authentication |
| `pm_card_chargeDeclined` | Card declined |
| `pm_card_insufficient_funds` | Insufficient funds |

## Deep dive: 3D Secure payment flow

This scenario demonstrates the core value of durable functions — the function genuinely suspends for an indeterminate amount of time while a person completes authentication in their browser, and resumes only when Stripe confirms the result through a webhook.

### What is 3D Secure?

3D Secure (3DS) is an authentication protocol required by many banks for online card payments. When triggered, the customer sees a prompt from their bank asking them to verify the transaction (through an SMS code, biometrics, or a test button in Stripe's test mode). The payment cannot complete until the customer authenticates.

This creates a problem for server-side processing: **you don't know when (or if) the customer will complete authentication**. It might be 5 seconds or 5 minutes. A traditional Lambda function would stay running the entire time, paying for compute.

### Step-by-step flow

Here is what happens when you choose **Pay Now** with the 3D Secure test card:

```
┌─────────────────────────────────────────────────────────────────┐
│ STEP 1: Browser sends POST /pay                                 │
│                                                                  │
│  Browser ──POST /pay──> API Gateway ──> API function             │
│                                          │                       │
│                                   Invokes durable function       │
│                                   asynchronously (InvocationType │
│                                   = "Event")                     │
│                                          │                       │
│  Browser <──202 Accepted──               │                       │
│  (returns immediately)                   ▼                       │
│                                   Durable function starts        │
└─────────────────────────────────────────────────────────────────┘
                                           │
┌──────────────────────────────────────────────────────────────────┐
│ STEP 2: Durable function creates PaymentIntent                   │
│                                                                  │
│  Durable function                                                │
│    │                                                             │
│    ├─ context.step(validate_payment_request(...))                │
│    │   → Validates amount, currency, payment_method_id           │
│    │   → Checkpointed ✓                                          │
│    │                                                             │
│    ├─ context.create_callback("stripe-payment-result",           │
│    │     timeout=5 minutes)                                      │
│    │   → Creates a "mailbox" with callback_id                    │
│    │                                                             │
│    ├─ context.step(create_stripe_payment_intent(...))            │
│    │   → Calls Stripe API: PaymentIntent.create(confirm=True)    │
│    │   → Stripe returns status: "requires_action"                │
│    │   → client_secret included in response                      │
│    │   → callback_id stored in PaymentIntent metadata            │
│    │   → Checkpointed ✓                                          │
│    │                                                             │
│    └─ callback.result()                                          │
│        → ██████████████████████████████████████████              │
│        → ██  FUNCTION SUSPENDS HERE  ██████████████              │
│        → ██  No CPU. No cost. Frozen.  ██████████                │
│        → ██████████████████████████████████████████              │
└──────────────────────────────────────────────────────────────────┘
                                           │
                              Function is suspended...
                              Might be seconds or minutes.
                                           │
┌──────────────────────────────────────────────────────────────────┐
│ STEP 3: Browser fetches client_secret and shows 3DS prompt       │
│                                                                  │
│  Browser                                                         │
│    │                                                             │
│    ├─ Waits ~6 seconds for intent to be created                  │
│    │                                                             │
│    ├─ GET /pay/latest-intent                                     │
│    │   → API function calls stripe.PaymentIntent.list(limit=1)   │
│    │   → Returns { client_secret: "pi_xxx_secret_yyy",           │
│    │               status: "requires_action" }                   │
│    │                                                             │
│    └─ stripe.confirmCardPayment(client_secret)                   │
│        → Stripe.js (running in browser) opens an iframe          │
│        → Shows the 3D Secure authentication page                 │
│                                                                  │
│    ┌──────────────────────────────────────┐                      │
│    │                                      │                      │
│    │     3D Secure 2 Test Page            │                      │
│    │                                      │                      │
│    │  This is a test 3D Secure 2          │                      │
│    │  authentication for a transaction    │                      │
│    │  with Stripe.                        │                      │
│    │                                      │                      │
│    │  In live mode, customers will be     │                      │
│    │  asked to verify their identity      │                      │
│    │  with a push notification, a text    │                      │
│    │  message, or another method chosen   │                      │
│    │  by their bank.                      │                      │
│    │                                      │                      │
│    │     [ FAIL ]    [ COMPLETE ]         │                      │
│    │                                      │                      │
│    └──────────────────────────────────────┘                      │
│                                                                  │
│  Customer chooses COMPLETE (or FAIL)                             │
└──────────────────────────────────────────────────────────────────┘
                                           │
                              Customer chose COMPLETE
                                           │
┌─────────────────────────────────────────────────────────────────┐
│ STEP 4: Stripe confirms payment and fires webhook                │
│                                                                  │
│  Stripe.js ──> Stripe servers                                    │
│                    │                                             │
│                    ├─ "Customer authenticated successfully"      │
│                    ├─ Confirms the PaymentIntent                 │
│                    ├─ Status: requires_action → succeeded        │
│                    ├─ Charge created: ch_xxxxx                   │
│                    │                                             │
│                    └─ Fires webhook: payment_intent.succeeded    │
│                       POST https://your-api/dev/stripe/webhook   │
│                       Body: { type: "payment_intent.succeeded",  │
│                               data: { object: { id: "pi_xxx",    │
│                                 metadata: { callback_id: "..." } │
│                               }}}                                │
└──────────────────────────────────────────────────────────────────┘
                                           │
┌──────────────────────────────────────────────────────────────────┐
│ STEP 5: Webhook handler resumes the durable function             │
│                                                                  │
│  API Gateway ──> Webhook function                                │
│                    │                                             │
│                    ├─ Verifies Stripe signature (whsec_...)      │
│                    ├─ Extracts callback_id from metadata         │
│                    ├─ Extracts charge_id, card_brand, card_last4 │
│                    │                                             │
│                    └─ lambda_client                              │
│                         .send_durable_execution_callback_success(│
│                           CallbackId=callback_id,                │
│                           Result=json.dumps({                    │
│                             "type": "payment_intent.succeeded",  │
│                             "charge_id": "ch_xxx",               │
│                             "card_brand": "visa",                │
│                             "card_last4": "3220"                 │
│                           })                                     │
│                         )                                        │
│                                                                  │
│  This call resumes the suspended durable function.               │
└──────────────────────────────────────────────────────────────────┘
                                           │
┌──────────────────────────────────────────────────────────────────┐
│ STEP 6: Durable function resumes and returns result              │
│                                                                  │
│  Durable function                                                │
│    │                                                             │
│    │  (replays checkpointed steps — validate, create_intent)     │
│    │  (skips to callback.result() which now has data)            │
│    │                                                             │
│    ├─ result = callback.result()                                 │
│    │   → Returns: {"type": "payment_intent.succeeded", ...}      │
│    │                                                             │
│    └─ return {                                                   │
│         "payment_intent_id": "pi_xxx",                           │
│         "status": "succeeded",                                   │
│         "charge_id": "ch_xxx",                                   │
│         "amount": 2500,                                          │
│         "currency": "usd",                                       │
│         "card": "visa ****3220"                                  │
│       }                                                          │
│                                                                  │
│  Durable execution status: SUCCEEDED                             │
└──────────────────────────────────────────────────────────────────┘
```

### What the browser shows

The checkout page displays a live timeline during this flow:

```
9:09:52 AM — POST /pay — invoking durable function async
9:09:52 AM — Durable function started — creating PaymentIntent...
9:09:58 AM — Fetching PaymentIntent from Stripe...
9:09:59 AM — PaymentIntent: pi_3TI0zL... (status: requires_action)
9:09:59 AM — Launching 3D Secure modal with Stripe.js...
             ┌─────────────────────────────────┐
             │  3D Secure prompt appears here  │
             │  Customer chooses COMPLETE      │
             └─────────────────────────────────┘
9:10:05 AM — 3D Secure completed — payment confirmed by customer
9:10:05 AM — Stripe firing webhook to resume durable function...

  ┌─────────────────────────────────────────────┐
  │  ✅ Payment succeeded!                      │
  │  PaymentIntent: pi_3TI0zL04vzZc6RmP0JoRahxY │
  │  Status: succeeded                          │
  │  Amount: $25.00                             │
  │                                             │
  │  The durable function resumed.              │
  └─────────────────────────────────────────────┘
```

### Why this matters

In a traditional architecture, you need:
- An AWS Step Functions state machine to orchestrate the wait
- An Amazon SQS queue or Amazon DynamoDB table to track pending payments
- A separate function to handle the webhook and update state
- Another function to check state and return results
- Code to connect these components

With durable functions, this becomes **one function** with sequential code:

```python
@durable_execution
def handler(event, context: DurableContext):
    payment = context.step(validate(...))        # checkpointed
    callback = context.create_callback(timeout=5min)
    intent = context.step(create_intent(...))    # checkpointed
    result = callback.result()                   # SUSPENDS HERE
    # ... resumes when webhook fires
    return {"status": "succeeded", ...}
```

No state machines. No queues. No DynamoDB. Sequential code.

### Timeout scenario

If the customer never completes 3D Secure (closes the browser, walks away), the durable function stays suspended. After 5 minutes, the callback times out. The SDK surfaces this by raising `CallbackError` from `callback.result()` (it does **not** return `None` — an empty `None` result only happens when a success callback carries no payload), so the timeout is handled in an `except` block:

```python
try:
    result = callback.result()  # SUSPENDS HERE
except CallbackError:
    return {
        "status": "timeout",
        "message": "No confirmation within 5 minutes"
    }
```

The function resumes, returns the timeout result, and the execution completes cleanly. No orphaned processes, no stuck state.

> **Note:** The durable `ExecutionTimeout` (set in `template.yaml`) must be **larger** than the longest callback timeout. Otherwise the whole execution hits `TIMED_OUT` before the callback's own timeout can fire and be handled gracefully. This example uses a 600s execution timeout against a 300s callback timeout.

## Key takeaways

### Traditional compared to durable functions flow

```
Traditional                              Durable functions
─────────────────────────────────────    ─────────────────────────────────────
POST /pay                                POST /pay
  Function A                               API function
    → create PaymentIntent                   → invoke durable function async
    → save state to DynamoDB                 → return 202
    → return payment_id                    Durable function (auto)
                                             → validate (checkpointed)
                                             → create PaymentIntent (checkpointed)
                                             → create_callback(5min)
                                             → SUSPEND (zero cost, state auto-saved)

POST /webhook                            POST /webhook
  Function B                               Webhook function
    → read state from DynamoDB               → extract callback_id from metadata
    → process Stripe event                   → send_callback_success(callback_id, result)
    → update state in DynamoDB               → done (1 API call)
    → maybe trigger next step              Durable function (auto)
                                             → RESUMES at callback.result()
                                             → processes result
                                             → returns final status

GET /status                              GET /status
  Function C                               API function
    → read state from DynamoDB               → get_durable_execution(arn)
    → return current status                  → returns status + result

Infrastructure needed:                   Infrastructure needed:
  3 functions                              3 functions
  1 DynamoDB table                         0 DynamoDB tables
  State management code                    0 state management code
  Error handling for stale state           Automatic checkpointing
  TTL cleanup for old records              Automatic retention (5 days)
```

### Summary

- The function is **suspended** while waiting for Stripe, so you pay **zero compute cost** during the wait
- If the webhook never arrives, the **5-minute timeout** handles it gracefully
- All steps are **checkpointed**, so if the function crashes, it replays from the last checkpoint
- The example uses real Stripe test API calls, not simulated responses
- `AutoPublishAlias` pins durable executions to a specific function version for consistent replay
- One sequential function replaces what traditionally requires Step Functions, Amazon SQS, and DynamoDB

## Security and compliance

This sample uses simplified patterns to keep the focus on the durable functions callback flow. Before using it as the basis for a production system, address the following:

- **Payment card data (PCI-DSS):** Card payments fall under [PCI-DSS](https://aws.amazon.com/compliance/pci-dss-level-1-faqs/). This example never handles raw card numbers — it uses Stripe payment method IDs and tokens so card data stays within Stripe — which keeps your PCI scope minimal. Follow [Stripe's integration security guidance](https://stripe.com/docs/security/guide) and never log or store full card numbers. Compliance is a [shared responsibility](https://aws.amazon.com/compliance/shared-responsibility-model/) between you, AWS, and Stripe.
- **Secrets management:** This example passes Stripe keys as Lambda environment variables (encrypted at rest with an AWS managed key) sourced from CloudFormation `NoEcho` parameters. For production, store keys in [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/) or [AWS Systems Manager Parameter Store](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-parameter-store.html) and retrieve them at runtime, so secrets are never set as plaintext environment variables and can be rotated without redeploying.
- **API authorization:** The API Gateway endpoints are unauthenticated for demo simplicity. The `/pay`, `/pay/{proxy+}`, and `/checkout` routes should sit behind an authorizer (Amazon Cognito, IAM, or a Lambda authorizer) in production. The `/stripe/webhook` route is intentionally public — it verifies authenticity through Stripe signature verification instead.
- **Alarm notifications:** The CloudWatch alarms are wired to an Amazon SNS topic (`AlarmTopic`). Subscribe an email address or operations endpoint to that topic to receive notifications.

## Cleaning up

To avoid ongoing charges, delete the resources you created. Run the following command from the example directory:

```bash
sam delete --stack-name payment-processing --region us-west-2
```

Also delete the webhook endpoint in the Stripe Dashboard.

## Conclusion

In this example, you deployed a card payment workflow that uses the callback pattern in Lambda durable functions. The durable function creates a PaymentIntent, suspends at zero compute cost while waiting for a Stripe webhook, and resumes when the payment confirms, fails, or times out. You also saw how to observe the workflow across its suspension boundary with Amazon CloudWatch metrics, alarms, structured logging, and AWS X-Ray tracing.

For more information about durable functions, see [Lambda durable functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html). To explore more reference architectures, browse [Serverless Land](https://serverlessland.com/).
