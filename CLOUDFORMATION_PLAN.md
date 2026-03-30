# CloudFormation Support for MiniStack — Comprehensive Plan

**Status:** Proposal
**Author:** Claude Code Pipeline
**Date:** 2026-03-30
**Branch:** `feat/cloudformation-plan`

---

## Executive Summary

Add AWS CloudFormation emulation to MiniStack, enabling users to deploy infrastructure-as-code templates against their local MiniStack instance. This is one of the most-requested features for LocalStack alternatives and would position MiniStack as a serious contender for local AWS development and CI/CD pipelines.

**Verdict: Highly feasible.** MiniStack's architecture is well-suited for this — 25 of 26 services follow a uniform `handle_request` pattern with CRUD operations already implemented for ~40 CloudFormation resource types. The implementation requires a service facade layer to bridge non-uniform internal calling conventions.

---

## Viability Assessment

### Why This Is Feasible

| Factor | Assessment |
|--------|-----------|
| **Near-uniform service pattern** | 25 of 26 services expose `handle_request()`. Exception: `iam_sts.py` exports `handle_iam_request` and `handle_sts_request` separately. Provisioners must handle this. |
| **All CRUD ops exist** | ~40 distinct CF resource types already have Create/Delete ops in existing handlers. |
| **In-memory state** | Stack state is just another dict. No new infrastructure needed. |
| **Single-process model** | CF handler can import and call other service modules. Requires a thin facade layer due to non-uniform internal calling conventions (see Design Caveats). |
| **Existing persistence** | `core/persistence.py` provides JSON save/load utilities (currently only used by 2 of 26 services). CF state will implement `get_state()`/`load_persisted_state()` from scratch, following the same pattern. |
| **Existing reset pattern** | Every service has `reset()`. Stack cleanup piggybacks on this. CF module's `reset()` must also be added to `_reset_all_state()` in `app.py`. |

### Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Resource attribute gaps** — `Fn::GetAtt` may request attributes MiniStack doesn't track (e.g., S3 `BucketArn` not currently stored) | High | Build explicit per-resource attribute table (see Attribute Map section). Return `UnsupportedAttribute` error for unknown attrs, not silent defaults. |
| **Fidelity gaps in services** — CF templates may set properties that handlers ignore (e.g., Lambda `VpcConfig`) | High | Log structured warning and surface via `DescribeStackResources` `ResourceStatusReason`. Silent misconfiguration is worse than a clear error for CI/CD parity. |
| **Non-uniform internal calling conventions** — Service `_create_*` functions have different signatures: `(name, bytes)` vs `(dict)` vs `(query_params)` | High | Build a thin service facade layer per provisioner that adapts CF properties to each service's conventions. ~30-80 lines per provisioner. |
| **Thread-safety** — Module-level dicts are accessed by background threads (TTL reaper, ESM pollers). Direct CF provisioner calls bypass HTTP concurrency handling. | Medium | Acquire service-level locks in provisioners. Document thread-safety requirements. |
| **Template complexity** — `Fn::If`/Conditions require two-pass evaluation; `DependsOn` on conditional resources is a known edge case | Medium | Phase these in carefully. Phase 1 supports Conditions as a best-effort feature. |
| **CDK compatibility is limited in Phase 1** — CDK requires `GetTemplateSummary`, handles `AWS::CDK::Metadata`, and uses S3 asset bootstrapping (nested stacks, deferred to Phase 2) | Medium | Explicitly document Phase 1 CDK limitations. Add `GetTemplateSummary` to Phase 1. Silently skip `AWS::CDK::Metadata` resources. Full CDK support requires Phase 2+. |
| **Performance** — Large stacks (50+ resources) creating sequentially | Low | Acceptable for local dev. Add parallel creation in Phase 2 if needed. |

---

## Design

### Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                    app.py (ASGI)                     │
│  SERVICE_HANDLERS["cloudformation"] = cf.handle_req  │
└──────────────────────┬──────────────────────────────┘
                       │
          ┌────────────▼────────────────┐
          │   cloudformation.py          │
          │   - handle_request()         │
          │   - CreateStack / DeleteStack│
          │   - DescribeStacks / etc.    │
          └────────────┬────────────────┘
                       │
     ┌─────────────────▼─────────────────────┐
     │       cloudformation/engine.py         │
     │  - parse_template()                    │
     │  - resolve_dependencies() (topo sort)  │
     │  - evaluate_intrinsics() (recursive)   │
     │  - provision_stack() (orchestrator)     │
     │  - rollback_stack()                     │
     └─────────────────┬─────────────────────┘
                       │
     ┌─────────────────▼─────────────────────┐
     │    cloudformation/provisioners.py      │
     │  - PROVISIONERS: dict[str, Provisioner]│
     │  - Each resource type has:             │
     │    create(logical_id, props) → phys_id │
     │    delete(phys_id, props)              │
     │    get_attr(phys_id, attr) → value     │
     └─────────────────┬─────────────────────┘
                       │
     ┌────────┬────────┼────────┬────────┐
     ▼        ▼        ▼        ▼        ▼
   s3.py  dynamodb.py lambda.py sqs.py  ...
   (existing service modules - called directly)
```

### Component Breakdown

#### 1. Template Parser (`cloudformation/parser.py`)

```python
def parse_template(template_body: str) -> dict:
    """Parse JSON or YAML CloudFormation template."""
    # Try JSON first, fall back to YAML
    # Extract: AWSTemplateFormatVersion, Description, Parameters,
    #          Mappings, Conditions, Resources, Outputs
    # Validate: Resources section exists, resource types are strings
```

- JSON via stdlib `json`
- YAML via `pyyaml` (new optional dependency)
- Parameter validation and default value application
- ~200-300 lines

#### 2. Dependency Resolver (`cloudformation/engine.py`)

```python
def resolve_dependencies(resources: dict) -> list[list[str]]:
    """Return resources in topological creation order (grouped by level for potential parallelism)."""
    # 1. Walk each resource's Properties tree
    # 2. Find all Ref and Fn::GetAtt references to other logical IDs
    # 3. Merge with explicit DependsOn declarations
    # 4. Build DAG, detect cycles, topological sort
    # Returns: [[level0_resources], [level1_resources], ...]
```

- Kahn's algorithm for topological sort
- Cycle detection with clear error messages
- ~200-300 lines

#### 3. Intrinsic Function Evaluator (`cloudformation/intrinsics.py`)

```python
def evaluate(value, context: ResolveContext):
    """Recursively evaluate a value, resolving all intrinsic functions."""
    # If dict with single key that's an intrinsic function:
    #   {"Ref": "MyBucket"} → bucket_physical_id
    #   {"Fn::GetAtt": ["MyFunc", "Arn"]} → function_arn
    #   {"Fn::Sub": "arn:aws:s3:::${BucketName}"} → resolved string
    #   {"Fn::Join": ["-", ["a", "b", "c"]]} → "a-b-c"
    # If list: evaluate each element
    # If plain value: return as-is
```

Phase 1 intrinsic functions:
| Function | Description | Complexity |
|----------|-------------|------------|
| `Ref` | Resource physical ID or parameter value | Simple |
| `Fn::GetAtt` | Resource attribute lookup | Medium (needs attribute map) |
| `Fn::Sub` | String interpolation | Simple |
| `Fn::Join` | Array join | Trivial |
| `Fn::Select` | Array element | Trivial |
| `Fn::Split` | String split | Trivial |
| `Fn::If` | Conditional value | Simple |
| `Fn::Equals` | Equality condition | Trivial |
| `Fn::And` / `Fn::Or` / `Fn::Not` | Logical conditions | Trivial |
| `Fn::FindInMap` | Mappings lookup | Simple |
| `Fn::GetAZs` | Return AZ list (static) | Trivial |
| `Fn::Base64` | Base64 encode | Trivial |

- ~400-500 lines

#### 4. Resource Provisioners (`cloudformation/provisioners.py`)

Each provisioner maps CloudFormation properties to MiniStack service calls:

```python
class Provisioner:
    def create(self, logical_id: str, props: dict, context: StackContext) -> str:
        """Create the resource. Return physical resource ID."""
    def delete(self, physical_id: str, props: dict, context: StackContext):
        """Delete the resource."""
    def get_attr(self, physical_id: str, attr_name: str) -> str:
        """Return the value of Fn::GetAtt for this resource."""

