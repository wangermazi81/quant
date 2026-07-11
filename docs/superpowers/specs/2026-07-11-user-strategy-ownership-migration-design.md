# User Strategy Ownership Migration Design

## Status

Approved design for local implementation. This document covers local source changes and a future explicit local data-migration command only. It does not authorize Alibaba Cloud deployment, production database changes, secret changes, or execution of a migration against user data.

## Problem

The current strategy manager stores all custom Python files directly under one global `strategies/` directory. Strategy API routes resolve resources by filename without a persistent owner boundary. A logged-in user who knows or guesses another filename can therefore read, overwrite, delete, rename, activate, or run another user's custom strategy.

The system must preserve ordinary users' ability to create and execute their own custom Python strategies while ensuring that custom strategies, strategy tasks, logs, and results are private to their owner. Factor mining remains root-administrator-only.

## Decision

Use physical per-user strategy directories for all new custom strategies. Preserve built-in strategies as a separate global class. Quarantine every legacy non-built-in strategy whose owner cannot be proven from existing data.

This design intentionally does not infer historical ownership from filenames, display names, session logs, modification times, or source comments. Such inference could disclose a strategy to the wrong account.

## Alternatives Considered

### 1. Per-user directories with legacy quarantine (chosen)

- Built-ins remain in the existing global strategy directory and retain their `builtin: true` metadata.
- New custom files live at `strategies/users/<user_id>/<filename>.py`.
- Legacy root-level files marked `builtin: false` move to a root-only quarantine directory and stay unavailable to normal strategy routes until explicitly assigned.

This gives every non-built-in path an unambiguous owner at the filesystem boundary, keeps code changes focused, and permits a reversible, auditable migration.

### 2. Global files plus an owner field in the META comment

This needs less path refactoring but leaves all tenant data intermingled and makes a missing, malformed, or edited metadata line an authorization ambiguity. It also cannot safely establish owners for existing files.

### 3. Database-backed strategy source records

This is attractive for long-term querying and audit history, but it adds database schema work, source-data migration, backup/restore concerns, and a substantially wider deployment risk. It is not needed to establish a correct ownership boundary now.

## Storage Model

### Built-in strategies

- Existing root-level `strategies/<filename>.py` files whose metadata says `builtin: true` remain built-ins.
- Users may list and run built-ins according to the existing product rules.
- Source visibility for built-ins remains governed by the existing protected-source rules.

### User-owned custom strategies

- The canonical path is `strategies/users/<user_id>/<filename>.py`.
- `user_id` comes only from the authenticated server-side session. It is never accepted from a request body, query parameter, task payload, or filename.
- `<filename>` continues to be normalized by the existing safe-name rule; all strategy-manager path construction remains internal.
- Saving a custom strategy always writes `builtin: false`, regardless of client-supplied META text.
- A list for a normal user contains only built-ins plus files in that user's directory. Read, save, delete, rename, activate, and run use the same owner-aware resolver.
- A root administrator does not receive a bypass for another user's owned custom strategy. A future support/export workflow requires a separate design, explicit audit record, and user-facing policy.

### Legacy unassigned custom strategies

- The migration destination is `strategies/legacy_unassigned/<filename>.py`.
- Each moved file is recorded in a migration manifest with its original path, destination path, SHA-256 hash, timestamp, and migration version.
- Quarantined strategies are hidden from all normal user strategy routes and cannot be run, activated, read, edited, renamed, or deleted through those routes.
- Root-only migration management may list quarantined metadata and explicitly assign one file to a chosen existing user. Assignment moves the file into that user's directory, writes the manifest event, and then makes it available only to that user.
- Assignment is an explicit administrative data operation. It must not expose the source to unrelated users and must fail without changing data if the target user does not exist or the destination filename is occupied.

## API and Execution Rules

### Owner-aware strategy manager

The strategy manager will expose owner-aware operations rather than returning a global custom file by filename:

- List strategy summaries for a caller context.
- Resolve a built-in by name or a custom strategy by `(user_id, filename)`.
- Save, delete, rename, and read only a caller's custom strategy.
- Run a selected built-in or the caller's custom strategy.

Route handlers remain responsible for authentication and pass the session `user_id` to the manager. The manager remains responsible for trusted path selection and never accepts an arbitrary filesystem path.

