// Set env var BEFORE importing modules that depend on it
const MOCK_PAYMENT_FUNCTION = 'mock-payment-processor';
process.env.PAYMENT_PROCESSOR_FUNCTION_NAME = MOCK_PAYMENT_FUNCTION;

import { LocalDurableTestRunner, OperationType } from '@aws/durable-execution-sdk-js-testing';
import { withDurableExecution, DurableContext } from '@aws/durable-execution-sdk-js';
import { handler as orderProcessor } from '../lib/lambda/order-processor';
import * as validation from '../lib/lambda/validation';
import { Order, PaymentResult } from '../lib/lambda/types';

// Mock the validation module
jest.mock('../lib/lambda/validation');
const mockedValidation = validation as jest.Mocked<typeof validation>;

// Create a mock payment processor that returns a PaymentResult
function createMockPaymentProcessor(paymentApproved: boolean, reason?: string) {
    return withDurableExecution(
        async (event: Order, context: DurableContext): Promise<PaymentResult> => {
            return {
                paymentApproved,
                orderId: event.orderId,
                customerId: event.customerId,
                amount: event.amount,
                timestamp: '2025-01-01T00:00:15.000Z',
                reason
            };
        }
    );
}

// Create a mock payment processor that throws an error (simulates invocation failure)
function createFailingPaymentProcessor() {
    return withDurableExecution(
        async (_event: Order, _context: DurableContext): Promise<PaymentResult> => {
            throw new Error('Payment service unavailable');
        }
    );
}