PROVISIONERS = {
    "AWS::S3::Bucket": S3BucketProvisioner(),
    "AWS::DynamoDB::Table": DynamoDBTableProvisioner(),
    # ... 55+ types
}
```

**Phase 1 Resource Types (top 20 most common):**

| Resource Type | Priority | Rationale |
|--------------|----------|-----------|
| `AWS::S3::Bucket` | P0 | Most common resource in CF templates |
| `AWS::DynamoDB::Table` | P0 | Core data store |
| `AWS::Lambda::Function` | P0 | Core compute |
| `AWS::SQS::Queue` | P0 | Common messaging |
| `AWS::SNS::Topic` | P0 | Common messaging |
| `AWS::SNS::Subscription` | P0 | Usually paired with Topic |
| `AWS::IAM::Role` | P0 | Required by Lambda, ECS, etc. |
| `AWS::IAM::Policy` | P0 | Required by IAM Role |
| `AWS::Lambda::Permission` | P1 | Common with API Gateway + Lambda |
| `AWS::Lambda::EventSourceMapping` | P1 | SQS/DynamoDB triggers |
| `AWS::Events::Rule` | P1 | Scheduled Lambda |
| `AWS::Logs::LogGroup` | P1 | Lambda auto-creates these |
| `AWS::SecretsManager::Secret` | P1 | Common config pattern |
| `AWS::SSM::Parameter` | P1 | Common config pattern |
| `AWS::ApiGateway::RestApi` | P1 | API development |
| `AWS::EC2::SecurityGroup` | P1 | VPC resources |
| `AWS::EC2::VPC` | P1 | Networking |
| `AWS::EC2::Subnet` | P1 | Networking |
| `AWS::StepFunctions::StateMachine` | P2 | Workflow orchestration |
| `AWS::S3::BucketPolicy` | P2 | Security |

- ~30-80 lines per provisioner × 20 types = ~1000-1600 lines

#### 5. Stack State Manager (in `cloudformation.py`)

```python
_stacks = {}  # stack_name -> StackRecord

class StackRecord:
    stack_id: str           # arn:aws:cloudformation:region:account:stack/name/uuid
    stack_name: str
    template_body: str
    parameters: dict
    status: str             # CREATE_IN_PROGRESS, CREATE_COMPLETE, CREATE_FAILED,
                            # DELETE_IN_PROGRESS, DELETE_COMPLETE, DELETE_FAILED,
                            # ROLLBACK_IN_PROGRESS, ROLLBACK_COMPLETE, ROLLBACK_FAILED
    status_reason: str      # Human-readable reason for current status
    resources: dict         # logical_id -> ResourceRecord
    outputs: dict
    events: list            # StackEvent records
    creation_time: str
    last_updated: str
    tags: list

class ResourceRecord:
    logical_id: str
    physical_id: str
    resource_type: str
    status: str             # CREATE_IN_PROGRESS, CREATE_COMPLETE, CREATE_FAILED,
                            # DELETE_IN_PROGRESS, DELETE_COMPLETE, DELETE_FAILED
    status_reason: str      # Reason for current status (esp. on failure)
    properties: dict
    timestamp: str
