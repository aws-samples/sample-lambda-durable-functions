/**
 * Order Processor Helpers - Response builder utilities for order processing
 */

import { CompensationAction, Order, OrderResult, OrderStatus, PaymentResult } from './types';

/**
 * Builds a validation failed response
 */
export function buildValidationFailedResponse(
    order: Order,
    validationMessage: string,
    orderReceived: string,
    validationTimestamp: string
): OrderResult {
    return {
        status: 'VALIDATION_FAILED',
        orderId: order.orderId || 'unknown',
        message: `Order validation failed: ${validationMessage}`,
        validationResult: validationMessage,
        processingTime: {
            orderReceived,
            validationCompleted: validationTimestamp,
            orderCompleted: new Date().toISOString()
        }
    };
}

/**
 * Builds a cancelled order response
 */
export function buildCancelledResponse(
    order: Order,
    validationMessage: string,
    orderReceived: string,
    validationTimestamp: string,
    cancellationTimestamp: string
): OrderResult {
    return {
        status: 'CANCELLED',
        orderId: order.orderId,
        message: 'Order was cancelled by user before payment processing',
        validationResult: validationMessage,
        processingTime: {
            orderReceived,
            validationCompleted: validationTimestamp,
            cancellationChecked: cancellationTimestamp,
            orderCompleted: new Date().toISOString()
        }
    };
}

/**
 * Builds a completed order response (payment approved or rejected without compensation)
 */
export function buildCompletedResponse(
    order: Order,
    paymentResult: PaymentResult,
    validationMessage: string,
    orderReceived: string,
    validationTimestamp: string,
    cancellationTimestamp: string,
    reservationId?: string
): OrderResult {
    const status: OrderStatus = paymentResult.paymentApproved ? 'PAYMENT_COMPLETED' : 'PAYMENT_FAILED';

    return {
        status,
        orderId: order.orderId,
        message: paymentResult.paymentApproved
            ? `Order completed successfully. Payment of ${order.amount} approved.`
            : `Order failed. Payment was ${paymentResult.reason || 'rejected'}.`,
        validationResult: validationMessage,
        paymentResult,
        reservationId,
        processingTime: {
            orderReceived,
            validationCompleted: validationTimestamp,
            cancellationChecked: cancellationTimestamp,
            paymentCompleted: paymentResult.timestamp,
            orderCompleted: new Date().toISOString()
        }
    };
}

/**
 * Builds a response for a failed order where saga compensations were executed
 */
export function buildCompensatedFailureResponse(
    order: Order,
    errorMessage: string,
    validationMessage: string,
    orderReceived: string,
    validationTimestamp: string,
    cancellationTimestamp: string,
    compensationActions: CompensationAction[],
    reservationId?: string,
    paymentResult?: PaymentResult
): OrderResult {
    const allCompensationsSucceeded = compensationActions.every(c => c.success);
    const compensationSummary = allCompensationsSucceeded
        ? 'All compensations completed successfully'
        : 'One or more compensations failed';

    return {
        status: 'PAYMENT_FAILED',
        orderId: order.orderId,
        message: `${errorMessage}. ${compensationSummary}.`,
        validationResult: validationMessage,
        paymentResult,
        reservationId,
        compensationActions,
        processingTime: {
            orderReceived,
            validationCompleted: validationTimestamp,
            cancellationChecked: cancellationTimestamp,
            paymentCompleted: paymentResult?.timestamp,
            orderCompleted: new Date().toISOString()
        }
    };
}
