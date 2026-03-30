# MiniStack Fidelity Report — AWS Compatibility Audit

**Date:** 2026-03-30
**Codebase:** 35,790 lines across 29 service modules
**Services:** 29 (26 original + CloudFormation + ALB + EFS + EMR)

---

## Service Ratings Summary

| Service | Lines | Rating | Key Strength | Biggest Gap |
|---------|-------|--------|-------------|-------------|
| SQS | 1,273 | 9/10 | FIFO, DLQ, long-poll, ESM, dual protocol | No StartMessageMoveTask |
| S3 | 1,809 | 8/10 | Multipart, versioning, range requests, disk persistence | No event notifications to Lambda/SQS/SNS |
| DynamoDB | 1,709 | 8/10 | Query/Scan expressions, GSI/LSI, TTL, transactions | No DynamoDB Streams |
| Lambda | 1,928 | 8/10 | Real execution (subprocess/docker), ESM, layers, aliases | Only Python/Node runtimes |
| RDS | 1,995 | 8/10 | Real Postgres/MySQL containers via Docker | Snapshots are metadata only |
| ElastiCache | 1,288 | 8/10 | Real Redis/Memcached containers | No Global Datastore |
| SecretsManager | 707 | 8/10 | Versioned secrets, staging labels, rotation stubs | No actual rotation Lambda invocation |
| SES | 1,008 | 8/10 | Dual v1/v2 API, all emails captured for inspection | No bounce/complaint simulation |
| Step Functions | 1,484 | 8/10 | Full ASL engine, Lambda/SQS/SNS/DDB integrations | No Distributed Map |
| Athena | 881 | 8/10 | DuckDB executes real SQL against S3 data | No CTAS/INSERT INTO |
| Cognito | 1,796 | 7/10 | User Pools + Identity Pools, auth flows, MFA config | Tokens are fake (not real JWTs) |
| IAM/STS | 1,499 | 7/10 | Wide API surface (60+ actions) | Policies stored but never enforced |
| CloudWatch Logs | 834 | 7/10 | Log groups/streams, put/get/filter events | Insights queries return empty |
| CloudWatch Metrics | 1,388 | 7/10 | PutMetricData, alarms, dashboards, CBOR support | Alarms never evaluate/trigger |
| SSM | 493 | 7/10 | Complete Parameter Store with history/labels | No SSM documents, Run Command, sessions |
| EventBridge | 1,004 | 7/10 | PutEvents->Lambda, archives, connections, API destinations | No scheduled rules (cron/rate) |
| Kinesis | 864 | 7/10 | Shard model, iterators, consumer registration | No enhanced fan-out |
| CloudFormation | 2,368 | 7/10 | 28 resource types, real cross-service provisioning | No nested stacks, no RDS/ECS/Cognito types |
| API Gateway v1 | 1,520 | 7/10 | Full REST API control plane + data plane | No VTL mapping templates |
| API Gateway v2 | 719 | 7/10 | HTTP API with Lambda proxy execution | No WebSocket APIs |
| EC2 | 2,357 | 7/10 | Deep VPC/networking, EBS volumes/snapshots | No actual compute (metadata only) |
| ECS | 1,232 | 7/10 | Docker integration for RunTask | No Fargate simulation |
| Firehose | 568 | 7/10 | S3 destination actually writes data | No Lambda transform, no other destinations |
| Glue | 1,091 | 7/10 | Full Data Catalog, crawler state machine | No Spark execution |
| SNS | 943 | 7/10 | Lambda/SQS fanout, filter policies | No FIFO topics, no HTTP delivery retries |
| Route 53 | 889 | 6/10 | Full XML wire protocol, health checks | Records not DNS-resolvable |
| ALB/ELBv2 | 1,055 | 6/10 | Control plane + rule matching | No actual traffic routing |
| EFS | 509 | 5/10 | Control plane with mount targets | No real filesystem |
| EMR | 579 | 5/10 | Cluster lifecycle management | No Spark/Hadoop execution |

---

## Detailed Service Assessments

### S3 (8/10)

**What works:** CreateBucket, PutObject, GetObject, DeleteObject, CopyObject, ListObjects (v1+v2), full multipart upload lifecycle, object versioning, range requests (206), bucket policies, CORS, lifecycle rules, encryption config, tagging, notifications config, logging config, website config, ACLs, Content-MD5 validation, optional disk persistence.