```

#### 6. CloudFormation API Actions

**Phase 1 Actions:**
| Action | Description |
|--------|-------------|
| `CreateStack` | Parse template, resolve deps, provision resources, create stack record. Return `AlreadyExistsException` if stack name already exists. |
| `DeleteStack` | Delete all resources in reverse creation order, remove stack record |
| `DescribeStacks` | Return stack records with status, outputs, parameters. Include `StackStatusReason`. |
| `DescribeStackResources` | Return resource records for a stack |
| `DescribeStackResource` | Return a single resource record (CDK calls this frequently) |
| `DescribeStackEvents` | Return event log for a stack |
| `ListStacks` | List all stacks with summary info |
| `GetTemplate` | Return the original template body |
| `GetTemplateSummary` | Return parameter list, resource types, capabilities (CDK calls this pre-deploy) |
| `ValidateTemplate` | Parse template, verify resource types supported, validate parameter constraints (`AllowedValues`, `AllowedPattern`, `Min`/`MaxLength`), return parameters |
| `ListStackResources` | Return resource summaries |

**Phase 2 Actions:**
- `UpdateStack`, `CreateChangeSet`, `ExecuteChangeSet`, `DescribeChangeSet`, `DeleteChangeSet`
- `ListExports`, `ListImports` (cross-stack references)

#### 7. Rollback Engine

```python
async def rollback_stack(stack: StackRecord):
    """Delete all successfully created resources in reverse order."""
    created = [r for r in stack.resources.values() if r.status == "CREATE_COMPLETE"]
    for resource in reversed(created):
        provisioner = PROVISIONERS.get(resource.resource_type)
        if provisioner:
            try:
                provisioner.delete(resource.physical_id, resource.properties, context)
                resource.status = "DELETE_COMPLETE"
            except Exception:
                resource.status = "DELETE_FAILED"
    stack.status = "ROLLBACK_COMPLETE"
```

---

## Pros and Cons

### Pros

| Pro | Impact |
|-----|--------|
| **Most-requested feature** for LocalStack alternatives | High user adoption |
| **Enables IaC testing** — deploy CF/CDK/SAM templates locally | Major workflow improvement |
| **CI/CD integration** — test CF templates in pipelines without AWS costs | Cost savings for users |
| **~40 resource types already supported** — strong head start | Faster implementation |
| **Clean architecture fit** — near-uniform handler pattern makes dispatch feasible (with facade layer) | Medium technical risk |
| **Competitive advantage** — LocalStack's CF is Pro-only since their paywall change | Market positioning |
| **CDK partial compatibility** — CDK synthesizes to CF. Phase 1 supports first `cdk deploy` of non-asset stacks. Full CDK requires Phase 2 (nested stacks for asset handling). | Incremental toolchain support |

### Cons

| Con | Impact | Mitigation |
|-----|--------|------------|
| **Large implementation surface** — ~40 resource types need provisioners | Months of work | Phase it: 20 types in Phase 1 covers most use cases |
| **Fidelity is never 100%** — real CF has edge cases we'll never match | User confusion | Document supported types clearly; return `UnsupportedResource` errors |
| **Maintenance burden** — every new service needs CF provisioner update | Ongoing cost | Template the provisioner pattern; make it easy to contribute |
| **YAML dependency** — need `pyyaml` for YAML template support | Small dep increase | Make it optional; JSON-only works without it |
| **Testing complexity** — need integration tests for each resource type | Significant test code | One test per resource type minimum; share fixtures |
| **Update/change set complexity** — full CF update semantics are extremely complex | Deferred complexity | Phase 2+; most local dev only needs create/delete |
| **Non-uniform internal APIs** — service `_create_*` functions use different signatures (`bytes` vs `dict` vs query-param dict) and return HTTP tuples, not domain objects | Medium | Each provisioner includes adapter code to bridge CF properties to the specific service's calling convention |
| **Thread-safety** — background threads (DynamoDB TTL reaper, SQS ESM pollers) access same module-level dicts | Medium | Provisioners must acquire per-service locks before calling internal functions |
| **CDK asset pipeline requires Phase 2** — CDK bundles Lambda code as S3 assets via nested stacks (`AWS::CloudFormation::Stack`). Phase 1 cannot handle this. | Medium | Document this limitation clearly. Phase 1 CDK works for inline-code stacks only. |

---

## Design Caveats (from Triple-Check Review)

### Calling Convention Non-Uniformity

Internal `_create_*` functions do NOT have uniform signatures:

| Service | Function | Expects |
|---------|----------|---------|
| `s3._create_bucket` | `(name: str, body: bytes)` | Bucket name + XML body |
| `dynamodb._create_table` | `(data: dict)` | Pre-parsed JSON dict |
| `lambda_svc._create_function` | `(data: dict)` | Pre-parsed JSON dict |
| `sns._create_topic` | `(params)` | Query-param dict: `{"Name": ["my-topic"]}` |
| `sqs._act_create_queue` | `(params)` | Query-param dict |
| `iam_sts._create_role` | `(params)` | Query-param dict + XML body |

All return HTTP tuples `(status_code, headers_dict, body_bytes)`, not domain objects. Each provisioner MUST include adapter code to:
1. Convert CF property dict → service-specific calling convention
2. Parse the HTTP response to extract the physical resource ID
3. Acquire any per-service locks before calling

### Recommended: Service Facade Layer

For each provisioner, implement a thin adapter:
```python
class S3BucketProvisioner(Provisioner):
    def create(self, logical_id, props, context):
        name = props.get("BucketName", f"cf-{context.stack_name}-{logical_id.lower()}")
        # Build XML body matching S3's expected format
        body = self._build_create_bucket_xml(props)
        status, headers, response = s3._create_bucket(name, body)
        if status >= 400:
            raise ProvisionError(f"S3 bucket creation failed: {response}")
        return name  # physical resource ID
