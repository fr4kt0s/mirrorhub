# Generic worker and SyncPlan refactor

## Goal

Introduce a single, reusable sync worker and method-specific adapters. This removes duplicated orchestration logic and prepares mirrorHub for parallel repository syncing.

## Core model

- `SyncPlan` describes one repository sync: method, source, target, options, credentials reference, and schedule.
- A generic worker accepts a `SyncPlan` and executes it through the matching adapter.
- Each adapter owns only method-specific command construction, environment, and output interpretation.

## Initial adapters

- `ftpsync`
- `rsync`
- `debmirror`
- `wget`

## Configuration

Keep repository configuration in the existing YAML files. The adapter receives a normalized `SyncPlan`; it does not read configuration directly.

## Execution model

- One generic worker executes one repository at a time.
- Repository-level locking prevents concurrent syncs of the same target.
- Later, multiple worker processes can run different repositories in parallel.

## First implementation step

1. Add the `SyncPlan` model.
2. Add the adapter interface and `FtpsyncAdapter`.
3. Route ftpsync jobs through the generic worker.
4. Keep the current UI and job model unchanged.
