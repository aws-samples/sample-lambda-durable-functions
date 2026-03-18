import * as cdk from 'aws-cdk-lib/core';
import { OrderProcessingStack } from '../lib/order-processing-stack';

const app = new cdk.App();
new OrderProcessingStack(app, 'OrderProcessingStack', {
  /* Specialize this stack for the AWS Account and Region that are 
   * implied by the current CLI configuration. */
  env: { 
    account: process.env.CDK_DEFAULT_ACCOUNT, 
    region: process.env.CDK_DEFAULT_REGION 
  },

  /* For more information, see https://docs.aws.amazon.com/cdk/latest/guide/environments.html */
});
