# Responsive frontend integration plan

Spec: ../specs/2026-10-24-responsive-frontend-integration.md
Evidence: development/work/njfu-responsive-integration-20261024
Execution: inline as authorized; executing-plans workflow; final independent read-only reviewer. No commits, worktree copying or dependency install; existing user dirty files retained by scoped baseline and candidate-only build.

1. Audit reference and production parity; shared tokens/AppShell/AppIcon/AppDialog/navigation.
2. Student login/overview/real plan editor and same-day flow/school records/profile; retain API/store/draft state contracts, Friday bound and explicit confirmations.
3. Root dashboard/charts/allowlist/audit/account context and responsive presentation; keep all real statistics/security/stale/session handling.
4. Focused/red-green/render tests + full suites and candidate build; fake-school browser journeys, desktop/iPad/phone/resize/accessibility/negative cases; source comparison screenshots.
5. Independent source/spec review, resolve blockers and important findings; frontend-only guarded atomic deployment with old asset compatibility; live read-only and public boundary checks.
6. Report, preserve rollback and acceptance evidence; stop/clean only owned fixtures, reset viewport/close temporary tabs, measure disk.

Preflight shared interfaces: Task1 produces shell/dialog/icon/tokens used by2/3; Task2 owns student pages/profile; Task3 owns admin files, never services/store; Task4 may require scoped behavior repairs to either with explicit ledger entry. Keep prop/event contracts stable before Tasks2/3. Backend/production state untouched in all tasks.
