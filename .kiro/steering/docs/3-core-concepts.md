#  Key concepts and capabilities of durable functions

## Key Concepts

**Checkpointing**: The process of saving the current state of execution to persistent storage, so the function can resume from that point if interrupted.

**Durable Promises**: An extension of the standard Promise paradigm that adds persistence capabilities. Unlike regular Promises that exist only in memory, Durable Promises maintain their state in persistent storage, allowing them to survive system restarts and failures.

**Core durable functions methods:** Durable Functions introduces two main methods that bring extra capabilities to your Lambdas: `step()`, `wait()`. Think of `step()` as placing bookmarks in your code that save your progress, and `wait()` as setting a timer that lets your function pause and resume without wasting resources. Let's look at how each one works:

* `step()`: Is a checkpoint. This is like placing a bookmark in your code. It saves the progress of your function.  
* `wait()`: Pauses your function for a specific amount of time, without using up resources or costing money during the waiting period.

**Additional durable functions methods:** Durable Functions also introduces the `waitForCallback()`, `waitForCondition()`, `parallel()`, and `map()` methods to simplify more complex control flow.

* `waitForCallback()`: Pauses your function until an external signal comes back to your Durable Lambda Function via the `SendDurableExecutionCallback[Success/Failure]` API.
* `parallel()`: Runs multiple durable promises in parallel, checkpointing the results of each step as they complete.
* `map()`: Iterates over an input and runs a durable promise for each entry.
* `waitForCondition()`: Pauses your function until a condition (e.g, the status of a job polled with an API) is met. 

**Replay**: The mechanism by which Durable Functions reconstruct their state after an interruption happens (that is, how durable functions recover from interruptions). If something goes wrong, the function goes back to the beginning and runs all the function code again, substituting in previously checkpointed values for any Durable Promise (e.g., `step()`) that has already been executed. 
**This means that any code not wrapped in a Durable Promise (such as `step()`) will be re-run every time your Durable Execution replays**. 

## **Lambda durable functions compared to Lambda functions**

**Durable function configuration:** Developers can configure Lambda functions as "Durable" during creation.

**Enhanced context object:** New features in the Context object allow users to define steps, durable waiters and durable promises. Developers can wait for external events without paying for idle time.

**Checkpointing and replay**: When a timer fires or an external event is received, the function is invoked again
Previous state is restored by replaying from checkpointed state.

**Long-running, multi-step executions**: Asynchronous invocations can run up to one year. Each step is persisted and retried in case of failures.


## **Out of Scope**

* We will not change the 15-minute behavior (invocation timeout).
* We will not take a snapshot of the entire micro-VM. Customers choose what  state they want to checkpoint.

## **Tenets**

1. We prioritize developer productivity without sacrificing system reliability.
2. We prioritize an idiomatic programming model that feels natural and familiar  to developers.
3. We optimize for fast iteration cycles during development and testing.
4. We ensure local development environments accurately reflect production to  minimize surprises.
5. We favor convention over configuration to reduce the amount of decisions the  developer has to make upfront.

