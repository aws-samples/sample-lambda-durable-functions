"""
Policy Agent — Retrieves and interprets payer medical policy.

Looks up whether a procedure requires prior authorization and
what clinical criteria must be met for approval.
"""
import os
import json
import logging
import threading
import boto3
from strands import Agent
from strands.models import BedrockModel
from strands.tools import tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp

logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()
lambda_client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "us-west-2"))

SYSTEM_PROMPT = """You are a medical policy interpretation specialist.

Given a payer, procedure code, and diagnosis code:
1. Look up the relevant medical policy
2. Determine if prior authorization is required
3. Extract the clinical criteria that must be met for approval

Use lookup_policy to query the policy database and report_policy to return findings."""

_last_tool_result = {}


@tool
def lookup_policy(payer_id: str, procedure_code: str, diagnosis_code: str) -> dict:
    """Query payer medical policy database."""
    return {
        "policyId": f"POL-{payer_id}-{procedure_code}",
        "requiresPriorAuth": True,
        "criteria": [
            "Failed conservative treatment for 6+ months",
            "BMI < 40 or medically optimized",
            "No active infection",
            "Documented functional limitation",
        ],
        "covered": True,
    }


@tool
def report_policy(covered: bool, requires_prior_auth: bool, criteria: list[str], policy_id: str) -> dict:
    """Report policy findings back to the coordinator."""
    global _last_tool_result
    _last_tool_result = {
        "covered": covered,
        "requiresPriorAuth": requires_prior_auth,
        "criteria": criteria,
        "policyId": policy_id,
    }
    return _last_tool_result


def run_agent(payload: dict, callback_id: str, task_id: str):
    global _last_tool_result
    try:
        _last_tool_result = {}
        model = BedrockModel(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0", max_tokens=2048)
        agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=[lookup_policy, report_policy])

        prompt = (
            f"Look up medical policy for payer {payload.get('payerId')}, "
            f"procedure {payload.get('procedureCode')}, "
            f"diagnosis {payload.get('diagnosisCode')}"
        )

        result = agent(prompt)
        answer = _last_tool_result if _last_tool_result else json.loads(str(result))

        lambda_client.send_durable_execution_callback_success(
            CallbackId=callback_id, Result=json.dumps(answer)
        )
    except Exception as e:
        logger.error("Policy agent failed: %s", e)
        lambda_client.send_durable_execution_callback_failure(CallbackId=callback_id, Error=str(e))
    finally:
        app.complete_async_task(task_id)


@app.entrypoint
def entrypoint(payload):
    callback_id = payload.get("callbackId")
    if not callback_id:
        return {"error": "Missing callbackId"}

    task_id = app.add_async_task("policy_lookup", {"callbackId": callback_id})
    threading.Thread(target=run_agent, args=(payload, callback_id, task_id), daemon=True).start()
    return {"status": "accepted", "callbackId": callback_id}


if __name__ == "__main__":
    app.run()