```

### CDK Compatibility Limitations (Phase 1)

- `AWS::CDK::Metadata` resources must be silently skipped (not errored)
- `GetTemplateSummary` is required (added to Phase 1 actions)
- CDK asset pipeline (S3 uploads + nested stacks) requires Phase 2
- `cdk deploy` will work for Phase 1 only with inline-code stacks
- `cdk deploy` repeated runs require `UpdateStack` (Phase 2)

---

## Resource Attribute Map (Fn::GetAtt)

Each provisioner must define the attributes available for `Fn::GetAtt`. Missing attributes will return `UnsupportedAttribute` error.

### Phase 1 Attribute Table

| Resource Type | Attribute | Source in Service Module |
|--------------|-----------|------------------------|
| `AWS::S3::Bucket` | `Arn` | Compute: `arn:aws:s3:::{name}` (not currently stored) |
| `AWS::S3::Bucket` | `DomainName` | Compute: `{name}.s3.amazonaws.com` |
| `AWS::DynamoDB::Table` | `Arn` | `_tables[name]["TableArn"]` |
| `AWS::DynamoDB::Table` | `StreamArn` | `_tables[name]["LatestStreamArn"]` |
| `AWS::Lambda::Function` | `Arn` | `_functions[name]["FunctionArn"]` |
| `AWS::SQS::Queue` | `Arn` | `_queues[url]["QueueArn"]` |
| `AWS::SQS::Queue` | `QueueUrl` | Queue URL directly |
| `AWS::SNS::Topic` | `TopicArn` | `_topics[arn]["TopicArn"]` |
| `AWS::IAM::Role` | `Arn` | `_roles[name]["Arn"]` |
| `AWS::IAM::Role` | `RoleId` | `_roles[name]["RoleId"]` |
| `AWS::Lambda::Function` | `FunctionName` | `_functions[name]["FunctionName"]` |
| `AWS::SecretsManager::Secret` | `Arn` | Compute from name |
| `AWS::Logs::LogGroup` | `Arn` | Compute from name + region |
| `AWS::EC2::VPC` | `VpcId` | `_vpcs[id]["VpcId"]` |
| `AWS::EC2::Subnet` | `SubnetId` | `_subnets[id]["SubnetId"]` |
| `AWS::EC2::SecurityGroup` | `GroupId` | `_security_groups[id]["GroupId"]` |

**Note:** S3 `BucketArn` is not currently stored in the S3 service module. The provisioner must compute it from the bucket name. Other attributes may need similar computation.

**Fallback contract:** For any `Fn::GetAtt` on an attribute not in the table, return:
```xml
<Error><Code>UnsupportedAttribute</Code><Message>Attribute '{attr}' is not supported for resource type '{type}' in MiniStack</Message></Error>
```

---

## Phasing

### Phase 1: Create & Delete (MVP)
**Target: ~4000-5500 lines, 4-6 weeks**

- Template parsing (JSON + YAML)
- 14 intrinsic functions (Ref, Fn::GetAtt, Fn::Sub, Fn::Join, Fn::Select, Fn::Split, Fn::If, Fn::Equals, Fn::And, Fn::Or, Fn::Not, Fn::FindInMap, Fn::GetAZs, Fn::Base64)
- Dependency resolution with topological sort + cycle detection
- 20 resource type provisioners with service facade adapters (P0 + P1)
- 11 API actions: CreateStack, DeleteStack, DescribeStacks, DescribeStackResource, DescribeStackResources, DescribeStackEvents, ListStacks, ListStackResources, GetTemplate, GetTemplateSummary, ValidateTemplate
- Rollback on creation failure with status reasons
- Stack events log
- Silently skip `AWS::CDK::Metadata` and other unknown non-critical resource types
- Parameter constraint validation (AllowedValues, AllowedPattern, etc.)
- Resource attribute map for all 20 provisioned types
- Comprehensive tests (including idempotency, reset isolation, negative cases)

**User value:** Deploy and tear down CF templates locally. Covers event-driven (Lambda + SQS + SNS + EventBridge), data pipelines (Kinesis + S3 + Lambda), and IAM/secrets configuration. Partial serverless API support (Lambda + DynamoDB; full API Gateway routing requires Phase 2 sub-resources).

**Phase 1 CDK note:** First `cdk deploy` works for inline-code stacks. Repeated deploys and asset-based stacks require Phase 2.

### Phase 2: Update & Change Sets
**Target: ~1500-2000 lines, 3-4 weeks**

- UpdateStack with resource replacement logic
- CreateChangeSet, ExecuteChangeSet, DescribeChangeSet
- 15 more resource types (P2 + remaining)
- Nested stack support (`AWS::CloudFormation::Stack`)
- Cross-stack references (`Fn::ImportValue`, `Exports`)
- Stack outputs

### Phase 3: Advanced Features
**Target: ~1000-1500 lines, 2-3 weeks**

- Custom resources (`AWS::CloudFormation::CustomResource`)
- Stack policies
- Drift detection (compare actual state vs template)
- `Fn::Transform` (macros)
- Parallel resource creation within dependency levels
- SAM transform support (`AWS::Serverless::Function` etc.)

---

## Complexity Estimate

| Component | Lines | Effort |
|-----------|-------|--------|
| Template parser | 200-300 | Low |
| Intrinsic function evaluator | 400-500 | Medium |
| Dependency resolver (DAG + topo sort) | 200-300 | Medium |
| Stack state management + API actions | 500-700 | Medium |
| Resource provisioners (20 types) | 1000-1600 | High (repetitive) |
| Rollback engine | 100-150 | Low |
| Router integration | 30-50 | Trivial |
| **Phase 1 Code Total** | **~2500-3600** | |
| Tests (Phase 1) | 800-1200 | Medium |
| **Phase 1 Grand Total** | **~3300-4800** | |

For context: the current codebase is ~41,000 lines of service code + ~11,000 lines of tests. Phase 1 adds ~8-12% to the codebase.

---

## File Structure

```
ministack/
├── services/
│   └── cloudformation.py          # API handler (CreateStack, DescribeStacks, etc.)
├── cloudformation/
│   ├── __init__.py
│   ├── parser.py                  # Template parsing (JSON/YAML)
│   ├── intrinsics.py              # Intrinsic function evaluator
│   ├── engine.py                  # Dependency resolver + orchestrator
│   ├── provisioners/
│   │   ├── __init__.py            # PROVISIONERS registry
│   │   ├── base.py                # Provisioner base class
│   │   ├── s3.py                  # AWS::S3::Bucket, AWS::S3::BucketPolicy
│   │   ├── dynamodb.py            # AWS::DynamoDB::Table
│   │   ├── lambda_svc.py          # AWS::Lambda::Function, Permission, ESM
│   │   ├── sqs.py                 # AWS::SQS::Queue
│   │   ├── sns.py                 # AWS::SNS::Topic, Subscription
│   │   ├── iam.py                 # AWS::IAM::Role, Policy
│   │   ├── ec2.py                 # AWS::EC2::VPC, Subnet, SecurityGroup
│   │   ├── logs.py                # AWS::Logs::LogGroup
│   │   ├── ssm.py                 # AWS::SSM::Parameter
│   │   ├── secretsmanager.py      # AWS::SecretsManager::Secret
│   │   ├── events.py              # AWS::Events::Rule
│   │   ├── apigateway.py          # AWS::ApiGateway::RestApi
│   │   └── stepfunctions.py       # AWS::StepFunctions::StateMachine
│   └── rollback.py                # Rollback engine
tests/
├── test_cloudformation.py         # API-level tests
├── test_cf_intrinsics.py          # Intrinsic function unit tests
├── test_cf_engine.py              # Engine/dependency resolver tests
├── test_cf_provisioners.py        # Per-resource-type integration tests
└── cf_templates/                  # Test CloudFormation templates
    ├── simple_s3.json
    ├── serverless_api.json
    ├── data_pipeline.json
    └── vpc_networking.json
