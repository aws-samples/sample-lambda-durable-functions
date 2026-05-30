import {
  BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
} from "@aws-sdk/client-bedrock-agentcore";
import {
  DurableContext,
  withDurableExecution,
} from "@aws/durable-execution-sdk-js";

const agentRuntimeArn = process.env.AGENT_RUNTIME_ARN!;
const region = process.env.AGENT_REGION || "us-east-1";
const functionArn = process.env.AWS_LAMBDA_FUNCTION_NAME || "";

const client = new BedrockAgentCoreClient({ region });

interface TaskInput {
  repo: string;
  task_description: string;
  github_token?: string;
  model_id?: string;
  max_turns?: number;
  branch_name?: string;
  output_bucket?: string;
}

interface TaskResult {
  status: "completed" | "failed" | "timeout";
  task_id: string;
  pr_url?: string;
  s3_uri?: string;
  error?: string;
}

/**
 * Durable Function orchestrator using the callback pattern.
 *
 * Flow:
 *   1. Validate input
 *   2. waitForCallback → starts AgentCore session (passes callback ID to agent)
 *      → suspends with ZERO compute cost until agent calls SendDurableExecutionCallbackSuccess
 *   3. Finalize and return result
 */
export const handler = withDurableExecution(
  async (event: TaskInput, context: DurableContext): Promise<TaskResult> => {
    const taskId = `task-${Date.now()}`;

    // Step 1: Validate input
    const config = await context.step("validate", async () => {
      if (!event.repo || !event.task_description) {
        throw new Error("Missing required fields: repo, task_description");
      }
      return {
        repo: event.repo,
        task_description: event.task_description,
        github_token: event.github_token || "",
        model_id: event.model_id || "us.anthropic.claude-sonnet-4-6",
        max_turns: event.max_turns || 50,
        branch_name: event.branch_name || `agent/${taskId}`,
        output_bucket: event.output_bucket || "",
      };
    });

    context.logger.info("Task started", { taskId, repo: config.repo });

    // Step 2: Wait for agent to complete via callback
    // The orchestrator suspends here (no compute charges) until the agent
    // calls SendDurableExecutionCallbackSuccess with the result.
    const agentResult = await context.waitForCallback<string>(
      "agent-execution",
      async (callbackId) => {
        // Start AgentCore session, passing the callback ID so the agent
        // can signal completion directly to the durable execution backend.
        const payload = JSON.stringify({
          input: {
            repo_url: `https://github.com/${config.repo}.git`,
            task_description: config.task_description,
            github_token: config.github_token,
            model_id: config.model_id,
            max_turns: config.max_turns,
            branch_name: config.branch_name,
            task_id: taskId,
            output_bucket: config.output_bucket,
            callback_id: callbackId,
            function_name: functionArn,
          },
        });

        const command = new InvokeAgentRuntimeCommand({
          agentRuntimeArn,
          qualifier: "DEFAULT",
          payload: Buffer.from(payload, "utf-8"),
          contentType: "application/json",
          accept: "application/json",
        });

        await client.send(command);
        context.logger.info("AgentCore session started, waiting for callback", { callbackId, taskId });
      },
      { timeout: { hours: 8 } }
    );

    // Step 3: Finalize — parse the agent's callback payload
    return context.step("finalize", async () => {
      try {
        const result = JSON.parse(agentResult);
        if (result.error) {
          return { status: "failed" as const, task_id: taskId, error: result.error };
        }
        return {
          status: "completed" as const,
          task_id: taskId,
          pr_url: result.pr_url,
          s3_uri: result.s3_uri,
        };
      } catch {
        return { status: "completed" as const, task_id: taskId, pr_url: agentResult };
      }
    });
  }
);
