# Worktree UI alignment

## Goal

Sync the main checkout's concise UI into the isolated feature worktree, then apply the same information hierarchy to date exceptions, personal seat preferences, root seat conflict notices, and form imports.

## Requirements and design

- Default view shows status, key numbers, and the next action. Help, rules, versions, and routine details expand on demand; failures and unresolved conditions stay visible.
- Preserve the green brand, existing responsive layout, account isolation, dirty-editor guards, truthful receipt states, and all four feature behaviors.
- Merge overlapping pages against the shared pre-feature source. Retain date-aware plan previews, personal seat controls, collision notices, and form import integration.
- Import only mainline UI changes. Independent macOS power APIs, helper installation, and background execution are outside this task.
- Form imports retain one whole-file confirmation; show the action/time/seats needed for review and the effect of confirmation. No per-row checkboxes.
- No backend changes, real school requests, production writes, restart, deploy, commit, or merge.

## Validation

Test empty receipts, visible failures versus collapsed help, full form review and confirmation, collision scope, and editor guards. Run frontend tests and H5 build; verify desktop, iPad, and phone layouts against isolated synthetic data. Run backend regression tests for existing reservations/plans. Review the source diff and obtain one independent read-only review.

## Sequence

1. Freeze mainline UI inputs and worktree source identity.
2. Synchronize shared UI and receipt tests; preserve feature integrations.
3. Add targeted rendering tests, then simplify the four new feature interfaces.
4. Validate browser layouts, run regression checks, review, and save source/evidence identities.
5. Save the UI principles in project documentation and an authorized memory note. Clean only this task's disposable outputs.

No open product decisions; the approved feature scope remains unchanged.
