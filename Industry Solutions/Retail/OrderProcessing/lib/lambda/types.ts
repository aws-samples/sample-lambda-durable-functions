/**
 * Shared type definitions for order processing workflow
 */

/**
 * Order information passed between functions
 */
export interface Order {
    orderId: string;
    customerId: string;
    amount: number;
}

/**
 * Order processing status
 */
export type OrderStatus = 'PAYMENT_COMPLETED' | 'PAYMENT_FAILED' | 'CANCELLED' | 'VALIDATION_FAILED' | 'PROCESSING_FAILED';

/**
 * Result from inventory reservation
 */
export interface ReservationResult {
    reservationId: string;
    orderId: string;
    amount: number;
    timestamp: string;
}

/**
 * Record of a compensation action executed during saga rollback
 */
export interface CompensationAction {
    action: string;
    success: boolean;
    timestamp: string;
    error?: string;
}

/**
 * Result returned by the order processor
 */
export interface OrderResult {
    status: OrderStatus;
    orderId: string;
    message: string;
    validationResult?: string;
    paymentResult?: PaymentResult;
    reservationId?: string;
    compensationActions?: CompensationAction[];
    processingTime: ProcessingTimestamps;
}

/**
 * Payment processing result
 */
export interface PaymentResult {
    paymentApproved: boolean;
    orderId: string;
    customerId: string;
    amount: number;
    timestamp: string;
    callbackId?: string;
    reason?: string;
}

/**
 * Validation step result
 */
export interface ValidationResult {
    isValid: boolean;
    message: string;
    timestamp: string;
}

/**
 * Cancellation check result
 */
export interface CancellationCheckResult {
    isCancelled: boolean;
    timestamp: string;
}

/**
 * Processing timestamps for tracking workflow progress
 */
export interface ProcessingTimestamps {
    orderReceived: string;
    validationCompleted?: string;
    cancellationChecked?: string;
    paymentCompleted?: string;
    orderCompleted: string;
}

/**
 * Callback result from payment approval
 */
export interface PaymentCallbackResult {
    approved: boolean;
    reason?: string;
}
