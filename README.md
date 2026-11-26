# Philly Job Radar

A local, single-user full-stack app for finding jobs and tracking applications. Python serves the dashboard and queries job feeds. SQLite stores listings, named searches, hidden jobs, notes and application history. Applications are submitted on the employer or provider site.

## Start

Requires Python 3.10+ and outbound HTTPS. There are no packages to install.

```sh
python radar.py
```

Open http://127.0.0.1:8000 and select **Search live jobs**. On Windows, use `py radar.py` if Python is installed through the Windows launcher. The first refresh may take several seconds. Remove filters to inspect more of the live index. Separate phrases by commas and locations by semicolons (for example, `Philadelphia, PA; Wilmington, DE`).

The server creates `radar.sqlite3` alongside the code. Use `RADAR_DB` to choose a different persistent SQLite path and `PORT` to change the port. The app binds to loopback by default. For a phone on the same trusted network, set `RADAR_HOST=0.0.0.0` and open the computer's LAN address and port on the phone. This single-user server has no authentication.

## Sources

- Key-free public ATS feeds ship for Stripe, Discord, Perpay and HealthVerity (Greenhouse), Ramp (Ashby), and Palantir and Gopuff (Lever).
- Use **Manage sources** to add a public Greenhouse, Ashby or Lever board URL. Custom careers sites and other ATS systems need their own adapters.
- Set both `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` before starting the server to enable Adzuna's US search API. Results with an Adzuna redirect use the provider link.
- Set `BRAVE_SEARCH_API_KEY` before starting the server to enable targeted Brave searches. The integration uses supported ATS hosts and verifies discovered URLs against the live job feed. It does not convert snippets into job records.
- Settings are process environment variables and never sent to the browser. On PowerShell, for example, use `$env:BRAVE_SEARCH_API_KEY = "your-key"` before `py radar.py`.

## Freshness and coverage

Ashby provides a published timestamp. Adzuna provides the ad creation timestamp, labeled provider-reported. Greenhouse edit timestamps do not establish when a listing was originally posted. Lever entries without public posting dates appear with their discovery dates. An employer listing missing from a later board response is marked Unconfirmed. Automatic closure confirmation and repost indicators are not implemented.

Radius sorting and filtering use a bundled Philadelphia-area city and ZIP lookup plus coordinates supplied by Adzuna. Other unknown places can be matched by their text labels. The app does not have a nationwide geocoding service. Experience, remote restrictions and graduation windows use structured fields where available and conservative text patterns otherwise. Ambiguous details need to be checked on the original listing. Minimum salary uses identified annual USD amounts. Deduplication requires a shared canonical URL or a requisition ID and employer name. Provider links that conceal both can remain separate.

## Verification

```sh
python -m unittest -v
```

The automated suite covers combined filters, date precision, Pennsylvania eligibility, required versus preferred experience, employer-link deduplication, partial failures and SQLite persistence via HTTP. The first live search returned posts from Stripe, Discord, Ramp and Palantir. Original Stripe and Discord listing URLs returned HTTP 200. Ramp and Palantir returned HTTP 403 on the sampled listing pages from this environment, although their feed requests succeeded. Adzuna and Brave require credentials for live verification.

A subsequent unrestricted Chromium desktop search retrieved 2,067 distinct postings across all seven bundled boards. Chromium at desktop size and with iPhone 13 emulation completed the search, details, save, and status update flow. The saved status survived reload. The phone check used browser emulation. The combined filters and remote eligibility checks use controlled fixtures because live listings can change.
