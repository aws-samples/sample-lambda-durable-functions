import {
  withDurableExecution,
  DurableContext,
  createRetryStrategy,
  JitterStrategy,
  StepSemantics,
  defaultSerdes,
} from '@aws/durable-execution-sdk-js';
import {
  BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
} from '@aws-sdk/client-bedrock-agentcore';

// --- Configuration ---

const DOCUMENT_AGENT_ARN = process.env.DOCUMENT_AGENT_ARN!;
const ELIGIBILITY_AGENT_ARN = process.env.ELIGIBILITY_AGENT_ARN!;
const POLICY_AGENT_ARN = process.env.POLICY_AGENT_ARN!;
const MEDICAL_NECESSITY_AGENT_ARN = process.env.MEDICAL_NECESSITY_AGENT_ARN!;
const SYNTHESIS_AGENT_ARN = process.env.SYNTHESIS_AGENT_ARN!;

const agentCoreClient = new BedrockAgentCoreClient();

// --- Types ---

interface PriorAuthRequest {
  patientId: string;
  providerId: string;
  payerId: string;
  procedureCode: string;
  diagnosisCode: string;
  clinicalNotes: string;
  requireHumanReview?: boolean;
}

interface ClinicalFacts {
  diagnosis: string;
  procedure: string;
  supportingEvidence: string[];
  missingFields: string[];
  requiresEligibilityCheck: boolean;
  requiresPolicyLookup: boolean;
  requiresMedicalNecessity: boolean;
}

interface SpecialistRequest {
  name: string;
  agentArn: string;
  prompt: string;
}

interface PriorAuthDecision {
  status: 'approved' | 'denied' | 'pending_review' | 'pending_info';
  referenceId: string;
  rationale: string;
  specialistsInvoked: string[];
  findings: Record<string, any>;
}

// --- AgentCore invocation helper ---

async function invokeAgent(agentArn: string, payload: Record<string, any>): Promise<string> {
  const command = new InvokeAgentRuntimeCommand({
    agentRuntimeArn: agentArn,
    payload: new TextEncoder().encode(JSON.stringify(payload)),
  });
  const response = await agentCoreClient.send(command);
  if (!response.response) throw new Error('No response from agent runtime');
  // Response is a streaming blob — collect it
  const bytes = await response.response.transformToByteArray();
  return new TextDecoder().decode(bytes);
}

// --- Retry strategy for payer API calls ---

const payerRetry = createRetryStrategy({
  maxAttempts: 3,
  initialDelay: { seconds: 2 },
  maxDelay: { seconds: 30 },
  backoffRate: 2.0,
  jitter: JitterStrategy.FULL,
});

// --- Handler ---

