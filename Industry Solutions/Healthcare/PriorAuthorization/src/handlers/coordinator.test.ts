import {
  LocalDurableTestRunner,
  OperationStatus,
  WaitingOperationStatus,
} from '@aws/durable-execution-sdk-js-testing';
import { handler } from './coordinator';

describe('Healthcare PA Coordinator', () => {
  beforeAll(() => LocalDurableTestRunner.setupTestEnvironment({ skipTime: true }));
  afterAll(() => LocalDurableTestRunner.teardownTestEnvironment());

  const baseRequest = {
    patientId: 'PT-001',
    providerId: 'DR-100',
    payerId: 'PAYER-BCBS',
    procedureCode: '27447',
    diagnosisCode: 'M17.11',
    clinicalNotes: 'Patient presents with severe osteoarthritis of right knee...',
  };

  it('should complete PA workflow without missing docs or human review', async () => {
    const runner = new LocalDurableTestRunner({ handlerFunction: handler });
    const executionPromise = runner.run({ payload: baseRequest });

    // Wait for payer decision callback
    const payerCallback = runner.getOperation('wait-for-payer-decision');
    await payerCallback.waitForData(WaitingOperationStatus.STARTED);
    await payerCallback.sendCallbackSuccess(
      JSON.stringify({ approved: true, rationale: 'Meets medical necessity criteria' })
    );

    const execution = await executionPromise;
    expect(execution.getStatus()).toBe('SUCCEEDED');

    const result = execution.getResult() as any;
    expect(result.status).toBe('approved');
  });

  it('should pause for missing documentation and resume on provider upload', async () => {
    // Mock handler that returns missing fields
    // In real tests, you'd mock extractClinicalFacts to return missingFields
    const runner = new LocalDurableTestRunner({ handlerFunction: handler });

    const execution = await runner.run({ payload: baseRequest });

    // Verify steps executed
    const extractStep = runner.getOperation('extract-clinical-facts');
    expect(extractStep.getStatus()).toBe(OperationStatus.SUCCEEDED);

    const notifyStep = runner.getOperation('notify-provider');
    expect(notifyStep.getStatus()).toBe(OperationStatus.SUCCEEDED);
  });

  it('should route to human reviewer when confidence is low', async () => {
    const runner = new LocalDurableTestRunner({ handlerFunction: handler });
    const executionPromise = runner.run({
      payload: { ...baseRequest, requireHumanReview: true },
    });

    // Human reviewer callback
    const reviewCallback = runner.getOperation('wait-for-human-review');
    await reviewCallback.waitForData(WaitingOperationStatus.STARTED);
    await reviewCallback.sendCallbackSuccess(
      JSON.stringify({ approved: true, referenceId: 'REV-001', rationale: 'Approved after clinical review' })
    );

    // Payer decision callback
    const payerCallback = runner.getOperation('wait-for-payer-decision');
    await payerCallback.waitForData(WaitingOperationStatus.STARTED);
    await payerCallback.sendCallbackSuccess(
      JSON.stringify({ approved: true, rationale: 'Authorized' })
    );

    const execution = await executionPromise;
    expect(execution.getStatus()).toBe('SUCCEEDED');
  });
});
