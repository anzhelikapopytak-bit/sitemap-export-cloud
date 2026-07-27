# Cloud Sitemap Export

Runs completely on a GitHub-hosted Ubuntu runner.

## Automatic schedule

Every day at 03:17 UTC.

## Input

Edit `sitemaps.txt`, one root sitemap URL per line.

## Output

Each workflow run uploads an artifact named:

`sitemap-results-<run number>`

Artifacts are retained for 7 days to reduce free storage usage.

The artifact contains:

- `summary.csv`
- one CSV per root sitemap
- `_debug/` responses for failed sitemap requests

## Cloudflare limitation

The script tries:

1. requests
2. Chromium through Playwright
3. waiting for an automatic browser challenge
4. repeated navigation in the same browser context

It does not solve interactive CAPTCHA or require manual interaction.