export const handler = withDurableExecution(
  async (event: PriorAuthRequest, context: DurableContext): Promise<PriorAuthDecision> => {
    context.logger.info('PA request received', { patientId: event.patientId, procedureCode: event.procedureCode });

    // ─── Step 1: Document Agent (planner) ───────────────────────────────────────
    // Extracts clinical facts AND determines which specialist agents are needed.
    // This acts as the "planner" — its output drives conditional routing.
    const clinicalFacts = await context.waitForCallback(
      'extract-clinical-facts',
      async (callbackId, ctx) => {
        ctx.logger.info('Invoking Document Agent on AgentCore', { callbackId });
        try {
          const result = await invokeAgent(DOCUMENT_AGENT_ARN, {
            task: 'extract_and_plan',
            clinicalNotes: event.clinicalNotes,
            procedureCode: event.procedureCode,
            diagnosisCode: event.diagnosisCode,
            patientId: event.patientId,
            payerId: event.payerId,
            callbackId,
          });
          ctx.logger.info('Document Agent invoked successfully', { result });
        } catch (err: any) {
          ctx.logger.error('Document Agent invocation FAILED', { error: err.message, name: err.name, code: err.$metadata?.httpStatusCode });
          throw err;
        }
      },
      { timeout: { minutes: 5 }, serdes: defaultSerdes }
    ) as ClinicalFacts;

    context.logger.info('Document Agent completed — routing plan determined', {
      missingFields: clinicalFacts.missingFields,
      eligibility: clinicalFacts.requiresEligibilityCheck,
      policy: clinicalFacts.requiresPolicyLookup,
      necessity: clinicalFacts.requiresMedicalNecessity,
    });

    // ─── Step 2: Pause for missing documentation if needed ──────────────────────
    let finalFacts = clinicalFacts;
    if (clinicalFacts.missingFields.length > 0) {
      context.logger.info('Missing documentation — suspending for provider upload');

      const uploadedDocs = await context.waitForCallback(
        'wait-for-provider-docs',
        async (callbackId) => {
          await notifyProviderMissingInfo(event.providerId, clinicalFacts.missingFields, callbackId);
        },
        { timeout: { hours: 72 }, serdes: defaultSerdes }
      );

      // Re-run document agent with additional info
      finalFacts = await context.waitForCallback(
        're-extract-clinical-facts',
        async (callbackId, ctx) => {
          await invokeAgent(DOCUMENT_AGENT_ARN, {
            task: 'extract_and_plan',
            clinicalNotes: event.clinicalNotes + '\n' + uploadedDocs.additionalNotes,
            procedureCode: event.procedureCode,
            diagnosisCode: event.diagnosisCode,
            patientId: event.patientId,
            payerId: event.payerId,
            callbackId,
          });
        },
        { timeout: { minutes: 5 }, serdes: defaultSerdes }
      ) as ClinicalFacts;
    }

    // ─── Step 3: Conditionally invoke specialist agents in parallel ──────────────
    // Only the specialists flagged by the Document Agent (planner) are invoked.
    const specialists: SpecialistRequest[] = [];

    if (finalFacts.requiresEligibilityCheck) {
      specialists.push({
        name: 'eligibility',
        agentArn: ELIGIBILITY_AGENT_ARN,
        prompt: JSON.stringify({
          task: 'check_eligibility',
          patientId: event.patientId,
          payerId: event.payerId,
          procedureCode: event.procedureCode,
        }),
      });
    }

    if (finalFacts.requiresPolicyLookup) {
      specialists.push({
        name: 'policy',
        agentArn: POLICY_AGENT_ARN,
        prompt: JSON.stringify({
          task: 'lookup_policy',
          payerId: event.payerId,
          procedureCode: event.procedureCode,
          diagnosisCode: event.diagnosisCode,
        }),
      });
    }

    if (finalFacts.requiresMedicalNecessity) {
      specialists.push({
        name: 'medical-necessity',
        agentArn: MEDICAL_NECESSITY_AGENT_ARN,
        prompt: JSON.stringify({
          task: 'assess_necessity',
          clinicalFacts: finalFacts,
          procedureCode: event.procedureCode,
          diagnosisCode: event.diagnosisCode,
        }),
      });
    }

    context.logger.info('Invoking specialist agents', {
      selected: specialists.map(s => s.name),
      skipped: [
        !finalFacts.requiresEligibilityCheck && 'eligibility',
        !finalFacts.requiresPolicyLookup && 'policy',
        !finalFacts.requiresMedicalNecessity && 'medical-necessity',
      ].filter(Boolean),
    });

    // Fan out selected specialists in parallel via AgentCore + waitForCallback
    const specialistResults = await context.map(
      'specialist-agents',
      specialists,
      async (mapCtx, specialist) => {
        const agentResponse = await mapCtx.waitForCallback(
          `invoke-${specialist.name}`,
          async (callbackId, ctx) => {
            ctx.logger.info('Invoking specialist on AgentCore', { name: specialist.name, callbackId });
            await invokeAgent(specialist.agentArn, {
              ...JSON.parse(specialist.prompt),
              callbackId,
            });
          },
          { timeout: { minutes: 5 }, serdes: defaultSerdes }
        );
        const parsed = typeof agentResponse === 'string' ? JSON.parse(agentResponse) : agentResponse;
        return { name: specialist.name, result: parsed };
      },
      { maxConcurrency: 3, itemNamer: (s) => s.name }
    );

    // ─── Graceful degradation: capture failures without halting the workflow ─────
    const findings: Record<string, any> = {};
    const failures: Record<string, string> = {};

    for (const item of specialistResults.all) {
      if (item.status === 'SUCCEEDED' && item.result) {
        findings[item.result.name] = item.result.result;
      } else {
        const failedSpecialist = specialists[item.index];
        failures[failedSpecialist.name] = item.error?.message ?? 'Unknown error';
        context.logger.warn('Specialist agent failed — continuing with partial results', {
          name: failedSpecialist.name,
          error: item.error?.message,
        });
      }
    }

    context.logger.info('Specialist agents completed', {
      succeeded: Object.keys(findings),
      failed: Object.keys(failures),
    });

    // ─── Step 4: Synthesis Agent — combine specialist findings ────────────────────
    // Synthesizes all available results, notes gaps from failed specialists,
    // and produces a unified recommendation with confidence score.
    const synthesis = await context.waitForCallback(
      'synthesize-findings',
      async (callbackId, ctx) => {
        ctx.logger.info('Invoking Synthesis Agent on AgentCore', { callbackId });
        await invokeAgent(SYNTHESIS_AGENT_ARN, {
          task: 'synthesize_pa_decision',
          clinicalFacts: finalFacts,
          findings,
          failures,
          patientId: event.patientId,
          procedureCode: event.procedureCode,
          callbackId,
        });
      },
      { timeout: { minutes: 5 }, serdes: defaultSerdes }
    ) as { recommendation: string; confidence: number; rationale: string; eligible: boolean };

    context.logger.info('Synthesis complete', {
      recommendation: synthesis.recommendation,
      confidence: synthesis.confidence,
      hasGaps: Object.keys(failures).length > 0,
    });

    // ─── Step 5: Deny if synthesis determines ineligibility ──────────────────────
    if (!synthesis.eligible) {
      return {
        status: 'denied',
        referenceId: '',
        rationale: synthesis.rationale,
        specialistsInvoked: specialists.map(s => s.name),
        findings: { ...findings, synthesis, failures },
      };
    }

    // ─── Step 6: Human review if low confidence, failures, or flagged ────────────
    const needsHumanReview = event.requireHumanReview
      || synthesis.confidence < 0.8
      || Object.keys(failures).length > 0;

    if (needsHumanReview) {
      context.logger.info('Routing to human reviewer', {
        confidence: synthesis.confidence,
        failedSpecialists: Object.keys(failures),
      });

      const reviewDecision = await context.waitForCallback(
        'wait-for-human-review',
        async (callbackId) => {
          await routeToHumanReviewer(callbackId, {
            clinicalFacts: finalFacts,
            synthesis,
            findings,
            failures,
            patientId: event.patientId,
          });
        },
        { timeout: { hours: 48 }, serdes: defaultSerdes }
      );

      if (!reviewDecision.approved) {
        return {
          status: 'denied',
          referenceId: reviewDecision.referenceId ?? '',
          rationale: reviewDecision.rationale ?? 'Denied by clinical reviewer',
          specialistsInvoked: specialists.map(s => s.name),
          findings: { ...findings, synthesis, failures },
        };
      }
    }

    // ─── Step 7: Submit PA to payer (AT_MOST_ONCE — no duplicates) ───────────────
    const submission = await context.step(
      'submit-prior-auth',
      async () => submitPriorAuthRequest(event, finalFacts, findings),
      { retryStrategy: payerRetry, semantics: StepSemantics.AtMostOncePerRetry }
    );

    // ─── Step 8: Wait for payer decision callback ────────────────────────────────
    const payerDecision = await context.waitForCallback(
      'wait-for-payer-decision',
      async (callbackId) => {
        await registerPayerWebhook(submission.trackingId, callbackId);
      },
      { timeout: { days: 14 }, serdes: defaultSerdes }
    );

    // ─── Step 9: Notify provider ─────────────────────────────────────────────────
    await context.step('notify-provider', async () => {
      await notifyProvider(event.providerId, payerDecision);
    });

    return {
      status: payerDecision.approved ? 'approved' : 'denied',
      referenceId: submission.trackingId,
      rationale: payerDecision.rationale,
      specialistsInvoked: specialists.map(s => s.name),
      findings: { ...findings, synthesis, failures },
    };
  }
);

// --- External system stubs ---

async function notifyProviderMissingInfo(providerId: string, missingItems: string[], callbackId: string): Promise<void> {}
async function routeToHumanReviewer(callbackId: string, caseSummary: any): Promise<void> {}
async function submitPriorAuthRequest(event: PriorAuthRequest, facts: ClinicalFacts, findings: Record<string, any>): Promise<{ trackingId: string }> {
  return { trackingId: `PA-${event.patientId}-${Date.now()}` };
}
async function registerPayerWebhook(trackingId: string, callbackId: string): Promise<void> {}
async function notifyProvider(providerId: string, decision: any): Promise<void> {}
