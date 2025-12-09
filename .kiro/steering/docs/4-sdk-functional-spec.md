# Lambda Durable Functions Language SDK Functional Specification


## Introduction

Lambda Durable Functions is a new Lambda feature that lets developers write multi-step applications that persist state as they progress.  Developers can create a Durable Function in Lambda and use durable operations like “steps” and “callbacks” to write their durable function code.  Durable functions checkpoint the result of each operation so its result becomes durable even if the function fails after the checkpoint.  Each operation can run up to the Lambda function’s timeout (maximum 15 minutes), and the entire multi-operation function can execute for up to one year when invoked asynchronously.  Sync invokes are limited to 15 minutes.  When a function needs to wait for an event such as a timeout or a response from an asynchronous callback, the durable function suspends and then resumes when the event arrives, so that functions only pay for active compute time, not for waiting.  Also see the [PRFAQ](https://amazon.awsapps.com/workdocs-amazon/index.html#/document/5510da7b93fb78a6a08db16bad6c400367f19f2f51bf1eb915316e68b50776f9).

For the Lambda API functional specification for durable functions, see [Lambda Durable Functions API Functional Specification](https://quip-amazon.com/GYDNAvHRrC4N).

## Language SDK Capabilities

When developers write a durable Lambda function, they write a Lambda event handler that accepts a `DurableContext` instead of a `Context`.  The `DurableContext` extends the Lambda `Context` interface by adding data and methods for managing durable executions.

Durable functions execute by running durable operations like “durable steps” and “durable callbacks” that run customer code and persist results upon completion.  By persisting each step’s result, durable functions can recover from failure without having to rerun already-completed steps.  For example, if a durable function successfully runs and persists the result of a step, and then fails at some future point, the function can recover by rerunning the function, but avoid running the step again by fetching its previously-persisted result.  Durable functions use a “replay” model to perform recovery after failure.

### Replay Model

Durable functions uses a “replay” model to run the durable function code. When a durable function is first invoked, Lambda runs the developer’s event handler from the beginning. Since this is the first invocation of the function, the code runs normally without any special replay semantics by running steps and persisting their results. The function may then terminate, either purposely to complete, purposely to wait for a timer or asynchronous event to fire, or unintentionally due to something like a sandbox failure. If not complete, the function will be invoked again in replay mode to recover and then continue from the point of failure or suspension.

When Lambda invokes the function for replay, the function runs from the beginning and checks each durable step (or other operation) to see if it has already completed in a previous invocation.  If so, the replay process avoids rerunning the step’s code and instead fetches its previously-returned result.  Replay continues and repeats this process for each durable operation until it reaches an operation that has not previously completed, at which point it runs the operation normally.  In this way, the replay process runs the function from the beginning but avoids rerunning operations that have previously completed.  For steps, code inside a checkpointed step will not be executed on replay, but any code outside a step will get executed again on replay, so it is imperative this code does the same thing every time.  In other words, all code outside a step needs to be deterministic, otherwise the replay process may fail.  This means code that, for example, fetches the current time, generates a random number or UUID, or calls an API that may return a different result on different calls, must be placed inside a step so it doesn’t generate different results on replay.

## Summary: Durable Operations and the DurableContext

This section contains an overview of the SDK’s durable operations, along with examples to illustrate some common use cases.  Each operation contains a link to its detailed description in the section that follows the examples.

The DurableContext extends the Lambda Context and contains additional capabilities for durable functions.  See the [Details: Durable Operations and the DurableContext](https://quip-amazon.com/Pat3AbBBXPvZ#temp:C:SfNd94481b39951439a9e9e981a0) section for details.

Durable functions pass a `DurableContext` to the Lambda event handler.  This gives the function access to all the durable operations described in this document.  This example shows the event handler signature, but the remaining examples omit this for brevity.

```
export const handler: DurableHandler<MyInput, string> 
      = async (event: MyInput, context: DurableContext) => {
  // Perform durable operations using the DurableContext.
  // E.g. context.step(...) or context.wait(...) as described below
}
```

All durable operations accept an optional `name` parameter to support operational visibility, but the name is excluded from the examples in this summary for brevity.  Also, the `config` parameter for every operation lets customers specify custom SerDes behaviour that operations use to serialize results for checkpointing, and deserialize when recalling previously-checkpointed values.  See the [Serialization](https://quip-amazon.com/Pat3AbBBXPvZ#temp:C:SfN874e68b0a9514d469af3b910c) section for details.

### Performing Durable Steps

```
`step<T>(fn: StepFunc<T>, config?: StepConfig<T>): DurablePromise<T>;`
```

Call a function and checkpoint the result.  The `StepConfig` lets customers specify a retry strategy and whether to run the step at-least- or at-most-once per retry in case of crashes.  `step` will call the `fn` function the first time it runs, and then checkpoint its result.  On replay, `step` will recall the checkpointed result if it exists instead of calling `fn` again.  If the function fails, the retry strategy will specify how long to wait before retrying, in which case the function will be suspended to avoid paying for CPU while waiting.  See the [ctx.step - Running Checkpointed Steps](https://quip-amazon.com/Pat3AbBBXPvZ#temp:C:SfN45044f9996004ea6b7ab8a2ce) section for details.

For example, this code sequentially runs two steps, each calling an API, and returns their results.  If, for example, the Lambda function were to ungracefully fail after successfully checkpointing the first step, the function will be reinvoked, recall the first step’s result without calling `FirstApi` again, and then run the second step which will call `SecondApi`.

```
const res1 = await context.step(async () => FirstApi());
const res2 = await context.step(async () => SecondApi());
return {res1, res2};
```

[Image: image.png]
### Chain Lambda Functions Together

```
`invoke``<``I``,``O``>(``funcId``:`` ``FunctionIdentifier``,`` input``:`` I``, ``config``?:`` ``InvokeConfig``):`` ``DurablePromise``<``O``>;`
```

Invoke another Durable Lambda function and wait for its result.  The durable application runtime will call the Lambda function on behalf of the durable execution, and guarantee idempotency with 'at-most-once' semantics, which means it will start a single durable execution if possible.  The caller’s invocation will be suspended and then be reinvoked when the Lambda function’s result is ready to avoid paying for CPU while waiting.

For example:

```
const result = await context.invoke(
  "arn:aws:lambda:::function:doSomething",
  {
    "hello": "there",
    "event": event
  }
);
```

[Image: image.png]
### Waiting for a Period of Time

```
`wait``(`seconds`:`` number``):`` ``DurablePromise``<void>``;`
```

Durably wait for a number of seconds.  `wait` makes the function invocation eligible for suspension so the invocation doesn’t have to continue or pay for CPU while waiting.  The durable execution runtime will checkpoint the amount of time to wait, shutdown the runtime, and then reinvoke the function when the timer fires.  See the [ctx.wait: Waiting for a Period of Time](https://quip-amazon.com/Pat3AbBBXPvZ#temp:C:SfN75b0d31a600a4ae8b84c15043) section for details.

For example, this code waits for 7 days after processing a transaction before sending a followup email.  Since the invocation doesn’t have to continue while waiting, it isn't constrained by Lambda’s 15 minute invocation limit.

```
await context.step(async () => await ProcessTransaction());

await context.wait(60 * 60 * 24 * 7);

await context.step(async () => await SendFollowupEmail());
```

[Image: image.png]
### Waiting for a Callback - Asynchronous Step Completion

```
waitForCallback<T>(submitter?: WaitForCallbackSubmitterFunc,
  config?: WaitForCallbackConfig): DurablePromise<T>;
```

Run a function to submit some work to an external system, and suspend the function to wait for the external system to call back with the result without paying for CPU while waiting.  The `WaitForCallbackConfig` lets customers specify a retry strategy and timeouts.  `waitForCallback` first generates a unique `callbackId` and checkpoints it.  Then it calls the `submitter` function, passes it the `callbackId`, and checkpoints its result.  The `submitter` function will typically send the `callbackId` to another part of the application such as an SQS queue, a database, or a public API.  When the external system generates a response, it will send it by calling the `SendDurableExecutionCallbackSuccess` or `SendDurableExecutionCallbackFailure` API with the `callbackId` and result, which will resolve the callback promise returned by `waitForCallback`.  See the [ctx.waitForCallback - Wait Until a Callback Occurs (Structured)](https://quip-amazon.com/Pat3AbBBXPvZ#temp:C:SfNeb8cd20f3cde400cb15c0be7d) section for details.

For example, this code runs the submitter which calls an external Stripe API and passes it the `callbackId`.  It will also pass it a webhook to call with the result.

```
// This will complete when the webhook lambda calls the
// SendDurableExecutionCallbackSuccess API (see below)
const result = await context.waitForCallback<StripeCallbackInfo>(
  async callbackId => await CallStripe(event, callbackId),
  {timeout: 300}
);
```


When finished, Stripe will complete the callback by calling a webhook the developer provides to the Stripe API call. The developer can create the webhook using API Gateway and set it up to call another function, which will send the callback result by calling `SendDurableExecutionCallbackSuccess` or `SendDurableExecutionCallbackFailure` like this.  We will also work with the API Gateway team to build in support so the customer doesn’t have to write this Lambda function.

```
// This gets called from the API Gateway webhook.
export const handler: Handler<StripeCallbackInfo, void> 
    = async (event: StripeCallbackInfo, context: Context) => {
  const client = new LambdaClient({});
  let command;
  if (event.success) {
    command = new SendDurableExecutionCallbackSuccessCommand(
        { CallbackId: event.callbackId, CallbackResult: event });
  } else {
    command = new SendDurableExecutionCallbackFailureCommand(
        { CallbackId: event.callbackId, CallbackResult: event });
  }
  return client.send(command);
};
```

### Wait for a Condition to be Met, and Durable SDK-style Waiters (Polling)

```
waitForCondition<T>(check: WaitForConditionCheckFunc<T>,
    config?: WaitForConditionConfig<T>) : DurablePromise<T>;
```

Wait for a condition to be met by repeatedly calling a `check` function to get, for example, a current status, then call a `waitStrategy` function with the `check` result to determine whether the condition has been met.  If not, it will return how long to wait before trying again, in which case the function will suspend and then be reinvoked when it’s time to try again, to avoid paying for CPU while waiting.  See the [ctx.waitForCondition - Wait Until a Given Condition is Met](https://quip-amazon.com/Pat3AbBBXPvZ#temp:C:SfN95aa94b7bcea45df9fe838984) section for details.

For example, this code repeatedly calls an API to get a status, and keeps trying until the status is `'CURRENT'`.  This example uses a `createWaitStrategy` helper function to create the strategy from a configuration.

```
const result = await context.waitForCondition(
  async (state) => getCurrentState(state),
  config: {
    waitStrategy: createWaitStrategy({
      maxAttempts: 60,
      initialDelaySeconds: 5,
      maxDelaySeconds: 30,
      backoffRate: 1.5,
      jitterSeconds: 1,
      shouldContinuePolling: (result) => result.status !== 'CURRENT', 
      timeoutSeconds: 600
    })
  }
}
```


#### Code-based Wait Strategy

Instead of specifying a `waitStrategy` using configuration as above, the customer can instead supply their own `waitStrategy` function to have full flexibility when implementing the strategy.  For example:

```
    waitStrategy: (result: StackInstance, attempt: number) => {
      const maxAttempts = 60;
      if (result.status === 'CURRENT') {
        return { shouldContinue: false };
      } else if (attempt > maxAttempts) {
        throw "Exceeded max attempts";
      } else {
        return { 
          shouldContinue: true, 
          delaySeconds: Math.min(5 * attempt, 30)
        }
      }
    }
```

#### Durable SDK Waiters

We will also provide durable SDK-style waiters so customers can use them to wait for a potentially long period (e.g. >15 minutes if waiting for a CloudFormation stack to be created) without paying for CPU while waiting.

For example, this code creates an S3 bucket and then durably waits until it exists.  The `sdkWaiter` function uses `context.waitForCondition` internally to durably perform the waiter’s polling loop.  We are working with the SDK team to refactor the existing waiters so they can accept and use an alternate polling strategy.  Note, we also have the option of creating a durable version of each SDK waiter instead of providing a single `sdkWaiter` wrapper as in this example.

```
const Bucket = "BUCKET_NAME";
const client = new S3Client({ region: "REGION" });
const command = new CreateBucketCommand({ Bucket });

await context.step(async () => client.send(command));
await context.sdkWaiter(
    waitUntilBucketExists, {client, maxWaitTime: 60}, {Bucket});
```


### Run Durable Operations in Parallel

```
parallel<T>(branches: ParallelFunc<T>[], config?: ParallelConfig): DurablePromise<BatchResult<T>>;
parallel<T>(name: string, branches: ParallelFunc<T>[], config?: ParallelConfig): DurablePromise<BatchResult<T>>;
```

Run multiple durable operations in parallel.  `parallel` lets the customer configure the maximum amount of concurrency to use, and the desired failure behaviour.  See the [ctx.parallel - Run Multiple Operations in Parallel](https://quip-amazon.com/Pat3AbBBXPvZ#temp:C:SfNd6a0706232434a50aa486aecf) section for details.

For example, this code runs three functions (`ChooseHotel` etc.) at most two at a time, and tolerates at most one failure.  Each function accepts a `context` which allows it to perform durable operations as needed.  Each ‘branch’ is managed and checkpointed by the `parallel` operation, controlling concurrency, and terminating when the completion conditions are met.

```
const result = await context.parallel([
  async (context) => await ChooseHotel(context),
  async (context) => await ChooseCar(context),
  async (context) => await ChoosePrize(context)
], {
  maxConcurrency: 2,
  completionConfig: {
    toleratedFailureCount: 1
  }
});

// Access successful results
const successfulResults = result.getResults();

// Check for failures
if (result.hasFailure) {
  console.log(`${result.failureCount} items failed`);
  result.getErrors().forEach(error => console.error(error));
}
```



### Map a Function that Performs Durable Operations Onto an Array

```
map<T>(items: any[], mapFunc: MapFunc<T>, config?: MapConfig): DurablePromise<BatchResult<T>>;
map<T>(name: string, items: any[], mapFunc: MapFunc<T>, config?: MapConfig): DurablePromise<BatchResult<T>>;
```

Run durable operations to process each item of an array, and run them concurrently.  `map` lets the customer configure the maximum concurrency to use and the desired failure behaviour similar to `parallel`.  See the [ctx.map - Run Durable Operations for Every Array Item](https://quip-amazon.com/Pat3AbBBXPvZ#temp:C:SfNa0ff43314c57442e8a21ccc65) section for details.


### Durable Promise Combinators

```
: {
  race<T>(promises: DurablePromise<T>[]): DurablePromise<T>;
  any<T>(promises: DurablePromise<T>[]): DurablePromise<T>;
  all<T>(promises: DurablePromise<T>[]): DurablePromise<T[]>;
  allSettled<T>(promises: DurablePromise<T>[]): DurablePromise<PromiseSettledResult<T>[]>;
}
```

Perform standard Promise combinators and checkpoint the result.  Regular promise combinators can be non-deterministic on replay due to differing promise completion orders.  For example, if an invocation runs `Promise.race([A, B])` and promise `A` completes first, but when reinvoked for replay promise B completes first, the durable execution will likely fail due to non-determinism.  Durable promise combinators checkpoint their results so they will deterministically return the same result on replay.  See the [ctx.promise.all/allSettled/any/race - Durable Promise Combinators](https://quip-amazon.com/Pat3AbBBXPvZ#temp:C:SfN3d8edc39373a49f08c108f41a) section for details.

For example, this code will perform two steps asynchronously and then race them to completion.  The durable `promise.race` implementation guarantees that the promise that won the race the first time will be returned as the winner on replay as well.

```
const step1Promise = context.step(async () => await doStep1());
const step2Promise = context.step(async () => await doStep2());
const result = await context.promise.race(
    [step1Promise, step2Promise]);
// result will always be the promise that won the race the first time.
```


### Running Durable Operations Concurrently

```
runInChildContext<T>(fn: ChildFunc<T>, config?: ChildConfig<T>): DurablePromise<T>;
```

Run a function inside a child context so it can be run concurrently with other durable operations.  This is a low-level operation that customers will not normally need unless they need to use concurrency in ways that `context.parallel` and `context.map` don’t support.  See the [ctx.runInChildContext - Deterministic Id Generation](https://quip-amazon.com/Pat3AbBBXPvZ#temp:C:SfN11856bc7812644fb8136e4559) section for details.

Because of how the replay process works, it is not generally safe to run durable operations concurrently.  This is because differing concurrent completion orders on replay can make it impossible to correlate the operations being executed during replay with the ones that have been previously checkpointed.  To fix this problem, durable operations need to run in a child context if they will be executed concurrently with other durable operations.  This allows the durable runtime to replay concurrent steps reliably.

For example, this shows a function that runs multiple durable operations.  To safely run the function concurrently, the `doWork` calls are wrapped in `context.runInChildContext` calls.  There is no `await` on the `ctx.runInChildContext` calls, so each starts running immediately.  Each call passes a different `waitTime` that will cause `doWork` to wait a different amount of time between steps, but this wait only occurs the first time the wait executes.  On replay, the waits will have already completed, so they will not wait again, and this can cause a different ordering of subsequent operations relative to those running concurrently.  `runInChildContext` makes sure this reordering doesn’t affect the replay process.

```
async function doWork(ctx, waitTime) {
  const x = await ctx.step(async () => ...);
  await ctx.wait(waitTime);
  const y = await ctx.step(async () => ...);
  return x + y;
}

// In the Lambda handler
const s1 = ctx.runInChildContext(async (ctx) => doWork(ctx, 1000));
const s2 = ctx.runInChildContext(async (ctx) => doWork(ctx, 2000));
const res1 = await s1;
const res2 = await s2;
return {res1, res2};
```


### Optional / Future Features

#### Connectors

`context.invokeConnector` will take advantage of optimized Step Functions integrations.  Step Functions connectors provide additional functionality over standard APIs such as automatically fetching Bedrock `InvokeModel` inputs from S3, or starting a Batch or Glue job and waiting for it to complete.  

```
`invokeConnector``<``I``,``O``>(``resource``:`` ``Resource``<``I``,``O``>,`` input``:`` I``,`` config``?:` `InvokeConfig``):` Durable`Promise``<``O``>;`
```

### Distributed Map

```
distributedMap<T>(config: DistributedMapConfig): DurablePromise<T[]|DistributedMapResult>;
```

Use Step Functions distributed map capability to read a large input array from a source like S3, process them at high scale using another Lambda function, and write the results to a destination like S3.  This will support concurrency control, and failure behaviour.  See the [ctx.distributedMap (NOT for launch) - Run Distributed Durable Operations for Every Array Item](https://quip-amazon.com/Pat3AbBBXPvZ#temp:C:SfN4af2fb7ecaaf43cf8ee1c8abc) section for details.

## Examples

### GenAI Agent

This example shows the basic form of a durable GenAI agent.  The core of the agent is the agentic loop that invokes the AI model and executes a tool, as shown in this blog https://aws.amazon.com/blogs/opensource/introducing-strands-agents-an-open-source-ai-agents-sdk/.  This example uses `context.step` to checkpoint the result of each long-running `invokeModel` or `runTool` call to avoid repeating expensive work in case of failure.  This does not suspend the function while waiting for `invokeModel` and `runTool`, but could do so by using `waitForCallback` instead of `step` if they support a callback pattern.

```
// Get the initial prompt.
const messages = [{`"role"``:`` ``"user"``,`` ``"content"``:`` `event.prompt}];
  
while (true) {
  // Invoke the model
  const {response, reasoning, tool} = 
    await context.step(async () => await invokeModel(messages));
  
  if (tool == null) {
    return response;
  }
    
  // Run the tool
  const result = await context.step(async () => await runTool(tool, response));
    
  // Add the result to the messages to use next time
  messages.push(result);
}
```

### Agent Handoff

The agent handoff pattern allows an agent to delegate to another agent, allowing agents to work together.  Agents are typically modelled as tools, so agent handoff involves calling the tool to invoke the other agent, and then quitting.  An agent written as a durable execution can hand off to another agent simply by doing an async Lambda `Invoke` to start the other agent’s durable execution, and then quitting.

The code in the previous example runs a tool this way:

```
  // Run the tool
  const result = await context.step(async () => await runTool(tool, response));
```

To turn this into a handoff, the code simply needs to quit after calling the other agent, like this:

```
  // Run the tool
  const result = await context.step(async () => await runTool(tool, response));
  if (isAgentHandoffTool(tool)) {
    // The other agent has now been durably started, so this agent can return
    return;
  }    
```

The `runTool` function can invoke the other agent using a normal async Lambda invocation.

```
`const runTool = async (tool, response) => {
  // Prepare context and other agent input
  ...
  // Hand off to the other agent
  const`` client ``=`` ``new`` ``LambdaClient``({});
`  const command = new InvokeCommand({
    FunctionName: agentArn,
    InvocationType: InvocationType.EVENT,
    Payload: agentInput
  });
`  return await client.send(command);
}`
```

### GenAI Chat

This example shows how to use Bedrock’s Converse API: https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference-examples.html.  

To make this code durable, it needs to wrap each call to `generate_conversation` inside a step like this.  This will avoid rerunning each Bedrock `Converse` API after it has completed successfully.

```
    ...
    // Start the conversation with the 1st message.
    messages.push(message1);
    response = await ctx.step(async () => 
        await generateConversation(bedrockClient, modelId, systemPrompts, messages));

    // Add the response message to the conversation.
    outputMessage = response.output.message;
    messages.push(outputMessage);

    // Continue the conversation with the 2nd message.
    messages.push(message2);
    response = await ctx.step(async () =>
        await generateConversation(bedrockClient, modelId, systemPrompts, messages));

    outputMessage = response.output.message;
    messages.push(outputMessage);
    ...
```

### Fan-out/Fan-in

This example shows how to fan out the processing of an array of work items across many Lambda functions.  In this case, a single durable execution starts a separate Lambda invocation to process each item, and then waits for all the invocations to complete before aggregating the results.  This also illustrates Lambda chaining with `ctx.invoke` which calls another Lambda function and then waits for its result.

```
export const handler: DurableHandler<MyInput, string> = async (event: MyInput, context: DurableContext) => {
  const workItems = event.workItems;
  const tasks = [];
  
  // Fan out the tasks - run them in parallel, each one in a different Lambda function
  for (const workItem of workItems) {
    const task = ctx.invoke(processItemFunctionArn, workItem);
    tasks.push(task);
  }

  // Fan in - Aggregate the results
  const results = await ctx.promise.all(tasks);
  return aggregate(ctx, results);
}
```

To perform a higher-scale fan-out, the customer can use `ctx.map` or `ctx.distributedMap` like this:

```
export const handler: DurableHandler<MyInput, string> = async (event: MyInput, context: DurableContext) => {
  const workItems = event.workItems;
  
  // Fan out using DMAP
  const results = await ctx.distributedMap({
    items: workItems,
    itemProcessor: {
      resource: processItemFunctionArn
    },
    maxConcurrency: 1000
  });

  // Fan in - Aggregate the results
  return aggregate(ctx, results);
}
```

### Human In The Loop - Wait for Callback

In this example, the durable execution prepares an action using a genAI model, asks a human for approval, then performs the action once the approval is received.

```
export const handler: DurableHandler<MyInput, string> = async (event: MyInput, context: DurableContext) => {
  const todo = await context.step(async () => {
    return await invokeModelToDetermineNextAction(event);
  });
  
  // Sends an email with callback info so the approver can send the answer back
  const answer = await context.waitForCallback(
    // The email message will include a link to a webhook that calls
    // the SendDurableExecutionCallbackSuccess API with the person's answer
    async (callbackId) => await sendEmail(event.approverEmail, todo, callbackId)
  );
  
  if (answer === 'APPROVED') {
    await context.step(async () => await performAction(todo));
  } else {
    await context.step(async () => await recordRejected(todo, event.approverEmail));
  }
}
```

### Saga Pattern to Rollback on Error

This example is similar to one for Restate.  Before performing each step, a compensating action is pushed onto an array.  If any of the steps cannot be completed successfully, the `catch` block will run the compensations inside steps in reverse.

```
export const handler: DurableHandler<MyInput, string> = async (event: MyInput, context: DurableContext) => {
  const { customerId, flight, car, hotel } = event;
  // create a list of undo actions
  const compensations = [];

  try {
    // For each action, register a compensation that will be executed on failures
    compensations.push(() => flightClient.cancel(customerId));
    await context.step(async () => await flightClient.book(customerId, flight));

    compensations.push(() => carRentalClient.cancel(customerId));
    await context.step(async () => await carRentalClient.book(customerId, car));

    compensations.push(() => hotelClient.cancel(customerId));
    await context.step(async () => await hotelClient.book(customerId, hotel));
  } catch (e) {
    for (const compensation of compensations.reverse()) {
      await ctx.step(async () => await compensation());
    }
    throw e;
  }
}
```

## Details: Durable Operations and the DurableContext

This section describes all the capabilities of the DurableContext in detail.

The `DurableContext` contains functions the developer calls in their code to achieve durability.

```
interface DurablePromise<T> extends Promise<T> {}

/**
` * Durably run a block of code and persist its result so that it does`
` * not run to completion more than once (only will run more than once for`
` * retriable failures/results, subject to optional retry config)`
` *`
` * @param name - Optional name (improves readability in logs and execution details) `
` * @param fn - A function that performs some work and returns a promise `
` * @param config - Optional configuration for the code block execution`
` */`
step<T>(name: string, fn: StepFunc<T>, config?: StepConfig<T>): DurablePromise<T>;
step<T>(fn: StepFunc<T>, config?: StepConfig<T>): DurablePromise<T>;

/**
 * Invoke another durable function, returning a promise that will complete
 * when that invocation completes.
 *
 * Has built-in idempotency / at-most-once execution
 */
invoke<I,O>(name: string, funcId: FunctionIdentifier, input: I, config?: InvokeConfig): DurablePromise<O>;
invoke<I,O>(funcId: FunctionIdentifier, input: I, config?: InvokeConfig): DurablePromise<O>;

/**
` * Returns a Promise that will resolve after the specified number of seconds` 
` * has passed, unless it is cancelled.`
` *`
` * @param name - Optional name (improves readability in logs and execution details)`
` * @param seconds - Amount of time to wait in seconds`
` *`
` * @returns A promise that resolves when the duration has passed.`
` */`
`wait``(``name``:`` ``string``,` seconds`:`` number``):`` ``DurablePromise``<void>``;`
`wait``(``seconds``:`` number``):`` ``DurablePromise``<void>``;`

/**
 * Creates a callbackID and calls a submitter function with that ID
 * 
 * @param name - Optional name (improves readability in logs and execution details) 
 * @param submitter - A function that receives a callbackId and sends it to an
 *                    external component
 * @param config - Optional configuration for the code block execution 
 * 
 * @returns A promise that resolves/rejects with the result of the callback.
 * 
 * The submitter function will send the callbackId to another part of the 
 * application, which will then send a response using the 
 * SendDurableExecutionCallbackSuccess or SendDurableExecutionCallbackFailure APIs.
 * These APIs will cause the durable promise returned by waitForCallback to 
 * resolve/reject.
 */
waitForCallback<T>(name: string, submitter?: WaitForCallbackSubmitterFunc,
  config?: WaitForCallbackConfig): DurablePromise<T>;
waitForCallback<T>(submitter?: WaitForCallbackSubmitterFunc,
  config?: WaitForCallbackConfig): DurablePromise<T>;

/**
 * Waits until a condition is met.
 *
 * @param name - Optional name (improves readability in logs and execution details) 
 * @param check - A function that receives a state and returns a new state, used to
 *                determine a status for comparison
 * @param config - Configuration for the wait-for-condition operation
 * 
 * @returns A promise that resolves/rejects with the final state.
 */
waitForCondition<T>(name: string, check: WaitForConditionCheckFunc<T>,
    config?: WaitForConditionConfig<T>) : DurablePromise<T>;
waitForCondition<T>(check: WaitForConditionCheckFunc<T>,
    config?: WaitForConditionConfig<T>) : DurablePromise<T>;


interface BatchItem<R> {
  result?: R;
  error?: Error;
  index: number;
  status: 'SUCCEEDED' | 'FAILED' | 'STARTED';
}

interface BatchResult<R> {
  all: Array<BatchItem<R>>;

  // Filtered access methods
  succeeded(): Array<BatchItem<R> & { result: R }>;
  failed(): Array<BatchItem<R> & { error: Error }>;
  started(): Array<BatchItem<R> & { status: 'STARTED' }>;

  // Overall status
  status: 'SUCCESS' | 'FAILURE';
  completionReason: 'ALL_COMPLETED' | 'MIN_SUCCESSFUL_REACHED' | 'FAILURE_TOLERANCE_EXCEEDED';
  hasFailure: boolean;

  // Utility methods
  throwIfError(): void;
  getResults(): Array<R>;  // Extract successful results only
  getErrors(): Array<Error>;

  // Statistics
  successCount: number;
  failureCount: number;
  startedCount: number;
}

/**
 * Run durable promises in parallel, return a promise that will complete when
 * the parallel promises complete according to the completion config.
 * Has built-in configurable concurrency control.
 *
 * @param name - Optional name (improves readability in logs and execution details)
 * @param branches - An array of functions, each of which accepts a durable context
 *                   and returns a promise
 * @param config - Optional configuration for the parallel execution
 * 
 * @returns a promise that resolves/rejects when the parallel operation completes.
 */
parallel<T>(branches: ParallelFunc<T>[], config?: ParallelConfig): DurablePromise<BatchResult<T>>;
parallel<T>(name: string, branches: ParallelFunc<T>[], config?: ParallelConfig): DurablePromise<BatchResult<T>>;

/**
 * Run a durable promise concurrently across an input array.
 * Return a promise that completes when the map promises complete according to the
 * completion config.  Has built-in configurable concurrency control.
 *
 * @param name - Optional name (improves readability in logs and execution details)
 * @param items - Array of items to process by the mapFunc
 * @param mapFunc - A function that accepts a durable context and the item, index
 *                  and array, and returns a promise for the mapped item
 * @param config - Optional configuration for the map execution
 * 
 * @returns a promise that resolves/rejects when the map operation completes.
 */
map<T>(items: any[], mapFunc: MapFunc<T>, config?: MapConfig): DurablePromise<BatchResult<T>>;
map<T>(name: string, items: any[], mapFunc: MapFunc<T>, config?: MapConfig): DurablePromise<BatchResult<T>>;

/**
 * Promise combinators for durable execution
 */
promise: {
  /**
   * Durably executes Promise.race() with tracked steps for each promise.
   * Returns the first resolved value among the promises.
   * 
   * @param name - Name for the race operation
   * @param promises - Array of promises to race
   * @returns A promise that resolves with the first fulfilled promise's value
   */
  race<T>(name: string, promises: DurablePromise<T>[]): DurablePromise<T>;
  race<T>(promises: DurablePromise<T>[]): DurablePromise<T>;

  /**
   * Durably executes Promise.any() with tracked steps for each promise.
   * Returns the first successfully resolved value, ignoring rejections.
   * Throws AggregateError if all promises reject.
   * 
   * @param name - Name for the any operation
   * @param promises - Array of promises to execute
   * @returns A promise that resolves with the first fulfilled promise's value
   */
  any<T>(name: string, promises: DurablePromise<T>[]): DurablePromise<T>;
  any<T>(promises: DurablePromise<T>[]): DurablePromise<T>;

  /**
   * Durably executes Promise.all() with tracked steps for each promise.
   * Ensures all promises complete successfully.
   * 
   * @param name - Name for the all operation
   * @param promises - Array of promises to execute
   * @returns A promise that resolves with an array of all fulfilled values
   */
  all<T>(name: string, promises: DurablePromise<T>[]): DurablePromise<T[]>;
  all<T>(promises: Promise<T>[]): DurablePromise<T[]>;

  /**
   * Durably executes Promise.allSettled() with tracked steps for each promise.
   * Tracks completion of all promises regardless of their outcome.
   * 
   * @param name - Name for the allSettled operation
   * @param promises - Array of promises to execute
   * @returns A promise that resolves with an array of promise results
   */
  allSettled<T>(name: string, promises: DurablePromise<T>[]): DurablePromise<PromiseSettledResult<T>[]>;
  allSettled<T>(promises: DurablePromise<T>[]): DurablePromise<PromiseSettledResult<T>[]>;
}

/**
 * Creates a new context for executing code blocks with deterministic ID generation. 
 * This is useful for handling nested asynchronous operations and ensuring 
 * consistent checkpoint IDs 
 * 
 * @param name - Optional name (improves readability in logs and execution details) 
 * @param fn - A function that receives a child context and returns a promise 
 * @param config - Optional configuration for the code block execution 
 * 
 * @returns A promise that resolves with the result of the executed code block 
 * 
 * Note: Inside the function passed to block, 
 * the childCtx must be used instead of the outer ctx. 
 * This ensures proper ID generation and context isolation. 
 */
runInChildContext<T>(name: string, fn: ChildFunc<T>, config?: ChildConfig<T>): DurablePromise<T>;
runInChildContext<T>(fn: ChildFunc<T>, config?: ChildConfig<T>): DurablePromise<T>;

// ======= Optional / Future features =======

/**
 * Invoke an AWS service connector.
 *
 * Basically Step Functions task state.
 */
invokeConnector<I,O>(name: string, resource: Resource<I,O>, input: I, config?: InvokeConfig): DurablePromise<O>;
invokeConnector<I,O>(resource: Resource<I,O>, input: I, config?: InvokeConfig): DurablePromise<O>;

/**
 * Run a durable promise concurrently, distributed across many child executions,
 * across an input array possibly provided by an external data source.
 * Return a promise that completes when all the child executions complete
 * according to the completion config.
 * Has built-in configurable concurrency control.
 *
 * @param name - Optional name (improves readability in logs and execution details)
 * @param config - Configuration that specifies where to read the input array from,
 *                 what Lambda function to use to process the items, and other
 *                 configuration information such as batching and error handling.
 * 
 * @returns a promise that resolves/rejects when the map operation completes.
 */
distributedMap<T>(name: string, config: DistributedMapConfig): DurablePromise<T[]|DistributedMapResult>;
distributedMap<T>(config: DistributedMapConfig): DurablePromise<T[]|DistributedMapResult>;

`// Function types`
`type ``StepFunc``<``T``>`` ``=`` ``async`` ``()`` ``=>`` ``Promise``<``T``>;`
`type ``ChildFunc``<``T``>`` ``=`` ``async`` ``(``ctx``:`` ``DurableContext``)`` ``=>`` ``DurablePromise``<``T``>;`
`type ``ParallelFunc``<``T``>`` ``=`` ``async`` ``(``ctx``:`` ``DurableContext``)`` ``=>`` ``DurablePromise``<``T``>;`
`type ``MapFunc``<``U``,``V``>`` ``=`` ``async`` ``(``ctx``:`` ``DurableContext``,`` item``:`` U``,`` index``?:`` number``,`` array``?:`` ``[``U``])`` `
`    ``=>`` ``DurablePromise``<``V``>;`
type WaitForCallbackSubmitterFunc = async (callbackId: CallbackID) => void;
`type ``WaitForConditionCheckFunc``<``T``>`` ``=`` ``async`` ``(``state``:`` T``)`` ``=>`` T``;`
`type ``WaitForConditionWaitStrategyFunc``<``T``>`` ``=`` ``(``state``:`` T``,`` attempt``:`` number``)`` ``=>`` ``Retry``Decision``;`
type RetryStrategyFunc = (error: Error, attempt: number) => RetryDecision

// Other types
export type RetryDecision = 
    | { shouldRetry: true; delaySeconds: number } 
    | { shouldRetry: false };

export enum StepSemantics {
  AtLeastOncePerRetry = 'AT_LEAST_ONCE_PER_RETRY',
  AtMostOncePerRetry = 'AT_MOST_ONCE_PER_RETRY';
}

export interface StepConfig {
  retryStrategy: (error: Error, attempt: number) => RetryDecision;
  stepSemantics: StepSemantics,
  serdes: Serdes
}

export interface InvokeConfig {
  retryStrategy: (error: Error, attempt: number) => RetryDecision;
  serdes: Serdes
}

export interface ChildConfig {
  serdes: Serdes
}

export interface ParallelConfig {
  maxConcurrency: number,
  completionConfig: {
    minSuccessful: number, // default MAX_INT
    toleratedFailureCount: number, // default 0
    toleratedFailurePercentage: number
  },
  serdes: Serdes
}

export interface MapConfig {  
  maxConcurrency: number,
  completionConfig: {
    minSuccessful: number,
    toleratedFailureCount: number,
    toleratedFailurePercentage: number
  },
  serdes: Serdes
}

export interface WaitForConditionConfig {
  waitStrategy: WaitForConditionWaitStrategyFunc<T>,
  initialState: T,
  serdes: Serdes
}

export interface WaitForCallbackConfig {
  timeout: number, //seconds
  heartbeatTimeout: number, //seconds
  retryStrategy: RetryStrategyFunc,
  serdes: Serdes
}

interface SerdesContext {
  entityId: string;
  durableExecutionArn: string;
}

// Can be added to any Config
interface Serdes<T> {
  serialize: (value: T | undefined, context?: SerdesContext) => Promise<string | undefined>;
  deserialize: (data: string | undefined, context?: SerdesContext) => Promise<T | undefined>;
}
```

### ctx.step - Running Checkpointed Steps

#### **Signature (TypeScript)**

```
`step``<``T``>(``name``:`` ``string``,`` fn``:`` ``StepFunc``<``T``>,`` config``?:`` ``StepConfig``<``T``>):`` ``DurablePromise``<``T``>;`
`step``<``T``>(``fn``:`` ``StepFunc``<``T``>,`` config``?:`` ``StepConfig``<``T``>):`` ``DurablePromise``<``T``>;`

`type ``StepFunc``<``T``>`` ``=`` ``()`` ``=>`` ``DurablePromise``<``T``>;`
`type ``RetryStrategyFunc`` ``=`` ``(``error``:`` ``Error``,`` attempt``:`` number``)`` ``=>`` ``RetryDecision`

`export`` ``interface`` ``StepConfig`` ``{`
`  retryStrategy``:`` ``RetryStrategyFunc`
`  stepSemantics``:`` ``StepSemantics,
  serdes: Serdes`
`}`

`export`` type ``RetryDecision`` ``=`` `
`    ``|`` ``{`` shouldRetry``:`` ``true``;`` delaySeconds``:`` number ``}`` `
`    ``|`` ``{`` shouldRetry``:`` ``false`` ``};`

`export`` ``enum`` ``StepSemantics`` ``{`
`  ``AtLeastOncePerRetry`` ``=`` ``'AT_LEAST_ONCE_PER_RETRY'``,`
`  ``AtMostOncePerRetry`` ``=`` ``'AT_MOST_ONCE_PER_RETRY'``;`
`};`
```

The developer can define a “step” in a durable function using the `DurableContext`’s `step` method.  The developer passes the step’s code block to the “`step`" function, along with an optional name and an optional configuration.  `step` runs the code block and then checkpoints its result.  Once successfully checkpointed, `step` will not run the code block again on replay and will instead fetch its previously-checkpointed result.

By default, `step` provides at-least-once semantics (i.e., at least once per retry).  This means the step’s result is only checkpointed when it completes.  If the customer’s code block starts but then fails before completing (for example, with a hardware failure), then the step will run again on replay regardless of the retry strategy, repeating any partially-completed work in the code block.  This is because `step` can’t tell whether or not the code block started on a previous replay.

Optionally, the developer can ask `step` to enforce at-most-once semantics by providing a `StepOptions` parameter.  To provide at-most-once semantics, `step` checkpoints its progress twice, once just before the code block starts, and once when the code block is complete.  If the customer’s code block starts and then fails before completing, the step will only run again according to the retry strategy because the replay process can see that it already started.

#### Step Name

The name parameter is optional and doesn't affect checkpointing or replay functionality. If provided, the name is used for observability and operational purposes. When a name isn't specified, the system will use the developer-defined function name (StepFunc). However, if the function is an anonymous arrow function and no name is provided, the step name will remain unset.

**Example**

```
// name will remain unset
const result1 = await context.step(async () => {
  return callMyApi(event.value);
});

// name will be set to `my step name`
const result1 = await context.step("my step name", async () => {
  return callMyApi(event.value);
});

// name will be set to `myStepFunction`
const result1 = await context.step(
    async function myStepFunction() {
        return callMyApi(event.value);
});

// name will be set to `my step name`
const result1 = await context.step("my step name",
    async function myStepFunction() {
        return callMyApi(event.value);
});
```

#### **Usage**

At-least-once semantics are achieved by default without providing any options.  The `context.step` method will checkpoint the code block’s result after is completes.

```
const result1 = await context.step("my step name", async () => {
  return callMyApi(event.value);
});
```

#### The Customer-Provided Step Function

```
type StepFunc<T> = () => Promise<T>;
```

The developer will provide a function for the code block that runs when the step runs.  

#### Step Configs

Developers can configure step behaviour with the optional `StepConfigs` parameter.  Developers can configure retries, and step semantics.

#### Retries

Each step may fail by throwing an exception. The developer can configure how to retry steps that fail with exceptions by adding a retryStrategy to context.step's StepConfigs argument. The retry strategy is a function that determines whether to retry and how long to wait before the next attempt.

The developer can configure how to retry steps in many different ways.

**Default Retry**
When no retryStrategy is defined for step, a default retryStrategy (with unlimited retries) will be used.

```
const result = await context.step(
  async () => return callMyApi(event.value),
);
```

**Built-in Retry Presets**
The library provides predefined retry presets for common scenarios:

```
const result = await context.step(
  async () => return callMyApi(event.value),
  {
    retryStrategy: retryPresets.resourceAvailability
  }
);
```

**createRetryStrategy helper function**
To simplify retry configuration, developers can use the createRetryStrategy helper function which provides a flexible way to create custom retry strategies.

```
const customRetryStrategy = createRetryStrategy({
  maxAttempts: 5,
  initialDelaySeconds: 1,
  maxDelaySeconds: 60,
  backoffRate: 2,
  jitterSeconds: 0.5,
  retryableErrors: ['Intentional failure', /Network error/, 'ServiceUnavailable'],
  retryableErrorTypes: [NetworkError, TimeoutError]
});

const result = await context.step(
  "my_step",
  async () => {
    return callMyApi(event.value);
  },
  {
    retryStrategy: customRetryStrategy
  }
);
```

**Custom retryStrategy**
For advance use cases, developers can write their own `retryStrategy` function.  For example:

```
const myCustomStrategy = (error: Error, attempt: number) => {
  const maxAttempts = 5;
  if (attempt >= maxAttempts) {
    return { shouldRetry: false };
  } else {
    return { 
      shouldRetry: true, 
      delaySeconds: 1 + attempt 
    };
  }
};

const result = await context.step(
  async () => callMyApi(event.value),
  {
    retryStrategy: myCustomStrategy
  }
);
```

#### Step Semantics

The developer can specify the `stepSemantics` step option with the value `'AT_MOST_ONCE_PER_RETRY'` or `'AT_LEAST_ONCE_PER_RETRY'`.  Steps run with `AT_LEAST_ONCE_PER_RETRY` semantics by default.  With `AT_MOST_ONCE_PER_RETRY` semantics, the `context.step` method will first persist that it is about to run the code block, then run the code block, and finally persist its result after it completes.  With `AT_LEAST_ONCE_PER_RETRY` semantics, `context.step` will only persist the result after the code block completes.

**Usage**
The developer can override the default at-least-once semantics by adding a 

```
const result1 = await context.step("my step name", async () => {
  return callMyApi(event.value);
}, {
  stepSemantics: StepSemantics.AtMostOncePerRetry
});
```

#### Handling Interrupted Steps

When using steps with different semantics (at-most-once and at-least-once), it’s important to understand how Language SDK handles interruptions during step execution. This section describes the behavior when a step is interrupted before completion.

**Interrupted Steps with at-most-once Semantics**

When a step with at-most-once semantics is interrupted after the initial checkpoint but before completion (i.e., there is a STARTED status but no COMPLETED or FAILED status), the system will:


1. Detect the interrupted execution when the workflow resumes
2. Generate a StepInterruptedError (Suggest a better name!) with information about the interrupted step
3. Pass this error to the step’s retry strategy (if provided) or the default retry strategy
4. Based on the retry decision:

* If retry is recommended, schedule a retry with the specified delay
* If retry is not recommended, mark the step as failed and throw the StepInterruptedError

This ensures that steps with at-most-once semantics are never executed more than once per retry attempt, even in the case of interruptions.

**Interrupted Steps with at-least-once Semantics**

When a step with at-least-once semantics is interrupted, the system will:

1. Re-execute the step function when the workflow resumes
2. Create a new checkpoint upon successful completion

This behavior is consistent with the at-least-once guarantee, ensuring that the step is executed at least once, even if it means potential re-execution.

### ctx.invoke: Invoke a Durable Function and Wait for its Result

#### Signature (TypeScript)

```
`invoke``<``I``,``O``>(``funcId``:`` ``FunctionIdentifier``,`` input``:`` I``, ``config``?:`` ``InvokeConfig``):`` ``DurablePromise``<``O``>;`
```

The developer can invoke another Durable Lambda function and wait for its result using the `context.invoke` function.  `context.invoke` will invoke the durable function identified by the `funcId` and then suspend the invoker to wait for the result.  `ctx.invoke` guarantees idempotency with 'at-most-once' semantics, which means it will start a single durable execution if possible.  The other durable execution will run and then send its result back to the caller, at which point the caller will be reinvoked.

The customer can provide a retry strategy in the `config` the same way they work for `ctx.step`.

#### Usage

```
const result = await context.invoke("arn:aws:lambda:::function:doSomething", payload);
```

### ctx.wait: Waiting for a Period of Time

#### Signature (TypeScript)

```
`wait``(``name``:`` ``string``,` seconds`:`` number``):`` ``DurablePromise``<void>``;`
`wait``(seconds``:`` number``):`` ``DurablePromise``<void>``;`
```

The developer can durably wait for a specified period of time in seconds using the `context.wait` function.  `context.wait` will persist the wait period and set a durable timer before terminating the invocation.  After the timer fires, the durable function will be reinvoked for replay.  

For short delays, the developer can use the runtime’s built-in wait and they can check-point it by wrapping  it with `ctx.step`

#### **Usage**

```
**`await`**` context.`**`wait`**`("wait for 30 seconds", 30);`
```

### ctx.waitForCallback - Wait Until a Callback Occurs (Structured)

#### Signature (TypeScript)

```
waitForCallback<T>(name: string, submitter?: WaitForCallbackSubmitterFunc,
  config?: WaitForCallbackConfig): DurablePromise<T>;
waitForCallback<T>(submitter?: WaitForCallbackSubmitterFunc,
  config?: WaitForCallbackConfig): DurablePromise<T>;

type WaitForCallbackSubmitterFunc = async (callbackId: CallbackID) => void;

export interface WaitForCallbackConfig {
  timeout: number, // seconds
  heartbeatTimeout: number, // seconds
  retryStrategy: RetryStrategyFunc,
  serdes: Serdes
}
```

`ctx.waitForCallback` accepts a submitter function, creates a new `callbackId`, calls the submitter function with the `callbackId`, and then returns a promise that will complete when the callback completes with the `SendDurableExecutionCallbackSuccess` or `SendDurableExecutionCallbackFailure` APIs.

#### Sample Implementation for Illustration

`ctx.waitForCallback` could be implemented this way, for illustration:

```
const waitForCallback = async <T>(submitter: async (callbackId: CallbackID) => void) => {
  const [callbackPromise, callbackId]
      = await createAndCheckpointANewCallbackIdAndPromise();
  await submitter(callbackId);
  return callbackPromise;
}
```

#### Example: Structured Wait

This example uses `waitForCallback` in a structured way, where the external component can send heartbeats to keep the callback task alive.

```
const result = await cxt.waitForCallback(
  async (callbackId) => await TriggerTask(event, callbackId),
  {
    timeout: 300,
    heartbeatTimeout: 15
  }
);
```

#### Configuration

Developers can configure `waitForCallback` behaviour with the optional WaitForCallback`Config` parameter.  Developers can configure retries and timeouts.

Signature:

```
export interface WaitForCallbackConfig {
  timeout: number, // seconds
  heartbeatTimeout: number, // seconds
  retryStrategy: RetryStrategyFunc,
  serdes: Serdes
}
```

The `timeout` configuration specifies the maximum number of seconds to wait for the callback to complete.  The default is no timeout if this field is omitted. If the callback doesn’t complete within this time, `waitForCallback` will throw an exception.

The `heartbeatTimeout` configuration specifies the maximum number of seconds to wait between heartbeats.  The default is no timeout if this field is omitted, in which case no heartbeat timeouts will occur.

The `retryStrategy` will be used whenever the `WaitForCallbackFunc` throws an exception, or if the callback completes with a failure using `SendDurableExecutionCallbackFailure`, or if the callback times out.  The `retryStrategy` is exactly the same as for `ctx.step`, and behaves the same way.

#### No Nesting

Since `ctx.waitForCallback` behaves similar to `ctx.step` in how it calls the `submitter` function, durable operations cannot be nested inside `ctx.waitForCallback`.  In other words, the following is not allowed.

```
const result = await ctx.waitForCallback(async callbackId => {
  // Cannot call `ctx.xyz` operations inside the `ctx.waitForCallback` function
  await ctx.step(() => await SendMessageToSQS(event, callbackId));
})
```

#### Heartbeating

An external async process may take a long time to complete, but it may also fail part way through.  `ctx.waitForCallback` accepts a `timeout` parameter to set the maximum amount of time before the callback expires.  The external process must call `SendDurableExecutionCallbackSuccess` or `SendDurableExecutionCallbackFailure` within this time, otherwise the `waitForCallback` promise will fail with a timeout exception.

But the external process may fail long before the timeout, in which case the durable function would only learn of its fate after the timeout expires.  For example, if the timeout is one day but the external process dies only 1 hour into its task, the durable execution wouldn’t find out for another 23 hours.  To prevent this delay, durable executions support callback heartbeats.  `ctx.waitForCallback` also accepts a `heartbeatTimeout` parameter to set the maximum amount of time between heartbeats.  The external process must call the `SendDurableExecutionCallbackHeartbeat` API at least that often to avoid a timeout.

The durable function can, for example, set a `timeout` of one day, and a `heartbeatTimeout` of 30 minutes.  The external process can be configured to call `SendDurableExecutionCallbackHeartbeat` every 20 minutes to prevent the heartbeat timer expiring.  In this case, if the external process fails, the durable execution’s `ctx.waitForCallback` call will timeout with a heartbeat timeout exception within 30 minutes of the external process failure.

```
// This will fail with a heartbeat timeout exception if the async process
// fails to call SendDurableExecutionCallbackHeartbeat at least every 30 minutes.
// If the async process continues to heartbeat at least every 30 minutes but
// takes more than a day to complete, this will fail with a timeout exception
// after 24 hours.
const result = await context.waitForCallback(
  "do async stuff",
  async callbackId => await InitiateAsyncWork(event, callbackId),
  {timeout: 24*60*60, heartbeatTimeout: 30*60}
);
```

#### Completing the Callback

The customer’s application can complete the callback by calling the `SendDurableExecutionCallbackSuccess` API to indicate a successful result, or the `SendDurableExecutionCallbackFailure` API to indicate a failure result.

The developer will first call `context.waitForCallback` to generate the `callbackId`.  The developer will pass a submitter function to `context.waitForCallback` which will call the external API (in this case SQS) and pass it the `callbackId`.  Another part of the application will poll the SQS queue, perform the required action, and then complete the promise by calling the `SendDurableExecutionCallbackSuccess(result, callbackId)` Lambda API.

```
// Don't await until later in this case
const callbackPromise = context.waitForCallback<SqsInfo>(
  // will add callbackId to the message
  async callbackId => await SendMessageToSQS(event, callbackId),
  {timeout: ONE_DAY}
);

// This will complete when the external component calls 
// SendDurableExecutionCallbackSuccess()
const result = await callbackPromise;
```

**Complete the Callback via Webhook**
The developer will first call `context.waitForCallback` to generate the `callbackId`.  The developer will pass a submitter function to `context.waitForCallback` which will call the external API (in this case Stripe) and pass it the `callbackId`.  The code to create the `callbackId` and wait for its result is similar to the previous example.

```
// This will complete when the webhook lambda calls the
// SendDurableExecutionCallbackSuccess API (see below)
const result = await context.waitForCallback<StripeCallbackInfo>(
  async callbackId => await CallStripe(event, callbackId),
  {timeout: ONE_DAY}
);
```

When finished, Stripe will complete the callback by calling a webhook the developer provides to the Stripe API call. The developer can create the webhook using API Gateway and set it up to call another function, which will send the callback result by calling `SendDurableExecutionCallbackSuccess` or `SendDurableExecutionCallbackFailure` like this.

```
export const handler: Handler<StripeCallbackInfo, void> 
    = async (event: StripeCallbackInfo, context: Context) => {
  const client = new LambdaClient({});
  let command;
  if (event.success) {
    command = new SendDurableExecutionCallbackSuccessCommand(
        { CallbackId: event.callbackId, CallbackResult: event });
  } else {
    command = new SendDurableExecutionCallbackFailureCommand(
        { CallbackId: event.callbackId, CallbackResult: event });
  }
  return client.send(command);
};
```

### ctx.createCallback - Create a CallbackId and Promise (Unstructured)

#### Signature (TypeScript)

```
createCallback<T>(name: string, config?: CreateCallbackConfig)
  : DurablePromise<CreateCallbackResult<T>>;
createCallback<T>(config?: CreateCallbackConfig)
  : DurablePromise<CreateCallbackResult<T>>;

export interface CreateCallbackConfig {
  timeout: number, //seconds
  heartbeatTimeout: number, //seconds
  serdes: Serdes
}

type CreateCallbackResult<T> = [DurablePromise<T>, string]
```

`ctx.createCallback` creates a new `callbackId` and returns the `callbackId` and a promise that will complete when the callback completes with the `SendDurableExecutionCallbackSuccess` or `SendDurableExecutionCallbackFailure` APIs.

#### Sample Implementation for Illustration

`ctx.createCallback` could be implemented this way, for illustration:

```
const createCallback = async <T>() => {
  const [callbackPromise, callbackId]
      = await createAndCheckpointANewCallbackIdAndPromise();
  return [callbackPromise, callbackId];
}
```

#### Example: Send the CallbackId to an External Task

This example shows how the customer can use `createCallback` to generate a `callbackId`, but then send it to an external component from another place in the code.

```
// Don't provide a submitter.
const [callbackPromise, callbackId] = await cxt.createCallback();

// In another part of the code, send the callbackId to an external component
await ctx.step(async () => await TriggerTask(event, callbackId));

// Wait until the callback returns a result
const result = await callbackPromise;
```

#### Example: Poll For Request Status

This example uses `createCallback` to trigger a task with a `callbackId`.  The `TriggerTask` function returns a `taskId which` allows the `waitForCondition` checker function to call `checkTask` with the `taskId`.

```
const [callbackPromise, callbackId] = await ctx.createCallback();

const taskId = await ctx.step(async () => await TriggerTask(event, callbackId));
 
// Use the taskId to periodically check to see if the task is still alive
const statusPromise = ctx.waitForCondition("check task",
  async () => await checkTask(taskId),
  myCustomPollingStrategy
); 
 
// first promise to complete unblocks the await call 
const result = await ctx.promise.race([callbackPromise, statusPromise])
```

#### Example: Cancel an In-Progress Callback Task

This example starts a callback task, and then sometime later cancels it using the `callbackId`.

```
const [callbackPromise, callbackId] = await cxt.createCallback();

await ctx.step(async () => await TriggerTask(event, callbackId));

// In another part of the durable function, a condition may require cancelling
// the callback task.  It can do it like this.
if (cancelCallback) {
  ctx.step(async () => await CancelTask(callbackId);
}

let result = await callbackPromise;
```

#### Configuration and Completion

Heartbeating, timeouts, and completing the callback work the same as with `waitForCallback`.

### ctx.waitForCondition - Wait Until a Given Condition is Met

#### Signature (TypeScript)

```
waitForCondition<T>(name: string, check: WaitForConditionCheckFunc<T>,
    config?: WaitForConditionConfig<T>) : DurablePromise<T>;
waitForCondition<T>(check: WaitForConditionCheckFunc<T>,
    config?: WaitForConditionConfig<T>) : DurablePromise<T>;

type WaitForConditionCheckFunc<T> = async (state: T) => T;
type WaitForConditionWaitStrategyFunc<T> = (state: T, attempt: number) => RetryDecision;

export interface WaitForConditionConfig {
  waitStrategy: WaitForConditionWaitStrategyFunc<T>,
  initialState: T,
  serdes: Serdes
}
```

To support waiting on arbitrary conditions without paying to wait, we’ll add a new `ctx.waitForCondition` function.  This will also be used to support durable SDK waiters.

The customer will provide a `check` function that accepts a state and returns an updated state.  The `check` function will typically perform some action to query a status, such as calling a `DescribeXxx` API.  The result will be checkpointed and also passed to the `waitStrategy` function.

The `config` will contain a `waitStrategy` function that accepts the result of the `check` function and the attempt #, and determines whether the condition has been met, along with the length of time to wait before checking again if the condition hasn’t yet been met.

The first time `waitForCondition` calls the `check` function, it will pass it the optional `initialState` value from the `config`.

`waitForCondition` continues calling the `check` function as long as the `waitStrategy` function says to delay and check again.  Each time `waitStrategy` says to continue, `waitForCondition` will set a durable timer for the requested number of delay seconds.  This will allow the Lambda function to terminate and then be reinvoked when the timer fires so it doesn’t have to pay to wait.  This will also allow long wait periods, such as might be necessary when waiting for a new CloudFormation stack to become active.

NOTE: the reason the `check` function accepts a `state` value is to support durable SDK waiters which return data about all attempts.  However, we could choose not to support that feature of durable waiters, in which case the `check` function could accept no arguments i.e. `type WaitForConditionCheckFunc<T> = async () => T`.

#### **createWaitStrategy helper function**

To simplify wait strategy configuration, developers can use the `createWaitStrategy` helper function, which provides a flexible way to create custom wait strategies.

```
const customWaitStrategy = createWaitStrategy({
  maxAttempts: 60,
  initialDelaySeconds: 5,
  maxDelaySeconds: 30,
  backoffRate: 1.5,
  jitterSeconds: 1,
`  shouldContinuePolling``:`` ``(``result``)`` ``=>`` result``.``status ``!==`` ``'CURRENT'``,`` `
  timeoutSeconds: 600
});

const result = await context.waitForCondition(
  // In this example, the `state` parameter is unused.
  // The function doesn't have to use the `state` parameter if not needed.
  async (state) => describeStackInstance(stackId),
  customWaitStrategy
);
```

#### **Custom waitStrategy**

For advanced use cases, developers can write their own `waitStrategy` function.  For example:

```
const myCustomWaitStrategy = (result: StackInstance, attempt: number) => {
  const maxAttempts = 60;
  if (result.status === 'CURRENT') {
    return { shouldContinue: false };
  } else if (attempt > maxAttempts) {
    throw "Exceeded max attempts";
  } else {
    return { 
      shouldContinue: true, 
      delaySeconds: Math.min(5 * attempt, 30)
    };
  }
};

const result = await context.waitForCondition(
  // In this example, the `state` parameter is unused.
  // The function doesn't have to use the `state` parameter if not needed.
  async (state) => describeStackInstance(stackId),
  myCustomWaitStrategy
);
```

#### Accumulating Attempt Data

Since the `check` function accepts and returns a state value, it can accumulate and return information about the attempts.  For example:

```
const { result, attemptInfo } = await context.waitForCondition(
  // In this example, the function uses the `state` parameter to accumulate
  // data about the attempts.
  async (state) => {
    const result = checkResourceReadiness(resourceId);
    const attemptInfo = state.attemptInfo.push(result);
    return { result, attemptInfo };
  },
  (state, attempt) => {
    shouldContinue: state.result === 'READY',
    delaySeconds: calculateBackoff(attempt)
  },
  { 'NOT READY', [] }
);
```

If `checkResourceReadiness` successively returns `'INITIALIZING'`, `'ACTIVATING'`, and `'READY'`, then the final result from `waitForCondition` will be: `{ 'READY', ['INITIALIZING', 'ACTIVATING', 'READY'] }`.

#### Durable SDK Waiters

The AWS SDK support “waiters” which are able to wait until a specific condition is met.  For example, to wait until a specific DynamoDB table exists, the customer can do this:

```
await waitUntilTableExists(
  { client, minDelay: 5, maxDelay: 30, maxWaitTime: 825 }, 
  { tableName }
);
```

With the `waitForCondition` operation, we can work with the SDK team to refactor the SDK waiters to accept a ‘polling strategy’ (see https://sim.amazon.com/issues/SDK-221954).  The default polling strategy would loop and `sleep` just as waiters do today.  But we would also supply a ‘durable polling strategy’ that uses `waitForCondition` to do the looping and waiting.  For example, we can supply this durable version of `waitUntilTableExists`:

```
`import`` ``{`` `
`  ``DynamoDBClient``,`` ``DescribeTableCommandInput``,`` 
  waitUntilTableExists ``as`` defaultWaitUntilTableExists`
`}`` ``from`` ``"@aws-sdk/client-dynamodb"``;`

// Durable version
export const waitUntilTableExists = async (
  context: DurableContext,
  params: WaiterConfiguration<DynamoDBClient>,
  input: DescribeTableCommandInput
): Promise<WaiterResult> => {
  // Inject a durable polling strategy to use instead of the default
  return defaultWaitUntilTableExists(
    { pollingStrategy: createDurablePollingStrategy(context), ...params }, 
    input);
};
```

We would provide a durable version of every SDK waiter in the same way.  `createDurablePollingStrategy` would call `waitUntilCondition` to perform a durable version of the polling loop that SDK waiters do.

This way, customers will import the durable version of the waiter they want, and then call it the same way they would call the regular version (possibly also having to pass a `DurableContext` as shown below).  Note we need to determine how we'll package durable waiters so customers can import them individually when needed and so durable waiters have access to the DurableContext.

```
import { 
  DynamoDBClient, CreateTableCommand
} from "@aws-sdk/client-dynamodb";
// To be decided - how to package the durable versions
import { waitUntilTableExists } from "durable-waiters";

export const handler: DurableHandler<MyInput, string> = async (event: MyInput, context: DurableContext) => {
  const TableName = "TABLE_NAME";
  const client = new DynamoDBClient({ region: "REGION" });
  const command = new CreateTableCommand({ TableName });

  await context.step(async () => await client.send(command));
  await waitUntilTableExists(context, { client, maxWaitTime: 60 }, { TableName });
} 
```

#### Option: Generic Durable Waiter

As an option, we can supply a generic wrapper function as part of the `DurableContext` that accepts an SDK waiter, like this.  The advantage is that we only have to vend a single function that will work with all waiters including future ones.  The disadvantage is that customers code is not as clean-looking as when using a regular waiter.

```
export const sdkWaiter<Client, Input> = async (
  waiter: (params: WaiterConfiguration<Client>, input: Input) => Promise<WaiterResult>
  params: WaiterConfiguration<Client>,
  input: Input
): Promise<WaiterResult> => {
  // Inject a durable poller to use instead of the default
  return waiter(
    { pollingStrategy: createDurablePollingStrategy(context), ...params }, 
    input);
};
```

The customer would use it like this:

```
`import`` ``{`` `
`  S3Client``,`` ``CreateBucketCommand``,`` waitUntilBucketExists `
`}`` ``from`` ``"@aws-sdk/client-s3"``;`

export const handler: DurableHandler<MyInput, string> = async (event: MyInput, context: DurableContext) => {
  const Bucket = "BUCKET_NAME";
  const client = new S3Client({ region: "REGION" });
  const command = new CreateBucketCommand({ Bucket });

  await context.step(async () => client.send(command));
  await context.sdkWaiter(waitUntilBucketExists, {client, maxWaitTime: 60}, {Bucket});
}
```

### ctx.parallel - Run Multiple Operations in Parallel

#### Signature (TypeScript)

```
parallel<T>(name: string, branches: ParallelFunc<T>[], config?: ParallelConfig): DurablePromise<T[]>;
parallel<T>(branches: ParallelFunc<T>[], config?: ParallelConfig): DurablePromise<T[]>;

export interface ParallelConfig {
  maxConcurrency: number,
  completionConfig: {
    minSuccessful: number, // default MAX_INT
    toleratedFailureCount: number, // default 0
    toleratedFailurePercentage: number
  },
  serdes: Serdes
}
```

Step Functions’ `Parallel` state accepts one or more sub-workflow branches, runs all in parallel with the same input, and waits for all to complete.  If any branch fails, the `Parallel` fails.  This is called “[task parallelism](https://en.wikipedia.org/wiki/Task_parallelism)” where different tasks are all executed in parallel on the same input.

`ctx.parallel` accepts an array of child functions, one per branch, where each function accepts a single context argument and can run one or more durable operations.  `ctx.parallel` wraps each child function in `ctx.runInChildContext` so it runs in its own durable context.  For example:

```
let results = await ctx.parallel([
    async ctx => await task1(ctx, ...), 
    async ctx => await task2(ctx, ...),
    ...
  ]
);
```

`ctx.parallel` calls the child functions in parallel, and awaits their results according to the configuration.  It wraps each function inside a `ctx.runInChildContext` call so each one runs with its own durable context.  On completion, it will create a completion checkpoint to record the order of completion.  `ctx.parallel` will mark each abandoned child/grandchild operation, if any, so they can not do any more durable operations.

```
ctx.parallel(childFns: (ctx => DurablePromise<any>)[],
  {
    // Similar to MaxConcurrency in Step Functions Map state
    maxConcurrency: number,
  
    completionConfig: {
      // Number of successful iterations to wait for before returning
      minSuccessful: number, // default MAX_INT
      // Same as ToleratedFailureCount in Step Functions
      toleratedFailureCount: number, // default 0
      // Same as ToleratedFailurePercentage in Step Functions
      toleratedFailurePercentage: number
    }
  }
)
```

Configuration:

* `maxConcurrency`: Run up to this many child functions concurrently, awaiting their results
* `completionConfig`: The conditions for when the `ctx.parallel` completes
    * `minSuccessful`: Number of successful child functions to wait for before returning
    * `toleratedFailureCount`: Number of child functions failures to tolerate before throwing 
    * `toleratedFailurePercentage`: Percentage of child function failures to tolerate before throwing 

We will define constants for common values of the `completionConfig` option:

* `ctx.FIRST_COMPLETED` = `Promise.race` semantics: `{ minSuccessful:1, toleratedFailureCount:0 }`
* `ctx.FIRST_SUCCESSFUL` = `Promise.any` semantics, `{ minSuccessful:1, toleratedFailureCount:MAX_INT }`
* `ctx.ALL_COMPLETED` = `Promise.allSettled` semantics, `{ minSuccessful:MAX_INT, toleratedFailureCount:MAX_INT }`
* `ctx.ALL_SUCCESSFUL` = `Promise.all` semantics, `{ minSuccessful:MAX_INT, toleratedFailureCount:0 }`

Defaults:

* `maxConcurrency = MAX_INT`: run as many child functions concurrently as possible
* `completionConfig = ALL_SUCCESSFUL`: `Promise.all` semantics: `{ minSuccessful:MAX_INT, toleratedFailureCount:0 }`

Example:

```
const task1 = async ctx => {
  await ctx.step(async () => await step1());
  await ctx.wait(2);
  await ctx.step(async () => await step2());
};

// This will complete when the external component calls the SendDurableExecutionCallbackSuccess API
const task2 = async ctx => {
  return await ctx.waitForCallback(async callbackId => {
    await SendMessageToSQS(event, callbackId); // will add callbackId to message
  })
};

// Now run task1 and task2 concurrently and then wait for their results
await ctx.parallel([task1, task2],
  {
    completionConfig: ctx.ALL_SUCCESSFUL
  }
);
```

In this example, the `ctx.parallel` call is equivalent to this:

```
Promise.all([ctx.runInChildContext(task1), ctx.runInChildContext(task2)])
```

### ctx.map - Run Durable Operations for Every Array Item

#### Signature (TypeScript)

```
map<T>(name: string, items: any[], mapFunc: MapFunc<T>, config?: MapConfig) : DurablePromise<T[]>;
map<T>(items: any[], mapFunc: MapFunc<T>, config?: MapConfig) : DurablePromise<T[]>;

export interface MapConfig {   
  maxConcurrency: number,
  completionConfig: {
    minSuccessful: number,
    toleratedFailureCount: number,
    toleratedFailurePercentage: number
  },
  serdes: Serdes
}
```

Step Functions’ `Map` state accepts a single sub-workflow and runs it concurrently, once per item in the input array. It then waits for all iterations to complete. If any iteration fails, the `Map` fails. This is called “[data parallelism](https://en.wikipedia.org/wiki/Data_parallelism)” where different pieces of data are processed in parallel by the same function.

The `ctx.map` construct accepts an input array and a child function.  The function accepts a single context argument and can run one or more durable operations.  `ctx.map` iterates over the input array, wraps the function in `ctx.runInChildContext`, runs it on each array item, awaits their results according to the configuration.  On completion, it will create a completion checkpoint to record the order of completion.  `ctx.map` will mark each abandoned child/grandchild operation, if any, so they can not do any more durable operations.

For example:

```
// Basic usage example
const result = await context.map(items, async (ctx, item) => processItem(item));

// Access successful results (backward compatibility)
const successfulResults = result.getResults();

// Check for failures
if (result.hasFailure) {
  console.log(`${result.failureCount} items failed`);
  result.getErrors().forEach(error => console.error(error));
}

// With completion config
const result2 = await context.map(items,
  async (ctx, item) => processItem(item),
  {
    maxConcurrency: 5,
    completionConfig: {
      minSuccessful: 3,
      toleratedFailureCount: 2
    }
  }
);

console.log(`Completed with reason: ${result2.completionReason}`);
console.log(`Success: ${result2.successCount}, Failed: ${result2.failureCount}, Started: ${result2.startedCount}`);
```

This would be equivalent to the following.

```
let results = [];
for (let i = 0; i < inputItems.length; ++i) {
  results.push(async ctx => await itemProcessor(ctx, inputItems[i], i, inputItems));
}

await ctx.parallel(results,
  {
    completionConfig: ctx.ALL_SUCCESSFUL
  }
);
```

`ctx.map` will accept the same configuration `ctx.parallel` accepts to control concurrency and completion.  
For example:

```
let results = await ctx.map(inputItems,
  async (ctx, item, i, array) => await task(ctx, item, i, array),
  { 
    // Same as MaxConcurrency in Step Functions Map state
    maxConcurrency: 3,
  
    completionConfig: {
      // Number of successful iterations to wait for before returning
      minSuccessful: 1,
      // Same as ToleratedFailureCount in Step Functions
      toleratedFailureCount: 0,
      // Same as ToleratedFailurePercentage in Step Functions
      toleratedFailurePercentage: 30
    }
  }
);
```

### ctx.promise.all/allSettled/any/race - Durable Promise Combinators

JavaScript has combinators on `Promise` that allow multiple promises to be waited on:

* `Promise.all` - This returned promise fulfills when all of the input's promises fulfill (including when an empty iterable is passed), with an array of the fulfillment values. It rejects when any of the input's promises rejects, with this first rejection reason.
* `Promise.allSettled` - This returned promise fulfills when all of the input's promises settle (including when an empty iterable is passed), with an array of objects that describe the outcome of each promise.
* `Promise.any` - This returned promise fulfills when any of the input's promises fulfills, with this first fulfillment value. It rejects when all of the input's promises reject (including when an empty iterable is passed), with an AggregateError containing an array of rejection reasons.
* `Promise.race` - This returned promise settles with the eventual state of the first promise that settles.

With the exception of `allSettled`, the result of these promises depends on the order in which the input promises complete. Without any changes, the order of promise completion may be different on replay vs initial run (as during replay some promises will immediately be complete).

To address this issue, the customer needs to use durable promise combinators.

These methods provide analogues to TypeScript’s `Promise.all` and related functions.  These versions behave similarly to TypeScript’s versions except they accept and return `DurablePromise`s instead of regular TypeScript `Promise` promises, and they checkpoint the promise completion order.  These functions will mark each abandoned child/grandchild operation, if any, so they can not do any more durable operations.

`ctx.promise.any(promises: DurablePromise<T>[]): DurablePromise<T>`
is equivalent to: `Promise.any(promises)`

`ctx.promise.all(promises: DurablePromise<any>[]): DurablePromise<T[]>`
is equivalent to: `Promise.all(promises)`

`ctx.promise.race(promises: DurablePromise<any>[]): DurablePromise<T>`
is equivalent to: `Promise.race(promises)`

`ctx.promise.allSettled(promises: DurablePromise<any>[]): DurablePromise<T[]>`
is equivalent to: `Promise.allSettled(promises)`

Example

```
const race_return = await ctx.promise.race("Promise race block", [p1, p2]);
```

### ctx.runInChildContext - Deterministic Id Generation

#### **Signature (TypeScript)**

```
runInChildContext<T>(name: string, fn: ChildFunc<T>, config?: ChildConfig<T>): DurablePromise<T>;
runInChildContext<T>(fn: BlockFunc<T>, config?: ChildConfig<T>): DurablePromise<T>;

// Currently there are no config options
export interface ChildConfig {
  serdes: Serdes
}
```

When async code is initiated serially like:

```
const foo = await ctx.step(async () => ...);
await ctx.wait(10000);
const bar = await ctx.step(async () => ...);
...
```

There are no issues with generating ids, every time we encounter a checkpointed entity we can increment a counter.

Issues arise when async code is initiated from other async code like:

```
async function doSomething(ctx, waitTime) {
  const foo = await ctx.step(async () => ...);
  await ctx.wait(waitTime);
  const bar = await ctx.step(async () => ...);
}

const foo = doSomething(ctx, 1000);
const bar = doSomething(ctx, 2000);
await foo;
await bar;
```

Due to timing differences in the various `ctx.step` blocks, on subsequent replays, the code inside each of the `doSomething` functions may run in a different interleaving than they did on the original run through.

We can solve this by explicitly modelling asynchrony by introducing `ctx.runInChildContext` that is *only* used for initiating async functions that may also need to use the context. `ctx.runInChildContext` looks like

```
let result = ctx.runInChildContext(async (childCtx) => { ... });
```

`ctx.runInChildContext` accepts a child function and calls it with a new durable context that deterministically creates durable operation IDs for child operations even when started concurrently.  A “child operation” is a durable operation started inside the child function.  `ctx.runInChildContext` checkpoints the start and its result to guarantee deterministic behaviour on replay.  In this way, `ctx.runInChildContext` is similar to `ctx.step` except that `ctx.runInChildContext` generates and passes a new durable context to the function, and it does not support retry or polling configurations.  On completion, `ctx.runInChildContext` will mark each abandoned child/grandchild operation, if any, so they can not do any more durable operations.

#### Rules for Using ctx.`runInChildContext`

1. All durable operations must be started sequentially inside a single `ctx.runInChildContext` unless wrapped in a nested `ctx.runInChildContext` call.
2. To run durable operations concurrently, enclose each set of operations in its own `ctx.runInChildContext` and then run the `ctx.runInChildContext` functions concurrently.
3. Inside the function passed to `ctx.runInChildContext` the `childCtx` *must* be used, and not the outer `ctx` (NOTE: not sure this is enforceable at runtime, but could be linted)

The above broken example becomes

```
async function doSomething(ctx, waitTime) {
  const foo = await ctx.step(async () => ...);
  await ctx.wait(waitTime);
  const bar = await ctx.step(async () => ...);
}

const foo = ctx.runInChildContext(async (childCtx) => doSomething(childCtx, 1000));
const bar = ctx.runInChildContext(async (childCtx) => doSomething(childCtx, 2000));
await foo;
await bar;
```

And now this works because the generated checkpoint ids will be something deterministic like  `1.1, 1.2, 1.3, 2.1, 2.2, 2.3` instead of `1, 2, 3, 4, 5, 6` 

`runInChildContext` can be indefinitely nested and will just add another level to the id and reset the counter (e.g. `1.1.1.1.1.2`  for the second checkpointed item in a 5-nested `ctx.runInChildContext`)

## Optional / Future Features

### ctx.invokeConnector (NOT for launch) - Invoke a Step Functions-style Optimized Integration

#### Signature (TypeScript)

```
`invokeConnector``<``I``,``O``>(``resource``:`` ``Resource``<``I``,``O``>,`` input``:`` I``,`` config``?:` `InvokeConfig``):` Durable`Promise``<``O``>;`
```

`context.invokeConnector` will take advantage of optimized Step Functions integrations.  Step Functions connectors provide additional functionality over standard APIs such as automatically fetching Bedrock `InvokeModel` inputs from S3, or starting a Batch or Glue job and waiting for it to complete.  

### ctx.distributedMap (NOT for launch) - Run Distributed Durable Operations for Every Array Item

#### Signature (TypeScript)

```
distributedMap<T>(name: string, config: DistributedMapConfig): DurablePromise<T[]|DistributedMapResult>;
distributedMap<T>(config: DistributedMapConfig): DurablePromise<T[]|DistributedMapResult>;

export interface DistributedMapConfig {
  // Provide items array directly
  items: inputItems,
  
  // Where to read the items from.  This will support the same sources
  // as with Step Functions.
  itemReader: {
    readerConfig: {
      maxItems: number,
      inputType: string,
      // etc.
    },
    resource: "arn",
    arguments: {
    }
  },
  
  // Do we need ItemSelector functionality?  The item processor will be a Lambda
  // function, which means the customer can choose which parts of the input
  // they need, but our DMap implementation (autobahn) restricts the input size
  // to 256KB.  We should modify autobahn to pass up to 6MB in this case.
  itemSelector: {}, // avoid
  
  // Whether and how to batch items.  Same features as Step Functions.
  itemBatcher: {
    maxItemsPerBatch: number,
    maxItemBytesPerBatch: number,
    batchInput: any
  },
  
  // The items / batches will be processed by another Lambda function.
  // This function does not have to be durable.  We will modify the autobahn
  // service to support this directly.
  itemProcessor: {
    resource: "Lambda ARN",
  },
  
  // Where to write the results.  Same features as Step Functions
  resultWriter: {
    resource: "arn",
    arguments: {
    }
  },
  
  // Same as MaxConcurrency in Step Functions
  maxConcurrency: number,
  
  completionConfig: {
    // Number of successful iterations to wait for before returning
    minSuccessful: number,
    // Same as ToleratedFailureCount in Step Functions
    toleratedFailureCount: number,
    // Same as ToleratedFailurePercentage in Step Functions
    toleratedFailurePercentage: number
  },
  
  // What to do with incomplete iterations when the completion criteria are satisfied
  terminationConfig: "TERMINATE|CANCEL|WAIT|ABANDON",
  serdes: Serdes
);
```

This is a variation of `ctx.map` that can run at high scale.  Each iteration is executed as a separate Lambda invocation (can be durable or not).  In addition to the `ctx.map` configuration, this also adds SFN-style configurations.  This will run as an “autobahn” maprun.

NOTE: we intend `ctx.distributedMap` to effectively be an extension of `ctx.map`, so customers can make the change by changing `map` to `distributedMap`, and then adding some additional configuration including `items` and `itemProcessor.resource` plus others if desired.  All `ctx.map` configurations should work as they do in `ctx.map` (if at all possible).

## Best Practice with Replay Behaviour

### Deterministic Replay

All non-deterministic code should be wrapped with `ctx.step`  to guarantee the result will be exactly the same in replay.
That means that developers can use `ctx.step`  around creating UUID, random number, current date and time, or API calls.

**Example**

```
// Generating UUID v4
const id = await ctx.step("Generate UUID", async () => {
  return uuid.v4();
});

// Random number between 0 and 1
const randomNum = await ctx.step("Generate random", async () => {
  return Math.random();
});

// Current timestamp
const timestamp = await ctx.step("Get timestamp", async () => {
  return Date.now();
});
```

### Serialization

By default, the TypeScript SDK uses the built-in JSON support to perform (de)serialization, but it is possible to override this behavior using the Serdes (`serialization` and `deserialization`) interface.

#### The Serdes Interface

The `Serdes` interface allows you to define custom serialization and deserialization methods for your data types. This is particularly useful for complex objects, custom types, or when you need to maintain type information during the serialization process.

Signature (TypeScript):

```
interface Serdes<T> {
  serialize: (value: T) => string;
  deserialize: (data: string) => T;
}
```

#### Using Serdes with Steps, Promise, ...

Any entities that checkpoint data can use Serdes, that includes Steps, Promise, ...
To use a custom Serdes with a step, you can pass it as part of the StepConfig:

```
const result = await ctx.step<Order>(
    "Process order",
    async () => {
        const result = await processOrder(orderId);
        return result;
    },
    { serdes: orderSerdes }
);
```

#### Creating a Serdes

Here's an example of creating a Serdes for an Order class:

```
const orderSerde: Serdes<Order> = {
    serialize: (order) => {
        // Custom serialization logic
        const clone = {...order};
        clone.createdAt = order.createdAt.toISOString();
        return JSON.stringify(clone);
    },
    deserialize: (data) => {
        // Custom deserialization logic
        const parsed = JSON.parse(data);
        const order = new Order();
        Object.assign(order, parsed);
        order.createdAt = new Date(parsed.createdAt);
        return order;
    }
};
```

#### Helper Function for Simple Serdes

For simple cases where you just need to maintain the class type, you can use a helper function:

```
function createClassSerdes<T>(cls: new () => T): Serdes<T> {
    return {
        serialize: async (value: T | undefined, context?: SerdesContext) =>
            value !== undefined ? JSON.stringify(value) : undefined,
        deserialize: async (data: string | undefined, context?: SerdesContext) =>
            data !== undefined ? Object.assign(new cls(), JSON.parse(data)) : undefined
    };
}

// Usage
const orderSerdes = createClassSerdes(Order);
```

#### Serdes for a class with special handling for Date properties

```
/**
 * Creates a Serdes for a class with special handling for Date properties
 * @param cls The class constructor
 * @param dateProps Array of property names that should be converted to Date objects
 * @returns A Serdes that maintains the class type and converts specified properties to Date objects
 */
export function createClassSerdesWithDates<T extends object>(
  cls: new () => T,
  dateProps: string[],
): Serdes<T>;

// Usage
class Order {
  id: string;
  amount: number;
  createdAt: Date;
  updatedAt: Date;

  constructor() {
    this.id = '';
    this.amount = 0;
    this.createdAt = new Date();
    this.updatedAt = new Date();
  }

  getFormattedDate(): string {
    return this.createdAt.toISOString();
  }
}

// Create serdes that handles Date properties correctly
const orderSerdes = createClassSerdesWithDates(Order, ['createdAt', 'updatedAt']);

const result = await ctx.step("process-order", async () => {
  const order = new Order();
  order.id = "12345";
  order.amount = 99.99;
  return order;
}, { serdes: orderSerdes });

```

When working with classes that contain Date properties, use createClassSerdesWithDates instead of createClassSerdes. This ensures that Date properties are properly converted from ISO strings back to Date objects during deserialization, maintaining the correct data types and allowing Date methods to work properly.

#### External Storage Example with Serdes

```
/**
 * Example: S3-based Serdes for large payloads
 */
function createS3Serdes<T>(bucketName: string, sizeThreshold: number = 256 * 1024): Serdes<T> {
  return {
    serialize: async (value: T | undefined, context?: SerdesContext) => {
      if (value === undefined) return undefined;

      const jsonData = JSON.stringify(value);
      if (jsonData.length <= sizeThreshold) {
        return jsonData; // Store inline for small payloads
      }

      // Store large payloads in S3 using context IDs
      const s3Key = `${context.durableExecutionArn}/${context.entityId}`;
      await s3Client.putObject({ Bucket: bucketName, Key: s3Key, Body: jsonData });

      return JSON.stringify({ __s3Pointer: true, bucket: bucketName, key: s3Key });
    },
    deserialize: async (data: string | undefined, context?: SerdesContext) => {
      if (data === undefined) return undefined;

      const parsed = JSON.parse(data);
      if (parsed.__s3Pointer) {
        const response = await s3Client.getObject({ Bucket: parsed.bucket, Key: parsed.key });
        return JSON.parse(await response.Body.transformToString());
      }
      return parsed;
    }
  };
}
```

### Logging Best Practices in DurableContext

While it's possible to create helper methods like ctx.log.info() that wrap logging in ctx.step, this is not recommended for performance reasons. Each wrapped log would:

* Create a separate checkpoint in the execution history
* Add latency to the original execution
* Increase replay time
* Consume limits

Instead, the recommended pattern is to include logging within your existing ctx.step calls:

**Anti-Pattern:**

```
// Don't do this - each log creates a separate checkpoint

// checkpoint #1
await ctx.step(()=> console.log("Starting order processing"));  

// checkpoint #2
const result = await ctx.step("Process order", async () => {
  const result = await processOrder(orderId);
  return result;
}); 

// checkpoint #3
await ctx.step(()=> console.log("Order processed successfully")); 
```


**Good Practice:**

```
// Logging is part of the business logic step
await ctx.step("Process order", async () => {
  console.log("Starting order processing");
  const result = await processOrder(orderId);
  console.log("Order processed successfully", { orderId });
  return result;
});
```

## Other Topics

### IAM Block Durable Functions

Needs investigation, but we probably need the ability to let customers block durable function capabilities using IAM.

Check with other Lambda features.

### IAM: Reinvoke after 15 minutes

As Durable Functions is Lambda, we shouldn’t be limited by the 15 minute timeout, so customers shouldn’t have to provide any additional permissions to allow Durable Functions to invoke repeatedly for replay for long periods of time.

Check with AppSec.

### Is Replaying?

Customers can avoid performing certain actions like logging or tracing on every replay by wrapping these in a step.  This way, once the step completes successfully, the actions won’t be executed again.

We will not provide a `ctx.isReplaying()` function that would allow developers to check if replay is occurring at any given point in the code.

### Programming Language Support

Developers will initially be able to write durable function code using TypeScript/JavaScript and Python.  We intend to support Java after that, followed by other programming languages as customer demand justifies.

### X-Ray

Developers can enable X-Ray tracing on their durable executions.  We do not plan to support OTel at launch, but we will ensure X-Ray support is forward-compatible with OTel.

## Tooling Changes

Describe details of all tooling changes, such as to CDK, AWS Toolkit, CloudFormation, SAM, validation.

## Usage Examples

Provide usage examples to clarify how we expect users to use the new APIs features

## Engineering Context

Add any additional context the engineering team needs to know related to any technical observations or constraints that influenced the proposed solution during working backwards.  For example, public facing performance or technical limitations that will shape the technical implementation details of the system.

## Feedback and Decisions

List all feedback given during reviews, and indicate what decisions were made based on this feedback.

* [16 April 2025 Review Feedback](https://quip-amazon.com/kmNxAslQZchT/16-April-2025-Review-Feedback)
* [27 June 2025 Review Feedback](https://quip-amazon.com/k3dAAytyULhs)


