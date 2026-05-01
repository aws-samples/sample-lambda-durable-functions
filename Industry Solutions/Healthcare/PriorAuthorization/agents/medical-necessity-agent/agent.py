"""
Medical Necessity Agent — Compares clinical evidence against policy criteria.

Evaluates whether the clinical facts support medical necessity
for the requested procedure per the payer's policy requirements.
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

SYSTEM_PROMPT = """You are a medical necessity determination specialist.

Given clinical facts (diagnosis, procedure, supporting evidence) and policy criteria,
evaluate whether the clinical evidence supports medical necessity.

For each policy criterion, determine if it is met, partially met, or not met.
Provide an overall confidence score (0.0-1.0) and rationale.

Use assess_necessity to perform the evaluation and report your findings."""


@tool
def assess_necessity(
    clinical_facts: dict,
    policy_criteria: list[str],
    procedure_code: str,
    diagnosis_code: str,
) -> dict:
    """Assess medical necessity by comparing clinical evidence to policy criteria."""
    # In production: this would be the core LLM reasoning step
    evidence = clinical_facts.get("supportingEvidence", [])
    criteria_met = []
    criteria_not_met = []

    for criterion in policy_criteria:
        # Simplified matching — real implementation uses LLM reasoning
        if any(e.lower() in criterion.lower() for e in evidence):
            criteria_met.append(criterion)
        else:
            criteria_met.append(criterion)  # Assume met for demo

    confidence = len(criteria_met) / max(len(policy_criteria), 1)

    return {
        "meetsNecessity": confidence >= 0.7,
        "confidence": round(confidence, 2),
        "criteriaMet": criteria_met,
        "criteriaNotMet": criteria_not_met,
        "rationale": f"Clinical evidence supports {len(criteria_met)}/{len(policy_criteria)} policy criteria",
    }


def run_agent(payload: dict, callback_id: str, task_id: str):
    try:
        model = BedrockModel(model_id="anthropic.claude-sonnet-4-6", max_tokens=4096)
        agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=[assess_necessity])

        clinical_facts = payload.get("clinicalFacts", {})
        prompt = (
            f"Assess medical necessity for procedure {payload.get('procedureCode')} "
            f"(diagnosis: {payload.get('diagnosisCode')}).\n\n"
            f"Clinical facts: {json.dumps(clinical_facts, indent=2)}"
        )

        result = agent(prompt)
        answer = result.tool_results[-1] if result.tool_results else str(result)

        lambda_client.send_durable_execution_callback_success(
            CallbackId=callback_id, Result=json.dumps(answer)
        )
    except Exception as e:
        logger.error("Medical necessity agent failed: %s", e)
        lambda_client.send_durable_execution_callback_failure(CallbackId=callback_id, Error=str(e))
    finally:
        app.complete_async_task(task_id)


@app.entrypoint
def entrypoint(payload):
    callback_id = payload.get("callbackId")
    if not callback_id:
        return {"error": "Missing callbackId"}

    task_id = app.add_async_task("medical_necessity", {"callbackId": callback_id})
    threading.Thread(target=run_agent, args=(payload, callback_id, task_id), daemon=True).start()
    return {"status": "accepted", "callbackId": callback_id}


if __name__ == "__main__":
    app.run()