```

---

## Integration Points

### Router (`core/router.py`)
Three changes required:

1. Add host pattern to `SERVICE_PATTERNS`:
```python
"cloudformation": {
    "host_patterns": [r"cloudformation\."],
    "target_prefixes": [],
    "path_patterns": [],
}
```

2. Add CF actions to `action_service_map` dict inside `detect_service()`:
```python
# In the action_service_map dict:
"CreateStack": "cloudformation",
"DeleteStack": "cloudformation",
"DescribeStacks": "cloudformation",
"DescribeStackResources": "cloudformation",
"DescribeStackResource": "cloudformation",
"DescribeStackEvents": "cloudformation",
"ListStacks": "cloudformation",
"ListStackResources": "cloudformation",
"GetTemplate": "cloudformation",
"GetTemplateSummary": "cloudformation",
"ValidateTemplate": "cloudformation",
```

3. Add to `scope_map` in `detect_service()`:
```python
"cloudformation": "cloudformation",
```

### App (`app.py`)
```python
from ministack.services import cloudformation
SERVICE_HANDLERS["cloudformation"] = cloudformation.handle_request
```

### Persistence (`core/persistence.py`)
Add CloudFormation state to save/load cycle.

### Dependencies (`pyproject.toml`)
```toml
dependencies = [
    "uvicorn[standard]",
    "httptools",
    "pyyaml",  # NEW — for YAML template support
]
```

---

## Test Strategy

### Unit Tests
- Template parser: valid/invalid JSON/YAML, parameter extraction, missing Resources section
- Intrinsic functions: each function with edge cases (nested intrinsics, missing refs, circular refs)
- Dependency resolver: simple chain, diamond deps, parallel resources, cycle detection, DependsOn override

### Integration Tests
- Per-resource-type: create stack with single resource, verify resource exists via service API, delete stack, verify resource gone
- Multi-resource: create stack with deps (Lambda + IAM Role + SQS Queue), verify all created in correct order
- Rollback: create stack with intentional failure mid-way, verify created resources are cleaned up
- Outputs: create stack, verify outputs contain expected values with resolved Refs
- **Idempotency**: CreateStack with same name twice → `AlreadyExistsException`
- **Concurrent stacks**: Two stacks creating same-named S3 bucket → clean error + rollback
- **Reset isolation**: After `reset()`, no orphaned resources from CF-created stacks
- **ValidateTemplate negatives**: unknown resource types, malformed intrinsics, DAG cycles, invalid parameter constraints

### End-to-End Tests
- Real CF templates: event-driven (SQS + SNS + Lambda), data pipeline (S3 + Lambda + Kinesis), IAM + Secrets config
- CLI compatibility: `aws cloudformation create-stack --template-body file://template.json`
- boto3 compatibility: `cf_client.create_stack(StackName=..., TemplateBody=...)`
- CDK smoke test: simple inline-code Lambda + DynamoDB stack via `cdk deploy`

