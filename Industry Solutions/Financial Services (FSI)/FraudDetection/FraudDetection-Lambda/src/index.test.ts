import { LocalDurableTestRunner, OperationStatus, OperationType, WaitingOperationStatus } from "@aws/durable-execution-sdk-js-testing";
import { handler } from "./index";

describe("Fraud Detection Durable Function", () => {
    beforeAll(() => LocalDurableTestRunner.setupTestEnvironment({ skipTime: true }));
    afterAll(() => LocalDurableTestRunner.teardownTestEnvironment());

    describe("Low Risk Transactions (score < 3)", () => {
        it("should authorize transaction immediately when score is 1", async () => {
            const runner = new LocalDurableTestRunner({
                handlerFunction: handler,
            });

            const execution = await runner.run({
                payload: {
                    id: 1001,
                    amount: 500,
                    location: "New York, NY",
                    vendor: "Amazon.com",
                    score: 1,
                },
            });

            const result = execution.getResult() as any;
            expect(result.statusCode).toBe(200);
            expect(result.body.transaction_id).toBe(1001);
            expect(result.body.result).toBe("authorized");
            expect(result.body.fraud_score).toBe(1);

            // Verify operations: fraudCheck + authorize
            const fraudCheckOp = runner.getOperation("fraudCheck");
            expect(fraudCheckOp.getType()).toBe(OperationType.STEP);
            expect(fraudCheckOp.getStatus()).toBe(OperationStatus.SUCCEEDED);

            const authorizeOp = runner.getOperation("authorize-1001");
            expect(authorizeOp.getType()).toBe(OperationType.STEP);
            expect(authorizeOp.getStatus()).toBe(OperationStatus.SUCCEEDED);
        });

        it("should authorize transaction when score is 2", async () => {
            const runner = new LocalDurableTestRunner({
                handlerFunction: handler,
            });

            const execution = await runner.run({
                payload: {
                    id: 1002,
                    amount: 250,
                    location: "Chicago, IL",
                    vendor: "Target",
                    score: 2,
                },
            });

            const result = execution.getResult() as any;
            expect(result.statusCode).toBe(200);
            expect(result.body.result).toBe("authorized");
        });
    });

    describe("High Risk Transactions (score >= 5)", () => {
        it("should send to fraud department when score is 5", async () => {
            const runner = new LocalDurableTestRunner({
                handlerFunction: handler,
            });

            const execution = await runner.run({
                payload: {
                    id: 2001,
                    amount: 10000,
                    location: "Unknown",
                    vendor: "Suspicious Store",
                    score: 5,
                },
            });

            const result = execution.getResult() as any;
            expect(result.statusCode).toBe(200);
            expect(result.body.transaction_id).toBe(2001);
            expect(result.body.result).toBe("SentToFraudDept");
            expect(result.body.fraud_score).toBe(5);

            // Verify operations: fraudCheck + sendToFraudDepartment
            const fraudCheckOp = runner.getOperation("fraudCheck");
            expect(fraudCheckOp.getType()).toBe(OperationType.STEP);
            expect(fraudCheckOp.getStatus()).toBe(OperationStatus.SUCCEEDED);

            const fraudDeptOp = runner.getOperation("sendToFraudDepartment-2001");
            expect(fraudDeptOp.getType()).toBe(OperationType.STEP);
            expect(fraudDeptOp.getStatus()).toBe(OperationStatus.SUCCEEDED);
        });
    });

    describe("Medium Risk Transactions (score 3-4) - Human Verification", () => {
        it("should authorize when customer approves via email callback", async () => {
            const runner = new LocalDurableTestRunner({
                handlerFunction: handler,
            });

            // Start execution (will pause at callback)
            const executionPromise = runner.run({
                payload: {
                    id: 3001,
                    amount: 6500,
                    location: "Los Angeles, CA",
                    vendor: "Electronics Store",
                    score: 3,
                },
            });

            // Get email callback operation and wait for it to start
            const emailCallbackOp = runner.getOperation("SendVerificationEmail");
            await emailCallbackOp.waitForData(WaitingOperationStatus.STARTED);

            // Customer approves via email
            await emailCallbackOp.sendCallbackSuccess(
                JSON.stringify({ approved: true, channel: "email" })
            );

            const execution = await executionPromise;
            const result = execution.getResult() as any;

            expect(result.statusCode).toBe(200);
            expect(result.body.transaction_id).toBe(3001);
            expect(result.body.result).toBe("authorized");
            expect(result.body.customerVerificationResult).toBe("TransactionApproved");

            // Verify the finalize step
            const advanceOp = runner.getOperation("finalize-3001");
            expect(advanceOp.getType()).toBe(OperationType.STEP);
            expect(advanceOp.getStatus()).toBe(OperationStatus.SUCCEEDED);
        });

        it("should authorize when customer approves via SMS callback", async () => {
            const runner = new LocalDurableTestRunner({
                handlerFunction: handler,
            });

            const executionPromise = runner.run({
                payload: {
                    id: 3002,
                    amount: 4500,
                    location: "Miami, FL",
                    vendor: "Jewelry Store",
                    score: 4,
                },
            });

            // Wait for SMS callback to start
            const smsCallbackOp = runner.getOperation("SendVerificationSMS");
            await smsCallbackOp.waitForData(WaitingOperationStatus.STARTED);

            // Customer approves via SMS
            await smsCallbackOp.sendCallbackSuccess(
                JSON.stringify({ approved: true, channel: "sms" })
            );

            const execution = await executionPromise;
            const result = execution.getResult() as any;

            expect(result.statusCode).toBe(200);
            expect(result.body.result).toBe("authorized");
            expect(result.body.customerVerificationResult).toBe("TransactionApproved");
        });

        it("should verify parallel operation structure exists", async () => {
            const runner = new LocalDurableTestRunner({
                handlerFunction: handler,
            });

            const executionPromise = runner.run({
                payload: {
                    id: 3003,
                    amount: 7000,
                    location: "San Francisco, CA",
                    vendor: "Tech Store",
                    score: 3,
                },
            });

            // Wait for callbacks to start
            const emailCallbackOp = runner.getOperation("SendVerificationEmail");
            await emailCallbackOp.waitForData(WaitingOperationStatus.STARTED);

            // Verify parallel operation exists
            const parallelOp = runner.getOperation("human-verification");
            expect(parallelOp).toBeDefined();

            // Complete via email
            await emailCallbackOp.sendCallbackSuccess(
                JSON.stringify({ approved: true })
            );

            await executionPromise;
        });
    });
});
