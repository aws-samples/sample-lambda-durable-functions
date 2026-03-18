/**
 * Cloud integration test for Order Processor
 *
 * Runs against a deployed Lambda function using CloudDurableTestRunner.
 * Tests the validation path with an incomplete order (no callback needed).
 *
 * Required environment variables:
 *   ORDER_PROCESSOR_FUNCTION_NAME  - Qualified function name (e.g., "order-processor:$LATEST")
 *
 * Optional:
 *   AWS_REGION                     - AWS region (default: us-east-1)
 *
 * Run with:
 *   ORDER_PROCESSOR_FUNCTION_NAME="order-processor:\$LATEST" npm run test:cloud
 */

import { LambdaClient } from '@aws-sdk/client-lambda';
import { CloudDurableTestRunner } from '@aws/durable-execution-sdk-js-testing';

const FUNCTION_NAME = process.env.ORDER_PROCESSOR_FUNCTION_NAME;
const REGION = process.env.AWS_REGION || 'us-east-1';

const describeOrSkip = FUNCTION_NAME ? describe : describe.skip;

if (!FUNCTION_NAME) {
    console.warn(
        '\n⚠️  Skipping cloud tests: ORDER_PROCESSOR_FUNCTION_NAME not set.\n' +
        '   Deploy the stack first, then run:\n\n' +
        '     export ORDER_PROCESSOR_FUNCTION_NAME="order-processor:$LATEST"\n' +
        '     npm run test:cloud\n'
    );
}

describeOrSkip('Order Processor (Cloud)', () => {
    let runner: CloudDurableTestRunner;

    beforeAll(() => {
        runner = new CloudDurableTestRunner({
            functionName: FUNCTION_NAME!,
            client: new LambdaClient({ region: REGION }),
        });
    });

    beforeEach(() => {
        runner.reset();
    });

    it('should fail validation for incomplete orders', async () => {
        const orderId = `ORD-INVALID-${Date.now()}`;

        const execution = await runner.run({
            payload: {
                orderId,
                // Missing customerId and amount — Bedrock should detect this
            },
        });

        const result = execution.getResult() as any;

        expect(result).toBeDefined();
        expect(result.orderId).toBe(orderId);
        expect(result.status).toBe('VALIDATION_FAILED');
        expect(result.processingTime.orderReceived).toBeDefined();
    }, 120_000);
});
