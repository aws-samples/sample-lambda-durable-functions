"""
Eligibility Agent — Verifies patient insurance coverage and benefits.

Checks active coverage, plan details, and whether the procedure
is within the patient's benefit structure.
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

SYSTEM_PROMPT = """You are a healthcare eligibility verification specialist.

Given a patient ID, payer, and procedure code, verify:
1. Whether the patient has active coverage
2. The plan ID and coverage type
3. Whether the procedure is within the plan's benefit structure

Use the check_eligibility tool to query the payer system and report_eligibility to return results."""

_last_tool_result = {}


@tool
def check_eligibility(patient_id: str, payer_id: str, procedure_code: str) -> dict:
    """Query payer eligibility system (X12 270/271 transaction)."""
    return {
        "eligible": True,
        "planId": f"PLAN-{payer_id}-001",
        "coverageType": "PPO",
        "effectiveDate": "2025-01-01",
        "procedureInNetwork": True,
    }


@tool
def report_eligibility(eligible: bool, plan_id: str, coverage_details: str) -> dict:
    """Report eligibility findings back to the coordinator."""
    global _last_tool_result
    _last_tool_result = {"eligible": eligible, "planId": plan_id, "coverageDetails": coverage_details}
    return _last_tool_result


def run_agent(payload: dict, callback_id: str, task_id: str):
    global _last_tool_result
    try:
        _last_tool_result = {}
        model = BedrockModel(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0", max_tokens=2048)
        agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=[check_eligibility, report_eligibility])

        prompt = (
            f"Check eligibility for patient {payload.get('patientId')} "
            f"with payer {payload.get('payerId')} "
            f"for procedure {payload.get('procedureCode')}"
        )

        result = agent(prompt)
        answer = _last_tool_result if _last_tool_result else json.loads(str(result))

        lambda_client.send_durable_execution_callback_success(
            CallbackId=callback_id, Result=json.dumps(answer)
        )
    except Exception as e:
        logger.error("Eligibility agent failed: %s", e)
        lambda_client.send_durable_execution_callback_failure(CallbackId=callback_id, Error=str(e))
    finally:
        app.complete_async_task(task_id)


@app.entrypoint
def entrypoint(payload):
    callback_id = payload.get("callbackId")
    if not callback_id:
        return {"error": "Missing callbackId"}

    task_id = app.add_async_task("eligibility_check", {"callbackId": callback_id})
    threading.Thread(target=run_agent, args=(payload, callback_id, task_id), daemon=True).start()
    return {"status": "accepted", "callbackId": callback_id}


if __name__ == "__main__":
    app.run()