**What's missing:**
- Event notifications don't fire (config stored but no Lambda/SQS/SNS dispatch)
- No S3 Select (SelectObjectContent)
- No Object Lock / Governance / Legal Hold
- No bucket replication (CRR/SRR)
- No server-side encryption (config stored, data unencrypted)
- No access points
- No Transfer Acceleration (config stored, no-op)
- No pre-signed URL validation
- No S3 Inventory, Analytics, Intelligent Tiering

**Best for:** Application development, integration testing, IaC testing.

---

### SQS (9/10)

**What works:** Standard + FIFO queues, dead-letter queues (RedrivePolicy), long polling, message attributes, batch operations, ChangeMessageVisibility, FIFO dedup (5-min window), FIFO message-group ordering, dual protocol (Query + JSON), Lambda ESM integration.

**What's missing:**
- No StartMessageMoveTask (DLQ redrive)
- No FIFO high-throughput mode
- No redrive allow policy
- DelaySeconds may not be enforced on receive timing

**Best for:** Message queue development, Lambda trigger testing, async workflow testing.

---

### DynamoDB (8/10)

**What works:** CreateTable, PutItem, GetItem, DeleteItem, UpdateItem, Query, Scan, BatchWriteItem, BatchGetItem, TransactWriteItems, TransactGetItems, GSI + LSI, KeyConditionExpression + FilterExpression, ExpressionAttributeNames/Values, ProjectionExpression, pagination, TTL with background reaper, tags, PITR config.

**What's missing:**
- No DynamoDB Streams (StreamSpecification stored but no change events)
- No PartiQL (ExecuteStatement)
- No parallel Scan (Segment/TotalSegments ignored)
- No table export/import to S3
- GSI eventual consistency not simulated (all queries are strongly consistent)
- Transaction conflict detection simplified

**Best for:** CRUD application development, query testing, data modeling.

---

### Lambda (8/10)

**What works:** CreateFunction, Invoke (RequestResponse/Event/DryRun), versions, aliases, layers, permissions, event source mappings (SQS), function URLs, tags. Real code execution via subprocess (Python/Node) or Docker containers.

**What's missing:**
- Only Python and Node.js runtimes
- No Java, Go, .NET, Ruby, or custom runtimes
- No Lambda Extensions or Layers with native code
- No provisioned concurrency (config stored, not enforced)
- No SnapStart, no response streaming
- ESM only supports SQS (no Kinesis, DynamoDB Streams, Kafka)
- No container image deployment (ZIP only)
- Timeout/memory enforcement is basic

**Best for:** Python/Node Lambda development, SQS-triggered Lambda testing, API Gateway integration.

---

### RDS (8/10)

**What works:** Full instance + cluster lifecycle, subnet groups, parameter groups, snapshots (metadata), read replicas (stub), tags, engine versions. **Docker integration spins up real Postgres/MySQL containers** with actual host:port endpoints.

**What's missing:**
- No Aurora Serverless v2
- No RDS Proxy
- Snapshots are metadata only (no data backup/restore)
- Read replicas are stubs
- No Performance Insights or Enhanced Monitoring

**Best for:** Integration testing against real databases, IaC testing, connection management testing.

---

### Step Functions (8/10)

**What works:** Full ASL execution engine — Pass, Task, Choice, Wait, Succeed, Fail, Parallel, Map states. Task states invoke Lambda. Service integrations: SQS SendMessage, SNS Publish, DynamoDB PutItem/GetItem/DeleteItem/UpdateItem, ECS RunTask. Background thread execution. SendTaskSuccess/Failure/Heartbeat for activities.

**What's missing:**
- No Distributed Map
- No Express Workflows optimization
- No intrinsic functions beyond basic JsonPath
- No Map state concurrency limits

**Best for:** Workflow development, state machine testing, service integration testing.

---

### EC2 (7/10)

**What works:** RunInstances (metadata only), security groups with ingress/egress rules, key pairs, full VPC networking (VPCs, subnets, internet gateways, route tables, routes, NAT gateways, network ACLs, VPC peering, DHCP options, flow logs, egress-only IGWs), EBS volumes + snapshots, elastic IPs, network interfaces, VPC endpoints, tags.

**What's missing:**
- Instances are metadata only (no actual compute)
- No AMI management (RegisterImage)
- No launch templates
- No placement groups, spot instances, reserved instances
- No transit gateways
- No auto-scaling groups (separate service)

