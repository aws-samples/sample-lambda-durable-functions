# Lambda Durable Functions API Functional Specification


## Introduction

Lambda Durable Functions is a new Lambda feature that lets developers write multi-step applications that persist state as they progress.  Developers use existing and new Lambda APIs to create and invoke durable functions, and perform other control and data plane operations.  Lambda also provides APIs that the durable function SDK uses to interact with the durable function data plane for checkpointing and getting state for replay.

Developers can create a Durable Function in Lambda and use durable operations like “steps” and “callbacks” to write their durable function code.  Durable functions checkpoint the result of each operation so its result becomes durable even if the function fails after the checkpoint.  Each operation can run up to the Lambda function’s timeout (maximum 15 minutes), and the entire multi-operation function can execute for up to one year.  When a function needs to wait for an event such as a timeout or a response from an asynchronous callback, Durable functions suspends and then resumes when the event arrives, so that functions only pay for active compute time, not for waiting.  For example:

```
export const handler: DurableHandler<MyInput, string> 
    = async (event: MyInput, context: DurableContext) => {
  try {
    // Create and checkpoint a callbackId, and pass a new child context and
    // the callbackId to the supplied function.  
    //
    // Once the function is complete, it will wait for the callback result
    // by setting a timer and terminating the Lambda.
    //
    // When the Stripe API is complete, it will call a webhook which will
    // call the new Lambda SendDurableExecutionCallbackSuccess API to send
    // the result to the function.
    const stripeResponse = await context.waitForCallback(async (context, callbackId) => 
      {
        // Call a Stripe API and send the callbackId to it
        // (e.g. in PaymentIntent metadata), and then checkpoint the result.
        await context.step("call stripe", async () => {
          await CallStripe(event, callbackId); // async call
        });
      }
    );

    // The durable functions service will reinvoke the function in replay mode
    // when the callback completes or the timer fires, recalling 
    // previously-checkpointed results to avoid redoing previously-completed work.
    // When the function gets to the context.waitForCallback call again,
    // the callback result will be the result Stripe sent to the webhook
    // (or it will throw an exception if it timed out).
  } catch (error) {
    ctx.log.error('Error in workflow:', error);
    throw new NonRetryableError((error as Error).message);
  }
  
  return stripeResponse;
}
```

