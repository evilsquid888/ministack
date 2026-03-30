"""
CloudFormation Service Emulator — AWS-compatible.

Supports: CreateStack, DeleteStack, UpdateStack, DescribeStacks,
          DescribeStackResource, DescribeStackResources, DescribeStackEvents,
          ListStacks, ListStackResources, GetTemplate, GetTemplateSummary,
          ValidateTemplate, CreateChangeSet, DescribeChangeSet,
          ExecuteChangeSet, DeleteChangeSet, ListChangeSets.

Orchestrates real resource creation across other MiniStack services (S3, SQS,
SNS, DynamoDB, Lambda, IAM, EC2, EventBridge, CloudWatch Logs,
SecretsManager, SSM, Step Functions).
"""

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from urllib.parse import parse_qs, urlencode
from uuid import uuid4
from xml.sax.saxutils import escape as _esc

from ministack.core.responses import new_uuid

# Service modules — imported for cross-service orchestration
from ministack.services import s3, sqs, sns, dynamodb, lambda_svc
from ministack.services import ssm, eventbridge, cloudwatch_logs
from ministack.services import secretsmanager, stepfunctions, ec2
from ministack.services import apigateway_v1
from ministack.services.iam_sts import handle_iam_request

logger = logging.getLogger("cloudformation")

# ── Module-level state ────────────────────────────────────────

_stacks: dict = {}       # stack_name -> stack record
_change_sets: dict = {}  # change_set_id -> change set record

ACCOUNT_ID = "000000000000"
REGION = "us-east-1"

# ── Helpers ───────────────────────────────────────────────────


def _p(params, key, default=""):
    """Extract single param value from query-param dict."""
    val = params.get(key, [default])
    return val[0] if isinstance(val, list) else val


def _resolve_stack_name(name_or_arn):
    """Resolve a stack name or ARN to the stack name key in _stacks."""
    if not name_or_arn:
        return name_or_arn or ""
    if name_or_arn in _stacks:
        return name_or_arn
    # Check if it's an ARN — format: arn:aws:cloudformation:region:account:stack/name/uuid
    if name_or_arn.startswith("arn:"):
        parts = name_or_arn.split("/")
        if len(parts) >= 2:
            candidate = parts[1]
            if candidate in _stacks:
                return candidate
    # Search by StackId
    for sname, sdata in _stacks.items():
        if sdata.get("StackId") == name_or_arn:
            return sname
    return name_or_arn  # return as-is, caller will handle not-found


def _parse_members(params, prefix):
    """Parse AWS-style numbered params like Parameters.member.N.ParameterKey/ParameterValue."""
    result = {}
    n = 1
    while True:
        key = _p(params, f"{prefix}.member.{n}.ParameterKey")
        if not key:
            break
        value = _p(params, f"{prefix}.member.{n}.ParameterValue")
        result[key] = value
        n += 1
    return result


def _parse_tags(params):
    """Parse Tags.member.N.Key / Tags.member.N.Value."""
    tags = []
    n = 1
    while True:
        key = _p(params, f"Tags.member.{n}.Key")
        if not key:
            break
        value = _p(params, f"Tags.member.{n}.Value")
        tags.append({"Key": key, "Value": value})
        n += 1
    return tags


def _xml(root_tag, body_xml):
    """Build CloudFormation XML response."""
    resp = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<{root_tag} xmlns="http://cloudformation.amazonaws.com/doc/2010-05-15/">'
        f'{body_xml}'
        f'<ResponseMetadata><RequestId>{new_uuid()}</RequestId></ResponseMetadata>'
        f'</{root_tag}>'
    ).encode("utf-8")
    return 200, {"Content-Type": "application/xml"}, resp


def _error(code, message, status=400):
    """CloudFormation XML error response."""
    resp = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<ErrorResponse xmlns="http://cloudformation.amazonaws.com/doc/2010-05-15/">'
        f'<Error><Code>{_esc(code)}</Code><Message>{_esc(message)}</Message></Error>'
        f'<RequestId>{new_uuid()}</RequestId>'
        f'</ErrorResponse>'
    ).encode("utf-8")
    return status, {"Content-Type": "application/xml"}, resp


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _xml_extract(resp_body, tag):
    """Extract text content from a simple XML tag in a response body."""
    if isinstance(resp_body, bytes):
        resp_body = resp_body.decode("utf-8", errors="replace")
    m = re.search(f"<{tag}>(.*?)</{tag}>", resp_body, re.DOTALL)
    return m.group(1) if m else ""


# ── Template Parsing ──────────────────────────────────────────


async def _fetch_template_url(url):
    """Fetch template body from S3 URL."""
    # Support s3:// and https://s3... URLs
    try:
        if url.startswith("s3://"):
            parts = url[5:].split("/", 1)
            bucket, key = parts[0], parts[1] if len(parts) > 1 else ""
        elif "s3" in url and ".amazonaws.com" in url:
            # https://s3.amazonaws.com/bucket/key or https://bucket.s3.amazonaws.com/key
            from urllib.parse import urlparse
            parsed = urlparse(url)
            path = parsed.path.lstrip("/")
            if parsed.hostname and parsed.hostname.endswith(".s3.amazonaws.com"):
                bucket = parsed.hostname.split(".s3.amazonaws.com")[0]
                key = path
            else:
                parts = path.split("/", 1)
                bucket, key = parts[0], parts[1] if len(parts) > 1 else ""
        else:
            return ""
        status, _, body = await s3.handle_request("GET", f"/{bucket}/{key}", {}, b"", {})
        if status == 200:
            return body.decode("utf-8") if isinstance(body, bytes) else body
    except Exception:
        pass
    return ""


def _parse_template(template_body):
    """Parse JSON or YAML CF template."""
    try:
        return json.loads(template_body)
    except (json.JSONDecodeError, TypeError):
        try:
            import yaml
            loader = yaml.SafeLoader
            _cf_tags = {
                '!Ref': lambda l, n: {"Ref": l.construct_scalar(n)},
                '!Sub': lambda l, n: {"Fn::Sub": l.construct_sequence(n) if n.id == 'sequence' else l.construct_scalar(n)},
                '!GetAtt': lambda l, n: {"Fn::GetAtt": l.construct_scalar(n).split(".")},
                '!Join': lambda l, n: {"Fn::Join": l.construct_sequence(n)},
                '!Select': lambda l, n: {"Fn::Select": l.construct_sequence(n)},
                '!Split': lambda l, n: {"Fn::Split": l.construct_sequence(n)},
                '!If': lambda l, n: {"Fn::If": l.construct_sequence(n)},
                '!Equals': lambda l, n: {"Fn::Equals": l.construct_sequence(n)},
                '!And': lambda l, n: {"Fn::And": l.construct_sequence(n)},
                '!Or': lambda l, n: {"Fn::Or": l.construct_sequence(n)},
                '!Not': lambda l, n: {"Fn::Not": l.construct_sequence(n)},
                '!FindInMap': lambda l, n: {"Fn::FindInMap": l.construct_sequence(n)},
                '!GetAZs': lambda l, n: {"Fn::GetAZs": l.construct_scalar(n)},
                '!Base64': lambda l, n: {"Fn::Base64": l.construct_scalar(n)},
            }
            for tag, ctor in _cf_tags.items():
                loader.add_constructor(tag, ctor)
            return yaml.load(template_body, Loader=loader)
        except Exception:
            raise ValueError("Invalid template: not valid JSON or YAML")


# ── Intrinsic Function Evaluator ──────────────────────────────


def _resolve(value, ctx):
    """Recursively resolve CloudFormation intrinsic functions."""
    if isinstance(value, dict):
        if len(value) == 1:
            key = next(iter(value))
            if key == "Ref":
                return _resolve_ref(value[key], ctx)
            elif key == "Fn::GetAtt":
                return _resolve_getatt(value[key], ctx)
            elif key == "Fn::Sub":
                return _resolve_sub(value[key], ctx)
            elif key == "Fn::Join":
                return _resolve_join(value[key], ctx)
            elif key == "Fn::Select":
                return _resolve_select(value[key], ctx)
            elif key == "Fn::Split":
                return _resolve_split(value[key], ctx)
            elif key == "Fn::If":
                return _resolve_if(value[key], ctx)
            elif key == "Fn::Equals":
                return _resolve_equals(value[key], ctx)
            elif key == "Fn::And":
                return _resolve_and(value[key], ctx)
            elif key == "Fn::Or":
                return _resolve_or(value[key], ctx)
            elif key == "Fn::Not":
                return _resolve_not(value[key], ctx)
            elif key == "Fn::FindInMap":
                return _resolve_find_in_map(value[key], ctx)
            elif key == "Fn::GetAZs":
                return _resolve_get_azs(value[key], ctx)
            elif key == "Fn::Base64":
                import base64
                inner = _resolve(value[key], ctx)
                return base64.b64encode(str(inner).encode()).decode()
        return {k: _resolve(v, ctx) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve(v, ctx) for v in value]
    return value


def _resolve_ref(ref_name, ctx):
    """Resolve Ref pseudo-parameters and resource logical IDs."""
    pseudo = {
        "AWS::StackName": ctx.get("stack_name", ""),
        "AWS::StackId": ctx.get("stack_id", ""),
        "AWS::Region": ctx.get("region", REGION),
        "AWS::AccountId": ctx.get("account_id", ACCOUNT_ID),
        "AWS::URLSuffix": "amazonaws.com",
        "AWS::NoValue": "",
        "AWS::NotificationARNs": [],
    }
    if ref_name in pseudo:
        return pseudo[ref_name]
    if ref_name in ctx.get("params", {}):
        return ctx["params"][ref_name]
    res = ctx.get("resources", {}).get(ref_name)
    if res:
        return res.get("ref", res.get("physical_id", ref_name))
    return ref_name


def _resolve_getatt(args, ctx):
    """Resolve Fn::GetAtt — args is [LogicalId, Attribute]."""
    if isinstance(args, str):
        parts = args.split(".", 1)
    else:
        parts = args
    logical_id = parts[0]
    attr = parts[1] if len(parts) > 1 else ""
    res = ctx.get("resources", {}).get(logical_id, {})
    attrs = res.get("attrs", {})
    return attrs.get(attr, f"{logical_id}.{attr}")


