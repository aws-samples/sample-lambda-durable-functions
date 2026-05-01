"""
Synthesis Agent — Combines specialist findings into a unified PA recommendation.

Takes results from all specialists (including any failures), identifies gaps,
and produces a final recommendation with confidence score and rationale.
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

SYSTEM_PROMPT = """You are a prior authorization synthesis specialist.

You receive findings from multiple specialist agents (eligibility, policy, medical necessity)
along with any failures. Your job is to:

1. Combine all available findings into a coherent assessment
2. Note any gaps from failed specialists and how they affect confidence
3. Determine if the PA should be recommended for approval
4. Provide a confidence score (0.0-1.0) — lower if specialists failed
5. Write a clear rationale explaining the recommendation

Use synthesize_decision to report your final recommendation."""

_last_tool_result = {}


@tool
def synthesize_decision(
    recommendation: str,
    confidence: float,
    rationale: str,
    eligible: bool,
    gaps: list[str],
) -> dict:
    """Produce the final synthesis decision."""
    global _last_tool_result
    _last_tool_result = {
        "recommendation": recommendation,
        "confidence": confidence,
        "rationale": rationale,
        "eligible": eligible,
        "gaps": gaps,
    }
    return _last_tool_result


def run_agent(payload: dict, callback_id: str, task_id: str):
    global _last_tool_result
    try:
        _last_tool_result = {}
        model = BedrockModel(model_id="us.anthropic.claude-sonnet-4-6", max_tokens=4096)
        agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=[synthesize_decision])

        findings = payload.get("findings", {})
        failures = payload.get("failures", {})
        clinical_facts = payload.get("clinicalFacts", {})

        prompt = (
            f"Synthesize PA decision for patient {payload.get('patientId')}, "
            f"procedure {payload.get('procedureCode')}.\n\n"
            f"Clinical Facts: {json.dumps(clinical_facts, indent=2)}\n\n"
            f"Specialist Findings: {json.dumps(findings, indent=2)}\n\n"
            f"Failed Specialists: {json.dumps(failures, indent=2) if failures else 'None'}"
        )

        result = agent(prompt)
        answer = _last_tool_result if _last_tool_result else json.loads(str(result))

        lambda_client.send_durable_execution_callback_success(
            CallbackId=callback_id, Result=json.dumps(answer)
        )
    except Exception as e:
        logger.error("Synthesis agent failed: %s", e)
        lambda_client.send_durable_execution_callback_failure(CallbackId=callback_id, Error=str(e))
    finally:
        app.complete_async_task(task_id)


@app.entrypoint
def entrypoint(payload):
    callback_id = payload.get("callbackId")
    if not callback_id:
        return {"error": "Missing callbackId"}

    task_id = app.add_async_task("synthesis", {"callbackId": callback_id})
    threading.Thread(target=run_agent, args=(payload, callback_id, task_id), daemon=True).start()
    return {"status": "accepted", "callbackId": callback_id}


if __name__ == "__main__":
    app.run()
