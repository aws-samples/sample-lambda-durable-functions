# Understanding Durable Functions

Lambda durable functions is a feature that allows you to build long-running, fault-tolerant workflows and applications using familiar programming languages on Lambda (Java, TypeScript, and Python). With durable functions, you can write code that persists state, handles failures, and manages long-running operations efficiently, all while maintaining the simplicity and scalability of Lambda. Lambda functions are designed to be stateless and ephemeral, lasting only for the duration of a single invocation. In contrast, with Durable Functions it's simpler to handle:

* Complex, reliable applications that require state persistence;
* Multi-step applications that need long-running operations;
* Processes that must maintain state across multiple invocations.

The interactive sample project for AWS Lambda durable functions is an internal development environment that allows you to experiment with and understand how durable functions will work. While the actual feature is still in development, this sample project provides an environment where you can test and explore the concepts and capabilities of durable functions.

In this sample project, you write your business logic using new methods like 'Steps' and 'Promises', allowing Lambda to automatically checkpoint progress, handle failures, and manage resources efficiently when waiting for extended periods. This approach simplifies the development of complex, stateful applications without requiring additional services or infrastructure management.

**What you can do with Lambda durable functions** Lambda durable functions are ideal for scenarios that require long-running, resilient processes that can persist state and recover from failures. For example, you can use durable functions for:

1. Order  fulfillment: Coordinate multiple steps in an order process, including  inventory checks, payment processing, and shipping, with built-in error  handling and state management.
2. AI/ML  workflows: Orchestrate complex AI/ML pipelines, including data  preprocessing, model training, and result aggregation, with the ability to  pause and resume long-running tasks.
3. Business  process automation: Implement multi-step approval workflows, document  processing, or customer onboarding processes that can span hours or days.
4. Payment  processing: Build reliable payment systems with automatic retries,  idempotency, and transaction consistency across multiple services.
5. Internet of Things (IoT)  device management: Manage long-running device provisioning, firmware  updates, and telemetry processing with durable state management.
6. Multi-step  API orchestration: Coordinate calls to multiple APIs with built-in error  handling, retries, and state persistence.

**How Lambda durable functions work**
When working with standard AWS Lambda functions, your code runs from start to finish in a single invocation. If a failure occurs at any point during the execution, the entire function must start over from the beginning. Any state that needs to be preserved between executions must be explicitly saved and retrieved, typically by using external storage services like Amazon DynamoDB or Amazon S3.

In contrast, Lambda Durable Functions introduces a unique *replay model* that changes how your functions behave. With durable functions, your code still appears to run from start to finish, but behind the scenes, the function leverages a sophisticated replay mechanism to maintain progress and handle failures.
Here's how the durable functions replay model works:

1. First Execution: When you first invoke your Durable Function, it runs through each step of your code from top to bottom. At each `ctx.step()` call, a checkpoint is created, and the state of the function is automatically preserved.
2. Handling Failures: If a failure occurs at any point during the execution, instead of starting the function over from the beginning, durable functions "replays" the function from the last successful checkpoint. Only the steps that haven't completed successfully are actually re-executed, while previously successful steps retrieve their previously checkpointed results without re-running the underlying logic.
3. Resuming Execution: After a failure, when the function is retried, it will resume execution from the last successful checkpoint. The state of the function is automatically restored, and execution continues from where it left off, without requiring any explicit state management or persistence.


For example, consider this simple workflow:


```
export const handler = async (event, ctx) => {
  // Step 1: Process Order
  await ctx.step("process-order", async () => {
    // First execution: This code runs
    // Later replays: This step is replayed but not re-executed
    return { orderId: "123" };
  });

  // Step 2: Charge Payment
  await ctx.step("charge-payment", async () => {
    // If this step fails...
    // On retry: Execution starts here
    chargeCustomer();
  });

  // Step 3: Send Confirmation
  await ctx.step("send-confirmation", async () => {
    // We only reach this after all previous steps succeed
    sendEmail();
  });
};
```

This replay model is central to how durable functions enables long-running, fault-tolerant workflows and applications. By automatically maintaining progress and handling failures, durable functions simplifies the development of complex, stateful applications that would traditionally require significant custom logic and infrastructure.

In addition to the replay model, Durable Functions provide several other key capabilities:

You write your code using familiar programming languages like JavaScript, TypeScript, and Python, incorporating new methods like 'Steps' and 'Promises' to define workflow logic. Lambda automatically manages state persistence, allowing your function to maintain progress even if interrupted. You can suspend execution for long periods (up to one year) using 'Wait' and 'Promises' methods, optimizing costs by charging only for active compute time. durable functions integrates seamlessly with existing Lambda features and event sources, allowing you to build complex, stateful applications within the Lambda ecosystem.

By leveraging this unique execution model and set of capabilities, durable functions empowers you to build reliable, long-running applications without the traditional complexities of distributed systems development.

**Key features**
Build reliable, long-running applications:

* Automatic  checkpointing and state management ensure progress is maintained across  failures or interruptions.
* Built-in  idempotency guarantees for reliable execution of business logic.
* Efficient  handling of long-running operations with automatic suspension and  resumption.

Simplify development of complex workflows:

* Write  workflow logic using familiar programming constructs in your preferred  language.
* Seamless  integration with existing Lambda event sources and AWS services.
* Local  testing and debugging capabilities for improved developer experience.

Optimize performance and cost:

* Pay  only for active compute time, even for workflows that run for days or  months.
* Automatic  scaling and resource management inherited from Lambda.
* Efficient  handling of concurrent executions and parallel processing.

Enhance observability and control:

* Detailed  execution insights available through the Lambda console.
* Integration  with AWS monitoring and logging services for comprehensive observability.
* Fine-grained  control over execution, including the ability to cancel or modify running  workflows.


Next steps: TBD, will be filled in shortly
