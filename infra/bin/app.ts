#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { KiroGatewayStack } from "../lib/kiro-gateway-stack";

const app = new cdk.App();

new KiroGatewayStack(app, "KiroGatewayStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? "us-east-1",
  },
});
