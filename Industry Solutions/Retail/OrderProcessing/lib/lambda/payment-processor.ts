/**
 * Payment Processor - Handles payment approval workflow
 * 
 * Flow:
 * 1. Receives order from order processor via durable invocation
 * 2. Creates waitForCallback for human approval
 * 3. Returns payment result based on callback response
 * 
 * Note: Payment approval is sent via AWS CLI using SendDurableExecutionCallbackSuccess
 */

import { DurableContext, withDurableExecution } from '@aws/durable-execution-sdk-js';
import { Order, PaymentResult, PaymentCallbackResult } from './types';
import { TIMEOUTS } from './config';

/**
 * Builds a payment result response
 */
function buildPaymentResult(
    order: Order,
    approved: boolean,
    reason?: string
): PaymentResult {
    const timestamp = new Date().toISOString();
    
    return {
        paymentApproved: approved,
        orderId: order.orderId,
        customerId: order.customerId,
        amount: order.amount,
        timestamp,
        reason: approved ? undefined : (reason || 'Payment rejected by approver')
    };
}

export const handler = withDurableExecution(
    async (event: Order, context: DurableContext): Promise<PaymentResult> => {
        context.logger.info('Payment processor invoked', { order: event });

        try {
            // Wait for human approval via callback (with configured timeout)
            // The callback ID will be logged and can be used with AWS CLI to send the result
            // Note: Callback result is returned as a JSON string that needs to be parsed
            const callbackResult = await context.waitForCallback<string>(
                'wait-for-payment-approval',
                async (callbackId, stepCtx) => {
                    stepCtx.logger.info('Waiting for payment approval callback', {
                        callbackId,
                        orderId: event.orderId,
                        amount: event.amount,
                        timeoutMinutes: TIMEOUTS.paymentCallbackTimeoutMinutes
                    });
                },
                { timeout: { minutes: TIMEOUTS.paymentCallbackTimeoutMinutes } }
            );

            context.logger.info('Callback received', { callbackResult });

            // Parse the JSON string from callback
            let parsedResult: PaymentCallbackResult;
            try {
                parsedResult = JSON.parse(callbackResult);
            } catch (error) {
                context.logger.error('Failed to parse callback result', { 
                    callbackResult, 
                    error: error instanceof Error ? error.message : String(error) 
                });
                return buildPaymentResult(event, false, 'Invalid callback JSON format');
            }

            const approved = parsedResult?.approved ?? false;
            const reason = parsedResult?.reason;

            if (approved) {
                context.logger.info('Payment approved', { 
                    orderId: event.orderId, 
                    amount: event.amount 
                });
            } else {
                context.logger.warn('Payment rejected', { 
                    orderId: event.orderId, 
                    reason 
                });
            }

            return buildPaymentResult(event, approved, reason);
        } catch (error) {
            // Handle timeout or other errors
            context.logger.error('Payment approval failed', { 
                orderId: event.orderId,
                error: error instanceof Error ? error.message : String(error)
            });

            return buildPaymentResult(
                event, 
                false, 
                `Payment approval timeout after ${TIMEOUTS.paymentCallbackTimeoutMinutes} minutes`
            );
        }
    }
);
