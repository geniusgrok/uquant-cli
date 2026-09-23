# Uquant scheduled runner

This public repository contains only the bootstrap for the private production source.
The existing schedule is weekdays at 09:01 UTC (17:01 Asia/Shanghai). It calls the
current private `ychenracing/uquant` main implementation in `scripts.daily_scan`.
That implementation checks the trading calendar and completed close, refreshes
live inputs, runs the production decision once, compares the previous trading day,
and persists report/account/input originals in the private report branch.

## Confidentiality and activation

`UQUANT_READ_TOKEN` is used only for private source checkout. It is never a writer.
The public runner also requires a separately authorized, already configured
`UQUANT_REPORT_WRITE_TOKEN`, limited to the private report destination. Adding this
workflow does not create that Secret, obtain its value, or grant permissions.
Without a private writer it fails visibly before production execution; green
checkout or a historic smoke run is not a successful daily report.

No private source, full report, account, private log, or private cache is uploaded
as a public Artifact or job summary. Only allowlisted status text is printed.
Private results and run logs are written to `ychenracing/uquant` branch
`uquant-daily-reports`, with `latest.json`, `reports/YYYY-MM-DD/report.md`, structured
results and per-run log manifests. Publishing uses a non-force Git update and
actual remote byte, SHA-256 and Git blob readback.

A persistent date claim is written before the engine call. In-flight/uncertain
claims block replay; concurrency alone is not the idempotency mechanism. Inspect
private claim/result records before retrying unknown runs. Missing or revised
historical inputs and missing account continuity fail closed, not by resetting
the observer. This is a no-execution observation account, not the user's brokerage
account; no fills or user holdings are invented. See the private repository's
`docs/DAILY_SCAN.md` for exact fields, recovery and validation limits.
