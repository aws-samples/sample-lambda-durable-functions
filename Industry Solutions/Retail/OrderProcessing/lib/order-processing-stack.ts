import * as cdk from 'aws-cdk-lib/core';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as nodejs from 'aws-cdk-lib/aws-lambda-nodejs';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as path from 'path';

export class OrderProcessingStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Explicitly create and manage log groups with proper cleanup on destroy
    const paymentProcessorLogGroup = new logs.LogGroup(this, 'PaymentProcessorLogGroup', {
      logGroupName: '/aws/lambda/payment-processor',
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Define the Payment Processor durable Lambda function
    const paymentProcessor = new nodejs.NodejsFunction(this, 'PaymentProcessorFunction', {
      functionName: 'payment-processor',
      runtime: lambda.Runtime.NODEJS_22_X,
      handler: 'handler',
      entry: path.join(__dirname, 'lambda', 'payment-processor.ts'),
      timeout: cdk.Duration.minutes(1),
      memorySize: 256,
      durableConfig: {
        executionTimeout: cdk.Duration.minutes(10),
        retentionPeriod: cdk.Duration.days(1),
      },
      bundling: {
        minify: true,
        sourceMap: true,
        format: nodejs.OutputFormat.ESM,
        banner: "import { createRequire } from 'module';const require = createRequire(import.meta.url);",
        externalModules: [], // Bundle all dependencies
      },
      environment: {
        NODE_OPTIONS: '--enable-source-maps',
      },
      logGroup: paymentProcessorLogGroup, // Link to our managed log group
    });

    // Add durable execution policy to payment processor
    paymentProcessor.role?.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicDurableExecutionRolePolicy')
    );

    const orderProcessorLogGroup = new logs.LogGroup(this, 'OrderProcessorLogGroup', {
      logGroupName: '/aws/lambda/order-processor',
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Define the Order Processor durable Lambda function
    const orderProcessor = new nodejs.NodejsFunction(this, 'OrderProcessorFunction', {
      functionName: 'order-processor',
      runtime: lambda.Runtime.NODEJS_22_X,
      handler: 'handler',
      entry: path.join(__dirname, 'lambda', 'order-processor.ts'),
      timeout: cdk.Duration.minutes(1),
      memorySize: 256,
      durableConfig: {
        executionTimeout: cdk.Duration.minutes(15),
        retentionPeriod: cdk.Duration.days(1),
      },
      bundling: {
        minify: true,
        sourceMap: true,
        format: nodejs.OutputFormat.ESM,
        banner: "import { createRequire } from 'module';const require = createRequire(import.meta.url);",
        externalModules: [], // Bundle all dependencies
      },
      environment: {
        NODE_OPTIONS: '--enable-source-maps',
        PAYMENT_PROCESSOR_FUNCTION_NAME: `${paymentProcessor.functionName}:$LATEST`,
        BEDROCK_MODEL_ID: 'amazon.nova-lite-v1:0',
      },
      logGroup: orderProcessorLogGroup, // Link to our managed log group
    });

    // Add durable execution policy to order processor
    orderProcessor.role?.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicDurableExecutionRolePolicy')
    );

    // Grant order processor permission to invoke payment processor
    paymentProcessor.grantInvoke(orderProcessor);

    // Grant order processor permission to invoke Bedrock (Amazon Nova Lite)
    orderProcessor.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [
          // Amazon Nova Lite foundation model
          `arn:aws:bedrock:*::foundation-model/amazon.nova-lite-v1:0`,
        ],
      })
    );

    // Output the function ARNs and names
    new cdk.CfnOutput(this, 'OrderProcessorFunctionArn', {
      value: orderProcessor.functionArn,
      description: 'ARN of the order processor durable Lambda function',
    });

    new cdk.CfnOutput(this, 'OrderProcessorFunctionName', {
      value: orderProcessor.functionName,
      description: 'Name of the order processor durable Lambda function',
    });

    new cdk.CfnOutput(this, 'PaymentProcessorFunctionArn', {
      value: paymentProcessor.functionArn,
      description: 'ARN of the payment processor durable Lambda function',
    });

    new cdk.CfnOutput(this, 'PaymentProcessorFunctionName', {
      value: paymentProcessor.functionName,
      description: 'Name of the payment processor durable Lambda function',
    });
  }
}
