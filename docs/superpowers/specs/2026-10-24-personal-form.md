# Personal booking form

The administrator sends one blank file to the group. Each user fills only their own information and returns the file privately. Root imports that file, reviews the account and booking, then confirms once.

- Replace the roster template with one vertical personal form: field, editable value, short guidance. Preserve all eleven existing booking fields and the scheduled/today choice.
- Every new upload contains exactly one applicant. Reject a multi-person file before school authentication or account writes. Keep single-row CSV/older single-applicant files readable and persisted import history accessible.
- Keep automatic validation, verified new account storage, existing-account protection until confirmation, expiry, duplicate protection and the existing booking executor unchanged.
- Root default UI says one person per file and shows a single booking card. No row numbers, selection checkboxes, or extra confirmation for the personal flow.
- Verify the exported XLSX by reading the real template and filling synthetic text cells, including leading-zero student IDs and password whitespace. Cover scheduled/today, invalid input, formulas, oversized/sparse/duplicate data and multi-person rejection. Run frontend/backend suites, H5 build and isolated real-browser import/confirmation.
- Preserve production plans, bookings and service state. No real school request, commit, merge, dependency installation or deployment.

Sequence: write regression tests, implement bounded personal-form parsing, export and inspect the template using the spreadsheet skill, simplify the import card, run full regression and browser checks, review the increment, preserve evidence, and clean task-owned disposable outputs.
