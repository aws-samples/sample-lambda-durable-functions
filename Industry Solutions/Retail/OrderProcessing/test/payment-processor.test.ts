import { LocalDurableTestRunner, OperationStatus, OperationType, WaitingOperationStatus } from '@aws/durable-execution-sdk-js-testing';
import { handler as paymentProcessor } from '../lib/lambda/payment-processor';

describe('Payment Processor', () => {
    beforeAll(() => LocalDurableTestRunner.setupTestEnvironment({ skipTime: true }));
    afterAll(() => LocalDurableTestRunner.teardownTestEnvironment());

    const validOrder = {
        orderId: 'ORD-123',
        customerId: 'CUST-456',
        amount: 99.99
    };

    describe('Payment Approved via Callback', () => {
        it('should approve payment when callback returns approved: true', async () => {
            const runner = new LocalDurableTestRunner({
                handlerFunction: paymentProcessor,
            });

            // Start execution (it will pause at callback)
            const executionPromise = runner.run({ payload: validOrder });

            // Get the callback operation by name for test reliability
            const callbackOp = runner.getOperation('wait-for-payment-approval');

            // Wait for callback operation to start
            await callbackOp.waitForData(WaitingOperationStatus.STARTED);

            // Send callback success with approved payload (must be JSON string)
            await callbackOp.sendCallbackSuccess(
                JSON.stringify({ approved: true })
            );

            const execution = await executionPromise;
            const result = execution.getResult() as any;

            expect(result.paymentApproved).toBe(true);
            expect(result.orderId).toBe('ORD-123');
            expect(result.customerId).toBe('CUST-456');
            expect(result.amount).toBe(99.99);
            expect(result.timestamp).toBeDefined();
            expect(result.reason).toBeUndefined();
        });
    });

    describe('Payment Rejected via Callback', () => {
        it('should reject payment when callback returns approved: false', async () => {
            const runner = new LocalDurableTestRunner({
                handlerFunction: paymentProcessor,
            });

            const executionPromise = runner.run({ payload: validOrder });

            const callbackOp = runner.getOperation('wait-for-payment-approval');
            await callbackOp.waitForData(WaitingOperationStatus.STARTED);

            // Send callback with rejection
            await callbackOp.sendCallbackSuccess(
                JSON.stringify({ approved: false, reason: 'Insufficient funds' })
            );

            const execution = await executionPromise;
            const result = execution.getResult() as any;

            expect(result.paymentApproved).toBe(false);
            expect(result.orderId).toBe('ORD-123');
            expect(result.reason).toBe('Insufficient funds');
        });
    });

    describe('Callback with Invalid JSON', () => {
        it('should reject payment when callback returns invalid JSON', async () => {
            const runner = new LocalDurableTestRunner({
                handlerFunction: paymentProcessor,
            });

            const executionPromise = runner.run({ payload: validOrder });

            const callbackOp = runner.getOperation('wait-for-payment-approval');
            await callbackOp.waitForData(WaitingOperationStatus.STARTED);

            // Send callback with invalid JSON (not a valid JSON string)
            await callbackOp.sendCallbackSuccess('not valid json');

            const execution = await executionPromise;
            const result = execution.getResult() as any;

            expect(result.paymentApproved).toBe(false);
            expect(result.reason).toContain('Invalid callback JSON format');
        });
    });

    describe('Callback with Missing approved Field', () => {
        it('should reject payment when callback returns empty object', async () => {
            const runner = new LocalDurableTestRunner({
                handlerFunction: paymentProcessor,
            });

            const executionPromise = runner.run({ payload: validOrder });

            const callbackOp = runner.getOperation('wait-for-payment-approval');
            await callbackOp.waitForData(WaitingOperationStatus.STARTED);

            // Send callback with empty object (approved field missing)
            await callbackOp.sendCallbackSuccess(JSON.stringify({}));

            const execution = await executionPromise;
            const result = execution.getResult() as any;

            // Should default to false when approved is not provided
            expect(result.paymentApproved).toBe(false);
        });
    });

    describe('Operation Verification', () => {
        it('should execute callback and complete successfully', async () => {
            const runner = new LocalDurableTestRunner({
                handlerFunction: paymentProcessor,
            });

            const executionPromise = runner.run({ payload: validOrder });

            const callbackOp = runner.getOperation('wait-for-payment-approval');
            await callbackOp.waitForData(WaitingOperationStatus.STARTED);

            // Complete the callback to finish the test
            await callbackOp.sendCallbackSuccess(JSON.stringify({ approved: true }));

            const execution = await executionPromise;

            // Verify execution completed successfully
            const result = execution.getResult() as any;
            expect(result.paymentApproved).toBe(true);
            expect(execution.getOperations().length).toBeGreaterThan(0);
        });
    });
});