describe('Order Processor', () => {
    beforeAll(() => LocalDurableTestRunner.setupTestEnvironment({ skipTime: true }));
    afterAll(() => LocalDurableTestRunner.teardownTestEnvironment());

    beforeEach(() => {
        jest.clearAllMocks();
    });

    const validOrder = {
        orderId: 'ORD-123',
        customerId: 'CUST-456',
        amount: 99.99
    };

    /**
     * Helper: set up mocks for a valid, non-cancelled order
     */
    function setupValidOrderMocks() {
        mockedValidation.validateOrderWithBedrock.mockResolvedValue({
            isValid: true,
            message: 'VALID',
            timestamp: '2025-01-01T00:00:00.000Z'
        });
        mockedValidation.checkOrderCancellation.mockReturnValue({
            isCancelled: false,
            timestamp: '2025-01-01T00:00:10.000Z'
        });
    }

    describe('Happy Path - Payment Approved', () => {
        it('should complete order with approved payment and inventory reservation', async () => {
            setupValidOrderMocks();

            const runner = new LocalDurableTestRunner({
                handlerFunction: orderProcessor,
            });

            // Register mock payment processor that returns approved
            runner.registerDurableFunction(MOCK_PAYMENT_FUNCTION, createMockPaymentProcessor(true));

            const execution = await runner.run({ payload: validOrder });

            const result = execution.getResult() as any;
            expect(result.status).toBe('PAYMENT_COMPLETED');
            expect(result.orderId).toBe('ORD-123');
            expect(result.message).toContain('Order completed successfully');
            expect(result.paymentResult.paymentApproved).toBe(true);

            // Verify reservation ID is present (inventory was reserved and kept)
            expect(result.reservationId).toBeDefined();
            expect(result.reservationId).toContain('RSV-ORD-123');

            // No compensations should have been executed on the happy path
            expect(result.compensationActions).toBeUndefined();

            // Verify workflow execution structure (6 ops: time, validate, wait, cancel-check, reserve, payment invoke)
            const operations = execution.getOperations();
            expect(operations.length).toBe(6);
            expect(execution.getInvocations().length).toBe(3); // initial + wait resume + durable invoke

            // Verify key operation types are present
            const operationsByIndex = operations.map((_, idx) => runner.getOperationByIndex(idx));
            const hasOperationType = (type: OperationType) => {
                return operationsByIndex.some(op => {
                    try {
                        return op.getType() === type;
                    } catch {
                        return false;
                    }
                });
            };

            expect(hasOperationType(OperationType.STEP)).toBe(true);
            expect(hasOperationType(OperationType.WAIT)).toBe(true);
            expect(hasOperationType(OperationType.CHAINED_INVOKE)).toBe(true);
        });
    });

    describe('Payment Rejected - Saga Compensation', () => {
        it('should compensate inventory when payment is rejected', async () => {
            setupValidOrderMocks();

            const runner = new LocalDurableTestRunner({
                handlerFunction: orderProcessor,
            });

            // Register mock payment processor that returns rejected
            runner.registerDurableFunction(MOCK_PAYMENT_FUNCTION, createMockPaymentProcessor(false, 'Insufficient funds'));

            const execution = await runner.run({ payload: validOrder });

            const result = execution.getResult() as any;
            expect(result.status).toBe('PAYMENT_FAILED');
            expect(result.orderId).toBe('ORD-123');
            expect(result.message).toContain('Payment rejected');

            // Should be one additional operation (compensation)
            const operations = execution.getOperations();
            expect(operations.length).toBe(7);

            // Verify compensation was executed
            expect(result.compensationActions).toBeDefined();
            expect(result.compensationActions).toHaveLength(1);
            expect(result.compensationActions[0].action).toBe('release-inventory');
            expect(result.compensationActions[0].success).toBe(true);

            // Payment result should be attached for observability
            expect(result.paymentResult).toBeDefined();
            expect(result.paymentResult.paymentApproved).toBe(false);
            expect(result.paymentResult.reason).toBe('Insufficient funds');
        });

        it('should compensate inventory when payment invocation fails', async () => {
            setupValidOrderMocks();

            const runner = new LocalDurableTestRunner({
                handlerFunction: orderProcessor,
            });

            // Register a payment processor that throws
            runner.registerDurableFunction(MOCK_PAYMENT_FUNCTION, createFailingPaymentProcessor());

            const execution = await runner.run({ payload: validOrder });

            const result = execution.getResult() as any;
            expect(result.status).toBe('PAYMENT_FAILED');
            expect(result.orderId).toBe('ORD-123');

            // Verify compensation was executed
            expect(result.compensationActions).toBeDefined();
            expect(result.compensationActions).toHaveLength(1);
            expect(result.compensationActions[0].action).toBe('release-inventory');
            expect(result.compensationActions[0].success).toBe(true);

            // No paymentResult since invocation itself failed
            expect(result.paymentResult).toBeUndefined();
        });
    });

    describe('Validation Failed', () => {
        it('should return validation failed when order is invalid', async () => {
            mockedValidation.validateOrderWithBedrock.mockResolvedValue({
                isValid: false,
                message: 'INVALID: Missing orderId',
                timestamp: '2025-01-01T00:00:00.000Z'
            });

            const runner = new LocalDurableTestRunner({
                handlerFunction: orderProcessor,
            });

            const execution = await runner.run({ payload: validOrder });

            const result = execution.getResult() as any;
            expect(result.status).toBe('VALIDATION_FAILED');
            expect(result.message).toContain('Order validation failed');
            expect(result.validationResult).toBe('INVALID: Missing orderId');

            // Verify limited operations executed (validation failed early)
            expect(execution.getOperations().length).toBeLessThanOrEqual(2);
        });
    });

    describe('Order Cancelled', () => {
        it('should return cancelled when order is cancelled during processing', async () => {
            mockedValidation.validateOrderWithBedrock.mockResolvedValue({
                isValid: true,
                message: 'VALID',
                timestamp: '2025-01-01T00:00:00.000Z'
            });

            mockedValidation.checkOrderCancellation.mockReturnValue({
                isCancelled: true,
                timestamp: '2025-01-01T00:00:10.000Z'
            });

            const runner = new LocalDurableTestRunner({
                handlerFunction: orderProcessor,
            });

            const execution = await runner.run({ payload: validOrder });

            const result = execution.getResult() as any;
            expect(result.status).toBe('CANCELLED');
            expect(result.message).toContain('cancelled by user');

            // Verify workflow stopped after cancellation check (no inventory or payment)
            expect(execution.getOperations().length).toBeLessThanOrEqual(4);
        });
    });

    describe('Processing Timestamps', () => {
        it('should capture orderReceived timestamp deterministically', async () => {
            setupValidOrderMocks();

            const runner = new LocalDurableTestRunner({
                handlerFunction: orderProcessor,
            });

            // Register mock payment processor
            runner.registerDurableFunction(MOCK_PAYMENT_FUNCTION, createMockPaymentProcessor(true));

            const execution = await runner.run({ payload: validOrder });

            const result = execution.getResult() as any;

            // Verify processing timestamps are present
            expect(result.processingTime.orderReceived).toBeDefined();
            expect(result.processingTime.validationCompleted).toBeDefined();
            expect(result.processingTime.cancellationChecked).toBeDefined();
            expect(result.processingTime.paymentCompleted).toBeDefined();
            expect(result.processingTime.orderCompleted).toBeDefined();
        });
    });
});
