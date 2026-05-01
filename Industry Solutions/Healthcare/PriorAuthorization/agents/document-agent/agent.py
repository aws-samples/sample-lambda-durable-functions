"""
Document Agent — Clinical fact extraction and routing planner.

Extracts diagnosis, procedure, and supporting evidence from clinical notes.
Determines which specialist agents are needed to complete the PA workflow.
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
lambda_client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "us-east-1"))

SYSTEM_PROMPT = """You are a clinical document extraction specialist for prior authorization workflows.

Given clinical notes, procedure codes, and diagnosis codes, you must:
1. Extract the primary diagnosis and procedure
2. Identify supporting clinical evidence (lab results, imaging, failed treatments)
3. List any missing required fields that would block PA submission
4. Determine which specialist agents are needed:
   - requiresEligibilityCheck: true if patient coverage hasn't been pre-verified
   - requiresPolicyLookup: true if the procedure requires prior auth per payer rules
   - requiresMedicalNecessity: true if clinical evidence must be compared against policy criteria

Return your analysis as a structured JSON object using the report_findings tool."""


@tool
def report_findings(
    diagnosis: str,
    procedure: str,
    supporting_evidence: list[str],
    missing_fields: list[str],
    requires_eligibility_check: bool,
    requires_policy_lookup: bool,
    requires_medical_necessity: bool,
) -> dict:
    """Report extracted clinical facts and routing decisions."""
    return {
        "diagnosis": diagnosis,
        "procedure": procedure,
        "supportingEvidence": supporting_evidence,
        "missingFields": missing_fields,
        "requiresEligibilityCheck": requires_eligibility_check,
        "requiresPolicyLookup": requires_policy_lookup,
        "requiresMedicalNecessity": requires_medical_necessity,
    }


def run_agent(payload: dict, callback_id: str, task_id: str):
    try:
        model = BedrockModel(model_id="anthropic.claude-sonnet-4-6", max_tokens=4096)
        agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=[report_findings])

        prompt = (
            f"Patient ID: {payload.get('patientId')}\n"
            f"Payer: {payload.get('payerId')}\n"
            f"Procedure Code: {payload.get('procedureCode')}\n"
            f"Diagnosis Code: {payload.get('diagnosisCode')}\n"
            f"Clinical Notes:\n{payload.get('clinicalNotes')}"
        )

        result = agent(prompt)
        # Extract the tool result from the agent's response
        answer = result.tool_results[-1] if result.tool_results else str(result)

        lambda_client.send_durable_execution_callback_success(
            CallbackId=callback_id, Result=json.dumps(answer)
        )
    except Exception as e:
        logger.error("Document agent failed: %s", e)
        lambda_client.send_durable_execution_callback_failure(
            CallbackId=callback_id, Error=str(e)
        )
    finally:
        app.complete_async_task(task_id)


@app.entrypoint
def entrypoint(payload):
    callback_id = payload.get("callbackId")
    if not callback_id:
        return {"error": "Missing callbackId"}

    task_id = app.add_async_task("document_extraction", {"callbackId": callback_id})
    threading.Thread(target=run_agent, args=(payload, callback_id, task_id), daemon=True).start()
    return {"status": "accepted", "callbackId": callback_id}


if __name__ == "__main__":
    app.run()