def _resolve_sub(value, ctx):
    """Resolve Fn::Sub — string with ${Var} substitutions."""
    if isinstance(value, list):
        template_str = str(value[0])
        extra_vars = value[1] if len(value) > 1 and isinstance(value[1], dict) else {}
    else:
        template_str = str(value)
        extra_vars = {}

    def _replace(m):
        var_name = m.group(1)
        if var_name in extra_vars:
            return str(_resolve(extra_vars[var_name], ctx))
        # Support ${Resource.Attribute} syntax (equivalent to Fn::GetAtt)
        if "." in var_name:
            parts = var_name.split(".", 1)
            return str(_resolve_getatt(parts, ctx))
        return str(_resolve_ref(var_name, ctx))

    return re.sub(r"\$\{([^}]+)\}", _replace, template_str)


def _resolve_join(args, ctx):
    """Resolve Fn::Join — [delimiter, [values]]."""
    delimiter = str(_resolve(args[0], ctx))
    values = [str(_resolve(v, ctx)) for v in args[1]]
    return delimiter.join(values)


def _resolve_select(args, ctx):
    """Resolve Fn::Select — [index, [values]]."""
    index = int(_resolve(args[0], ctx))
    values = _resolve(args[1], ctx)
    if isinstance(values, list) and 0 <= index < len(values):
        return values[index]
    return ""


def _resolve_split(args, ctx):
    """Resolve Fn::Split — [delimiter, string]."""
    delimiter = str(_resolve(args[0], ctx))
    source = str(_resolve(args[1], ctx))
    return source.split(delimiter)


def _resolve_if(args, ctx):
    """Resolve Fn::If — [condition_name, true_value, false_value]."""
    cond_name = args[0]
    cond_val = ctx.get("conditions", {}).get(cond_name, False)
    if cond_val:
        return _resolve(args[1], ctx)
    return _resolve(args[2], ctx)


def _resolve_equals(args, ctx):
    """Resolve Fn::Equals — [value1, value2]."""
    return str(_resolve(args[0], ctx)) == str(_resolve(args[1], ctx))


def _resolve_and(args, ctx):
    """Resolve Fn::And."""
    return all(bool(_resolve(a, ctx)) for a in args)


def _resolve_or(args, ctx):
    """Resolve Fn::Or."""
    return any(bool(_resolve(a, ctx)) for a in args)


def _resolve_not(args, ctx):
    """Resolve Fn::Not."""
    return not bool(_resolve(args[0], ctx))


def _resolve_find_in_map(args, ctx):
    """Resolve Fn::FindInMap — [MapName, FirstLevelKey, SecondLevelKey]."""
    map_name = str(_resolve(args[0], ctx))
    first_key = str(_resolve(args[1], ctx))
    second_key = str(_resolve(args[2], ctx))
    mappings = ctx.get("mappings", {})
    try:
        return mappings[map_name][first_key][second_key]
    except (KeyError, TypeError):
        return ""


def _resolve_get_azs(value, ctx):
    """Resolve Fn::GetAZs."""
    region = str(_resolve(value, ctx)) if value else ctx.get("region", REGION)
    return [f"{region}a", f"{region}b", f"{region}c"]


# ── Dependency Resolution ─────────────────────────────────────


def _scan_refs(value, known_resources):
    """Scan a value tree for references to known resource logical IDs."""
    refs = set()
    if isinstance(value, dict):
        if "Ref" in value and value["Ref"] in known_resources:
            refs.add(value["Ref"])
        if "Fn::GetAtt" in value:
            att = value["Fn::GetAtt"]
            lid = att[0] if isinstance(att, list) else att.split(".")[0]
            if lid in known_resources:
                refs.add(lid)
        if "Fn::Sub" in value:
            sub_val = value["Fn::Sub"]
            tmpl_str = sub_val[0] if isinstance(sub_val, list) else sub_val
            for m in re.finditer(r"\$\{([^}]+)\}", str(tmpl_str)):
                var = m.group(1)
                # Support ${Resource.Attr} — extract resource name before the dot
                base = var.split(".")[0]
                if base in known_resources:
                    refs.add(base)
                elif var in known_resources:
                    refs.add(var)
        for v in value.values():
            refs |= _scan_refs(v, known_resources)
    elif isinstance(value, list):
        for item in value:
            refs |= _scan_refs(item, known_resources)
    return refs


def _build_deps(resources):
    """Build dependency graph: {logical_id: set of dependency logical IDs}."""
    known = set(resources.keys())
    deps = {}
    for lid, res_def in resources.items():
        dep_set = set()
        # Explicit DependsOn
        depends_on = res_def.get("DependsOn", [])
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        for d in depends_on:
            if d in known:
                dep_set.add(d)
        # Implicit refs in Properties
        props = res_def.get("Properties", {})
        dep_set |= _scan_refs(props, known)
        dep_set.discard(lid)
        deps[lid] = dep_set
    return deps


def _topological_sort(deps):
    """Kahn's algorithm — returns ordered list or raises on cycle."""
    in_degree = defaultdict(int)
    graph = defaultdict(set)
    all_nodes = set(deps.keys())

    for node, node_deps in deps.items():
        in_degree.setdefault(node, 0)
        for dep in node_deps:
            graph[dep].add(node)
            in_degree[node] += 1

    queue = sorted([n for n in all_nodes if in_degree[n] == 0])
    result = []
    while queue:
        node = queue.pop(0)
        result.append(node)
        for neighbor in sorted(graph.get(node, [])):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
        queue.sort()

    if len(result) != len(all_nodes):
        raise ValueError("Circular dependency detected in resources")
    return result


# ── Resource Type Registry ────────────────────────────────────

_RESOURCE_CREATORS = {}
_RESOURCE_DELETERS = {}


# ── Resource Handlers — CREATE ────────────────────────────────


async def _create_s3_bucket(logical_id, props, ctx):
    name = props.get("BucketName", f"{ctx['stack_name']}-{logical_id.lower()}-{str(uuid4())[:8]}")
    name = str(_resolve(name, ctx)) if isinstance(name, dict) else name
    status, _, body = await s3.handle_request("PUT", f"/{name}", {}, b"", {})
    return {
        "physical_id": name,
        "ref": name,
        "attrs": {
            "Arn": f"arn:aws:s3:::{name}",
            "DomainName": f"{name}.s3.amazonaws.com",
            "RegionalDomainName": f"{name}.s3.{ctx.get('region', REGION)}.amazonaws.com",
            "WebsiteURL": f"http://{name}.s3-website-{ctx.get('region', REGION)}.amazonaws.com",
        },
    }


async def _create_s3_bucket_policy(logical_id, props, ctx):
    bucket = str(_resolve(props.get("Bucket", ""), ctx))
    policy_doc = _resolve(props.get("PolicyDocument", {}), ctx)
    policy_json = json.dumps(policy_doc).encode()
    await s3.handle_request("PUT", f"/{bucket}", {}, policy_json, {"policy": [""]})
    return {"physical_id": f"{bucket}-policy", "ref": f"{bucket}-policy", "attrs": {}}


