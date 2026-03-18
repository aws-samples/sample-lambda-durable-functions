import { BedrockAgentCoreClient, InvokeAgentRuntimeCommand } from "@aws-sdk/client-bedrock-agentcore";
import { DurableContext, withDurableExecution } from "@aws/durable-execution-sdk-js";

const agentRuntimeArn = process.env.AGENT_RUNTIME_ARN;
const agentRegion = process.env.AGENT_REGION || 'us-east-1';
const client = new BedrockAgentCoreClient({ region: agentRegion });

interface transaction {
    id: number;
    amount: number;
    location: string;
    vendor: string;
    score?: number;
}

interface TransactionResult {
    statusCode: number;
    body: {
        transaction_id: number;
        amount: number;
        fraud_score?: number;
        result?: string;
        customerVerificationResult?: string;
    };

}

class fraudTransaction implements transaction {

    constructor(
        public id: number,
        public amount: number,
        public location: string,
        public vendor: string,
        public score: number = 0
    ) { }

    async authorize(tx: fraudTransaction, cusRejection: boolean = false, options?: { idempotency_key: string }): Promise<TransactionResult> {
        //IMPLEMENT LOGIC TO AUTHORIZE TRANSCATION
        // Best Practice: Use idempotency_key to prevent duplicate charges on step retries
        // Example: payment.charges.create({ amount: tx.amount, idempotency_key: options?.idempotency_key })

        let result: TransactionResult = {
            statusCode: 200,
            body: {
                transaction_id: tx.id,
                amount: tx.amount,
                fraud_score: tx.score,
                result: 'authorized'
            }
        };

        //IF AUTHORIZATION CAME FROM CUSTOMER, INDICATE IN RESPONSE
        if (cusRejection) { result.body.customerVerificationResult = 'TransactionApproved' };

        return result;
    }

    async suspend(tx: fraudTransaction): Promise<boolean> {
        //IMPLEMENT LOGIC TO SUSPEND TRANSCATION
        return true;
    }

    async sendToFraud(tx: fraudTransaction, cusRejection: boolean = false): Promise<TransactionResult> {
        //IMPLEMENT LOGIC TO SEND TO FRAUD DEPARTMENT

        let result: TransactionResult = {
            statusCode: 200,
            body: {
                transaction_id: tx.id,
                amount: tx.amount,
                fraud_score: tx.score,
                result: 'SentToFraudDept'
            }
        };

        //IF DECLINE CAME FROM CUSTOMER, INDICATE IN RESPONSE
        if (cusRejection) { result.body.customerVerificationResult = 'TransactionDeclined' };

        return result;
    }

    async sendCustomerNotification(callbackId: string, type: string, tx: fraudTransaction): Promise<void> {
        //IMPLEMENT LOGIC TO SEND CUSTOMER NOTIFICATION
    }
}



export const handler = withDurableExecution(async (event: transaction, context: DurableContext) => {

    // Extract transaction information
    const tx = new fraudTransaction(event.id, event.amount, event.location, event.vendor, event?.score ?? 0)

    // Step 1: AI fraud assessment with error handling
    tx.score = await context.step("fraudCheck", async () => {
        if (tx.score === 0) {
            try {
                if (!agentRuntimeArn) {
                    throw new Error('AGENT_RUNTIME_ARN environment variable is not set');
                }

                // Prepare the payload for AgentCore
                const payloadJson = JSON.stringify({ input: { amount: tx.amount } });

                // Invoke the agent
                const command = new InvokeAgentRuntimeCommand({
                    agentRuntimeArn: agentRuntimeArn,
                    qualifier: 'DEFAULT',
                    payload: Buffer.from(payloadJson, 'utf-8'),
                    contentType: 'application/json',
                    accept: 'application/json'
                });

                const response = await client.send(command);

                // Parse the streaming response from AgentCore
                if (response.response) {
                    const responseText = await response.response.transformToString();
                    const result = JSON.parse(responseText);
                    return result?.output?.risk_score ?? 5;  // Default to high-risk if score unavailable
                }

                return 5;
            } catch (error) {
                context.logger.error("Fraud check failed", { error, txId: tx.id });
                return 5;  // Default to high-risk on failure
            }
        } else {
            return tx.score;
        }
    });

    context.logger.info("Transaction Score", { score: tx.score, txId: tx.id });

    // Route based on AI decision
    //Low Risk, Authorize Transaction
    if (tx.score < 3) return context.step(`authorize-${tx.id}`, async () => await tx.authorize(tx, false, { idempotency_key: `tx-${tx.id}` }));
    if (tx.score >= 5) return context.step(`sendToFraudDepartment-${tx.id}`, async () => await tx.sendToFraud(tx));  //High Risk, Send to Fraud Department

    // Medium risk: need human verification
    if (tx.score > 2 && tx.score < 5) {

        //Step 2: Suspend the transaction
        await context.step(`suspend-${tx.id}`, async () => await tx.suspend(tx));

        //Step 3: Ask cardholder to authorize transaction
        let verified;
        try {
            verified = await context.parallel("human-verification", [
                // Email verification with callback
                (ctx: DurableContext) => ctx.waitForCallback("SendVerificationEmail",
                    async (callbackId: string) => {
                        await tx.sendCustomerNotification(callbackId, 'email', tx);
                    },
                    { timeout: { days: 1 } }
                ),
                // SMS verification with callback
                (ctx: DurableContext) => ctx.waitForCallback("SendVerificationSMS",
                    async (callbackId: string) => {
                        await tx.sendCustomerNotification(callbackId, 'sms', tx);
                    },
                    { timeout: { days: 1 } }
                )
            ],
                {
                    maxConcurrency: 2,
                    completionConfig: {
                        minSuccessful: 1 // Continue after cardholder verifies (email or sms)
                    }
                }
            );
        } catch (error) {
            const isTimeout = (error instanceof Error && error.message?.includes("timeout")) ||
                (typeof error === 'string' && error.includes("timeout"));
            context.logger.warn(
                isTimeout ? "Customer verification timeout" : "Customer verification failed",
                { error, txId: tx.id }
            );
            // Fallback: escalate to fraud department
            return await context.step(`timeout-escalate-${tx.id}`, async () =>
                tx.sendToFraud(tx, true)
            );
        }

        // Idempotent final step with idempotency key
        return await context.step(`finalize-${tx.id}`, async () => {
            const action = !verified.hasFailure && verified.successCount > 0
                ? "authorize"
                : "escalate";
            if (action === "authorize") {
                // Customer approved via email or SMS - use idempotency key to prevent duplicate charges
                return await tx.authorize(tx, true, { idempotency_key: `finalize-${tx.id}` });
            }
            return await tx.sendToFraud(tx, true);
        });
    }

    return {
        statusCode: 400,
        body: {
            transaction_id: tx.id,
            amount: tx.amount,
            fraud_score: tx.score,
            result: 'Unknown'
        }
    };
});
