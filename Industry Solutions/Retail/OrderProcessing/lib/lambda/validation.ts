/**
 * Order validation utilities using Amazon Bedrock
 */

import { BedrockRuntimeClient, InvokeModelCommand } from '@aws-sdk/client-bedrock-runtime';
import { DurableContextLogger, DurableLogger } from '@aws/durable-execution-sdk-js';
import { Order, ValidationResult } from './types';
import { BEDROCK_CONFIG } from './config';

const bedrockClient = new BedrockRuntimeClient({ region: BEDROCK_CONFIG.region });

/**
 * Builds a validation prompt for Bedrock to check order completeness
 */
export function buildValidationPrompt(order: Order): string {
    return `You are a validator. Check if this order data is complete:
Order ID: ${order.orderId || 'MISSING'}
Customer ID: ${order.customerId || 'MISSING'}
Amount: ${order.amount !== undefined ? order.amount : 'MISSING'}

Respond with only "VALID" if all three fields are present and non-empty, or "INVALID: <reason>" if any are missing.`;
}

/**
 * Checks if required order fields are present
 */
export function checkMissingFields(order: Order): string[] {
    const missingFields: string[] = [];
    if (!order.orderId) missingFields.push('orderId');
    if (!order.customerId) missingFields.push('customerId');
    if (order.amount === undefined || order.amount === null) missingFields.push('amount');
    return missingFields;
}

/**
 * Validates an order using Amazon Bedrock
 * @param order - The order to validate
 * @param stepCtx - Step context logger for scoped logging
 * @returns Validation result with status and message
 */
export async function validateOrderWithBedrock(
    order: Order,
    stepCtx: DurableContextLogger<DurableLogger>
): Promise<ValidationResult> {
    stepCtx.info('Validating order with Bedrock', { order });

    // Simulate flakiness to demonstrate step retries
    // ~50% chance of failure on each attempt
    if (Math.random() < 0.5) {
        throw new Error('Simulated transient failure for retry demonstration');
    }

    // Check for missing fields
    const missingFields = checkMissingFields(order);
    if (missingFields.length > 0) {
        stepCtx.warn('Order has missing fields', { missingFields });
    }

    // Build validation prompt
    const prompt = buildValidationPrompt(order);

    // Call Bedrock for validation (Amazon Nova Lite format)
    const bedrockCommand = new InvokeModelCommand({
        modelId: BEDROCK_CONFIG.modelId,
        body: JSON.stringify({
            messages: [{
                role: 'user',
                content: [{ text: prompt }]
            }],
            inferenceConfig: {
                maxTokens: BEDROCK_CONFIG.maxTokens
            }
        }),
        contentType: 'application/json',
        accept: 'application/json'
    });

    const response = await bedrockClient.send(bedrockCommand);
    const responseBody = JSON.parse(new TextDecoder().decode(response.body));
    const validationText = responseBody.output.message.content[0].text;

    stepCtx.info('Bedrock validation result', { validation: validationText });

    return {
        isValid: validationText.includes('VALID') && !validationText.includes('INVALID'),
        message: validationText,
        timestamp: new Date().toISOString()
    };
}

/**
 * Checks if an order has been cancelled (mock implementation)
 * In production, this would query a database or cache
 * 
 * @param order - The order to check
 * @param stepCtx - Step context logger for scoped logging
 * @returns Cancellation status
 */
export function checkOrderCancellation(
    order: Order,
    stepCtx: DurableContextLogger<DurableLogger>
): { isCancelled: boolean; timestamp: string } {
    stepCtx.info('Checking if order was cancelled', { orderId: order.orderId });

    // Mock cancellation check - in a real system, this would check a database
    // For demo purposes, we'll always return false (not cancelled)
    // You can modify this to return true to test the cancellation flow
    const isCancelled = false; // Change to true to test cancellation

    const timestamp = new Date().toISOString();
    stepCtx.info('Cancellation check result', { isCancelled, timestamp });

    return {
        isCancelled,
        timestamp
    };
}