**Best for:** VPC/networking IaC testing, security group management, CloudFormation template testing.

---

### CloudFormation (7/10)

**What works:** 28 resource types with real cross-service provisioning, 14 intrinsic functions, topological dependency resolution, rollback on failure, change sets, stack events, parameter validation, YAML + JSON templates.

**What's missing:**
- No nested stacks (AWS::CloudFormation::Stack)
- No stack sets
- No custom resources (Lambda-backed)
- Many resource types unsupported (RDS, ECS, Cognito, Route53, ElastiCache, Kinesis, Glue, Athena, etc.)
- No Fn::Transform / macros
- No SAM transform
- No drift detection

**Best for:** Deploying serverless + networking templates locally, CI/CD template validation.

---

### Cognito (7/10)

**What works:** User Pool full lifecycle, clients, users, groups, auth flows (AdminInitiateAuth, InitiateAuth with USER_PASSWORD_AUTH/ADMIN_NO_SRP_AUTH), SignUp/ConfirmSignUp, ForgotPassword, MFA config, Identity Pools with GetId/GetCredentials, tags.

**What's missing:**
- Tokens are fake (not real signed JWTs)
- No hosted UI or OAuth2 authorization code flow
- No SAML/OIDC federation
- No user migration Lambda triggers
- No MFA enforcement (config stored only)

**Best for:** Auth flow development, user management testing, token-based app development (with fake tokens).

---

### IAM/STS (7/10)

**What works:** Users, roles, policies (managed + inline), groups, access keys, instance profiles, OIDC providers, service-linked roles, policy simulation (SimulateCustomPolicy/SimulatePrincipalPolicy), tags. AssumeRole returns fake temporary credentials.

**What's missing:**
- **Policies are never enforced** — any request succeeds regardless of permissions
- No SAML providers
- No MFA device management
- No password policies, credential reports
- No permission boundaries
- No service control policies

**Best for:** IaC testing, resource creation that requires IAM roles, credential management.

---

### EventBridge (7/10)

**What works:** Event buses, rules, targets, PutEvents with Lambda dispatch, archives, connections, API destinations, permissions, tags.

**What's missing:**
- No scheduled rules (cron/rate expressions)
- No event pattern matching (beyond Lambda dispatch)
- No cross-account events
- No schema registry, pipes, or replay

**Best for:** Event-driven Lambda invocation, rule management.

---

### Remaining Services (Brief)

**CloudWatch Logs (7/10):** Good for capturing logs. Insights queries return empty. Subscription filters stored but don't forward.

**CloudWatch Metrics (7/10):** Stores metrics and alarm configs. Supports CBOR protocol. Alarms never evaluate or trigger actions.

**SSM (7/10):** Complete Parameter Store with versioning and history. No documents, Run Command, or sessions.

**Kinesis (7/10):** Full shard model with iterators. No enhanced fan-out or KCL support.

**Glue (7/10):** Complete Data Catalog. Job execution via basic Python subprocess. No Spark.

**SES (8/10):** Dual v1/v2 API. All emails captured for testing. No actual delivery.

**Firehose (7/10):** S3 destination writes real data. No Lambda transforms or other destinations.

**Route 53 (6/10):** Full control plane with XML protocol. Records are not DNS-resolvable.

**ALB/ELBv2 (6/10):** Control plane with rule matching. No actual traffic routing.

**EFS (5/10):** Control plane stub. No real filesystem.

**EMR (5/10):** Cluster lifecycle. No Spark/Hadoop execution.

---

## Underlying Store Patterns

| Pattern | Services | Description |
|---------|----------|-------------|
| **Pure in-memory dicts** | Most services | Module-level Python dicts, cleared on reset(), lost on restart |
| **In-memory + Docker containers** | RDS, ElastiCache, ECS, Lambda (docker mode) | Control plane in memory, data plane in real containers |
| **In-memory + optional disk** | S3 | `S3_PERSIST=1` writes objects to filesystem |
| **In-memory + DuckDB** | Athena | SQL executed against S3 data via DuckDB |
| **In-memory + subprocess** | Lambda (local mode), Glue | Code executed in subprocess with piped I/O |

---

## TODO: Implementable Improvements

### High Impact (would unlock major use cases)

