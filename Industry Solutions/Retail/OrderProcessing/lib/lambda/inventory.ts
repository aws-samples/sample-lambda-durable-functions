/**
 * Inventory management utilities (simulated)
 *
 * In production, these functions would call an inventory service or database.
 * For this demo they log their actions and return result objects, keeping the
 * example self-contained while demonstrating the saga compensation pattern.
 */

import { DurableContextLogger, DurableLogger } from '@aws/durable-execution-sdk-js';
import { Order, ReservationResult } from './types';

/**
 * Reserves inventory for an order.
 * Creates a side effect that must be compensated if a downstream step fails.
 */
export function reserveInventory(
    order: Order,
    stepCtx: DurableContextLogger<DurableLogger>
): ReservationResult {
    const reservationId = `RSV-${order.orderId}-${Date.now()}`;
    const timestamp = new Date().toISOString();

    stepCtx.info('Inventory reserved', {
        reservationId,
        orderId: order.orderId,
        amount: order.amount,
        timestamp,
    });

    return {
        reservationId,
        orderId: order.orderId,
        amount: order.amount,
        timestamp,
    };
}

/**
 * Releases a previously reserved inventory allocation (saga compensation).
 * Called when a downstream step such as payment fails and the reservation
 * must be rolled back.
 */
export function releaseInventory(
    reservationId: string,
    orderId: string,
    stepCtx: DurableContextLogger<DurableLogger>
): { released: boolean; reservationId: string; timestamp: string } {
    const timestamp = new Date().toISOString();

    stepCtx.info('Inventory reservation released (compensation)', {
        reservationId,
        orderId,
        timestamp,
    });

    return {
        released: true,
        reservationId,
        timestamp,
    };
}
