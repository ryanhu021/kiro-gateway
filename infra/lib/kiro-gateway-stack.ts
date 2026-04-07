import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecs_patterns from "aws-cdk-lib/aws-ecs-patterns";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import * as path from "path";

export class KiroGatewayStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // VPC with public + private subnets across 2 AZs
    const vpc = new ec2.Vpc(this, "Vpc", {
      maxAzs: 2,
      natGateways: 1,
    });

    // ECS Cluster
    const cluster = new ecs.Cluster(this, "Cluster", { vpc });

    // Secrets in Secrets Manager
    const kiroApiKeySecret = new secretsmanager.Secret(this, "KiroApiKey", {
      secretName: "kiro-gateway/kiro-api-key",
      description: "Kiro API key for gateway authentication",
    });

    const proxyApiKeySecret = new secretsmanager.Secret(this, "ProxyApiKey", {
      secretName: "kiro-gateway/proxy-api-key",
      description: "Password to protect the proxy server",
    });

    // Fargate service with ALB
    const service = new ecs_patterns.ApplicationLoadBalancedFargateService(
      this,
      "Service",
      {
        cluster,
        cpu: 512,
        memoryLimitMiB: 1024,
        desiredCount: 1,
        publicLoadBalancer: true,
        taskImageOptions: {
          image: ecs.ContainerImage.fromAsset(path.join(__dirname, "../.."), {
            platform: cdk.aws_ecr_assets.Platform.LINUX_AMD64,
          }),
          containerPort: 8000,
          environment: {
            SERVER_HOST: "0.0.0.0",
            SERVER_PORT: "8000",
            LOG_LEVEL: "INFO",
          },
          secrets: {
            KIRO_API_KEY: ecs.Secret.fromSecretsManager(kiroApiKeySecret),
            PROXY_API_KEY: ecs.Secret.fromSecretsManager(proxyApiKeySecret),
          },
          logDriver: ecs.LogDrivers.awsLogs({
            streamPrefix: "kiro-gateway",
            logRetention: logs.RetentionDays.TWO_WEEKS,
          }),
        },
        healthCheck: {
          command: [
            "CMD-SHELL",
            "python -c \"import httpx; httpx.get('http://localhost:8000/health', timeout=5)\" || exit 1",
          ],
          interval: cdk.Duration.seconds(30),
          timeout: cdk.Duration.seconds(10),
          retries: 3,
          startPeriod: cdk.Duration.seconds(30),
        },
      }
    );

    // ALB health check on the target group
    service.targetGroup.configureHealthCheck({
      path: "/health",
      interval: cdk.Duration.seconds(30),
      healthyThresholdCount: 2,
      unhealthyThresholdCount: 3,
    });

    // Outputs
    new cdk.CfnOutput(this, "LoadBalancerDNS", {
      value: service.loadBalancer.loadBalancerDnsName,
      description: "ALB DNS name for the Kiro Gateway",
    });

    new cdk.CfnOutput(this, "ServiceURL", {
      value: `http://${service.loadBalancer.loadBalancerDnsName}`,
      description: "Gateway URL",
    });
  }
}
