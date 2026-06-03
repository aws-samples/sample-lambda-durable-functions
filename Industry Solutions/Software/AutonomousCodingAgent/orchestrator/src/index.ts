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
const githubTokenSecretArn = process.env.GITHUB_TOKEN_SECRET_ARN || "";

const agentCoreClient = new BedrockAgentCoreClient({ region });

interface TaskInput {
  repo: string;
  task_description: string;
  github_token?: string;
  model_id?: string;
  max_turns?: number;
  branch_name?: string;
  output_bucket?: string;
}

interface AgentResult {
  status: string;
  task_id: string;
  branch_name?: string;
  files?: string[];
  s3_uri?: string;
  error?: string;
}

interface TaskResult {
  status: "completed" | "failed";
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
 *   2. waitForCallback → AgentCore generates code, commits, pushes branch
 *      → suspends at ZERO cost until agent sends callback
 *   3. Create GitHub PR (orchestrator owns this step)
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

    // Step 2: Wait for agent to finish code generation + push
    const agentResult = await context.waitForCallback<string>(
      "agent-execution",
      async (callbackId) => {
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

        await agentCoreClient.send(command);
        context.logger.info("AgentCore session started, waiting for callback", { callbackId, taskId });
      },
      { timeout: { hours: 8 } }
    );

    // Step 3: Parse agent result
    const parsed: AgentResult = await context.step("parse-agent-result", async () => {
      return JSON.parse(agentResult);
    });

    if (parsed.error) {
      return { status: "failed", task_id: taskId, error: parsed.error };
    }

    // If S3 output mode, we're done (no PR needed)
    if (parsed.s3_uri) {
      return { status: "completed", task_id: taskId, s3_uri: parsed.s3_uri };
    }

    // Step 4: Create GitHub PR (orchestrator handles this)
    const prUrl = await context.step("create-pull-request", async () => {
      const token = config.github_token || await getGithubToken();
      const [owner, repo] = config.repo.split("/");

      const response = await fetch(
        `https://api.github.com/repos/${owner}/${repo}/pulls`,
        {
          method: "POST",
          headers: {
            Authorization: `token ${token}`,
            Accept: "application/vnd.github.v3+json",
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            title: `feat: ${config.task_description.slice(0, 72)}`,
            body: `Automated PR created by coding agent.\n\n**Task:** ${config.task_description}\n\n**Files changed:** ${(parsed.files || []).join(", ")}`,
            head: parsed.branch_name || config.branch_name,
            base: "main",
          }),
        }
      );

      if (!response.ok) {
        const err = await response.text();
        throw new Error(`GitHub PR creation failed (${response.status}): ${err}`);
      }

      const data = await response.json() as { html_url: string };
      return data.html_url;
    });

    context.logger.info("PR created", { taskId, prUrl });

    return { status: "completed", task_id: taskId, pr_url: prUrl };
  }
);

async function getGithubToken(): Promise<string> {
  if (!githubTokenSecretArn) {
    throw new Error("No GitHub token available");
  }
  // Dynamic import — available in Lambda runtime without bundling
  const { SecretsManagerClient, GetSecretValueCommand } = await import("@aws-sdk/client-secrets-manager");
  const client = new SecretsManagerClient({ region });
  const resp = await client.send(
    new GetSecretValueCommand({ SecretId: githubTokenSecretArn })
  );
  return resp.SecretString!;
}