For the functional specification of how the customer will write durable function code, see [Lambda Durable Functions Language SDK Functional Specification](https://quip-amazon.com/Pat3AbBBXPvZ).

We will introduce durable functions so it can support as many other Lambda features as possible.  For example, we will not disrupt the `Invoke` path by requiring new parameters or making durable functions difficult or impossible to use with other Lambda features.  Customers should be able to invoke durable functions either sync or async including as ESM (Event Source Mapping) poller targets, and durable functions should support SnapStart or Provisioned Concurrency or Elevator (unsure if we can support Lite at this time).

## Terminology

* Durable Function: A Lambda function with durability enabled.  Durability causes each invocation to run the function as a multi-step workflow (backed by SWF), where each step can run for up to the invocation timeout of 15 minutes.
* Durable Execution: The potentially long-lived execution of durable function invocation.
* Durable Execution ARN: Each durable execution gets a unique ARN used to identify it when getting information about it (e.g. get, list) or performing actions on it (e.g. stop).
* Durable Execution Name: A unique name optionally provided on `Invoke` used to identify the execution.  Only one execution with the same name can be open at the same time.
* Client Token: a unique idempotency token optionally provided on `Invoke`.  Lambda will not start a new execution when `Invoke` is called with the same parameters including `ClientToken` within 15 minutes of the first use of the same `ClientToken`.
* Durable Execution History: A record of what steps have completed in a durable execution.
* Durable Execution Timeout: The timeout for a durable execution, up to one year.
* Callback: A durable promise/future-like construct that allows a durable execution to wait for external work to be completed asynchronously.
* Step: A code block that produces a result that gets persisted for durability.
* Replay: The internal algorithm durable functions use to recover from failure and then continue from the last completed step.  After a failure, replay runs the durable function from the beginning, and avoids rerunning already-completed steps by fetching previously-persisted step results.
* Retention Period: The period of time after a durable execution completes to retain the durable execution history.

## Lambda API Changes - Creating and Invoking

### CreateFunction

The developer can create a new Durable Function using the existing Lambda `CreateFunction` API.  We will add a new configuration request parameter called `DurableConfig` which the developer will include to create a durable function.  The response will contain a new `DurableConfig` object that specifies durable function configuration.

Durable functions will support two timeouts: the total time a durable execution can run (up to one year), and the amount of time a single invocation can run (i.e. a single replay, up to 15 minutes).  The existing `Timeout` request parameter will retain the same meaning, i.e. the maximum time a single invocation can run (up to 15 minutes), with a default of 15 minutes or 900 seconds for durable functions.  An `ExecutionTimeout` field in the `DurableConfig` object specifies the total time the durable execution can run (up to one 366-day year, or 31,622,400 seconds).  `ExecutionTimeout` is required for `CreateFunction` but is optional for `UpdateFunctionConfiguration`.  See [Durable Function Timeouts](https://quip-amazon.com/7IqQAbX36Jc1) for the rationale.

Durable executions generate a history that summarizes all the steps the function has performed.  Lambda will send log messages containing history information to CloudWatch Logs, allowing developers to view how their steps progressed over time.  However, since durable executions are potentially long-lived (up to one year), searching CloudWatch Logs will not always be convenient, so Lambda will also make the execution history available through the new `GetDurableExecutionHistory` API.  The execution history will be available through this API while the durable function is still running, and for a period of time after it completes, called the “retention period”.  An optional `RetentionPeriodInDays` field in the `DurableConfig` object specifies the retention period in days for the function (from one to 90 days) with a default of 30 days.

Durable executions run multiple invocations over their lifetime.  Normally, all invocations of a single execution should invoke the same version of the function, otherwise an execution may encounter an error if it starts running a new backward-incompatible version part way through the execution.  To protect against this, we will encourage customers to publish and invoke function versions so Lambda can remember the version on the initial invoke and reinvoke the same version throughout the execution’s lifetime.  To allow customers to invoke using the unqualified function ARN, Lambda will, by default, remember the latest published version number on the initial invoke.

However, developers may wish to invoke `$LATEST` without publishing versions when testing in their test account.  Some customers also may choose to do this in production despite the risks.  To support this use case, an optional `AllowInvokeLatest` field in the `DurableConfig` object specifies whether to allow invoking `$LATEST` or not.  If `false` (the default), `Invoke` will invoke the latest published version when given the unqualified ARN, and will not allow invoking `$LATEST` explicitly.  If `true`, `Invoke` will invoke `$LATEST` when given the unqualified ARN, and will allow invoking `$LATEST` explicitly.  (Note, instead of an `AllowInvokeLatest` field, we could add an `UnqualifiedInvoke: 'LATEST | LATEST_PUBLISHED'` field.  However, this may be confusing if it also allows or prohibits the explicit use of `$LATEST`.)

#### Request Syntax

```
{
  ...
  "DurableConfig": {
    "ExecutionTimeout": number,
    "RetentionPeriodInDays": number,
    "AllowInvokeLatest": boolean,
    "DeletionMode": "FORCE | SAFE (default: SAFE)"
  },
  ...
}
```

**DurableConfig**
A durable function configuration that must be specified to create a durable function.  It provides the durable execution timeout and the retention period.
Type: A DurableConfig object
Required: No.  If provided, the `ExecutionTimeout` field must be provided for `CreateFunction`

#### **DurableConfig Contents**

**ExecutionTimeout**
The amount of time (in seconds) that Lambda allows a durable function to run before stopping it.  The maximum is one 366-day year or 31,622,400 seconds. 
Type: Integer
Required: Yes

**RetentionPeriodInDays**
The number of days after a durable execution is closed that Lambda retains its history, from one to 90 days.  The default is 30 days.
Type: Integer
Required: No

**AllowInvokeLatest**
Whether to allow invoking `$LATEST` or not.  If `false`, `Invoke` will invoke the latest published version if given the unqualified ARN, and will not allow invoking `$LATEST` explicitly.  If `true`, `Invoke` will invoke `$LATEST` if given the unqualified ARN, and will allow invoking `$LATEST` explicitly.  The default is `false`.
Type: Boolean
Required: No

**DeletionMode**
The desired deletion behavior for the durable function/version. If FORCE, the function/version will forcefully be deleted with all its open executions. If SAFE, the DeleteFunction API call for a specific version will return ResourceConflictException if the version being deleted has open executions. In case of deleting the whole function, DeletionMode from the latest published version will be used and it returns ResourceConflictException if any version of the function has open executions if the latest published version is using SAFE mode. When there is no published version, a function with open executions can be deleted.
Type: Enum (FORCE or SAFE)
Required: No. Default = SAFE.

#### Response Syntax

```
{
  ...
  "DurableConfig": DurableConfig,
  ...
}
```

**DurableConfig**
Type: A [DurableConfig](https://quip-amazon.com/GYDNAvHRrC4N/Lambda-Durable-Functions-and-DAR-Functional-Specification#temp:C:LNXbbdf4c3adb7242dc9fdb0ebd1) object
Required: No

#### Errors

**InvalidParameterValueException**
The `DurableConfig` `ExecutionTimeout` < 1 second or > 31,622,400 seconds, or the `RetentionPeriodInDays` < 1 day or > 90 days.
HTTP Status Code: 400.

### Invoke

We will not disrupt the `Invoke` path by requiring new parameters or making durable functions difficult or impossible to use with other Lambda features.  We aim to support as many other Lambda features as possible with durable functions.

The developer can invoke a durable function asynchronously using the `Invoke` API by choosing the `Event` option for the `InvocationType` request parameter, or synchronously using the `RequestResponse` option.  When invoking a durable function, the `Invoke` API starts a new “durable execution” and returns a new response header element called `DurableExecutionArn` containing its ARN.  Since durable executions can be long-lived (up to one year), Lambda will provide APIs to list, get, and manage them, and these operations will use the `DurableExecutionArn` to identify the durable execution.

When invoking a durable function synchronously, its `ExecutionTimeout` must be no more than 15 minutes (900 seconds).  If it is more than 15 minutes, `Invoke` will return a 400 (`InvalidParameterValueException`).  This means a synchronous invoke of a durable function will always return or timeout within 15 minutes.

When a durable execution starts, its execution timeout and retention period are set to the function’s `DurableConfig` `ExecutionTimeout` or `RetentionPeriodInDays` values as they are at start time.  If these values are changed after the execution starts, the execution will continue to run with the original values.  In other words, updates to these values only take effect for executions started after the change is made.

When invoking a durable function synchronously, the invocation `Payload` is limited to 6MB.

Lambda will identify the new durable execution with an ARN with this format: `arn:{Partition}:lambda:{Region}:{Account}:durable-execution:{FunctionName}:{DurableExecutionName}:{InvocationId}`.  The `InvocationId` is a unique ID generated by Lambda.  Since an execution’s `DurableExecutionName` can be reused after the execution completes, different executions with the same name are distinguished by their `InvocationId`.

#### Idempotency

The developer can provide an optional `DurableExecutionName` for the durable execution,  The `DurableExecutionName` must be unique for an AWS account, Region, and durable function. If the developer doesn’t provide either parameter, Lambda sets the `DurableExecutionName` to a random name.  Only one execution with a given `DurableExecutionName` can be open at a time.  The `DurableExecutionName` will become part of the execution ARN.  An execution name can only be reused after completion once its retention period has expired. Invokes with the same name for a completed execution will return the same result as the completed execution if the Payload is the same, or a DurableExecutionAlreadyExists error if the Payload is different.


#### Versions - Invoking with Qualified and Unqualified ARNs

Durable functions cannot support the proposed `$LATEST.PUBLISHED` behaviour described in [Unqualified ARN - Lambda’s Default Alias (v2)](https://quip-amazon.com/jY5kAfvfa0FR) because `$LATEST.PUBLISHED` will not keep old versions around.  Durable executions need to be pinned to the same version for its execution lifetime which can last up to one year.  Instead, customers will be able to publish and invoke versions to guarantee consistent behaviour in production.  However, we will also support invoking `$LATEST` for testing purposes based on the `DurableConfig`’s `AllowInvokeLatest` option.  See [Durable Functions - Versioning](https://quip-amazon.com/Xl6QA2464hBT) for rationale (to be updated).

When invoking a durable function in production, the customer can provide a version-qualified ARN, an alias-qualified ARN, or an unqualified ARN.  If the customer provides an alias-qualified ARN, Lambda will resolve this to the version it points to, and if the customer provides an unqualified ARN, by default Lambda will resolve this to the latest published version.  Internally, the durable functions service will persist this version and invoke the function using a version-qualified ARN with the persisted version for every replay.

When invoking a durable function for testing, developers can set the `DurableConfig`’s `AllowInvokeLatest` option to `true` to allow invoking with `$LATEST` so developers don’t have to publish new versions every time they want to test.  They can then invoke with the unqualified ARN or by explicitly qualifying with `$LATEST`.

Despite the risk of inconsistent behaviour, some customers may decide to invoke `$LATEST` in production without having to publish versions, in which case they can set the `DurableConfig`’s `AllowInvokeLatest` option to `true`.  In this case it is their responsibility to make sure every code update is backward compatible to previous revisions, otherwise they risk making a change that breaks in-progress executions.

#### URI Request Parameters

**ClientToken**
An optional identifier that you provide to ensure the idempotency of the request. It must be unique and is case sensitive. Up to 64 characters are allowed. The valid characters are characters in the range of 33-126, inclusive.

Must not be provided if the function is not a durable function.

If provided, it will be associated with the durable execution when it starts.  If an execution is already associated with the token, it has been less than 15 minutes since it started, and the request parameters are the same as in the original `Invoke` request, then this request will return the same result as the original.  If the parameters are different, the result will be a `ConflictException`.

Type: String
Length Constraints: Minimum length of 1. Maximum length of 64.
Required: No

**DurableExecutionName**
Optional name of the durable execution.  This name must be unique for your AWS account, Region, and durable function.  If a `ClientToken` is provided but not valid, or if no `ClientToken` is provided, then Lambda will return a `DurableExecutionAlreadyStartedException` if an execution with the same name is still running.  Otherwise it will start a new execution.

Must not be provided if the function is not a durable function.

If you don't provide a name for the durable execution, Lambda automatically sets the name to the `ClientToken` if provided, or to a universally unique identifier (UUID) otherwise.

Type: String
Length Constraints: Minimum length of 1. Maximum length of 64.
Required: No

~~**TestWithLatestUnpublished**~~
~~Optional parameter to control behaviour when invoking with an unqualified ARN.  If `true`, invoking with an unqualified ARN invokes `$LATEST` even if this is not a published version.  If `false`, invoking with an unqualified ARN invokes the latest published version.~~
~~Type: Boolean~~
~~Required: No~~

#### Response Header Elements

**DurableExecutionArn**
The durable function execution’s Amazon Resource Name (ARN).
Type: String
Length Constraints: Minimum length of 1. Maximum length of 279.
Required: Yes

Pattern: `arn:(aws[a-zA-Z-]*)?:lambda:[a-z]{2}(-gov)?-[a-z]+-\d{1}:\d{12}:durable-execution:[a-zA-Z0-9-_\.]+:[a-zA-Z0-9-_\.]+:[a-zA-Z0-9-_\.]+`

Example: `arn:aws:lambda:us-east-1:123456789012:durable-execution:myDurableFunction:myDurableExecutionName:ce67da72-3701-4f83-9174-f4189d27b0a5`

#### Errors

**ConflictException**
The `Invoke` request could not be processed due to conflicts. The provided `ClientToken` is associated with a different `Invoke` request, it has been less than 15 minutes since the execution started, and the parameters in this request are different than in the original request.  The `DurableExecutionArn` is the existing ARN which is already associated with the `ClientToken`.

To fix this issue:

* Run `Invoke` with a unique `ClientToken`.
* Run `Invoke` with the same `ClientToken` and the original set of parameters

HTTP Status Code: 400

**InvalidParameterValueException**
A `DurableExecutionName` was provided for a non-durable function, or the request specified `RequestResponse` for a durable function that has an `ExecutionTimeout` greater than 15 minutes, or a function ARN qualified with `$LATEST` is provided.

HTTP Status Code: 400

**ResourceNotFoundException**
The `Invoke` request could not be processed because an unqualified function ARN was provided but there are no published versions.

**DurableExecutionAlreadyStartedException**
The `Invoke` request could not be processed because an execution with the same name is still running and either a `ClientToken` was provided but not valid or no `ClientToken` was provided.

### InvokeAsync

The developer cannot use the `InvokeAsync` API to invoke a durable function because `InvokeAsync` is deprecated.

#### Errors

**ValidationError**
The `InvokeAsync` request is not supported for durable functions.
HTTP Status Code: 400

## Lambda API Changes - Callback Support

Callbacks require a way for other parts of an application to provide a successful or failure response for the callback.  From the previous example, customer code will look like this, and a call to `SendDurableExecutionCallbackSuccess` or `SendDurableExecutionCallbackFailure` will complete the `await ctx.waitForCallback` call.

```
const stripeResponse = await context.waitForCallback((context, callbackId) => {
  await context.step("call stripe", async () => {
    return await CallStripe(event, callbackId); // async call
  });
});
```

### `SendDurableExecutionCallbackFailure`

The developer can send a callback failure using the `SendDurableExecutionCallbackFailure` API.  Calling `SendDurableExecutionCallbackFailure` provides an error object describing why the callback failed.  This causes the callback to fail with the provided error object as the result.

#### Request Syntax

```
POST /2025-09-31/durable-execution-callbacks/CallbackId/fail HTTP/1.1
Content-Type: application/json

{
  "ErrorType": string,
  "ErrorMessage": string,
  "ErrorData": string,
  "StackTrace": [ string ]
}
```

**CallbackId**
The ID that represents this callback. Callback Ids are generated by the `DurableContext` when a durable function calls `ctx.waitForCallback`.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 2048.
Required: Yes

**ErrorType**
Customer-defined error type. Will compose part of the exception thrown by `ctx.waitForCallback`
Type: String
Length Constraints: Combined size of Type/Message/Data/StackTrace array must be < 256KB
Required: No

**ErrorMessage**
Customer-defined human-readable error message. Will compose part of the exception thrown by `ctx.waitForCallback`
Type: String
Length Constraints: Combined size of Type/Message/Data/StackTrace array must be < 256KB
Required: No

**ErrorData**
Customer-defined machine-redable error data. Will compose part of the exception thrown by `ctx.waitForCallback`
Type: String
Length Constraints: Combined size of Type/Message/Data/StackTrace array must be < 256KB
Required: No

**StackTrace**
Stack trace information. Will compose part of the exception thrown by `ctx.waitForCallback`
Type: List of Strings
Length Constraints: Combined size of Type/Message/Data/StackTrace array must be < 256KB
Required: No

#### Response Syntax

If the action is successful, the service sends back an HTTP 200 response with an empty HTTP body.

#### Errors

**InvalidParameterValueException**
One of the parameters in the request is not valid, for example the Payload is malformed.
HTTP Status Code: 400

**InvalidTokenException**
The provided callback ID token is not valid.
HTTP Status Code: 400

**KmsAccessDeniedException**
Either your AWS KMS key policy or API caller does not have the required permissions.
HTTP Status Code: 400

**KmsInvalidStateException**
The AWS KMS key is not in valid state, for example: Disabled or Deleted.
HTTP Status Code: 400

**KmsThrottlingException**
Received when AWS KMS returns `ThrottlingException` for a AWS KMS call that Lambda makes on behalf of the caller.
HTTP Status Code: 400

**CallbackTimeoutException**
The callback ID token has either expired or the callback associated with the token has already been closed.
HTTP Status Code: 400

**RequestTooLargeException**
The request payload exceeded the `Invoke` request body JSON input quota. For more information, see [Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html).
HTTP Status Code: 413

**ResourceNotFoundException**
The callback does not exist.
HTTP Status Code: 400

### `SendDurableExecutionCallbackSuccess`

The developer can send a successful callback result using the `SendDurableExecutionCallbackSuccess` API.  Calling `SendDurableExecutionCallbackSuccess` provides the output payload for the callback.  This causes the callback to succeed with the provided output payload as the result.

#### Request Syntax

```
POST /2025-09-31/durable-execution-callbacks/CallbackId/succeed HTTP/1.1
Content-Type: application/json

Payload
```

**CallbackId**
The ID that represents this callback. Callback Ids are generated by the `DurableContext` when a durable function calls `ctx.waitForCallback`.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 2048.
Required: Yes

**Payload**
The JSON response which will become the result of the successful callback.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 256KB.
Required: Yes

#### Response Syntax

If the action is successful, the service sends back an HTTP 200 response with an empty HTTP body.

#### Errors

**InvalidParameterValueException**
One of the parameters in the request is not valid, for example the Payload is malformed.
HTTP Status Code: 400

**InvalidTokenException**
The provided callback ID token is not valid.
HTTP Status Code: 400

**KmsAccessDeniedException**
Either your AWS KMS key policy or API caller does not have the required permissions.
HTTP Status Code: 400

**KmsInvalidStateException**
The AWS KMS key is not in valid state, for example: Disabled or Deleted.
HTTP Status Code: 400

**KmsThrottlingException**
Received when AWS KMS returns `ThrottlingException` for a AWS KMS call that Lambda makes on behalf of the caller.
HTTP Status Code: 400

**CallbackTimeoutException**
The callback ID token has either expired or the callback associated with the token has already been closed.
HTTP Status Code: 400

**RequestTooLargeException**
The request payload exceeded the `Invoke` request body JSON input quota. For more information, see [Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html).
HTTP Status Code: 413

**ResourceNotFoundException**
The callback does not exist.
HTTP Status Code: 400

### `SendDurableExecutionCallbackHeartbeat`

The developer can send a callback heartbeat using the `SendDurableExecutionCallbackHeartbeat` API.  Calling `SendDurableExecutionCallbackHeartbeat` causes the callback to reset its heartbeat timeout to let the durable execution know that it is still running.

#### Request Syntax

```
POST /2025-09-31/durable-execution-callbacks/CallbackId/heartbeat HTTP/1.1
```

**CallbackId**
The ID that represents this callback. Callback Ids are generated by the `DurableContext` when a durable function calls `ctx.waitForCallback`.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 2048.
Required: Yes

#### Response Syntax

If the action is successful, the service sends back an HTTP 200 response with an empty HTTP body.

#### Errors

**InvalidTokenException**
The provided callback ID token is not valid.
HTTP Status Code: 400

**CallbackTimeoutException**
The callback ID token has either expired or the callback associated with the token has already been closed.
HTTP Status Code: 400

**ResourceNotFoundException**
The callback does not exist.
HTTP Status Code: 400

## Lambda API Changes - Runtime Support

The Durable Functions programming SDK will use the following APIs to support checkpointing and replay.  Custom runtime developers can also use these APIs to implement their own support.

These APIs will be **Public**, in the `lambda` namespace. This is in order to simplify the support for Lambda Elevator and facilitate support for Lambda functions-in-VPC without networking surprises. Customers inside a VPC will need to have a VPC-Endpoint (PrivateLink) set in their account that will have network access to Lambda endpoint (or a NAT gateway with internet access). This is also a requirement for Elevator as data cannot leave their VPC without consent.

### Checkpoint Tokens

The main identifier the state management APIs accept is a `checkpointToken`. The `checkpointToken` is used for several things:

* As a routing token, to route `CheckpointDurableExecution` and `GetDurableExecutionState` calls to the DAR worker host that is performing the synchronous `Invoke` (i.e. a way to get the calls to the right location)
* As a way to ensure that a checkpoint with a given token can only be done once (to avoid state data being overwritten by stale API calls). Each call to `CheckpointDurableExecution` consumes the token and returns a new one. `GetDurableExecutionState` does not consume the token, but it must use the latest valid one.

The token contains:

* All of the information in the `durableExecution` ARN (partition, region, account, function name, durable execution name, durable execution run id) - This info can be used to facilitate IAM authorization on API calls as well as preventing accidental cross-region/cross-account token usage, etc.
* Routing information - Used to route requests to correct location that is managing the state
    * The IP address of the DAR worker that is invoking the lambda - Used to route request to correct host
    * A unique id for the specific lambda invocation- Used to route request to correct invoke handler in the host
* A unique token specific value. This value is used to ensure that a token is only used once in a given lambda invocation. This is also stored in memory on the DAR worker and validated on each call / consumed+updated on each `CheckpointDurableExecution` call.

The token is made opaque/unmodifiable by customers by encrypting the value.

### The `EXECUTION` operation

There will always be a single `EXECUTION` operation in the state. It is not possible to create new `EXECUTION` operations.

### `CheckpointDurableExecution`

Persists completed `ctx.step` data / schedules async actions

It is possible to checkpoint completion of the execution via `CheckpointDurableExecution` (using a `SUCCEED` or `FAIL` update) rather than returning SUCCEEDED/FAILED status in the lambda invocation output. Once this is done, no further checkpoints can be done.

A `CheckpointDurableExecution` call can still be made with an empty `updates` list. In this case, the response will still contain any new changes to state since the last time the checkpoint API was called.

#### Request Syntax

```
`POST ``/``2025``-``09``-``31``/``durable``-``execution``-``state``/``CheckpointToken``/``checkpoint HTTP``/``1.1`
` ``Content``-``type``:`` application``/``json`

{
  "ClientToken": string, // idempotency token
  "Updates": [
    {
      // Unique id of the entity to take action on
      "Id": string,
      // Id of the parent operation (if there is one)
      "ParentId": string,
      // Customer provided name - useful for metrics/logs/etc - doesn't have to
      // be unique
      "Name": string,
      // The types of entities - these map to the things in the SDK durable context
      "Type": "EXECUTION | CONTEXT | STEP | WAIT | CALLBACK | INVOKE",
      "SubType": string,
      // Take the specified action on the entity. Some actions are only relevant
      // for some entities
      //
      // START - STEP is starting (at-most-once semantics), 
      //          TIMER/CALLBACK/INVOKE is being created
      // CANCEL - WAIT / INVOKE should be canceled / stopped
      // SUCCEED - EXECUTION / STEP / CONTEXT has completed successfully
      // FAIL - EXECUTION / STEP / CONTEXT has failed and should not be retried
      // RETRY - STEP should be error-retried (increment retry attempt count)
      // POLL - STEP should be poll-retried (clear retry attempt count, increment
      //        poll attempt count)
      "Action": "START | SUCCEED | FAIL | RETRY | POLL | CANCEL",
      // Used for successful actions
      "Payload": string,
      // Used for failure actions
      "Error": {
        "ErrorType": string,
        "ErrorMessage": string,
        "ErrorData": string,
        "StackTrace": [ string ]
      },
      // For STEP RETRY/POLL actions
      "StepOptions": {
        // How long to wait to retry
        "NextAttemptDelaySeconds": number
      },
      // For WAIT CREATE action
      "WaitOptions": {
        // How long to wait
        "WaitSeconds": number
      },
      // For CONTEXT SUCCEED/FAIL actions
      "ContextOptions: {
        // Whether the state data of children of the completed context should be included
        // in the invoke payload / GetDurableExecutionState
        "ReplayChildren": boolean
      },      
      // For CALLBACK
      "CallbackOptions": {
        // 0 or not provided == unlimited
        "TimeoutSeconds": number,
        // 0 or not provided == unlimited
        "HeartbeatTimeoutSecond": number
      }
      // For INVOKE CREATE action
      "InvokeOptions": {
        "FunctionName": string,
        // Will be generated if not provided
        "DurableExecutionName": string
      },
      ...
    ]
}
```

#### Response

```
{
  // The new token to use on the next state management API call.
  "CheckpointToken": string,
  // This is a GetDurableExecutionState response object. It contains any updates
  // to state that occurred since the last checkpoint (e.g. due to async work
  // completing - a timer firing, or a callback completing, etc). If not populated,
  // no async work has completed. If a nextToken is present, it means that there 
  // is more state to retrieve and GetDurableExecutionState must be called to 
  // paginate.
  "NewExecutionState": GetDurableExecutionStateOutput
}
```

#### Errors

* All the [Common Errors](https://docs.aws.amazon.com/lambda/latest/api/CommonErrors.html) (includes auth errors, etc)
* `InvalidParameterValueException` - Some part of the request object failed input validation (HTTP 400)
* `InvalidCheckpointTokenException`  - The `checkpointToken` is not valid or has already been used (HTTP 400)
* `TooManyRequestsException` - Request was throttled (HTTP 429)
* KMS errors?

### `GetDurableExecutionState`

Retrieves execution state required for the replay process. The results are ordered by the start sequence number, in ascending order. If an operation is complete and has children, information about the child operations will not be returned in the API (as that state does not need to be replayed)

NOTE: marker/nextMarker and maxItems are used here for [consistency with Lambda APIs](https://w.amazon.com/bin/view/Lambda/Dev/Guidance/APIDesign/Standards#HA.Consistency)

#### Request Syntax

```
GET /2025-09-31/durable-execution-state/CheckpointToken/getState&Marker=Marker&MaxItems=MaxItems HTTP/1.1

{
  // Specified from a previous GetDurableExecutionState call
  "Marker": string,
  "MaxItems": number
}
```

#### Response

```
{
  "Operations": [
    {
      "Id": string,
      "ParentId": string,
      "Name": string,
      "Type": "EXECUTION | CONTEXT | STEP | WAIT | CALLBACK | INVOKE",
      "SubType": string,
      "StartTimestamp": timestamp,
      "EndTimestamp": timestamp,
      // The valid values depend on the type:
      //
      // STARTED - STEP has been started (at-most-once) but not completed yet, WAIT / CALLBACK / INVOKE have started are not complete yet
      // PENDING - STEP is waiting for a retry (either from error or poll)
      // READY - STEP is ready for a retry (either from error or poll)
      // SUCCEEDED - STEP / CONTEXT succeded, WAIT completed, CALLBACK succeeded, INVOKE succeeded
      // FAILED - STEP / CONTEXT failed terminally, CALLBACK failed, INVOKE failed
      // CANCELED - WAIT / CALLBACK canceled
      // TIMED_OUT - CALLBACK / INVOKE timed out
      // STOPPED - INVOKE stopped
      "Status": "STARTED | PENDING | READY | SUCCEEDED | FAILED | CANCELED | TIMED_OUT | STOPPED",
      "ExecutionDetails": {
        "InputPayload": string // The original invoke payload for this execution
      },
      "ContextDetails": {
        // Whether child operations of this completed context will be available for replay
        // Only populated if the CONTEXT is SUCCEEDED/FAILED
        "ReplayChildren": boolean,
        // Only one of these is populated      
        "Result": string,
        "Error": {
          "ErrorType": string,
          "ErrorMessage": string,
          "ErrorData": string,
          "StackTrace": [ string ]
        }
      },
      "StepDetails": {
        "Attempt": number,
        // When the next attempt should happen. (populated only when step is PENDING)
        "NextAttemptTimestamp": timetamp,        
        // Only one of these is populated
        "Result": string,
        "Error": {
          "ErrorType": string,
          "ErrorMessage": string,
          "ErrorData": string,
          "StackTrace": [ string ]
        }
      },
      "WaitDetails": {
        // When the wait will complete
        "ScheduledTimestamp": timestamp
      },
      "CallbackDetails": {
        "CallbackId": string,
        // Only one of these is populated
        "Result": string,
        "Error": {
          "ErrorType": string,
          "ErrorMessage": string,
          "ErrorData": string,
          "StackTrace": [ string ]
        }
      },
      "InvokeDetails": {
        "DurableExecutionArn": string
        // Only one of these is populated
        "Result": string,
        "Error": {
          "ErrorType": string,
          "ErrorMessage": string,
          "ErrorData": string,
          "StackTrace": [ string ]
        }
      }
    },
    ...
  ]
  // Present if there are more results
  "NextMarker": string
}
```

#### Errors

* All the [Common Errors](https://docs.aws.amazon.com/lambda/latest/api/CommonErrors.html) (includes auth errors, etc)
* `InvalidParameterValueException` - Some part of the request object failed input validation (HTTP 400)
* `InvalidCheckpointTokenException`  - The `checkpointToken` is not valid or has already been used (HTTP 400)
* `TooManyRequestsException` - Request was throttled (HTTP 429)
* KMS errors?

### Lambda Input / Output

While these are not APIs, we need to specify what data is passed into into the Lambda invocation by DAR and what information DAR expects the Lambda to return. The client SDK will use the input and generate the output. The customer’s handler code will never directly receive this as input or return as output.

#### `DurableExecutionInvocationInput`

```
{
  "DurableExecutionArn": string,
  // Initial checkpoint token
  "CheckpointToken": string,
  // This is a GetDurableExecutionState response object. It is used to provide 
  // initial state without needing to immediately call GetDurableExecutionState.
  // It will not be set if none of the initial state fits in 6MB. If provided 
  // and the nextToken is null, the initial state is exhaustive and
  // GetDurableExecutionState need not be called. The invocation input will be
  // in here (for the "EXECUTION" operation)
  "InitialExecutionState": GetDurableExecutionStateOutput
}
```

#### `DurableExecutionInvocationOutput` 

```
{
  // SUCCEEDED / FAILED => Durable Execution completed (either with a success, 
  //                       or a failure)
  //
  // PENDING => We're completing the invocation waiting for async work (and
  //            a re-invoke in the future)
  //
  // Note: There is no status for unexpected errors. If an unexpected error 
  //       happens in DAR client SDK code, we will cause the function to fail 
  //.      (i.e. the lambda will complete but there will be a FunctionError set
  //       in the response). This is separate from what happens if the _customer's_ 
  //       handler throws an error, in which case the status will be COMPLETE with 
  //       a failure result.
  //
  // Note: If the output paylaod is very close to 6MB, the SUCCEEDED / FAILED result
  //       can also be communicated via CheckpointDurableExecution updating the
  //       "EXECUTION" operation.
  "Status": "SUCCEEDED | FAILED | PENDING",
  "Result": string, // Only can set for SUCCEEDED
  "Error": { // Only can set for FAILED
        "ErrorType": string,
        "ErrorMessage": string,
        "ErrorData": string,
        "StackTrace": [ string ]
    }
}
```

### Usage Example

#### Starting a STEP

```
CheckpointDurableExecution

{
  "CheckpointToken": "12345",
  "Update": {
    "Id": "step-1",
    "Name": "Call Service Foo",
    "Type": "STEP",
    "Action": "CREATE"
  }
}
```

#### Completing a STEP

```
CheckpointDurableExecution

{
  "CheckpointToken": "12345",
  "Update": {
    "Id": "step-1",
    "Name": "Call Service Foo",
    "Yype": "STEP",
    "Action": "SUCCEED",
    "Payload": "{\"foo\":\"bar\"}"
  }
}
```

#### Retry a STEP in 10 seconds

```
CheckpointDurableExecution

{
  "CheckpointToken": "12345",
  "Update": {
    "Id": "step-1",
    "Name": "Call Service Foo",
    "Type": "STEP",
    "Action": "RETRY",
    "Error": {
        "ErrorMessage": "Something bad happened"
    },
    "StepOptions": {
      "NextAttemptDelaySeconds": 10
    }
  }
}
```

### Security

There will be a new resource, the `durableExecution`, which will have an ARN of this pattern: `arn:${Partition}:lambda:${Region}:${Account}:durable-execution:${FunctionName}:${DurableExecutionName}:${InvocationId}`

There will be 2 new IAM actions:

* `lambda:CheckpointDurableExecution`
    * Access level: `Write`
    * Resource types: None (or should this be `durable-execution`?)
    * Condition Keys: `lambda:FunctionArn`  (? do we need this if we enforce a `durable-execution` resource type)
* `lambda:GetDurableExecutionState`
    * Access level: `Read`
    * Resource types: None (or should this be `durable-execution`?)
    * Condition Keys: `lambda:FunctionArn`  (? do we need this if we enforce a `durable-execution` resource type)

This should allow customers to scope down the lambda execution role for a given function to allow these APIs to only be called on that function. In addition to the above, all of the standard [global condition keys](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_condition-keys.html#AvailableKeys)should be usable.

As outlined in previous sections, the main identifier passed into the two state management APIs is the `checkpointToken`. This token can only be used by the account that the lambda was invoked in. Additionally the `CheckpointDurableExecution` API consumes this token which invalidates it for further use. This prevents both incorrect duplicate/out-of-order checkpointing from a misbehaving function as well as any replay attacks if the token is attempted to be reused somehow at a later point. A new `checkpointToken` is returned from `CheckpointDurableExecution` which can be used for the next call.

If the function execution role does not have permission to call the two state management APIs, the client SDK will receive an access denied error. It will then fail the lambda invocation. DAR will then retry based on its default retry policy the same way it would if the state management APIs had encountered repeated faults. Eventually, if the permission configuration is not fixed (customers should be able to alarm on their lambda invocations failing unexpectedly), the durable execution will eventually time out.

However, since we do pass in initial state information in the invoke payload, it is possible for a short/small durable execution to work without permission to `GetDurableExecutionState` as we will not call it if the initial state is exhaustive. However, as soon as the total amount of state data is larger than 6MB, or more than the maximum page size of state data is present, things would start failing as mentioned in the previous paragraph.

## Managing Durable Functions

### DeleteFunction

**NOTE: a new doc is created specifically for deletion behavior, which overrides this section.
[Lambda Durable Functions API Functional Specification for DeleteFunction API](https://quip-amazon.com/0AC4Arw6BFbK)**

NOTE: this will change.  We’re considering [Elevator’s activate/deactivate behaviour](https://quip-amazon.com/B2BwAhNSaI9G/Elevator-Full-API-Spec#temp:C:EBJ03bc859f21724782bd3adcddd) and [asynchronous function deletion](https://quip-amazon.com/BgrgA8HIRJjb/OP1-2026-Asynchronous-Function-Deletion) proposal.

The developer can delete a durable function using the `DeleteFunction` API.  Lambda will delete the function immediately.  `DeleteFunction` will delete a specific function version if the `Qualifier` parameter is specified, or all versions of a function, just as it does today.  After deletion, each execution of the durable function will fail when the system next invokes the function for replay.  The developer will still be able to list all durable executions of the function as long as they have the ARN of the deleted function.  However, the function will no longer be included in the `ListFunctions` response.

There are no changes to the `DeleteFunction` request or response parameters.

### PublishVersion

The developer can publish a new version of a durable function using the `PublishVersion` API.  The response will contain a new `DurableConfig` object that specifies durable function configuration.

#### Response Syntax

```
{
  ...
  "DurableConfig": DurableConfig,
  ...
}
```

**DurableConfig**
Type: A [DurableConfig](https://quip-amazon.com/GYDNAvHRrC4N/Lambda-Durable-Functions-and-DAR-Functional-Specification#temp:C:LNXbbdf4c3adb7242dc9fdb0ebd1) object
Required: No

### StopDurableExecution

The developer can stop a durable execution using the `StopDurableExecution` API, and can provide an error object using the `Payload` field.

#### Request Syntax

```
`POST /2025-09-31/durable-executions/DurableExecutionArn/stop HTTP/1.1
 Content-type: application/json`

{
  "ErrorType": string,
  "ErrorMessage": string,
  "ErrorData": string,
  "StackTrace": [ string ]
}
```

**DurableExecutionArn**
The Amazon Resource Name (ARN) of the durable execution to stop.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 279.
Required: Yes

**ErrorType**
Customer-defined error type. 
Type: String
Length Constraints: Combined size of Type/Message/Data/StackTrace array must be < 256KB
Required: No

**ErrorMessage**
Customer-defined human-readable error message.
Type: String
Length Constraints: Combined size of Type/Message/Data/StackTrace array must be < 256KB
Required: No

**ErrorData**
Customer-defined machine-redable error data. 
Type: String
Length Constraints: Combined size of Type/Message/Data/StackTrace array must be < 256KB
Required: No

**StackTrace**
Stack trace information. 
Type: List of Strings
Length Constraints: Combined size of Type/Message/Data/StackTrace array must be < 256KB
Required: No

#### Response Syntax

```
`{ 
  "S`topDate`": `number` 
}`
```

**StopDate**
The date the durable execution is stopped.
Type: Timestamp

#### Errors

**InvalidParameterValueException**
One of the parameters in the request is not valid, for example the Payload is malformed, or the ARN is invalid.
HTTP Status Code: 400

**KmsAccessDeniedException**
Either your AWS KMS key policy or API caller does not have the required permissions.
HTTP Status Code: 400

**KmsInvalidStateException**
The AWS KMS key is not in valid state, for example: Disabled or Deleted.
HTTP Status Code: 400

**KmsThrottlingException**
Received when AWS KMS returns `ThrottlingException` for a AWS KMS call that Step Functions makes on behalf of the caller.
HTTP Status Code: 400

**RequestTooLargeException**
The request payload exceeded the `Invoke` request body JSON input quota. For more information, see [Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html).
HTTP Status Code: 413

**ResourceNotFoundException**
The specified durable execution does not exist.
HTTP Status Code: 400

### UpdateFunctionConfiguration

The developer cannot update the unpublished version (`$LATEST`) of a durable function to enable or disable durability.  We do, however, recognize the value of this and propose to support it post-launch.  See [Updating Non-Durable to/from Durable](https://quip-amazon.com/FSoLAT2X4Dds) for rationale.

The developer can change durability options, using the `UpdateFunctionConfiguration` API, including the `ExecutionTimeout` and the `RetentionPeriodInDays`  We will add a new configuration request parameter called `DurableConfig` which contains an optional integer field called `ExecutionTimeout` that specifies the total time in seconds the durable function can run, and an optional integer field called `RetentionPeriodInDays` that specifies the number of days to retain the durable execution history after the durable execution completes.  The response will contain a new `DurableConfig` object that specifies durable function configuration.

#### Request Syntax

```
`PUT /2015-03-31/functions/FunctionName/configuration HTTP/1.1 
Content-type: application/json

`{
  ...
  "DurableConfig": DurableConfig,
  ...
}
```

**DurableConfig**
Type: A [DurableConfig](https://quip-amazon.com/GYDNAvHRrC4N/Lambda-Durable-Functions-and-DAR-Functional-Specification#temp:C:LNXbbdf4c3adb7242dc9fdb0ebd1) object
Required: No

#### Response Syntax

```
{
  ...
  "DurableConfig": DurableConfig,
  ...
}
```

**DurableConfig**
Type: A [DurableConfig](https://quip-amazon.com/GYDNAvHRrC4N/Lambda-Durable-Functions-and-DAR-Functional-Specification#temp:C:LNXbbdf4c3adb7242dc9fdb0ebd1) object
Required: No

#### Errors

**InvalidParameterValueException**
If the `ExecutionTimeout` < 1 second or > 31,622,400 seconds, or the `RetentionPeriodInDays` < 1 day or > 90 days.
HTTP Status Code: 400

## Read APIs

### GetDurableExecution - New API

The developer can get information about a durable execution using the `GetDurableExecution` API.

#### Request Syntax

```
`GET ``/2025-09``-``31``/``durable``-``executions`/`DurableExecutionArn` HTTP/1.1
```

**DurableExecutionArn**
The Amazon Resource Name (ARN) of the durable execution to get.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 279.
Required: Yes

#### Response Syntax

```
`HTTP/1.1 200
 Content-type: application/json

{`
  "DurableExecutionArn": string, 
  "`DurableExecutionName`": string,
  "FunctionArn": string, 
  "InputPayload": string, 
  "Result": string, // For success
  "Error": { // For failure
    "ErrorType": string,
    "ErrorMessage": string,
    "ErrorData": string,
    "StackTrace": [ string ]
  },
  "StartDate": number,
  "Status": string, 
  "StopDate": number, 
  "TraceHeader": {
    "X-Ray": string,
  },
  "UsageReport": {
    "MaxMemoryUsed": number
    "ReplayCount": number,
    "TotalDuration": number,
  },
  "Version": string
}
```

**DurableExecutionArn**
The Amazon Resource Name (ARN) that identifies the durable execution.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 279.
Required: Yes

**DurableExecutionName**
The name of the durable execution.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 64.
Required: Yes

**FunctionArn**
The Amazon Resource Name (ARN) of the durable function that was invoked.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 140.
Required: Yes

**InputPayload**
The JSON that you provided to the durable function as input.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 256KB for asynchronously-invoked executions, or 6MB for synchronously-invoked executions.
Required: No

**Result**
The JSON response from the durable function if successful
Type: String
Length Constraints: Minimum length of 0. Maximum length of 256KB.
Required: No

**Error**
The Error object if not successful
Type: Error
Length Constraints: Combined size of error fields have a maximum length of 256KB

**Error.ErrorType**
Customer-defined error type. 
Type: String
Length Constraints: Combined size of Type/Message/Data/StackTrace array must be < 256KB
Required: No

**Error.ErrorMessage**
Customer-defined human-readable error message. 
Type: String
Length Constraints: Combined size of Type/Message/Data/StackTrace array must be < 256KB
Required: No

**Error.ErrorData**
Customer-defined machine-redable error data. 
Type: String
Length Constraints: Combined size of Type/Message/Data/StackTrace array must be < 256KB
Required: No

**Error.StackTrace**
Stack trace information. 
Type: List of Strings
Length Constraints: Combined size of Type/Message/Data/StackTrace array must be < 256KB
Required: No

**ReplayCount**
The number of times the Lambda function has been invoked throughout the durable execution.
Type: Integer
Required: Yes

**StartDate**
The date the durable execution started
Type: Timestamp
Required: Yes

**Status**
The current status of the durable execution
Type: String
Valid Values: `RUNNING | SUCCEEDED | FAILED | TIMED_OUT | STOPPED`
Required: Yes

**StopDate**
If the durable execution already ended, the date the durable execution stopped.
Type: Timestamp
Required: No

**TraceHeader**
The trace header configuration.
Type: TraceHeader
Required: Yes

**UsageReport**
The durable execution’s usage report.
Type: DurableExecutionUsageReport
Required: Yes

**Version**
The version of the durable function that was invoked for this durable execution.
Type: String
Pattern: [0-9]+
Required: No

#### TraceHeader Contents

In the future we may support OTel or other trace systems by adding additional fields here.

**X-Ray**
The AWS X-Ray trace header that was passed to the durable execution.
Type: String
Length Constraints: Minimum length of 0. Maximum length of 256.
Pattern: `\p{ASCII}*`

#### **DurableExecutionUsageReport Contents**

**BilledDuration**
The total billed duration in milliseconds across all invocations of the durable function for this execution.
Type: Integer
Required: Yes

**MaxMemoryUsed**
The maximum amount of memory in MB used across all invocations.
Type: Integer
Required: Yes

**InvocationCount**
The total number of invocations performed for this durable execution including the initial invoke and all subsequent invocations for replay.
Type: Integer
Required: Yes

**TotalDuration**
The total duration in milliseconds across all invocations of the durable function for this execution, rounded to the nearest millisecond.
Type: Integer
Required: Yes

#### Errors

**InvalidParameterValueException**
The provided Amazon Resource Name (ARN) is not valid.
HTTP Status Code: 400

**ResourceNotFoundException**
The specified durable execution does not exist.
HTTP Status Code: 404

### GetDurableExecutionHistory - New API

(More info in - [DAR Visibility APIs Spec - v1](https://quip-amazon.com/r8FhAbX0vNuI))
The developer can list the history of a durable execution using the `GetDurableExecutionHistory` API.

Lambda will also send history information to CloudWatch Logs, allowing developers to view how their steps progressed over time.  However, since durable executions are potentially long-lived (up to one year), searching CloudWatch Logs will not always be convenient, so Lambda will also make the execution history available through `GetDurable``Execution``History`.

#### Request Syntax

```
`GET ``/2025-09``-``31``/``durable``-``executions`/`DurableExecutionArn``/``history``&``IncludeDurableExecutionData``=``IncludeDurableExecutionData``&``Marker``=``Marker``&``MaxItems``=``MaxItems``&``ReverseOrder``=``ReverseOrder` HTTP/1.1
```

**DurableExecutionArn**
The Amazon Resource Name (ARN) of the durable execution.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 279.
Required: Yes

**IncludeDurableExecutionData**
You can select whether durable execution data (execution input or output, or the result of each step or callback) is returned. The default is `true`.

Type: Boolean
Required: No

**MaxItems**
The maximum number of items that are returned per call. You can use `Marker` to obtain further pages of results. The default is 100 and the maximum allowed page size is 1000. A value of 0 uses the default.

This is only an upper limit. The actual number of results returned per call might be fewer than the specified maximum.

Type: Integer
Valid Range: Minimum value of 0. Maximum value of 1000.
Required: No

**Marker**
If `nextMarker` is returned from the previous request, there are more results available. The value of `Marker` is a unique pagination token for each page. Make the call again using the returned token to retrieve the next page. Keep all other arguments unchanged. Each pagination token expires after 24 hours. Using an expired pagination token will return an *HTTP 400* *InvalidParameterValueException* error.

Type: String
Length Constraints: Minimum length of 1. Maximum length of 1024.
Required: No

**ReverseOrder**
Lists history in descending order of their `timeStamp`.
Type: Boolean
Required: No

#### Response Syntax

```
`HTTP/1.1 200 
Content-type: application/json
`
{
  "Events": [
    // TO BE DETERMINED
  ],
  "NextMarker": string
}
```

#### Errors

**InvalidParameterValueException**
The provided Amazon Resource Name (ARN) is not valid.
HTTP Status Code: 400

**InvalidTokenException**
The provided token is not valid.
HTTP Status Code: 400

**KmsAccessDeniedException**
Either your AWS KMS key policy or API caller does not have the required permissions.
HTTP Status Code: 400

**KmsInvalidStateException**
The AWS KMS key is not in valid state, for example: Disabled or Deleted.
HTTP Status Code: 400

**KmsThrottlingException**
Received when AWS KMS returns `ThrottlingException` for a AWS KMS call that Lambda makes on behalf of the caller.
HTTP Status Code: 400

**ResourceNotFoundException**
The specified durable execution does not exist.
HTTP Status Code: 404

### GetFunction

The developer can get information about the durable function using the `GetFunction` API.  The response will contain a new `DurableConfig` object that specifies durable function configuration.

#### Response Syntax

```
{
  ...
  "Configuration": {
    ...
    "DurableConfig": DurableConfig
    ...
  },
  ...
}
```

**DurableConfig**
Type: A [DurableConfig](https://quip-amazon.com/GYDNAvHRrC4N/Lambda-Durable-Functions-and-DAR-Functional-Specification#temp:C:LNXbbdf4c3adb7242dc9fdb0ebd1) object
Required: No

### GetFunctionConfiguration

The developer can get information about the durable function configuration using the `GetFunctionConfiguration` API.  The response will contain a new `DurableConfig` object that specifies durable function configuration.

#### Response Syntax

```
{
  ...
  "DurableConfig": DurableConfig,
  ...
}
```

**DurableConfig**
Type: A [DurableConfig](https://quip-amazon.com/GYDNAvHRrC4N/Lambda-Durable-Functions-and-DAR-Functional-Specification#temp:C:LNXbbdf4c3adb7242dc9fdb0ebd1) object
Required: No

### ListDurableExecutions - New API

(More info in - [DAR Visibility APIs Spec - v1](https://quip-amazon.com/r8FhAbX0vNuI))
The developer can list all durable executions of a durable function using the `ListDurableExecutions` API.

#### Request Syntax

```
`GET ``/2025-09``-``31``/``functions``/``FunctionName``/``durable``-``executions``?``FunctionVersion``=``FunctionVersion``&``Marker``=``Marker``&``MaxItems``=``MaxItems``&``StatusFilter``=``StatusFilter` HTTP/1.1
```

#### URI Request Parameters

**FunctionName**
The name or ARN of the Lambda function whose durable executions are listed.

**Name formats**

    * **Function name** – `my-function`.
    * **Function ARN** – `arn:aws:lambda:us-west-2:123456789012:function:my-function`.
    * **Partial ARN** – `123456789012:function:my-function`.

The length constraint applies only to the full ARN. If you specify only the function name, it is limited to 64 characters in length.

Length Constraints: Minimum length of 1. Maximum length of 140.
Pattern: `(arn:(aws[a-zA-Z-]*)?:lambda:)?([a-z]{2}(-gov)?-[a-z]+-\d{1}:)?(\d{12}:)?(function:)?([a-zA-Z0-9-_]+)(:(\$LATEST|[a-zA-Z0-9-_]+))?`
Required: Yes

**FunctionVersion**
Set to a version number to include executions for the specified version of the function.  If not specified, include all executions of the function.  If `NONE` is specified, include all executions of the function that were not pinned to a specific version.  Unpinned executions are started when the function’s `DurableConfig`’s `AllowInvokeLatest` parameter is set to `true`.
Type: String
Required: No

**MaxItems**
The maximum number of items that are returned per call. You can use `Marker` to obtain further pages of results. The default is 100 and the maximum allowed page size is 1000. A value of 0 uses the default.

This is only an upper limit. The actual number of results returned per call might be fewer than the specified maximum.

Type: Integer
Valid Range: Minimum value of 0. Maximum value of 1000.
Required: No

**Marker**
If `nextMarker` is returned from the previous call, there are more results available. The value of `Marker` is a unique pagination token for each page. Make the call again using the returned token to retrieve the next page. Keep all other arguments unchanged. Each pagination token expires after 24 hours. Using an expired pagination token will return an *HTTP 400* *InvalidParameterValueException* error.

Type: String
Length Constraints: Minimum length of 1. Maximum length of 3096.
Required: No

**StatusFilter**
If specified, only list the durable executions whose current execution status matches the given filter.
Type: String
Valid Values: `RUNNING | SUCCEEDED | FAILED | TIMED_OUT | ABORTED`
Required: No

#### Response Syntax

```
`HTTP``/``1.1`` ``200`
`Content``-``type``:`` application``/``json`

{
  "ListVersionsByFunction": [
    {
      "DurableExecutionArn": string,
      "DurableExecutionName": string,
      "StartDate": number,
      "FunctionArn": string,
      "Status": string,
      "StopDate": number
    }
  ],
  "NextMarker": string
}
```

**DurableExecutionArn**
The Amazon Resource Name (ARN) that identifies the durable execution.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 279.
Required: Yes

**DurableExecutionName**
The name of the durable execution.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 64.
Required: Yes

**FunctionArn**
The Amazon Resource Name (ARN) of the durable function that was invoked.
Type: String
Length Constraints: Minimum length of 1. Maximum length of 140.
Required: Yes

**StartDate**
The date the durable execution started
Type: Timestamp
Required: Yes

**Status**
The current status of the durable execution
Type: String
Valid Values: `RUNNING | SUCCEEDED | FAILED | TIMED_OUT | ABORTED`
Required: Yes

**StopDate**
If the durable execution already ended, the date the durable execution stopped.
Type: Timestamp
Required: No

#### Errors

**InvalidParameterValueException**
The provided Amazon Resource Name (ARN) is not valid, or the given `StatusFilter` value is not valid.
HTTP Status Code: 400

**InvalidTokenException**
The provided token is not valid.
HTTP Status Code: 400

**ResourceNotFoundException**
The specified function does not exist.
HTTP Status Code: 404

### ListFunctions

The developer can list durable functions using the `ListFunctions` API.  `ListFunctions` will return all functions, both durable and standard.  The response will contain a new `DurableConfig` object for each function in the `Functions` list that specifies durable function configuration.

#### Response Syntax

```
{
  "Functions": [
    {
      ...
      "DurableConfig": DurableConfig,
      ...
    }
  ],
  ...
}
```

**DurableConfig**
Type: A [DurableConfig](https://quip-amazon.com/GYDNAvHRrC4N/Lambda-Durable-Functions-and-DAR-Functional-Specification#temp:C:LNXbbdf4c3adb7242dc9fdb0ebd1) object
Required: No

### ListVersionsByFunction

The developer can list the versions of a durable function using the `ListVersionsByFunction` API.  `ListVersionsByFunction` will return all function versions of a specified function, both durable and standard.  The response will contain a new `DurableConfig` object for each function in the `Versions` list that specifies durable function configuration.

#### Response Syntax

```
{
  "Versions": [
    {
      ...
      "DurableConfig": DurableConfig,
      ...
    }
  ],
  ...
}
```

**DurableConfig**
Type: A [DurableConfig](https://quip-amazon.com/GYDNAvHRrC4N/Lambda-Durable-Functions-and-DAR-Functional-Specification#temp:C:LNXbbdf4c3adb7242dc9fdb0ebd1) object
Required: No


## Quotas

### Payload Size Limit

All payloads for durable executions are limited to 256KB.  This includes the input to a durable execution for asynchronous invokes (i.e. the `Invoke` API’s `Payload` request parameter), the result of each step, the completion payload of each callback, and the final output of a durable execution.  For synchronous invokes, the input and output (i.e. the `Invoke` API’s `Payload` request and response parameters) can be up to 6MB.

### Durable Execution Name Semantics

The `DurableExecutionName` names the execution.  Only one execution with a given name can be open at a time.  As soon as an execution closes, its name can be reused for a new execution.

### Client Token Validity

The `ClientToken` provided to the `Invoke` API remains valid for 15 minutes.  `Invoke` API calls cannot use the same `ClientToken` for the same region and durable function for at least 15 minutes.

### Retention Period

The retention period is the period of time when the durable execution history is available through the `GetDurableExecutionHistory` API.  Durable execution history will be kept up to 90 days after a durable execution is closed.  After this time, customers can no longer retrieve or view the durable execution history. 

Customers can configure the durable execution history retention period to any number of days between one and 90.  The retention period can be configured separately for each durable function (including all its versions).  The default is 30 days.

Logs sent to CloudWatch Logs have their own separate retention period ( see https://docs.aws.amazon.com/managedservices/latest/userguide/log-customize-retention.html).

## Tooling Changes

TODO

Describe details of all tooling changes, such as to CDK, AWS Toolkit, CloudFormation, SAM, validation

## Usage Examples

TODO

Provide usage examples to clarify how we expect users to use the new APIs

E.g.

* Create a new durable function
* Update a regular function to be durable
* Update a durable function to disable durability
* Delete a durable function and then find durable executions of it (still running and already complete)
* Change the durable execution timeout or retention period of a durable function that has running durable executions
* Invoke a durable function asynchronously with retries, preventing duplicate durable executions
* Invoke a durable function synchronously and wait for completion if long-running (>15 minutes)

## Engineering Context

TODO

Add any additional context the engineering team needs to know related to any technical observations or constraints that influenced the proposed solution during working backwards.  For example, public facing performance or technical limitations that will shape the technical implementation details of the system.

## Feedback and Decisions

List all feedback given during reviews, and indicate what decisions were made based on this feedback.

### 11 April 2025

* We will support Sync Invoke
* Naming : `DurableExecutionName` → `Name`
* DurableConfig: historical and real-time insight parameters
* ExecutionTimeout → ExecutionTimeoutSeconds?
* Timeout vs ExecutionTimeout - which is the default?
    * Suggestion to make the Timeout field the durableExecution timeout, and add another timeout setting for the single replay timeout
    * Have a higher default for the replay timeout e.g. 1 minute
    * Have to check IaC - what validation do they do for high big the Timeout can be?
* `DurableExecutionName` lifetime for idempotency - 1 hour?  24 hours?
* ~~`DurableExecutionName` is similar to RunId - and the arn will have both so let’s come up with better names~~
* `DurableExecutionName` naming - ClientToken? - look at ECS RunTask
* OTel - TraceHeader field - should we change this to be forward compatible with OTel traces?
* Error/Cause => Reason - Bytestream?
* NextToken vs NextMarker
* Reject/Resolve naming re: other language terminology
* > Durable Runtime APIs are now public
* > Retention - make it clear: through the api, they can still access through the logs
* > Retention period - how low can they set it?
* > All APIs:
    * For all of these - we need to indicate error conditions / what errors will they throw?
    * E.g what happens if you try to resolve an already resolved promise, or stop an already stopped execution
* > Remove Durable-Runtime-Context - the durable execution’s latest context from /state runtime API response
* Delete behaviour - ability to find executions of deleted functions
    * How long will running ones continue until they get killed?
    * BatchTerminate API - or StopDurableExecution can accept a function arn to delete all running executions - coupled with inability to delete a function with running executions
    * Lookup DeleteFunction docs - concurrent operation exception - in what case?
    * one pager
* > GetDurableExecution
    * return how many times has the function been invoked for replay
    * billed runtime / memory aggregated across all replays

### 23 April 2025 - Review with Lambda Team

[Jie Leng](https://quip-amazon.com/VbE9EA30hpx), [Kuldeep Gupta](https://quip-amazon.com/OUC9EAR0caW), [Arash Ghoreyshi](https://quip-amazon.com/ZLL9EA9ePu2), [Nikita Tibrewal](https://quip-amazon.com/IIa9EAwbu9i) 

* Prefer no “Enabled” in DurableConfig
* Prefer top-level Timeout field to denote execution timeout
* jriecken@ questions/feedback from runtime support APIs:
    * Throttling limits - what should they be? Currently lambda’s API throttling limits are hard limits that cannot be increased. Should we even have this? Should we just add protection/load shed and not have limits that customers need to think about?
    * Additional information in Invocation Input - Do we need additional metadata in here (trace headers? attempt specific info ? retry count? anything else needed to bootstrap the DurableContext?)
    * ~~Do we need to allow more than one update to be checkpointed on each call to CheckpointDurableExecution?~~
        * No reason not to
    * How do we deal with checkpointed actions that are fallible? Do we mark the entity as failed with an appropriate error and the promise will fail / customer can catch on next re-invoke?
        * Completing a promise that does not exist or is already completed
        * Failing to start a chained invocation (no permission, function doesn’t exist, etc)
    * We need to think more about Promise and Invoke (chained durable functions)
        * Do we need to support cross-account somehow?
* Find out other Lambda teams that need to review - runtime & invoke
    * ****Arash Ghoreyshi**
        **
    * Hi Scott, here are the PoCs:
        invoke/data plane: [@conniall](https://amzn-aws.slack.com/team/W017VPHFPKK)
        runtimes: [@petermd](https://amzn-aws.slack.com/team/W017GJ7L4UD)
        elevator: [@gfarr](https://amzn-aws.slack.com/team/U01E1TM8X4Z)
        function control plane: [@kuldeepg](https://amzn-aws.slack.com/team/W017GS054E7)
* $LATEST issue - Function control plane team [Kuldeep Gupta](https://quip-amazon.com/OUC9EAR0caW)

### 28 April 2025 - Changes from Feedback

* `Invoke`: changed to disallow invoking `$LATEST`.  We will only support invoking specified versions of durable functions (two way door) because we can’t pin to `$LATEST` for the entire execution.
* `Invoke`: changed so a `ClientToken` parameter governs idempotency and `DurableExecutionName` parameter only names the execution
* `DurableConfig`
    * Removed `Enabled` field - we’ve decided to disallow changing the durability setting on Update so we don’t need this field any more
    * `ExecutionTimeout` field is now required for `CreateFunction` but remains optional for `UpdateFunctionConfiguration`
    * Renamed `RetentionPeriod` to `RetentionPeriodInDays` for clarity.  Other time fields in Lambda APIs omit the time unit but assume seconds.
    * `RetentionPeriodInDays` minimum is one (changed from zero for implementation reasons - 2-way door)
* `UpdateFunctionConfiguration` cannot change the durability setting - once durable always durable and vice versa
* `GetDurableExecution` - renamed `Input` to `InputPayload` and `Output` to `OutputPayload`
* `GetDurableExecutionHistory`, `ListDurableExecutions` - changed field names for consistency: `Marker`, `NextMarker`, `MaxItems`