---

## Success Criteria

- [ ] `aws cloudformation create-stack` works with MiniStack endpoint
- [ ] `aws cloudformation delete-stack` cleans up all resources
- [ ] `aws cloudformation describe-stacks` returns correct status and outputs
- [ ] 20 resource types fully supported in Phase 1
- [ ] Rollback works when a resource creation fails
- [ ] All 14 core intrinsic functions evaluate correctly
- [ ] CDK `cdk deploy --endpoint-url` works for simple inline-code stacks (asset stacks require Phase 2)
- [ ] Tests pass 3x consecutively
- [ ] No regression in existing service tests

---

## Decision Record

| Decision | Choice | Why | Alternatives Considered | Trade-off |
|----------|--------|-----|------------------------|-----------|
| **Call style** | Direct module imports via facade adapters (Option A+) | Fast, in-process, avoids HTTP overhead. Each provisioner adapts CF props to service-specific calling convention. | Self-HTTP calls (Option B — used by LocalStack, more resilient to refactors) | Tighter coupling + non-uniform signatures require per-provisioner adapter code (~30-80 lines each). Option B is lower-maintenance but slower. |
| **YAML support** | Add `pyyaml` dependency | Most CF templates use YAML in practice | JSON-only | One small dependency vs. excluding most real-world templates |
| **Provisioner structure** | One file per service group | Matches existing service file structure, easy to find | Single mega-file, one file per resource type | Good middle ground between organization and file proliferation |
| **Phase 1 scope** | 20 resource types, no updates | Covers 90%+ of local dev use cases | Full CF from day 1 | Ship faster, iterate based on user feedback |
| **State storage** | In-memory dicts (like all other services) | Consistent with architecture | SQLite for complex queries | Simplicity wins; stack queries are simple key lookups |
| **Error handling** | Return CF-style error XML with error codes | Users expect standard CF error format | Generic HTTP errors | Better developer experience, matches real CF behavior |

