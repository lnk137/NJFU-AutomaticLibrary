# Responsive frontend integration

Authorization: user requests careful audit of the other chat's design, fixes as needed, then production integration for student/root on desktop/iPad/phone preserving all current behavior. The provided design is the visual authority; implementation and idle deployment authorized now, no repeat approval gate.

## Goals and design
Use AL-FrontEnd/design-preview only as visual/interaction reference: NJFU-AUTO-LIB wordmark, muted forest-green/white tokens, restrained spacing, thin separators, compact row editor, desktop summary sidebar, phone bottom navigation/actions and responsive drawers. Preserve original prototype and its evidence. Do not ship its fake runtime, dates, seats, users, metrics, role URL switch, state-review tools or unsafe-eval CSP.

Retain Uni-app/Vue/Pinia and existing production API/service modules. Keep current URLs valid. Build shared AppShell, icons and AppDialog; migrate presentation around existing controllers. Student: login, overview/detail, full plan editor/day/week/custom/Friday/live preview/seat priority, same-day preflight+expiring explicit confirmation, school snapshot/filter/query/reconcile, dedicated profile with alias/password/auth-check/logout/help. Root: full dashboard and3 charts/date/type/count/details, account inspection/context, allowlist/search/notes/disable/re-enable/audit, administrator status. No fake success and no losing error/loading/stale/conflict/draft/permission states.

## Contracts
- AppShell props active (overview/plan/records/profile/admin), beforeLeave async predicate; role from useWorkspace only; handles desktop/top and phone/bottom navigation; root uses real admin routes; slot content. SessionBar keeps existing logout/auth lifecycle.
- AppIcon props name/size, only locally bundled licensed Feather SVG assets.
- AppDialog props open/title/busy, emits close; footer slot; desktop side drawer, phone bottom sheet; semantic dialog/focus containment/Escape/restore; caller owns dirty confirmation and processing guards.
- Existing account_info controller remains real draft/API owner, moved presentation and new view components only. All seat IDs come from store.seats; dates/policy from backend/drafts.mjs; current revisions/session generation used on every existing action.
- Root dashboard keeps backend build_dashboard JSON and single-flight controller; no recomputed demo metrics. Keep missing timing/missing first attempt, release/use day distinction, denominator, retry semantics and cap50.

## Validation
Fresh reference screenshots, scoped TDD + real SFC renders and pure helper tests; existing full229 backend and39+ frontend regressions; isolated candidate H5 build. Fake-school/temp-Mongo production app on localhost test-only host, synthetic credentials, no external network, complete student/root journeys including manual preflight/confirm only against fake school, draft/conflict/stale/session isolation. Desktop1440, iPad834x1194/1194x834, phone390x844/min320: visual/functional/44px controls/16px inputs/focus/safe-area/reduced-motion and resize draft retention. Compare new screenshots against provided reference. Fresh independent final source review before deployment.

## Activation and invariants
Prefer frontend-only atomic directory swap at idle; do not restart/change backend/scheduler50, do not change Mongo/accounts/plans/access/history or credentials/config. Retain prior H5 and scoped source baseline. Stage previous immutable assets for already-open tabs. Check source and protected DB hashes, backend PID/status/next9 plans before/after, local root real read-only UI and public anonymous student login/static-assets/denial. No real school requests/bookings/cancellations/late replay. No dependency install/commit, no whole checkout copies.