- [ ] **DynamoDB Streams** — Emit change events, wire to Lambda ESM. Unlocks event-driven DynamoDB patterns. ~400-600 lines.
- [ ] **S3 Event Notifications** — Fire events to Lambda/SQS/SNS on PutObject/DeleteObject. Config already stored, just needs dispatch. ~200-300 lines.
- [ ] **EventBridge Scheduled Rules** — Execute cron/rate rules to invoke Lambda targets. ~200-300 lines.
- [ ] **CloudWatch Alarm Evaluation** — Periodically evaluate alarm thresholds, trigger SNS/Lambda actions. ~300-400 lines.
- [ ] **SNS HTTP Endpoint Delivery** — Actually POST to HTTP/HTTPS subscribers with retry. Many integration tests need this. ~100-150 lines.

### Medium Impact (fills notable gaps)

- [ ] **Lambda Kinesis/DynamoDB Streams ESM** — Add event source mapping support for Kinesis and DynamoDB Streams (requires DynamoDB Streams first). ~300-400 lines.
- [ ] **CloudFormation: Nested Stacks** — Support AWS::CloudFormation::Stack for CDK asset pipeline. ~200-300 lines.
- [ ] **CloudFormation: More Resource Types** — Add RDS::DBInstance, ECS::Cluster/Service/TaskDefinition, Cognito::UserPool, Route53::HostedZone, ElastiCache::CacheCluster, Kinesis::Stream. ~400-600 lines.
- [ ] **SNS FIFO Topics** — .fifo suffix, MessageGroupId, MessageDeduplicationId (mirrors SQS FIFO). ~200-300 lines.
- [ ] **IAM Policy Evaluation** — Basic allow/deny evaluation for SimulateCustomPolicy. ~300-500 lines.
- [ ] **Cognito Real JWTs** — Sign tokens with a local key so apps can validate them. ~100-200 lines.
- [ ] **DynamoDB PartiQL** — ExecuteStatement/BatchExecuteStatement. ~200-300 lines.
- [ ] **S3 Select** — SelectObjectContent for CSV/JSON using DuckDB or stdlib csv/json. ~200-300 lines.
- [ ] **Lambda Go/Java Runtime Stubs** — Basic execution for compiled runtimes. ~100-200 lines.
- [ ] **CloudWatch Logs Insights** — Basic log query execution against stored events. ~200-300 lines.

### Low Impact (polish and completeness)

- [ ] **S3 Object Lock** — Governance/compliance mode for versioned objects. ~100-200 lines.
- [ ] **SQS StartMessageMoveTask** — DLQ redrive. ~100-150 lines.
- [ ] **EventBridge Event Pattern Matching** — Evaluate patterns against PutEvents payloads. ~200-300 lines.
- [ ] **Route 53 DNS Resolution** — Spin up a minimal DNS server (port 53) that resolves records. ~200-300 lines.
- [ ] **Firehose Lambda Transform** — Call Lambda for record transformation before S3 write. ~100-150 lines.
- [ ] **EC2 DescribeInstanceTypes** — Return instance type metadata. ~50-100 lines.
- [ ] **SNS Subscription Filter Policy (full)** — prefix, exists, anything-but, numeric matching. ~100-200 lines.
- [ ] **SecretsManager Rotation** — Actually invoke rotation Lambda. ~100-150 lines.
- [ ] **CloudWatch Logs Subscription Filters** — Forward matching log events to Lambda/Kinesis. ~100-200 lines.
- [ ] **ECS Fargate Simulation** — Run containers without requiring Docker daemon access. ~200-300 lines.

### Won't Implement (by design)

- IAM policy enforcement on all API calls (would break the "just works" dev experience)
- Real email/SMS delivery in SES/SNS (security/cost concern)
- KMS encryption (no local HSM)
- Cross-account/cross-region (single-tenant local emulator)
- AWS Organizations, Control Tower, Service Catalog
- Real EC2 compute (use Docker/ECS instead)

---

## How to Use This Report

1. **Choosing what to test locally vs AWS:** If a feature is rated 7+, test locally with MiniStack. If it's 5-6 or depends on a "missing" feature, test against real AWS.
2. **Prioritizing contributions:** The "High Impact" TODOs unlock the most user value. DynamoDB Streams and S3 Event Notifications are the two most-requested features.
3. **Template compatibility:** Check your CloudFormation template's resource types against the 28 supported types. Unsupported types will be stubbed (dummy IDs, no real provisioning).

---

*Generated by MiniStack Fidelity Audit. See individual service files in `ministack/services/` for implementation details.*