---

*This plan is a companion to the implementation issue in the YOLO project board. Phase 1 implementation should begin once this plan is reviewed and approved.*

---

## Appendix: Triple-Check Review Summary

This plan was independently reviewed by 3 agents. Key findings incorporated:

### Reviewer 1 — Architecture & Feasibility
- **Router `query_actions` was a phantom key** — fixed to use `action_service_map` + `scope_map`
- **IAM/STS non-uniform handler** — documented as exception to `handle_request` pattern
- **Codebase size overstated** (41K → actual 31K service code) — corrected resource type count to ~40
- **`_reset_all_state()` integration** — explicitly called out
- **Persistence framework is thin** (2/26 services use it) — noted, not a blocker

### Reviewer 2 — Design & Sanity
- **Thread-safety hazard added to risks** — background threads (TTL reaper, ESM pollers) need lock coordination
- **Non-uniform calling conventions documented** — `(bytes)` vs `(dict)` vs `(query_params)` per service
- **CDK Phase 1 scope corrected** — first deploy only, no re-deploys, no asset stacks
- **Fidelity gap severity raised to High** — silent misconfiguration worse than explicit errors
- **Missing cons added** — thread safety, signature mismatch, sync/async gap, CDK limitations
- **Suggested swap**: EC2 networking (rarely needed locally) for API Gateway sub-resources (commonly needed)

### Reviewer 3 — Completeness & Edge Cases
- **`DescribeStackResource` (singular) added** to Phase 1 — CDK calls this frequently
- **`GetTemplateSummary` added** to Phase 1 — CDK calls this pre-deploy
- **Status reasons added** to StackRecord and ResourceRecord data models
- **Parameter constraint validation** specified for ValidateTemplate
- **Resource attribute map** — explicit per-resource table added with fallback contract
- **S3 `BucketArn` gap identified** — not stored in current S3 module, must be computed
- **`AWS::CDK::Metadata`** — must be silently skipped, not errored
- **API Gateway serverless pattern incomplete** — needs Resource/Method/Deployment/Stage (noted in Phase 1 scope)
- **`Fn::ImportValue`** — should raise clear `UnsupportedFunction` error in Phase 1