### Activation and asynchronous work

- The active strategy stored in user state is interpreted in that user's context. A filename is never resolved later against the global custom directory.
- A strategy async task stores `uid`, selected strategy identity, and its own progress snapshot. Its worker resolves a custom strategy using the captured owner ID.
- Progress, result, error, cancellation, and any task-derived log retrieval require the same `uid`. Missing and foreign task IDs keep the same not-found response.
- Per-task progress replaces exposure of a mutable global scan-progress object, preventing concurrent users from seeing each other's scan details.

### Responses and enumeration resistance

- For owner-owned custom resources, a missing file and a file owned by someone else produce the same `404` response: `{"ok": false, "error": "not found"}`.
- Client requests cannot select `legacy_unassigned` or another user's directory through any filename spelling, separator, path traversal sequence, or request field.
- Server logs retain minimal operational information and do not write strategy source code or sensitive client input.

## Migration Procedure

Migration is explicit and never runs implicitly on service startup.

1. Run a dry-run command that inventories root-level strategy files, validates META parsing, classifies only `builtin: true` files as built-ins, and reports all remaining files as unassigned.
2. Review the dry-run report locally. Stop if duplicate names, unreadable files, malformed metadata, or unexpected classifications need an operator decision.
3. Run the apply command while the service is stopped or strategy mutations are disabled. For each eligible file, revalidate its hash, create the quarantine directory, move it atomically where supported, and append a manifest record only after the move succeeds.
4. Start the service and verify that normal accounts see only built-ins plus their own directory contents; no account sees quarantined files.
5. Root explicitly assigns quarantined files one at a time through a separate root-only operation after identifying the rightful owner outside this system's guesswork.

No file is deleted, silently reassigned, or copied into multiple user directories. A manifest-backed rollback command may move only files that still match their recorded hash back to their original legacy location; it must refuse mismatched files and leave them untouched for manual review.

## Related Authorization Remediation

The same implementation phase closes these confirmed issues:

- Change every admin factor-mining and factor-watchlist endpoint from `strategy_manage` permission to `require_root_admin`.
- Change Vibe Alpha factor activation/list/deactivation routes to `require_root_admin`.
- Return the identical not-found result for missing and foreign bridge signal status IDs.
- Replace truncated factor-zoo asynchronous IDs with full `uuid4().hex` values.

These changes are scoped to authorization and identifier isolation. They do not change factor algorithms, market data, strategy semantics, credentials, or cloud infrastructure.

## Error Handling and Audit Expectations

- Unexpected filesystem failures return a generic operation error without exposing absolute paths or source content.
- A failed migration or assignment leaves the source file where it was and creates no successful manifest entry.
- Root-only legacy assignment operations create structured audit events containing actor user ID, target user ID, strategy filename, operation, and timestamp, but no source code.
- Existing application logs must not include custom source text, session secrets, API keys, or full request payloads for these operations.

## Tests and Acceptance Criteria

Automated tests must prove at least the following:

- User A can save, list, read, rename, delete, activate, and run User A's custom strategy.
- User B cannot list, read, save over, rename, delete, activate, or run that strategy, and receives the same not-found response as for an unknown strategy.
- Built-in behavior and protected-source behavior remain unchanged.
- A custom strategy task stores the owner and exposes only its own progress, result, and error to that owner.
- A concurrent User B scan cannot reveal its progress through User A's task endpoint.
- Legacy unassigned strategies are invisible and unusable by normal accounts, and only an explicit root assignment makes one visible to its selected owner.
- Non-root administrators are denied every factor-mining, factor-watchlist, and Vibe Alpha factor route with `403 root_admin_required`.
- Missing and foreign bridge signal status IDs return identical not-found responses.
- Factor-zoo asynchronous task IDs are full UUID hex values.
- The focused authorization suite and the repository's relevant strategy tests pass under the supported Python interpreter.

## Non-Goals

- No Alibaba Cloud deployment or worker activation.
- No access to production data, secrets, `.env` files, credentials, API keys, or financial-account information.
- No automatic ownership inference for historical strategies.
- No new role system and no root override of an already user-owned custom strategy.
- No database schema migration in this phase.
