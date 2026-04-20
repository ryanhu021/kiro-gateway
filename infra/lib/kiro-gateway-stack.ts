import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecs_patterns from "aws-cdk-lib/aws-ecs-patterns";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as logs from "aws-cdk-lib/aws-logs";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as iam from "aws-cdk-lib/aws-iam";
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
            CLOUDWATCH_METRICS_ENABLED: "true",
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

    // Increase ALB idle timeout for long-running LLM requests (default 60s)
    service.loadBalancer.setAttribute(
      "idle_timeout.timeout_seconds",
      "600"
    );

    // Grant CloudWatch PutMetricData to the task role
    service.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
      })
    );

    // ---------------------------------------------------------------
    // CloudWatch Dashboard
    // ---------------------------------------------------------------
    const metricsNamespace = "KiroGateway";

    const dashboard = new cloudwatch.Dashboard(this, "Dashboard", {
      dashboardName: "KiroGateway",
      defaultInterval: cdk.Duration.hours(3),
    });

    // Use raw CloudFormation to define SEARCH-based widgets
    const cfnDashboard = dashboard.node.defaultChild as cloudwatch.CfnDashboard;
    cfnDashboard.addPropertyOverride("DashboardBody", JSON.stringify({
      widgets: [
        // Row 1: Request volume & errors
        {
          type: "metric", x: 0, y: 0, width: 8, height: 6,
          properties: {
            title: "Request Count",
            metrics: [
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model,status_code} MetricName="RequestCount"', 'Sum', 60)`, id: "e1" }],
            ],
            view: "timeSeries", stacked: false, region: cdk.Aws.REGION, period: 60,
          },
        },
        {
          type: "metric", x: 8, y: 0, width: 8, height: 6,
          properties: {
            title: "Error Count",
            metrics: [
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model,error_type} MetricName="ErrorCount"', 'Sum', 60)`, id: "e1" }],
            ],
            view: "timeSeries", stacked: false, region: cdk.Aws.REGION, period: 60,
          },
        },
        {
          type: "metric", x: 16, y: 0, width: 8, height: 6,
          properties: {
            title: "Retry Count",
            metrics: [
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="RetryCount"', 'Sum', 60)`, id: "e1" }],
            ],
            view: "timeSeries", stacked: false, region: cdk.Aws.REGION, period: 60,
          },
        },
        // Row 2: Latency
        {
          type: "metric", x: 0, y: 6, width: 8, height: 6,
          properties: {
            title: "Duration (ms) — p50 / p90 / p99",
            metrics: [
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="Duration"', 'p50', 60)`, id: "e1", label: "p50" }],
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="Duration"', 'p90', 60)`, id: "e2", label: "p90" }],
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="Duration"', 'p99', 60)`, id: "e3", label: "p99" }],
            ],
            view: "timeSeries", stacked: false, region: cdk.Aws.REGION, period: 60,
          },
        },
        {
          type: "metric", x: 8, y: 6, width: 8, height: 6,
          properties: {
            title: "Kiro Upstream Duration (ms) — p50 / p90 / p99",
            metrics: [
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="KiroDuration"', 'p50', 60)`, id: "e1", label: "p50" }],
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="KiroDuration"', 'p90', 60)`, id: "e2", label: "p90" }],
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="KiroDuration"', 'p99', 60)`, id: "e3", label: "p99" }],
            ],
            view: "timeSeries", stacked: false, region: cdk.Aws.REGION, period: 60,
          },
        },
        {
          type: "metric", x: 16, y: 6, width: 8, height: 6,
          properties: {
            title: "First Token Latency (ms) — p50 / p90 / p99",
            metrics: [
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="FirstTokenLatency"', 'p50', 60)`, id: "e1", label: "p50" }],
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="FirstTokenLatency"', 'p90', 60)`, id: "e2", label: "p90" }],
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="FirstTokenLatency"', 'p99', 60)`, id: "e3", label: "p99" }],
            ],
            view: "timeSeries", stacked: false, region: cdk.Aws.REGION, period: 60,
          },
        },
        // Row 3: Tokens
        {
          type: "metric", x: 0, y: 12, width: 12, height: 6,
          properties: {
            title: "Input Tokens",
            metrics: [
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="InputTokens"', 'Sum', 60)`, id: "e1", label: "Sum" }],
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="InputTokens"', 'Average', 60)`, id: "e2", label: "Avg" }],
            ],
            view: "timeSeries", stacked: false, region: cdk.Aws.REGION, period: 60,
          },
        },
        {
          type: "metric", x: 12, y: 12, width: 12, height: 6,
          properties: {
            title: "Output Tokens",
            metrics: [
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="OutputTokens"', 'Sum', 60)`, id: "e1", label: "Sum" }],
              [{ expression: `SEARCH('{${metricsNamespace},api_format,model} MetricName="OutputTokens"', 'Average', 60)`, id: "e2", label: "Avg" }],
            ],
            view: "timeSeries", stacked: false, region: cdk.Aws.REGION, period: 60,
          },
        },
      ],
    }));

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