async def _create_dynamodb_table(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    data = {
        "TableName": resolved.get("TableName", f"{ctx['stack_name']}-{logical_id}"),
        "KeySchema": resolved.get("KeySchema", []),
        "AttributeDefinitions": resolved.get("AttributeDefinitions", []),
    }
    billing = resolved.get("BillingMode", "PAY_PER_REQUEST")
    data["BillingMode"] = billing
    if billing == "PROVISIONED":
        tp = resolved.get("ProvisionedThroughput", {})
        data["ProvisionedThroughput"] = tp
    gsi = resolved.get("GlobalSecondaryIndexes")
    if gsi:
        data["GlobalSecondaryIndexes"] = gsi
    lsi = resolved.get("LocalSecondaryIndexes")
    if lsi:
        data["LocalSecondaryIndexes"] = lsi

    body = json.dumps(data).encode()
    headers = {"x-amz-target": "DynamoDB_20120810.CreateTable", "content-type": "application/x-amz-json-1.0"}
    status, _, resp = await dynamodb.handle_request("POST", "/", headers, body, {})
    result = json.loads(resp)
    table_name = result.get("TableDescription", {}).get("TableName", data["TableName"])
    table_arn = result.get("TableDescription", {}).get("TableArn", f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{table_name}")
    return {
        "physical_id": table_name,
        "ref": table_name,
        "attrs": {"Arn": table_arn, "StreamArn": result.get("TableDescription", {}).get("LatestStreamArn", "")},
    }


async def _create_sqs_queue(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    queue_name = resolved.get("QueueName", f"{ctx['stack_name']}-{logical_id}")
    qp = {"Action": ["CreateQueue"], "QueueName": [queue_name]}
    # Map common attributes
    attr_idx = 1
    for attr_key in ("VisibilityTimeout", "MessageRetentionPeriod", "DelaySeconds",
                     "MaximumMessageSize", "ReceiveMessageWaitTimeSeconds"):
        if attr_key in resolved:
            qp[f"Attribute.{attr_idx}.Name"] = [attr_key]
            qp[f"Attribute.{attr_idx}.Value"] = [str(resolved[attr_key])]
            attr_idx += 1
    if resolved.get("FifoQueue"):
        qp[f"Attribute.{attr_idx}.Name"] = ["FifoQueue"]
        qp[f"Attribute.{attr_idx}.Value"] = ["true"]
        attr_idx += 1

    status, _, resp = await sqs.handle_request("POST", "/", {}, b"", qp)
    url = _xml_extract(resp, "QueueUrl")
    if not url:
        url = f"http://localhost:4566/000000000000/{queue_name}"
    arn = f"arn:aws:sqs:{ctx.get('region', REGION)}:{ctx.get('account_id', ACCOUNT_ID)}:{queue_name}"
    return {
        "physical_id": url,
        "ref": url,
        "attrs": {"Arn": arn, "QueueUrl": url, "QueueName": queue_name},
    }


async def _create_sns_topic(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    topic_name = resolved.get("TopicName", f"{ctx['stack_name']}-{logical_id}")
    qp = {"Action": ["CreateTopic"], "Name": [topic_name]}
    status, _, resp = await sns.handle_request("POST", "/", {}, b"", qp)
    arn = _xml_extract(resp, "TopicArn")
    if not arn:
        arn = f"arn:aws:sns:{ctx.get('region', REGION)}:{ctx.get('account_id', ACCOUNT_ID)}:{topic_name}"
    return {
        "physical_id": arn,
        "ref": arn,
        "attrs": {"TopicArn": arn, "TopicName": topic_name},
    }


async def _create_sns_subscription(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    topic_arn = resolved.get("TopicArn", "")
    protocol = resolved.get("Protocol", "")
    endpoint = resolved.get("Endpoint", "")
    qp = {"Action": ["Subscribe"], "TopicArn": [topic_arn],
          "Protocol": [protocol], "Endpoint": [endpoint]}
    status, _, resp = await sns.handle_request("POST", "/", {}, b"", qp)
    sub_arn = _xml_extract(resp, "SubscriptionArn")
    fallback = f"{topic_arn}:{str(uuid4())[:8]}"
    pid = sub_arn or fallback
    return {
        "physical_id": pid,
        "ref": pid,
        "attrs": {},
    }


async def _create_lambda_function(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    func_name = resolved.get("FunctionName", f"{ctx['stack_name']}-{logical_id}")
    data = {
        "FunctionName": func_name,
        "Runtime": resolved.get("Runtime", "python3.12"),
        "Role": resolved.get("Role", f"arn:aws:iam::{ACCOUNT_ID}:role/cfn-lambda-role"),
        "Handler": resolved.get("Handler", "index.handler"),
        "Code": resolved.get("Code", {}),
    }
    if "Environment" in resolved:
        data["Environment"] = resolved["Environment"]
    if "Timeout" in resolved:
        data["Timeout"] = resolved["Timeout"]
    if "MemorySize" in resolved:
        data["MemorySize"] = resolved["MemorySize"]
    if "Description" in resolved:
        data["Description"] = resolved["Description"]

    body = json.dumps(data).encode()
    headers = {"content-type": "application/json"}
    status, _, resp = await lambda_svc.handle_request("POST", "/2015-03-31/functions", headers, body, {})
    result = json.loads(resp)
    return {
        "physical_id": result.get("FunctionName", func_name),
        "ref": result.get("FunctionName", func_name),
        "attrs": {"Arn": result.get("FunctionArn", f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:{func_name}")},
    }


async def _create_lambda_permission(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    func_name = resolved.get("FunctionName", "")
    data = {
        "StatementId": resolved.get("StatementId", logical_id),
        "Action": resolved.get("Action", "lambda:InvokeFunction"),
        "Principal": resolved.get("Principal", ""),
        "SourceArn": resolved.get("SourceArn", ""),
    }
    body = json.dumps(data).encode()
    headers = {"content-type": "application/json"}
    path = f"/2015-03-31/functions/{func_name}/policy"
    status, _, resp = await lambda_svc.handle_request("POST", path, headers, body, {})
    return {
        "physical_id": f"{func_name}-{logical_id}",
        "ref": f"{func_name}-{logical_id}",
        "attrs": {},
    }


async def _create_lambda_esm(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    data = {
        "EventSourceArn": resolved.get("EventSourceArn", ""),
        "FunctionName": resolved.get("FunctionName", ""),
        "Enabled": resolved.get("Enabled", True),
        "BatchSize": resolved.get("BatchSize", 10),
    }
    if "StartingPosition" in resolved:
        data["StartingPosition"] = resolved["StartingPosition"]
    body = json.dumps(data).encode()
    headers = {"content-type": "application/json"}
    status, _, resp = await lambda_svc.handle_request(
        "POST", "/2015-03-31/event-source-mappings", headers, body, {}
    )
    result = json.loads(resp)
    esm_id = result.get("UUID", str(uuid4()))
    return {
        "physical_id": esm_id,
        "ref": esm_id,
        "attrs": {},
    }


async def _create_iam_role(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    role_name = resolved.get("RoleName", f"{ctx['stack_name']}-{logical_id}")
    assume_doc = resolved.get("AssumeRolePolicyDocument", {})
    form_body = urlencode({
        "Action": "CreateRole",
        "RoleName": role_name,
        "AssumeRolePolicyDocument": json.dumps(assume_doc) if isinstance(assume_doc, dict) else str(assume_doc),
        "Path": resolved.get("Path", "/"),
    }).encode()
    status, _, resp = await handle_iam_request(
        "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body, {}
    )
    arn = _xml_extract(resp, "Arn")
    role_id = _xml_extract(resp, "RoleId")
    if not arn:
        arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{role_name}"
    if not role_id:
        role_id = f"AROA{str(uuid4()).replace('-', '')[:16].upper()}"

    # Attach managed policies if specified
    for policy_arn_val in resolved.get("ManagedPolicyArns", []):
        form_body2 = urlencode({
            "Action": "AttachRolePolicy",
            "RoleName": role_name,
            "PolicyArn": str(policy_arn_val),
        }).encode()
        await handle_iam_request(
            "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body2, {}
        )

    # Inline policies
    for pol in resolved.get("Policies", []):
        pol_name = pol.get("PolicyName", "")
        pol_doc = pol.get("PolicyDocument", {})
        form_body3 = urlencode({
            "Action": "PutRolePolicy",
            "RoleName": role_name,
            "PolicyName": pol_name,
            "PolicyDocument": json.dumps(pol_doc) if isinstance(pol_doc, dict) else str(pol_doc),
        }).encode()
        await handle_iam_request(
            "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body3, {}
        )

    return {
        "physical_id": role_name,
        "ref": role_name,
        "attrs": {"Arn": arn, "RoleId": role_id},
    }


async def _create_iam_policy(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    policy_name = resolved.get("PolicyName", f"{ctx['stack_name']}-{logical_id}")
    policy_doc = resolved.get("PolicyDocument", {})
    form_body = urlencode({
        "Action": "CreatePolicy",
        "PolicyName": policy_name,
        "PolicyDocument": json.dumps(policy_doc) if isinstance(policy_doc, dict) else str(policy_doc),
    }).encode()
    status, _, resp = await handle_iam_request(
        "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body, {}
    )
    arn = _xml_extract(resp, "Arn")
    if not arn:
        arn = f"arn:aws:iam::{ACCOUNT_ID}:policy/{policy_name}"
    return {
        "physical_id": arn,
        "ref": arn,
        "attrs": {"Arn": arn},
    }


async def _create_events_rule(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    rule_name = resolved.get("Name", f"{ctx['stack_name']}-{logical_id}")
    data = {
        "Name": rule_name,
        "State": resolved.get("State", "ENABLED"),
    }
    if "ScheduleExpression" in resolved:
        data["ScheduleExpression"] = resolved["ScheduleExpression"]
    if "EventPattern" in resolved:
        ep = resolved["EventPattern"]
        data["EventPattern"] = json.dumps(ep) if isinstance(ep, dict) else str(ep)
    if "EventBusName" in resolved:
        data["EventBusName"] = resolved["EventBusName"]
    if "Description" in resolved:
        data["Description"] = resolved["Description"]

    body = json.dumps(data).encode()
    headers = {"x-amz-target": "AWSEvents.PutRule", "content-type": "application/x-amz-json-1.1"}
    status, _, resp = await eventbridge.handle_request("POST", "/", headers, body, {})
    result = json.loads(resp)
    rule_arn = result.get("RuleArn", f"arn:aws:events:{REGION}:{ACCOUNT_ID}:rule/{rule_name}")

    # Add targets if specified
    targets = resolved.get("Targets", [])
    if targets:
        target_data = {"Rule": rule_name, "Targets": targets}
        body2 = json.dumps(target_data).encode()
        headers2 = {"x-amz-target": "AWSEvents.PutTargets", "content-type": "application/x-amz-json-1.1"}
        await eventbridge.handle_request("POST", "/", headers2, body2, {})

    return {
        "physical_id": rule_name,
        "ref": rule_name,
        "attrs": {"Arn": rule_arn},
    }


async def _create_logs_log_group(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    log_group_name = resolved.get("LogGroupName", f"/aws/cfn/{ctx['stack_name']}/{logical_id}")
    data = {"logGroupName": log_group_name}
    if "RetentionInDays" in resolved:
        data["retentionInDays"] = resolved["RetentionInDays"]

    body = json.dumps(data).encode()
    headers = {"x-amz-target": "Logs_20140328.CreateLogGroup", "content-type": "application/x-amz-json-1.1"}
    await cloudwatch_logs.handle_request("POST", "/", headers, body, {})
    arn = f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:{log_group_name}"
    return {
        "physical_id": log_group_name,
        "ref": log_group_name,
        "attrs": {"Arn": arn},
    }


async def _create_secretsmanager_secret(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    name = resolved.get("Name", f"{ctx['stack_name']}/{logical_id}")
    data = {"Name": name}
    if "SecretString" in resolved:
        data["SecretString"] = resolved["SecretString"]
    if "Description" in resolved:
        data["Description"] = resolved["Description"]
    if "KmsKeyId" in resolved:
        data["KmsKeyId"] = resolved["KmsKeyId"]

    body = json.dumps(data).encode()
    headers = {"x-amz-target": "secretsmanager.CreateSecret", "content-type": "application/x-amz-json-1.1"}
    status, _, resp = await secretsmanager.handle_request("POST", "/", headers, body, {})
    result = json.loads(resp)
    secret_arn = result.get("ARN", f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:{name}")
    return {
        "physical_id": secret_arn,
        "ref": secret_arn,
        "attrs": {"Arn": secret_arn},
    }


async def _create_ssm_parameter(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    name = resolved.get("Name", f"/{ctx['stack_name']}/{logical_id}")
    data = {
        "Name": name,
        "Value": resolved.get("Value", ""),
        "Type": resolved.get("Type", "String"),
    }
    if "Description" in resolved:
        data["Description"] = resolved["Description"]

    body = json.dumps(data).encode()
    headers = {"x-amz-target": "AmazonSSM.PutParameter", "content-type": "application/x-amz-json-1.1"}
    await ssm.handle_request("POST", "/", headers, body, {})
    return {
        "physical_id": name,
        "ref": name,
        "attrs": {"Type": data["Type"], "Value": data["Value"]},
    }


async def _create_stepfunctions_statemachine(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    name = resolved.get("StateMachineName", f"{ctx['stack_name']}-{logical_id}")
    definition = resolved.get("DefinitionString", resolved.get("Definition", "{}"))
    if isinstance(definition, dict):
        definition = json.dumps(definition)
    role_arn = resolved.get("RoleArn", f"arn:aws:iam::{ACCOUNT_ID}:role/cfn-sfn-role")

    data = {
        "name": name,
        "definition": definition,
        "roleArn": role_arn,
    }
    body = json.dumps(data).encode()
    headers = {"x-amz-target": "AWSStepFunctions.CreateStateMachine", "content-type": "application/x-amz-json-1.0"}
    status, _, resp = await stepfunctions.handle_request("POST", "/", headers, body, {})
    result = json.loads(resp)
    sm_arn = result.get("stateMachineArn", f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:{name}")
    return {
        "physical_id": sm_arn,
        "ref": sm_arn,
        "attrs": {"Arn": sm_arn, "Name": name},
    }


async def _create_ec2_vpc(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    cidr = resolved.get("CidrBlock", "10.0.0.0/16")
    form_body = urlencode({"Action": "CreateVpc", "CidrBlock": cidr}).encode()
    status, _, resp = await ec2.handle_request(
        "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body, {}
    )
    vpc_id = _xml_extract(resp, "vpcId")
    if not vpc_id:
        vpc_id = f"vpc-{str(uuid4()).replace('-', '')[:8]}"
    return {
        "physical_id": vpc_id,
        "ref": vpc_id,
        "attrs": {
            "VpcId": vpc_id,
            "CidrBlock": cidr,
            "DefaultNetworkAcl": f"acl-{str(uuid4()).replace('-', '')[:8]}",
            "DefaultSecurityGroup": f"sg-{str(uuid4()).replace('-', '')[:8]}",
        },
    }


async def _create_ec2_subnet(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    vpc_id = resolved.get("VpcId", "")
    cidr = resolved.get("CidrBlock", "10.0.0.0/24")
    az = resolved.get("AvailabilityZone", f"{REGION}a")
    params = {"Action": "CreateSubnet", "VpcId": vpc_id, "CidrBlock": cidr}
    if az:
        params["AvailabilityZone"] = az
    form_body = urlencode(params).encode()
    status, _, resp = await ec2.handle_request(
        "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body, {}
    )
    subnet_id = _xml_extract(resp, "subnetId")
    if not subnet_id:
        subnet_id = f"subnet-{str(uuid4()).replace('-', '')[:8]}"
    return {
        "physical_id": subnet_id,
        "ref": subnet_id,
        "attrs": {
            "SubnetId": subnet_id,
            "VpcId": vpc_id,
            "CidrBlock": cidr,
            "AvailabilityZone": az,
        },
    }


async def _create_ec2_security_group(logical_id, props, ctx):
    resolved = _resolve(props, ctx)
    group_name = resolved.get("GroupName", f"{ctx['stack_name']}-{logical_id}")
    description = resolved.get("GroupDescription", group_name)
    vpc_id = resolved.get("VpcId", "")
    params = {
        "Action": "CreateSecurityGroup",
        "GroupName": group_name,
        "GroupDescription": description,
    }
    if vpc_id:
        params["VpcId"] = vpc_id
    form_body = urlencode(params).encode()
    status, _, resp = await ec2.handle_request(
        "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body, {}
    )
    group_id = _xml_extract(resp, "groupId")
    if not group_id:
        group_id = f"sg-{str(uuid4()).replace('-', '')[:8]}"
    return {
        "physical_id": group_id,
        "ref": group_id,
        "attrs": {
            "GroupId": group_id,
            "GroupName": group_name,
            "VpcId": vpc_id,
        },
    }


async def _create_apigateway_restapi(logical_id, props, ctx):
    """Create API Gateway REST API."""
    resolved = {k: _resolve(v, ctx) for k, v in props.items()}
    name = resolved.get("Name", f"{ctx.get('stack_name', '')}-{logical_id}")
    data = {"name": name}
    if "Description" in resolved:
        data["description"] = resolved["Description"]
    body = json.dumps(data).encode()
    headers = {"content-type": "application/json"}
    status, _, resp = await apigateway_v1.handle_request("POST", "/restapis", headers, body, {})
    result = json.loads(resp) if resp else {}
    api_id = result.get("id", str(uuid4())[:10])
    root_resource_id = result.get("rootResourceId", "")
    return {
        "physical_id": api_id,
        "ref": api_id,
        "attrs": {
            "RestApiId": api_id,
            "RootResourceId": root_resource_id,
        },
    }


# Register all resource creators
_RESOURCE_CREATORS = {
    "AWS::S3::Bucket": _create_s3_bucket,
    "AWS::S3::BucketPolicy": _create_s3_bucket_policy,
    "AWS::DynamoDB::Table": _create_dynamodb_table,
    "AWS::SQS::Queue": _create_sqs_queue,
    "AWS::SNS::Topic": _create_sns_topic,
    "AWS::SNS::Subscription": _create_sns_subscription,
    "AWS::Lambda::Function": _create_lambda_function,
    "AWS::Lambda::Permission": _create_lambda_permission,
    "AWS::Lambda::EventSourceMapping": _create_lambda_esm,
    "AWS::IAM::Role": _create_iam_role,
    "AWS::IAM::Policy": _create_iam_policy,
    "AWS::Events::Rule": _create_events_rule,
    "AWS::Logs::LogGroup": _create_logs_log_group,
    "AWS::SecretsManager::Secret": _create_secretsmanager_secret,
    "AWS::SSM::Parameter": _create_ssm_parameter,
    "AWS::StepFunctions::StateMachine": _create_stepfunctions_statemachine,
    "AWS::EC2::VPC": _create_ec2_vpc,
    "AWS::EC2::Subnet": _create_ec2_subnet,
    "AWS::EC2::SecurityGroup": _create_ec2_security_group,
    "AWS::ApiGateway::RestApi": _create_apigateway_restapi,
}


# ── Resource Handlers — DELETE ────────────────────────────────


async def _delete_s3_bucket(physical_id, resource_type, ctx):
    try:
        await s3.handle_request("DELETE", f"/{physical_id}", {}, b"", {})
    except Exception as e:
        logger.warning("Failed to delete S3 bucket %s: %s", physical_id, e)


async def _delete_s3_bucket_policy(physical_id, resource_type, ctx):
    # Bucket policy deletion is a no-op when bucket is deleted
    pass


async def _delete_dynamodb_table(physical_id, resource_type, ctx):
    try:
        data = {"TableName": physical_id}
        body = json.dumps(data).encode()
        headers = {"x-amz-target": "DynamoDB_20120810.DeleteTable", "content-type": "application/x-amz-json-1.0"}
        await dynamodb.handle_request("POST", "/", headers, body, {})
    except Exception as e:
        logger.warning("Failed to delete DynamoDB table %s: %s", physical_id, e)


async def _delete_sqs_queue(physical_id, resource_type, ctx):
    try:
        qp = {"Action": ["DeleteQueue"], "QueueUrl": [physical_id]}
        await sqs.handle_request("POST", "/", {}, b"", qp)
    except Exception as e:
        logger.warning("Failed to delete SQS queue %s: %s", physical_id, e)


async def _delete_sns_topic(physical_id, resource_type, ctx):
    try:
        qp = {"Action": ["DeleteTopic"], "TopicArn": [physical_id]}
        await sns.handle_request("POST", "/", {}, b"", qp)
    except Exception as e:
        logger.warning("Failed to delete SNS topic %s: %s", physical_id, e)


async def _delete_sns_subscription(physical_id, resource_type, ctx):
    try:
        qp = {"Action": ["Unsubscribe"], "SubscriptionArn": [physical_id]}
        await sns.handle_request("POST", "/", {}, b"", qp)
    except Exception as e:
        logger.warning("Failed to delete SNS subscription %s: %s", physical_id, e)


async def _delete_lambda_function(physical_id, resource_type, ctx):
    try:
        await lambda_svc.handle_request(
            "DELETE", f"/2015-03-31/functions/{physical_id}", {}, b"", {}
        )
    except Exception as e:
        logger.warning("Failed to delete Lambda function %s: %s", physical_id, e)


async def _delete_lambda_permission(physical_id, resource_type, ctx):
    pass  # Permissions are cleaned up with the function


async def _delete_lambda_esm(physical_id, resource_type, ctx):
    try:
        await lambda_svc.handle_request(
            "DELETE", f"/2015-03-31/event-source-mappings/{physical_id}", {}, b"", {}
        )
    except Exception as e:
        logger.warning("Failed to delete Lambda ESM %s: %s", physical_id, e)


async def _delete_iam_role(physical_id, resource_type, ctx):
    try:
        # Detach all managed policies first
        form_body = urlencode({"Action": "ListAttachedRolePolicies", "RoleName": physical_id}).encode()
        status, _, resp = await handle_iam_request(
            "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body, {}
        )
        # Best-effort detach
        import re as _re
        for arn_match in _re.finditer(r"<PolicyArn>(.*?)</PolicyArn>", resp.decode("utf-8", errors="replace") if isinstance(resp, bytes) else resp):
            form_body2 = urlencode({
                "Action": "DetachRolePolicy",
                "RoleName": physical_id,
                "PolicyArn": arn_match.group(1),
            }).encode()
            await handle_iam_request(
                "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body2, {}
            )

        form_body = urlencode({"Action": "DeleteRole", "RoleName": physical_id}).encode()
        await handle_iam_request(
            "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body, {}
        )
    except Exception as e:
        logger.warning("Failed to delete IAM role %s: %s", physical_id, e)


async def _delete_iam_policy(physical_id, resource_type, ctx):
    try:
        form_body = urlencode({"Action": "DeletePolicy", "PolicyArn": physical_id}).encode()
        await handle_iam_request(
            "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body, {}
        )
    except Exception as e:
        logger.warning("Failed to delete IAM policy %s: %s", physical_id, e)


async def _delete_events_rule(physical_id, resource_type, ctx):
    try:
        # Remove targets first
        data = {"Rule": physical_id}
        body = json.dumps(data).encode()
        headers = {"x-amz-target": "AWSEvents.ListTargetsByRule", "content-type": "application/x-amz-json-1.1"}
        status, _, resp = await eventbridge.handle_request("POST", "/", headers, body, {})
        result = json.loads(resp)
        target_ids = [t["Id"] for t in result.get("Targets", [])]
        if target_ids:
            rm_data = {"Rule": physical_id, "Ids": target_ids}
            rm_body = json.dumps(rm_data).encode()
            headers2 = {"x-amz-target": "AWSEvents.RemoveTargets", "content-type": "application/x-amz-json-1.1"}
            await eventbridge.handle_request("POST", "/", headers2, rm_body, {})

        del_data = {"Name": physical_id}
        del_body = json.dumps(del_data).encode()
        headers3 = {"x-amz-target": "AWSEvents.DeleteRule", "content-type": "application/x-amz-json-1.1"}
        await eventbridge.handle_request("POST", "/", headers3, del_body, {})
    except Exception as e:
        logger.warning("Failed to delete EventBridge rule %s: %s", physical_id, e)


async def _delete_logs_log_group(physical_id, resource_type, ctx):
    try:
        data = {"logGroupName": physical_id}
        body = json.dumps(data).encode()
        headers = {"x-amz-target": "Logs_20140328.DeleteLogGroup", "content-type": "application/x-amz-json-1.1"}
        await cloudwatch_logs.handle_request("POST", "/", headers, body, {})
    except Exception as e:
        logger.warning("Failed to delete log group %s: %s", physical_id, e)


async def _delete_secretsmanager_secret(physical_id, resource_type, ctx):
    try:
        data = {"SecretId": physical_id, "ForceDeleteWithoutRecovery": True}
        body = json.dumps(data).encode()
        headers = {"x-amz-target": "secretsmanager.DeleteSecret", "content-type": "application/x-amz-json-1.1"}
        await secretsmanager.handle_request("POST", "/", headers, body, {})
    except Exception as e:
        logger.warning("Failed to delete secret %s: %s", physical_id, e)


async def _delete_ssm_parameter(physical_id, resource_type, ctx):
    try:
        data = {"Name": physical_id}
        body = json.dumps(data).encode()
        headers = {"x-amz-target": "AmazonSSM.DeleteParameter", "content-type": "application/x-amz-json-1.1"}
        await ssm.handle_request("POST", "/", headers, body, {})
    except Exception as e:
        logger.warning("Failed to delete SSM parameter %s: %s", physical_id, e)


async def _delete_stepfunctions_statemachine(physical_id, resource_type, ctx):
    try:
        data = {"stateMachineArn": physical_id}
        body = json.dumps(data).encode()
        headers = {"x-amz-target": "AWSStepFunctions.DeleteStateMachine", "content-type": "application/x-amz-json-1.0"}
        await stepfunctions.handle_request("POST", "/", headers, body, {})
    except Exception as e:
        logger.warning("Failed to delete state machine %s: %s", physical_id, e)


async def _delete_ec2_vpc(physical_id, resource_type, ctx):
    try:
        form_body = urlencode({"Action": "DeleteVpc", "VpcId": physical_id}).encode()
        await ec2.handle_request(
            "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body, {}
        )
    except Exception as e:
        logger.warning("Failed to delete VPC %s: %s", physical_id, e)


async def _delete_ec2_subnet(physical_id, resource_type, ctx):
    try:
        form_body = urlencode({"Action": "DeleteSubnet", "SubnetId": physical_id}).encode()
        await ec2.handle_request(
            "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body, {}
        )
    except Exception as e:
        logger.warning("Failed to delete Subnet %s: %s", physical_id, e)


async def _delete_ec2_security_group(physical_id, resource_type, ctx):
    try:
        form_body = urlencode({"Action": "DeleteSecurityGroup", "GroupId": physical_id}).encode()
        await ec2.handle_request(
            "POST", "/", {"content-type": "application/x-www-form-urlencoded"}, form_body, {}
        )
    except Exception as e:
        logger.warning("Failed to delete security group %s: %s", physical_id, e)


async def _delete_apigateway_restapi(physical_id, resource_type, ctx):
    try:
        await apigateway_v1.handle_request("DELETE", f"/restapis/{physical_id}", {}, b"", {})
    except Exception as e:
        logger.warning("Failed to delete REST API %s: %s", physical_id, e)


_RESOURCE_DELETERS = {
    "AWS::S3::Bucket": _delete_s3_bucket,
    "AWS::S3::BucketPolicy": _delete_s3_bucket_policy,
    "AWS::DynamoDB::Table": _delete_dynamodb_table,
    "AWS::SQS::Queue": _delete_sqs_queue,
    "AWS::SNS::Topic": _delete_sns_topic,
    "AWS::SNS::Subscription": _delete_sns_subscription,
    "AWS::Lambda::Function": _delete_lambda_function,
    "AWS::Lambda::Permission": _delete_lambda_permission,
    "AWS::Lambda::EventSourceMapping": _delete_lambda_esm,
    "AWS::IAM::Role": _delete_iam_role,
    "AWS::IAM::Policy": _delete_iam_policy,
    "AWS::Events::Rule": _delete_events_rule,
    "AWS::Logs::LogGroup": _delete_logs_log_group,
    "AWS::SecretsManager::Secret": _delete_secretsmanager_secret,
    "AWS::SSM::Parameter": _delete_ssm_parameter,
    "AWS::StepFunctions::StateMachine": _delete_stepfunctions_statemachine,
    "AWS::EC2::VPC": _delete_ec2_vpc,
    "AWS::EC2::Subnet": _delete_ec2_subnet,
    "AWS::EC2::SecurityGroup": _delete_ec2_security_group,
    "AWS::ApiGateway::RestApi": _delete_apigateway_restapi,
}


# ── Stack Operations ──────────────────────────────────────────


def _add_event(stack, logical_id, resource_type, status, reason=""):
    """Append an event to the stack's event list."""
    stack.setdefault("events", []).append({
        "StackId": stack["StackId"],
        "StackName": stack["StackName"],
        "LogicalResourceId": logical_id,
        "ResourceType": resource_type,
        "ResourceStatus": status,
        "ResourceStatusReason": reason,
        "Timestamp": _now(),
        "EventId": str(uuid4()),
    })


async def _do_create_stack(stack_name, template, params, tags):
    """Full stack creation pipeline."""
    stack_id = f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:stack/{stack_name}/{str(uuid4())}"

    stack = {
        "StackName": stack_name,
        "StackId": stack_id,
        "StackStatus": "CREATE_IN_PROGRESS",
        "StackStatusReason": "",
        "CreationTime": _now(),
        "LastUpdatedTime": _now(),
        "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()],
        "Tags": tags,
        "Outputs": [],
        "Resources": {},
        "Template": template,
        "TemplateBody": json.dumps(template),
        "events": [],
        "creation_order": [],
    }
    _stacks[stack_name] = stack

    _add_event(stack, stack_name, "AWS::CloudFormation::Stack", "CREATE_IN_PROGRESS", "User Initiated")

    try:
        # 1. Validate template structure
        resources = template.get("Resources", {})
        if not resources:
            raise ValueError("Template must contain at least one resource")

        # 2. Build parameter context (apply defaults from template)
        template_params = template.get("Parameters", {})
        resolved_params = {}
        for pname, pdef in template_params.items():
            if pname in params:
                resolved_params[pname] = params[pname]
            elif "Default" in pdef:
                resolved_params[pname] = str(pdef["Default"])
            else:
                raise ValueError(f"Missing required parameter: {pname}")

        # 2b. Validate parameter constraints
        for pname, pdef in template_params.items():
            val = resolved_params.get(pname, "")
            allowed = pdef.get("AllowedValues")
            if allowed and val not in [str(v) for v in allowed]:
                desc = pdef.get("ConstraintDescription", f"Value must be one of: {allowed}")
                raise ValueError(f"Parameter '{pname}' value '{val}' is not an allowed value. {desc}")
            pattern = pdef.get("AllowedPattern")
            if pattern:
                import re as _re
                if not _re.fullmatch(pattern, str(val)):
                    desc = pdef.get("ConstraintDescription", f"Value must match pattern: {pattern}")
                    raise ValueError(f"Parameter '{pname}' value '{val}' does not match pattern. {desc}")
            ptype = pdef.get("Type", "String")
            if ptype == "Number":
                try:
                    num = float(val)
                    if "MinValue" in pdef and num < float(pdef["MinValue"]):
                        raise ValueError(f"Parameter '{pname}' value {val} is less than minimum {pdef['MinValue']}")
                    if "MaxValue" in pdef and num > float(pdef["MaxValue"]):
                        raise ValueError(f"Parameter '{pname}' value {val} exceeds maximum {pdef['MaxValue']}")
                except ValueError:
                    if "MinValue" in pdef or "MaxValue" in pdef:
                        raise
            if "MinLength" in pdef and len(str(val)) < int(pdef["MinLength"]):
                raise ValueError(f"Parameter '{pname}' value is shorter than minimum length {pdef['MinLength']}")
            if "MaxLength" in pdef and len(str(val)) > int(pdef["MaxLength"]):
                raise ValueError(f"Parameter '{pname}' value exceeds maximum length {pdef['MaxLength']}")

        # 3. Evaluate conditions
        conditions = {}
        for cond_name, cond_def in template.get("Conditions", {}).items():
            ctx_for_cond = {
                "params": resolved_params,
                "resources": {},
                "stack_name": stack_name,
                "stack_id": stack_id,
                "region": REGION,
                "account_id": ACCOUNT_ID,
                "conditions": conditions,
                "mappings": template.get("Mappings", {}),
            }
            conditions[cond_name] = bool(_resolve(cond_def, ctx_for_cond))

        # 4. Filter resources by conditions
        active_resources = {}
        for lid, rdef in resources.items():
            cond = rdef.get("Condition")
            if cond and not conditions.get(cond, True):
                continue
            active_resources[lid] = rdef

        # 5. Build dependency graph and sort
        deps = _build_deps(active_resources)
        ordered = _topological_sort(deps)

        # 6. Create resources in order
        created_resources = {}
        ctx = {
            "params": resolved_params,
            "resources": created_resources,
            "stack_name": stack_name,
            "stack_id": stack_id,
            "region": REGION,
            "account_id": ACCOUNT_ID,
            "conditions": conditions,
            "mappings": template.get("Mappings", {}),
        }

        # Resource types to silently skip (CDK metadata, etc.)
        _SKIP_TYPES = {"AWS::CDK::Metadata", "AWS::CloudFormation::WaitConditionHandle"}

        for lid in ordered:
            rdef = active_resources[lid]
            rtype = rdef.get("Type", "")
            props = rdef.get("Properties", {})

            # Silently skip CDK metadata and other non-provisioned types
            if rtype in _SKIP_TYPES:
                stub_id = f"{stack_name}-{lid}-skipped"
                created_resources[lid] = {"physical_id": stub_id, "ref": stub_id, "attrs": {}}
                stack["Resources"][lid] = {
                    "LogicalResourceId": lid, "PhysicalResourceId": stub_id,
                    "ResourceType": rtype, "ResourceStatus": "CREATE_COMPLETE", "Timestamp": _now(),
                }
                continue

            _add_event(stack, lid, rtype, "CREATE_IN_PROGRESS")

            creator = _RESOURCE_CREATORS.get(rtype)
            if creator:
                try:
                    result = await creator(lid, props, ctx)
                    created_resources[lid] = result
                    stack["Resources"][lid] = {
                        "LogicalResourceId": lid,
                        "PhysicalResourceId": result["physical_id"],
                        "ResourceType": rtype,
                        "ResourceStatus": "CREATE_COMPLETE",
                        "ResourceStatusReason": "",
                        "Timestamp": _now(),
                    }
                    stack["creation_order"].append(lid)
                    _add_event(stack, lid, rtype, "CREATE_COMPLETE")
                except Exception as e:
                    logger.error("Failed to create resource %s (%s): %s", lid, rtype, e)
                    stack["Resources"][lid] = {
                        "LogicalResourceId": lid,
                        "PhysicalResourceId": "",
                        "ResourceType": rtype,
                        "ResourceStatus": "CREATE_FAILED",
                        "ResourceStatusReason": str(e),
                        "Timestamp": _now(),
                    }
                    _add_event(stack, lid, rtype, "CREATE_FAILED", str(e))
                    raise
            else:
                # Unsupported resource type — create a stub
                stub_id = f"{stack_name}-{lid}-{str(uuid4())[:8]}"
                created_resources[lid] = {
                    "physical_id": stub_id,
                    "ref": stub_id,
                    "attrs": {},
                }
                stack["Resources"][lid] = {
                    "LogicalResourceId": lid,
                    "PhysicalResourceId": stub_id,
                    "ResourceType": rtype,
                    "ResourceStatus": "CREATE_COMPLETE",
                    "Timestamp": _now(),
                }
                stack["creation_order"].append(lid)
                _add_event(stack, lid, rtype, "CREATE_COMPLETE")

        # 7. Resolve outputs
        outputs = []
        for out_name, out_def in template.get("Outputs", {}).items():
            out_val = _resolve(out_def.get("Value", ""), ctx)
            out_desc = out_def.get("Description", "")
            export_name = ""
            if "Export" in out_def:
                export_name = str(_resolve(out_def["Export"].get("Name", ""), ctx))
            outputs.append({
                "OutputKey": out_name,
                "OutputValue": str(out_val),
                "Description": out_desc,
                "ExportName": export_name,
            })
        stack["Outputs"] = outputs

        # 8. Mark complete
        stack["StackStatus"] = "CREATE_COMPLETE"
        _add_event(stack, stack_name, "AWS::CloudFormation::Stack", "CREATE_COMPLETE")

    except Exception as e:
        logger.error("Stack creation failed for %s: %s", stack_name, e)
        stack["StackStatus"] = "CREATE_FAILED"
        stack["StackStatusReason"] = str(e)
        _add_event(stack, stack_name, "AWS::CloudFormation::Stack", "CREATE_FAILED", str(e))

        # Rollback — delete created resources in reverse order
        await _rollback_resources(stack)
        stack["StackStatus"] = "ROLLBACK_COMPLETE"
        _add_event(stack, stack_name, "AWS::CloudFormation::Stack", "ROLLBACK_COMPLETE")

    return stack


async def _rollback_resources(stack):
    """Delete created resources in reverse order (best-effort)."""
    order = list(reversed(stack.get("creation_order", [])))
    ctx = {"stack_name": stack["StackName"], "region": REGION, "account_id": ACCOUNT_ID}
    for lid in order:
        res_info = stack["Resources"].get(lid)
        if not res_info:
            continue
        rtype = res_info.get("ResourceType", "")
        physical_id = res_info.get("PhysicalResourceId", "")
        deleter = _RESOURCE_DELETERS.get(rtype)
        if deleter and physical_id:
            try:
                await deleter(physical_id, rtype, ctx)
            except Exception as e:
                logger.warning("Rollback: failed to delete %s (%s): %s", lid, physical_id, e)


async def _do_delete_stack(stack_name):
    """Delete all resources in reverse creation order."""
    stack = _stacks.get(stack_name)
    if not stack:
        return

    stack["StackStatus"] = "DELETE_IN_PROGRESS"
    _add_event(stack, stack_name, "AWS::CloudFormation::Stack", "DELETE_IN_PROGRESS", "User Initiated")

    order = list(reversed(stack.get("creation_order", [])))
    ctx = {"stack_name": stack_name, "region": REGION, "account_id": ACCOUNT_ID}

    for lid in order:
        res_info = stack["Resources"].get(lid)
        if not res_info:
            continue
        rtype = res_info.get("ResourceType", "")
        physical_id = res_info.get("PhysicalResourceId", "")
        _add_event(stack, lid, rtype, "DELETE_IN_PROGRESS")

        deleter = _RESOURCE_DELETERS.get(rtype)
        if deleter and physical_id:
            try:
                await deleter(physical_id, rtype, ctx)
                _add_event(stack, lid, rtype, "DELETE_COMPLETE")
            except Exception as e:
                logger.warning("Failed to delete %s (%s): %s", lid, physical_id, e)
                _add_event(stack, lid, rtype, "DELETE_FAILED", str(e))
        else:
            _add_event(stack, lid, rtype, "DELETE_COMPLETE")

    stack["StackStatus"] = "DELETE_COMPLETE"
    stack["DeletionTime"] = _now()
    _add_event(stack, stack_name, "AWS::CloudFormation::Stack", "DELETE_COMPLETE")


async def _do_update_stack(stack_name, template, params):
    """Simplified update: delete old resources, create new ones."""
    stack = _stacks.get(stack_name)
    if not stack:
        raise ValueError(f"Stack {stack_name} does not exist")

    stack["StackStatus"] = "UPDATE_IN_PROGRESS"
    _add_event(stack, stack_name, "AWS::CloudFormation::Stack", "UPDATE_IN_PROGRESS")

    # Delete old resources
    old_tags = stack.get("Tags", [])
    old_stack_id = stack["StackId"]
    await _rollback_resources(stack)

    # Rebuild with new template
    new_stack = await _do_create_stack(stack_name, template, params, old_tags)
    new_stack["StackId"] = old_stack_id

    if new_stack["StackStatus"] in ("CREATE_COMPLETE",):
        new_stack["StackStatus"] = "UPDATE_COMPLETE"
        _add_event(new_stack, stack_name, "AWS::CloudFormation::Stack", "UPDATE_COMPLETE")
        _stacks[stack_name] = new_stack
    else:
        # _do_create_stack failed (returns ROLLBACK_COMPLETE) — restore old stack
        new_stack["StackStatus"] = "UPDATE_ROLLBACK_COMPLETE"
        new_stack["StackStatusReason"] = new_stack.get("StackStatusReason", "Update failed")
        _add_event(new_stack, stack_name, "AWS::CloudFormation::Stack", "UPDATE_ROLLBACK_COMPLETE")
        _stacks[stack_name] = new_stack


# ── API Actions ───────────────────────────────────────────────


async def _act_create_stack(params):
    stack_name = _p(params, "StackName")  # Raw name for create, not ARN-resolved
    if not stack_name:
        return _error("ValidationError", "StackName is required")
    if stack_name in _stacks and _stacks[stack_name]["StackStatus"] != "DELETE_COMPLETE":
        return _error("AlreadyExistsException", f"Stack [{stack_name}] already exists")

    template_body = _p(params, "TemplateBody")
    template_url = _p(params, "TemplateURL")
    if not template_body and template_url:
        template_body = await _fetch_template_url(template_url)
    if not template_body:
        return _error("ValidationError", "Either TemplateBody or TemplateURL is required")

    try:
        template = _parse_template(template_body)
    except ValueError as e:
        return _error("ValidationError", str(e))

    param_values = _parse_members(params, "Parameters")
    tags = _parse_tags(params)

    stack = await _do_create_stack(stack_name, template, param_values, tags)
    return _xml("CreateStackResponse",
                f"<CreateStackResult><StackId>{_esc(stack['StackId'])}</StackId></CreateStackResult>")


async def _act_delete_stack(params):
    stack_name = _resolve_stack_name(_p(params, "StackName"))
    if not stack_name:
        return _error("ValidationError", "StackName is required")
    if stack_name not in _stacks:
        # AWS is lenient on deleting non-existent stacks
        return _xml("DeleteStackResponse", "<DeleteStackResult/>")

    await _do_delete_stack(stack_name)
    return _xml("DeleteStackResponse", "<DeleteStackResult/>")


async def _act_update_stack(params):
    stack_name = _resolve_stack_name(_p(params, "StackName"))
    if not stack_name:
        return _error("ValidationError", "StackName is required")
    if stack_name not in _stacks:
        return _error("ValidationError", f"Stack [{stack_name}] does not exist")

    template_body = _p(params, "TemplateBody")
    template_url = _p(params, "TemplateURL")
    if not template_body and template_url:
        template_body = await _fetch_template_url(template_url)
    if not template_body:
        return _error("ValidationError", "Either TemplateBody or TemplateURL is required")

    try:
        template = _parse_template(template_body)
    except ValueError as e:
        return _error("ValidationError", str(e))

    param_values = _parse_members(params, "Parameters")
    await _do_update_stack(stack_name, template, param_values)
    stack = _stacks[stack_name]
    return _xml("UpdateStackResponse",
                f"<UpdateStackResult><StackId>{_esc(stack['StackId'])}</StackId></UpdateStackResult>")


def _act_describe_stacks(params):
    stack_name = _resolve_stack_name(_p(params, "StackName"))
    if stack_name:
        stack = _stacks.get(stack_name)
        if not stack:
            return _error("ValidationError", f"Stack [{stack_name}] does not exist")
        stacks = [stack]
    else:
        stacks = [s for s in _stacks.values() if s["StackStatus"] != "DELETE_COMPLETE"]

    members_xml = ""
    for s in stacks:
        params_xml = ""
        for p in s.get("Parameters", []):
            params_xml += (
                f"<member>"
                f"<ParameterKey>{_esc(p['ParameterKey'])}</ParameterKey>"
                f"<ParameterValue>{_esc(p['ParameterValue'])}</ParameterValue>"
                f"</member>"
            )
        outputs_xml = ""
        for o in s.get("Outputs", []):
            outputs_xml += (
                f"<member>"
                f"<OutputKey>{_esc(o['OutputKey'])}</OutputKey>"
                f"<OutputValue>{_esc(o['OutputValue'])}</OutputValue>"
                f"<Description>{_esc(o.get('Description', ''))}</Description>"
                f"</member>"
            )
        tags_xml = ""
        for t in s.get("Tags", []):
            tags_xml += (
                f"<member>"
                f"<Key>{_esc(t['Key'])}</Key>"
                f"<Value>{_esc(t['Value'])}</Value>"
                f"</member>"
            )
        desc = s.get("Description", s.get("Template", {}).get("Description", ""))
        members_xml += (
            f"<member>"
            f"<StackName>{_esc(s['StackName'])}</StackName>"
            f"<StackId>{_esc(s['StackId'])}</StackId>"
            f"<Description>{_esc(desc)}</Description>"
            f"<StackStatus>{_esc(s['StackStatus'])}</StackStatus>"
            f"<StackStatusReason>{_esc(s.get('StackStatusReason', ''))}</StackStatusReason>"
            f"<CreationTime>{_esc(s.get('CreationTime', ''))}</CreationTime>"
            f"<LastUpdatedTime>{_esc(s.get('LastUpdatedTime', ''))}</LastUpdatedTime>"
            f"<Parameters>{params_xml}</Parameters>"
            f"<Outputs>{outputs_xml}</Outputs>"
            f"<Tags>{tags_xml}</Tags>"
            f"</member>"
        )

    return _xml("DescribeStacksResponse",
                f"<DescribeStacksResult><Stacks>{members_xml}</Stacks></DescribeStacksResult>")


def _act_describe_stack_resource(params):
    stack_name = _resolve_stack_name(_p(params, "StackName"))
    logical_id = _p(params, "LogicalResourceId")
    stack = _stacks.get(stack_name)
    if not stack:
        return _error("ValidationError", f"Stack [{stack_name}] does not exist")
    res = stack.get("Resources", {}).get(logical_id)
    if not res:
        return _error("ValidationError", f"Resource [{logical_id}] does not exist in stack [{stack_name}]")

    detail_xml = (
        f"<LogicalResourceId>{_esc(res['LogicalResourceId'])}</LogicalResourceId>"
        f"<PhysicalResourceId>{_esc(res.get('PhysicalResourceId', ''))}</PhysicalResourceId>"
        f"<ResourceType>{_esc(res.get('ResourceType', ''))}</ResourceType>"
        f"<ResourceStatus>{_esc(res.get('ResourceStatus', ''))}</ResourceStatus>"
        f"<ResourceStatusReason>{_esc(res.get('ResourceStatusReason', ''))}</ResourceStatusReason>"
        f"<Timestamp>{_esc(res.get('Timestamp', ''))}</Timestamp>"
        f"<StackId>{_esc(stack['StackId'])}</StackId>"
        f"<StackName>{_esc(stack['StackName'])}</StackName>"
    )
    return _xml("DescribeStackResourceResponse",
                f"<DescribeStackResourceResult><StackResourceDetail>{detail_xml}</StackResourceDetail></DescribeStackResourceResult>")


def _act_describe_stack_resources(params):
    stack_name = _resolve_stack_name(_p(params, "StackName"))
    stack = _stacks.get(stack_name)
    if not stack:
        return _error("ValidationError", f"Stack [{stack_name}] does not exist")

    members_xml = ""
    for lid, res in stack.get("Resources", {}).items():
        members_xml += (
            f"<member>"
            f"<LogicalResourceId>{_esc(res['LogicalResourceId'])}</LogicalResourceId>"
            f"<PhysicalResourceId>{_esc(res.get('PhysicalResourceId', ''))}</PhysicalResourceId>"
            f"<ResourceType>{_esc(res.get('ResourceType', ''))}</ResourceType>"
            f"<ResourceStatus>{_esc(res.get('ResourceStatus', ''))}</ResourceStatus>"
            f"<ResourceStatusReason>{_esc(res.get('ResourceStatusReason', ''))}</ResourceStatusReason>"
            f"<Timestamp>{_esc(res.get('Timestamp', ''))}</Timestamp>"
            f"</member>"
        )
    return _xml("DescribeStackResourcesResponse",
                f"<DescribeStackResourcesResult><StackResources>{members_xml}</StackResources></DescribeStackResourcesResult>")


def _act_describe_stack_events(params):
    stack_name = _resolve_stack_name(_p(params, "StackName"))
    stack = _stacks.get(stack_name)
    if not stack:
        return _error("ValidationError", f"Stack [{stack_name}] does not exist")

    events_xml = ""
    for evt in stack.get("events", []):
        events_xml += (
            f"<member>"
            f"<EventId>{_esc(evt.get('EventId', ''))}</EventId>"
            f"<StackId>{_esc(evt.get('StackId', ''))}</StackId>"
            f"<StackName>{_esc(evt.get('StackName', ''))}</StackName>"
            f"<LogicalResourceId>{_esc(evt.get('LogicalResourceId', ''))}</LogicalResourceId>"
            f"<ResourceType>{_esc(evt.get('ResourceType', ''))}</ResourceType>"
            f"<ResourceStatus>{_esc(evt.get('ResourceStatus', ''))}</ResourceStatus>"
            f"<ResourceStatusReason>{_esc(evt.get('ResourceStatusReason', ''))}</ResourceStatusReason>"
            f"<Timestamp>{_esc(evt.get('Timestamp', ''))}</Timestamp>"
            f"</member>"
        )
    return _xml("DescribeStackEventsResponse",
                f"<DescribeStackEventsResult><StackEvents>{events_xml}</StackEvents></DescribeStackEventsResult>")


def _act_list_stacks(params):
    filter_status = []
    n = 1
    while True:
        s = _p(params, f"StackStatusFilter.member.{n}")
        if not s:
            break
        filter_status.append(s)
        n += 1

    stacks = list(_stacks.values())
    if filter_status:
        stacks = [s for s in stacks if s["StackStatus"] in filter_status]

    members_xml = ""
    for s in stacks:
        members_xml += (
            f"<member>"
            f"<StackName>{_esc(s['StackName'])}</StackName>"
            f"<StackId>{_esc(s['StackId'])}</StackId>"
            f"<StackStatus>{_esc(s['StackStatus'])}</StackStatus>"
            f"<CreationTime>{_esc(s.get('CreationTime', ''))}</CreationTime>"
            f"<LastUpdatedTime>{_esc(s.get('LastUpdatedTime', ''))}</LastUpdatedTime>"
            f"</member>"
        )
    return _xml("ListStacksResponse",
                f"<ListStacksResult><StackSummaries>{members_xml}</StackSummaries></ListStacksResult>")


def _act_list_stack_resources(params):
    stack_name = _resolve_stack_name(_p(params, "StackName"))
    stack = _stacks.get(stack_name)
    if not stack:
        return _error("ValidationError", f"Stack [{stack_name}] does not exist")

    members_xml = ""
    for lid, res in stack.get("Resources", {}).items():
        members_xml += (
            f"<member>"
            f"<LogicalResourceId>{_esc(res['LogicalResourceId'])}</LogicalResourceId>"
            f"<PhysicalResourceId>{_esc(res.get('PhysicalResourceId', ''))}</PhysicalResourceId>"
            f"<ResourceType>{_esc(res.get('ResourceType', ''))}</ResourceType>"
            f"<ResourceStatus>{_esc(res.get('ResourceStatus', ''))}</ResourceStatus>"
            f"<LastUpdatedTimestamp>{_esc(res.get('Timestamp', ''))}</LastUpdatedTimestamp>"
            f"</member>"
        )
    return _xml("ListStackResourcesResponse",
                f"<ListStackResourcesResult><StackResourceSummaries>{members_xml}</StackResourceSummaries></ListStackResourcesResult>")


def _act_get_template(params):
    stack_name = _resolve_stack_name(_p(params, "StackName"))
    stack = _stacks.get(stack_name)
    if not stack:
        return _error("ValidationError", f"Stack [{stack_name}] does not exist")

    template_body = stack.get("TemplateBody", "{}")
    return _xml("GetTemplateResponse",
                f"<GetTemplateResult><TemplateBody>{_esc(template_body)}</TemplateBody></GetTemplateResult>")


def _act_get_template_summary(params):
    template_body = _p(params, "TemplateBody")
    stack_name = _resolve_stack_name(_p(params, "StackName"))

    if stack_name:
        stack = _stacks.get(stack_name)
        if not stack:
            return _error("ValidationError", f"Stack [{stack_name}] does not exist")
        template = stack.get("Template", {})
    elif template_body:
        try:
            template = _parse_template(template_body)
        except ValueError as e:
            return _error("ValidationError", str(e))
    else:
        return _error("ValidationError", "Either TemplateBody or StackName is required")

    resources = template.get("Resources", {})
    resource_types = sorted(set(r.get("Type", "") for r in resources.values()))

    params_xml = ""
    for pname, pdef in template.get("Parameters", {}).items():
        params_xml += (
            f"<member>"
            f"<ParameterKey>{_esc(pname)}</ParameterKey>"
            f"<DefaultValue>{_esc(str(pdef.get('Default', '')))}</DefaultValue>"
            f"<ParameterType>{_esc(pdef.get('Type', 'String'))}</ParameterType>"
            f"<Description>{_esc(pdef.get('Description', ''))}</Description>"
            f"</member>"
        )

    types_xml = "".join(f"<member>{_esc(t)}</member>" for t in resource_types)

    return _xml("GetTemplateSummaryResponse",
                f"<GetTemplateSummaryResult>"
                f"<Parameters>{params_xml}</Parameters>"
                f"<ResourceTypes>{types_xml}</ResourceTypes>"
                f"<Description>{_esc(template.get('Description', ''))}</Description>"
                f"</GetTemplateSummaryResult>")


def _act_validate_template(params):
    template_body = _p(params, "TemplateBody")
    if not template_body:
        return _error("ValidationError", "TemplateBody is required")

    try:
        template = _parse_template(template_body)
    except ValueError as e:
        return _error("ValidationError", str(e))

    if not isinstance(template, dict):
        return _error("ValidationError", "Template must be a JSON/YAML object")

    params_xml = ""
    for pname, pdef in template.get("Parameters", {}).items():
        params_xml += (
            f"<member>"
            f"<ParameterKey>{_esc(pname)}</ParameterKey>"
            f"<DefaultValue>{_esc(str(pdef.get('Default', '')))}</DefaultValue>"
            f"<Description>{_esc(pdef.get('Description', ''))}</Description>"
            f"</member>"
        )

    return _xml("ValidateTemplateResponse",
                f"<ValidateTemplateResult>"
                f"<Parameters>{params_xml}</Parameters>"
                f"<Description>{_esc(template.get('Description', ''))}</Description>"
                f"</ValidateTemplateResult>")


async def _act_create_change_set(params):
    stack_name = _resolve_stack_name(_p(params, "StackName"))
    change_set_name = _p(params, "ChangeSetName")
    template_body = _p(params, "TemplateBody")
    change_set_type = _p(params, "ChangeSetType") or "UPDATE"

    if not stack_name or not change_set_name:
        return _error("ValidationError", "StackName and ChangeSetName are required")

    if change_set_type == "CREATE" and stack_name not in _stacks:
        # Pre-create the stack entry
        stack_id = f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:stack/{stack_name}/{str(uuid4())}"
        _stacks[stack_name] = {
            "StackName": stack_name,
            "StackId": stack_id,
            "StackStatus": "REVIEW_IN_PROGRESS",
            "StackStatusReason": "",
            "CreationTime": _now(),
            "LastUpdatedTime": _now(),
            "Parameters": [],
            "Tags": [],
            "Outputs": [],
            "Resources": {},
            "Template": {},
            "TemplateBody": "{}",
            "events": [],
            "creation_order": [],
        }

    if template_body:
        try:
            template = _parse_template(template_body)
        except ValueError as e:
            return _error("ValidationError", str(e))
    else:
        template = _stacks.get(stack_name, {}).get("Template", {})

    param_values = _parse_members(params, "Parameters")

    cs_id = f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:changeSet/{change_set_name}/{str(uuid4())}"
    change_set = {
        "ChangeSetId": cs_id,
        "ChangeSetName": change_set_name,
        "StackName": stack_name,
        "StackId": _stacks.get(stack_name, {}).get("StackId", ""),
        "Status": "CREATE_COMPLETE",
        "ExecutionStatus": "AVAILABLE",
        "CreationTime": _now(),
        "ChangeSetType": change_set_type,
        "Template": template,
        "Parameters": param_values,
        "Changes": [],
    }

    # Compute changes
    new_resources = template.get("Resources", {})
    old_resources = _stacks.get(stack_name, {}).get("Template", {}).get("Resources", {})
    for lid, rdef in new_resources.items():
        if lid not in old_resources:
            change_set["Changes"].append({
                "Type": "Resource",
                "ResourceChange": {
                    "Action": "Add",
                    "LogicalResourceId": lid,
                    "ResourceType": rdef.get("Type", ""),
                },
            })
        else:
            change_set["Changes"].append({
                "Type": "Resource",
                "ResourceChange": {
                    "Action": "Modify",
                    "LogicalResourceId": lid,
                    "ResourceType": rdef.get("Type", ""),
                },
            })
    for lid in old_resources:
        if lid not in new_resources:
            change_set["Changes"].append({
                "Type": "Resource",
                "ResourceChange": {
                    "Action": "Remove",
                    "LogicalResourceId": lid,
                    "ResourceType": old_resources[lid].get("Type", ""),
                },
            })

    _change_sets[cs_id] = change_set

    return _xml("CreateChangeSetResponse",
                f"<CreateChangeSetResult>"
                f"<Id>{_esc(cs_id)}</Id>"
                f"<StackId>{_esc(change_set['StackId'])}</StackId>"
                f"</CreateChangeSetResult>")


def _act_describe_change_set(params):
    cs_name = _p(params, "ChangeSetName")
    stack_name = _resolve_stack_name(_p(params, "StackName"))

    # Find the change set
    cs = None
    for c in _change_sets.values():
        if c["ChangeSetName"] == cs_name or c["ChangeSetId"] == cs_name:
            if not stack_name or c["StackName"] == stack_name:
                cs = c
                break

    if not cs:
        return _error("ChangeSetNotFoundException", f"ChangeSet [{cs_name}] does not exist")

    changes_xml = ""
    for ch in cs.get("Changes", []):
        rc = ch.get("ResourceChange", {})
        changes_xml += (
            f"<member><Type>{_esc(ch.get('Type', ''))}</Type>"
            f"<ResourceChange>"
            f"<Action>{_esc(rc.get('Action', ''))}</Action>"
            f"<LogicalResourceId>{_esc(rc.get('LogicalResourceId', ''))}</LogicalResourceId>"
            f"<ResourceType>{_esc(rc.get('ResourceType', ''))}</ResourceType>"
            f"</ResourceChange></member>"
        )

    params_xml = ""
    for pk, pv in cs.get("Parameters", {}).items():
        params_xml += (
            f"<member>"
            f"<ParameterKey>{_esc(pk)}</ParameterKey>"
            f"<ParameterValue>{_esc(pv)}</ParameterValue>"
            f"</member>"
        )

    return _xml("DescribeChangeSetResponse",
                f"<DescribeChangeSetResult>"
                f"<ChangeSetId>{_esc(cs['ChangeSetId'])}</ChangeSetId>"
                f"<ChangeSetName>{_esc(cs['ChangeSetName'])}</ChangeSetName>"
                f"<StackName>{_esc(cs['StackName'])}</StackName>"
                f"<StackId>{_esc(cs.get('StackId', ''))}</StackId>"
                f"<Status>{_esc(cs['Status'])}</Status>"
                f"<ExecutionStatus>{_esc(cs['ExecutionStatus'])}</ExecutionStatus>"
                f"<CreationTime>{_esc(cs['CreationTime'])}</CreationTime>"
                f"<Changes>{changes_xml}</Changes>"
                f"<Parameters>{params_xml}</Parameters>"
                f"</DescribeChangeSetResult>")


async def _act_execute_change_set(params):
    cs_name = _p(params, "ChangeSetName")
    stack_name = _resolve_stack_name(_p(params, "StackName"))

    cs = None
    for c in _change_sets.values():
        if c["ChangeSetName"] == cs_name or c["ChangeSetId"] == cs_name:
            if not stack_name or c["StackName"] == stack_name:
                cs = c
                break

    if not cs:
        return _error("ChangeSetNotFoundException", f"ChangeSet [{cs_name}] does not exist")

    if cs["ExecutionStatus"] != "AVAILABLE":
        return _error("InvalidChangeSetStatus",
                       f"ChangeSet [{cs_name}] is not in AVAILABLE status")

    cs["ExecutionStatus"] = "EXECUTE_IN_PROGRESS"

    sn = cs["StackName"]
    template = cs["Template"]
    param_values = cs["Parameters"]

    if cs["ChangeSetType"] == "CREATE":
        await _do_create_stack(sn, template, param_values, [])
    else:
        await _do_update_stack(sn, template, param_values)

    cs["ExecutionStatus"] = "EXECUTE_COMPLETE"

    return _xml("ExecuteChangeSetResponse", "<ExecuteChangeSetResult/>")


def _act_delete_change_set(params):
    cs_name = _p(params, "ChangeSetName")
    stack_name = _resolve_stack_name(_p(params, "StackName"))

    to_delete = None
    for cs_id, cs in _change_sets.items():
        if cs["ChangeSetName"] == cs_name or cs["ChangeSetId"] == cs_name:
            if not stack_name or cs["StackName"] == stack_name:
                to_delete = cs_id
                break

    if to_delete:
        del _change_sets[to_delete]

    return _xml("DeleteChangeSetResponse", "<DeleteChangeSetResult/>")


def _act_list_change_sets(params):
    stack_name = _resolve_stack_name(_p(params, "StackName"))
    if not stack_name:
        return _error("ValidationError", "StackName is required")

    members_xml = ""
    for cs in _change_sets.values():
        if cs["StackName"] == stack_name:
            members_xml += (
                f"<member>"
                f"<ChangeSetId>{_esc(cs['ChangeSetId'])}</ChangeSetId>"
                f"<ChangeSetName>{_esc(cs['ChangeSetName'])}</ChangeSetName>"
                f"<StackName>{_esc(cs['StackName'])}</StackName>"
                f"<Status>{_esc(cs['Status'])}</Status>"
                f"<ExecutionStatus>{_esc(cs['ExecutionStatus'])}</ExecutionStatus>"
                f"<CreationTime>{_esc(cs['CreationTime'])}</CreationTime>"
                f"</member>"
            )
    return _xml("ListChangeSetsResponse",
                f"<ListChangeSetsResult><Summaries>{members_xml}</Summaries></ListChangeSetsResult>")


# ── Action dispatch table ─────────────────────────────────────

_ASYNC_HANDLERS = {
    "CreateStack": _act_create_stack,
    "DeleteStack": _act_delete_stack,
    "UpdateStack": _act_update_stack,
    "CreateChangeSet": _act_create_change_set,
    "ExecuteChangeSet": _act_execute_change_set,
}

_SYNC_HANDLERS = {
    "DescribeStacks": _act_describe_stacks,
    "DescribeStackResource": _act_describe_stack_resource,
    "DescribeStackResources": _act_describe_stack_resources,
    "DescribeStackEvents": _act_describe_stack_events,
    "ListStacks": _act_list_stacks,
    "ListStackResources": _act_list_stack_resources,
    "GetTemplate": _act_get_template,
    "GetTemplateSummary": _act_get_template_summary,
    "ValidateTemplate": _act_validate_template,
    "DescribeChangeSet": _act_describe_change_set,
    "DeleteChangeSet": _act_delete_change_set,
    "ListChangeSets": _act_list_change_sets,
}


# ── Entry Point ───────────────────────────────────────────────


async def handle_request(method: str, path: str, headers: dict,
                         body: bytes, query_params: dict) -> tuple:
    """Handle CloudFormation requests — Action-based query protocol."""
    params = dict(query_params)
    if method == "POST" and body:
        for k, v in parse_qs(body.decode("utf-8", errors="replace")).items():
            params[k] = v

    action = _p(params, "Action")
    if not action:
        return _error("MissingAction", "Missing Action parameter")

    if action in _ASYNC_HANDLERS:
        return await _ASYNC_HANDLERS[action](params)
    if action in _SYNC_HANDLERS:
        return _SYNC_HANDLERS[action](params)

    return _error("InvalidAction", f"Unknown action: {action}")


# ── Reset ─────────────────────────────────────────────────────


def reset():
    _stacks.clear()
    _change_sets.clear()
