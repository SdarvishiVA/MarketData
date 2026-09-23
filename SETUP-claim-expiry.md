# Claim expiry: what is done and what is left

A permit lead claimed in the Hub writes a row into that broker's Lead Tracker
- now a SharePoint workbook, not Box - with Source of Lead set to "Permit
tool". If nobody works it within 14 days, `expire_claims.py` strikes the row
out in red, appends a released note, and puts the lead back in the pool.

It reads and writes through Graph's plain file-content endpoint (download the
whole workbook, edit it, re-upload it as a new version) rather than Graph's
Excel "workbook" API. That's deliberate: the workbook/range/format endpoints
only accept a signed-in user, never an unattended app, and have no
strikethrough property regardless - confirmed against Microsoft's own Graph
docs, not assumed. Same whole-file approach as Box, same reason 3am matters:
nobody should have the file open when it runs.

It runs at 07:00 UTC, which is 3am Montreal, so it never collides with a
broker who has the file open. It is in dry run until you turn it off.

## Done

The script, the workflow, `schema.py`, and `trackers.json` with the six
migrated brokers' SharePoint item ids already filled in (Marc, Raph, Kevin,
Odi, Suzana, Hussein - the same driveId for all of them, from
`hub-sharepoint-handoff.md`). Yousif and Hung are left out of the registry
entirely for now: Yousif's file isn't live yet, and Hung's is still on the
old schema pending the phantom-rows decision. Add them once each is actually
on SharePoint - copy the block for any existing broker and swap the item id.

Fields are resolved by header NAME through `schema.py`, the same resolver the
dashboard aggregator uses, so this handles both the new template (banner on
row 1, headers on row 2) and any tracker still on the old layout, such as
Hung's. It is tested against the live converted Marc file and against an old
schema file: only permit rows are ever touched, rows with an unreadable date
are skipped rather than guessed at, and a second run changes nothing.

A lead counts as worked, and is left alone, if `Date Contacted`, `Responded?`,
`Qualified?` or `Next Follow-up` carries anything, or if `Status` is set to
anything other than blank, New or To contact. `Call Notes` is deliberately
excluded: the Hub writes the permit details there at claim time, so it is
never empty on a permit row.

Round-tripping the new template preserves everything: the merged banner, all
nine dropdowns, the conditional formatting on Next Follow-up, freeze panes,
autofilter, column widths and every formula. Verified cell by cell, nothing
dropped.

## Left to do

**1. Create an Entra app registration with write access to the site.** This
can be the same app registration `hub-sharepoint-handoff.md` recommends for
the Hub itself, or a separate one - either works, same permission:

- In the Entra admin center (or Azure Portal → App registrations), New
  registration, any name (e.g. "VA Capital Lead Tracker Automation").
- API permissions → Add a permission → Microsoft Graph → Application
  permissions → `Sites.Selected`. Grant admin consent.
- Certificates & secrets → New client secret. Copy the value immediately,
  it's shown once.
- `Sites.Selected` alone grants access to nothing until a site admin
  explicitly grants this app a role on the VA Capital site. That's a
  separate Graph call (`POST /sites/{siteId}/permissions`) an admin runs
  once, granting this app's id `write` on the site - this is what keeps the
  app scoped to just this one SharePoint site rather than the whole tenant.
- Add three repository secrets: `MS_TENANT_ID`, `MS_CLIENT_ID` (the app's
  Application ID), `MS_CLIENT_SECRET` (the secret value from above).

**1b. Add "Permit tool" to the Source of Lead dropdown.** The new template
restricts column H to Instagram Ad, COI Referral, Property Listing, Networking
Event, LinkedIn, Cold Outreach, Past Client/Contact and Other. "Permit tool"
is not among them, and it is the exact string both the Hub's claim write and
this script key off. Until it is added, every permit row sits outside its own
column's validation.

**2. Check `trackers.json`.** Every entry is `"enabled": false` so nothing runs
until you have looked at it. The item ids are the same ones in
`hub-sharepoint-handoff.md`, confirmed live via the read-only Microsoft 365
connector, so this is a read-through rather than an investigation. Only the
six migrated brokers are in the file; Yousif and Hung aren't, for the reasons
above.

**3. Run it by hand first.** Actions tab, "Expire Uncontacted Permit Claims",
Run workflow, leave dry run ticked. It writes nothing and uploads a
`claim-expiry-report.json` artifact listing every row it would have struck out.
Check that list against what the brokers were actually doing before you trust
it. A tracker whose columns do not match shows up as an ERROR line rather than
being silently skipped.

**4. Turn off dry run.** Change `DRY_RUN` in
`.github/workflows/expire-claims.yml` from `'true'` to `'false'`. Scheduled
runs will then write. Every write goes to SharePoint as a new file version,
so anything wrong can be rolled back from that file's version history there.

**5. Get a release endpoint from the Hub.** This is the real gap. Striking the
row in the tracker does not tell the Hub or the dashboard that the lead is free
again, so until that endpoint exists an expired lead reads as released in the
spreadsheet and still claimed everywhere else. When they ship it, set
`HUB_RELEASE_URL` (and `HUB_API_KEY` if they want auth) and the script starts
calling it. It currently posts `{permitId, tracker, reason}`, so that shape may
need adjusting to match whatever they build.

## Two things to decide later

The Weekly Summary sheet counts "Leads Contacted" with
`COUNTIF('Lead Log'!$A:$A, week)`, which counts every row in that week
including released ones. So released permit claims still inflate that number.
Fixing it means changing the formula to exclude struck rows, which is your
call since it changes a metric you report on.

There is no warning before release. A broker who is mid-conversation but has
not written anything in the tracker loses the lead silently at day 14, and the
next broker to claim it phones the same developer. A flag at day 10 plus a note
to the broker would avoid that. Worth adding once the Hub side exists.

## Configuration

All via environment, defaults in brackets:

`DRY_RUN` [true], `EXPIRY_DAYS` [14], `MAX_RELEASES_PER_RUN` [25],
`TRACKER_REGISTRY` [trackers.json], `PERMIT_SOURCE` ["Permit tool"],
`REPORT_FILE` [claim_expiry_report.json].

`MAX_RELEASES_PER_RUN` is a circuit breaker. If a run would strike more rows
than that in one file it writes nothing and reports why, so a bad date parse
cannot wipe out a whole tracker overnight.

## Enrichment, separately

Unrelated to claim expiry but fixed at the same time: `enrich_leads.py` was
reading `data["priority_leads"]`, a key `fetch_permits.py` has never written
(it writes `data["leads"]`), so enrichment has been silently doing nothing on
every run. Fixed to read the right key. Per your call, there's no nightly cap
- the very next scheduled run will attempt all ~400 backlogged leads in one
go, each a Claude API call with web search. Expect that run to take
meaningfully longer than a normal permit-scan run and to use real API spend;
after that, only genuinely new leads get enriched each night since results
are cached in `enrichment_cache.json`.
