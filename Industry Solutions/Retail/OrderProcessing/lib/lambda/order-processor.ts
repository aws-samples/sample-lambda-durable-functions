/**
 * Order Processor - Main workflow orchestrator for order processing
 * 
 * Flow:
 * 1. Validates order using Bedrock AI
 * 2. Waits 10 seconds (simulating processing time / cancellation window)
 * 3. Checks if order was cancelled
 * 4. Reserves inventory (side effect registered for compensation)
 * 5. Invokes payment processor via durable invocation
 *    - On payment failure or invocation error: executes saga compensations
 *      in reverse order (releases inventory) before returning
 * 6. Returns final order status
 */

import { DurableContext, withDurableExecution } from '@aws/durable-execution-sdk-js';
import { PAYMENT_PROCESSOR, TIMEOUTS, createRetryStrategy } from './config';
import { releaseInventory, reserveInventory } from './inventory';
import {
    buildCancelledResponse,
    buildCompensatedFailureResponse,
    buildCompletedResponse,
    buildValidationFailedResponse,
} from './order-processor-helpers';
import { CompensationAction, Order, OrderResult, PaymentResult } from './types';
import { checkOrderCancellation, validateOrderWithBedrock } from './validation';

/**
 * Entry registered in the saga compensation list. Each side-effecting step
 * pushes a compensation entry after it succeeds. On failure the list is
 * iterated in reverse to undo completed work.
 */
interface CompensationEntry {
    name: string;
    fn: () => Promise<void>;
}

export const handler = withDurableExecution(
    async (event: Order, context: DurableContext): Promise<OrderResult> => {
        // Capture order received time inside a step to ensure deterministic replay
        const orderReceived = await context.step('capture-order-time', async () => {
            return new Date().toISOString();
        });
        context.logger.info('Order received', { order: event, timestamp: orderReceived });

        // Step 1: Validate order using Bedrock (with retries)
        let validationResult;
        try {
            validationResult = await context.step('validate-order', async (stepCtx) => {
                return validateOrderWithBedrock(event, stepCtx.logger);
            }, { retryStrategy: createRetryStrategy() });
        } catch (error) {
            context.logger.error('Order validation failed after all retries', {
                error: error instanceof Error ? error.message : String(error),
                orderId: event.orderId
            });
            return {
                status: 'PROCESSING_FAILED',
                orderId: event.orderId || 'unknown',
                message: `Order processing failed during validation: ${error instanceof Error ? error.message : String(error)}`,
                processingTime: {
                    orderReceived,
                    orderCompleted: new Date().toISOString()
                }
            };
        }

        if (!validationResult.isValid) {
            context.logger.warn('Order validation failed', { validation: validationResult });
            return buildValidationFailedResponse(
                event,
                validationResult.message,
                orderReceived,
                validationResult.timestamp
            );
        }

        context.logger.info('Order validated successfully', { validation: validationResult });

        // Wait for configured delay (simulating user being able to cancel)
        await context.wait('processing-delay', { seconds: TIMEOUTS.orderProcessingDelaySeconds });

        // Step 2: Check if order was cancelled (with retries)
        let cancellationCheck;
        try {
            cancellationCheck = await context.step('check-cancellation', async (stepCtx) => {
                return checkOrderCancellation(event, stepCtx.logger);
            }, { retryStrategy: createRetryStrategy() });
        } catch (error) {
            context.logger.error('Cancellation check failed after all retries', {
                error: error instanceof Error ? error.message : String(error),
                orderId: event.orderId
            });
            return {
                status: 'PROCESSING_FAILED',
                orderId: event.orderId,
                message: `Order processing failed during cancellation check: ${error instanceof Error ? error.message : String(error)}`,
                validationResult: validationResult.message,
                processingTime: {
                    orderReceived,
                    validationCompleted: validationResult.timestamp,
                    orderCompleted: new Date().toISOString()
                }
            };
        }

        if (cancellationCheck.isCancelled) {
            context.logger.warn('Order was cancelled by user');
            return buildCancelledResponse(
                event,
                validationResult.message,
                orderReceived,
                validationResult.timestamp,
                cancellationCheck.timestamp
            );
        }

        context.logger.info('Order not cancelled, proceeding to inventory and payment');

        // -------------------------------------------------------------------
        // Saga section: steps that create side effects register compensations
        // -------------------------------------------------------------------
        const compensations: CompensationEntry[] = [];

        try {
            // Step 3: Reserve inventory
            const reservation = await context.step('reserve-inventory', async (stepCtx) => {
                return reserveInventory(event, stepCtx.logger);
            }, { retryStrategy: createRetryStrategy() });

            context.logger.info('Inventory reserved', { reservationId: reservation.reservationId });

            // Register compensation so inventory is released on downstream failure
            compensations.push({
                name: 'release-inventory',
                fn: async () => {
                    await context.step('compensate-release-inventory', async (stepCtx) => {
                        return releaseInventory(reservation.reservationId, event.orderId, stepCtx.logger);
                    });
                },
            });

            // Step 4: Invoke payment processor
            const paymentResult = await context.invoke<Order, PaymentResult>(
                'process-payment',
                PAYMENT_PROCESSOR.functionName,
                {
                    orderId: event.orderId,
                    customerId: event.customerId,
                    amount: event.amount
                }
            );

            context.logger.info('Payment processing completed', { paymentResult });

            // Check for business-level rejection
            if (!paymentResult.paymentApproved) {
                throw Object.assign(
                    new Error(`Payment rejected: ${paymentResult.reason || 'rejected'}`),
                    { paymentResult }
                );
            }

            // Happy path: everything succeeded
            return buildCompletedResponse(
                event,
                paymentResult,
                validationResult.message,
                orderReceived,
                validationResult.timestamp,
                cancellationCheck.timestamp,
                reservation.reservationId
            );

        } catch (error: any) {
            // Execute compensations in reverse order
            context.logger.error('Order processing failed, executing compensations', {
                error: error instanceof Error ? error.message : String(error),
                compensationCount: compensations.length,
                orderId: event.orderId,
            });

            const compensationActions: CompensationAction[] = [];
            for (const comp of compensations.reverse()) {
                const timestamp = new Date().toISOString();
                try {
                    context.logger.info(`Executing compensation: ${comp.name}`);
                    await comp.fn();
                    compensationActions.push({ action: comp.name, success: true, timestamp });
                } catch (compError) {
                    context.logger.error(`Compensation failed: ${comp.name}`, {
                        error: compError instanceof Error ? compError.message : String(compError),
                    });
                    compensationActions.push({
                        action: comp.name,
                        success: false,
                        timestamp,
                        error: compError instanceof Error ? compError.message : String(compError),
                    });
                }
            }

            return buildCompensatedFailureResponse(
                event,
                error instanceof Error ? error.message : String(error),
                validationResult.message,
                orderReceived,
                validationResult.timestamp,
                cancellationCheck.timestamp,
                compensationActions,
                undefined,
                error.paymentResult
            );
        }
    }
);
