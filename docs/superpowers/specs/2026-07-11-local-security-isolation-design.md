?# Local Security Isolation Design

## Decision

Implement authorization isolation locally before making any Alibaba Cloud deployment change.

Ordinary users may save and execute their own custom Python strategies. Their custom strategies, tasks, logs, and results must be private to their own account. Factor mining and every factor-related management, execution, task, progress, and result operation are restricted to the root administrator.

The Alibaba Cloud host had about 583 MiB available memory during the July 11, 2026 review. Do not enable a persistent Docker strategy Worker there until capacity is upgraded or a separate worker host is provisioned.

## Product Rules

### Ordinary Users

- May create, save, modify, and execute their own custom strategies.
- May view only their own strategy data, asynchronous task status, results, and associated logs.
- Must not access factor mining, the factor zoo, Windows GUI trading diagnostics, another user's signals, or another user's tasks.

### Root Administrator

- Has access to factor management and factor-mining operations.
- Has access to globally sourced Windows GUI trading diagnostics and historical trading screenshots.
- Is still subject to explicit object ownership checks when an endpoint is designed around a user-owned numeric ID.

## Initial Authorization Model

- User-owned persistent and in-memory resources carry an owner identifier such as `user_id` or `owner_user_id`.
- Globally sensitive operations use the existing root-administrator authorization decorator.
- Asynchronous strategy, backtest, and AI records include an owner identifier plus creation and expiry metadata where applicable.
- Newly created asynchronous task IDs use a full UUID rather than a truncated identifier.
- Unknown and unauthorized user-owned resource IDs return the same not-found response, preventing resource enumeration.

## Strategy Execution Boundary

- Preserve ordinary-user custom Python strategy execution, subject to the repository's existing execution controls.
- Do not deploy a multi-user production Worker on the current cloud host.
- A future Worker receives a narrow, per-job payload and never receives the Flask session secret, user API keys, or webhooks.
- A future Worker must run as a non-root account with CPU, memory, process-count, wall-time, network, and filesystem limits.

## Immediate Local Scope

- Make all factor and factor-zoo endpoints root-admin-only.
- Enforce ownership for strategy asynchronous task progress and results.
- Enforce signal ownership before a bridge report changes state.
- Restrict global Windows-order data and historical screenshots to the root administrator until ownership metadata exists.
- Enforce ownership for all-market backtest and asynchronous AI task reads, results, and cancellation.

## Separate Security Workstreams

- Unified server-side trading hard-risk controls that cannot be bypassed by a client request.
- Encryption and redaction of secrets, session revocation, CSRF protection, SSRF restrictions, and endpoint ACL review.
- Windows file ACLs limited to the service identity, Administrators, and SYSTEM.
- Removal of all secrets from source control and protection of `.env` files.
- Structured audit logging for sensitive access and execution decisions.
- Worker deployment only after a new capacity review of an upgraded or separate host.
