# Code platforms

[简体中文](PLATFORMS.zh-CN.md)

Repository maintenance uses platform adapters for external data. Service work, triage, authority, conversations, reports and native execution use common contracts. GitHub is the currently implemented provider.

| Capability | GitHub | Other providers |
| --- | --- | --- |
| Repository issues, change requests and comments | Implemented | Adapter required |
| Known change-request comments, reviews and checks | Implemented | Adapter required |
| Publish branch, create change request, comment or merge | Planned in P3 | Same delivery contract |

Unsupported providers or capabilities are rejected during host configuration. Selecting a different endpoint does not translate one platform's API into another. A future provider registers its project validator and implemented adapters in the host catalog.

## Repository source

Use an explicit platform declaration in the external ServiceSpec:

```json
{
  "name": "project",
  "provider": "github",
  "instance": "primary",
  "project": "OWNER/PROJECT",
  "api_url_env": "PROJECT_API_ENDPOINT",
  "token_env": "PROJECT_READ_TOKEN",
  "poll_seconds": 43200,
  "max_pages": 5
}
```

`instance` is a stable host alias for the platform installation. `project` is validated by the selected adapter; GitHub uses owner/project. Future adapters can accept their own project IDs or nested namespaces. Connection values remain in external host files. Sources using the same provider/instance must identify the same host connection.

For an instance using a private certificate authority, add `ca_file_env` referencing an environment variable whose value is an absolute CA-file path outside the mutable workspaces. The certificate is loaded explicitly and participates in the connection binding. HTTPS certificate verification remains enabled; ordinary public endpoints use default certificate trust.

## Run feedback

The external Run control configuration uses a `change_request` declaration:

```json
{
  "version": 1,
  "change_request": {
    "provider": "github",
    "instance": "primary",
    "project": "OWNER/PROJECT",
    "number": 1,
    "actor_ids": [7],
    "app_ids": [11],
    "api_url_env": "PROJECT_API_ENDPOINT",
    "token_env": "PROJECT_READ_TOKEN",
    "poll_seconds": 30
  }
}
```

Actor and application IDs select admitted feedback under host policy. They grant no execution permissions. The native controller checks local/current head, object versions, inbox capacity and stable event identity before committing feedback. Notifications retain their separate host declaration.

## Identity and state

An object reference contains provider, instance, project, kind and an opaque stable ID; an optional number is for display. IDs are unique within this namespace rather than assumed globally unique. GitHub uses stable project numbers for issue/change-request subjects and service object IDs for comments, reviews and checks.

Normalized source events identify both their object and subject. An issue or change-request update targets itself; comments identify their parent subject. Work links use the complete subject reference, so different projects, instances, platforms, and issue/change-request namespaces remain distinct. Duplicate feeds can share the same work and event while keeping independent collection positions.

Adapters return bounded `SourcePage` or `ChangeFeedback` data. They handle platform paths, fields, authentication and pagination. Service and Run controllers own durable acceptance, cursor commits, permissions and recovery. Repository cursor positions are opaque to the Service and bound to their adapter/connection identity. Changed connections or projects cannot reuse another source's cursor.

## Extension boundary

`platforms/contracts.py` defines common data and adapter protocols. `platforms/registry.py` contains the host catalog; `platforms/http.py` provides bounded transport. `platforms/github/` implements GitHub translation. Services and Runs consume normalized output.

A new provider implements repository polling and/or change-request feedback, declares those capabilities, and validates its project selector. Delivery will use fixed actions such as publish_branch, ensure_change_request, post_comment and merge_change_request with the same object references; approval, receipts and uncertain-result recovery belong to the delivery runtime.

Maintenance skills describe requirement assessment and planning. They do not select endpoints, parse platform APIs or authorize writes. GitLab and other adapters are not included in the current implementation.
