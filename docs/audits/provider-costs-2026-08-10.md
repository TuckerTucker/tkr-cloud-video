# Object storage pricing inputs — 2026-08-10

The executable model in `operations/cost_model.py` records decimal units and
source dates. Backblaze B2 pay-as-you-go is $6.95/TB-month, includes the first
10 GB of storage, free egress up to three times average monthly storage, then
$0.01/GB; A/B/C transactions are free and Class D is excluded from the measured
worker workload. Cloudflare R2 Standard is $0.015/GB-month, $4.50/million Class
A, $0.36/million Class B, and no egress charge, with its documented monthly free
tier. Rates must be revalidated when the source date, region, or workload changes.

Sources: Backblaze B2 pricing and Cloudflare R2 pricing official pages.
