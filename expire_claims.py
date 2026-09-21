"""
Claim expiry for permit-tool leads.

A broker claims a permit lead in the Hub, which writes a row into that
broker's Lead Tracker in Box with Source of Lead = "Permit tool". If nobody
works the lead within EXPIRY_DAYS, this job strikes the row out in red,
notes that it was released, and (once the Hub exposes an endpoint) tells the
Hub to put the lead back in the pool.

Runs at 3am local so it never fights a broker who has the file open.

SAFETY: dry run is the default. It changes nothing and prints what it would
have done. Set DRY_RUN=false to let it write. Every write goes to Box as a
NEW VERSION, so anything it gets wrong can be rolled back from Box's version
history.

Config, all via environment:
  DRY_RUN              "false" to actually write. Default true.
  EXPIRY_DAYS          Days before an unworked claim is released. Default 14.
  TRACKER_REGISTRY     Path to the broker -> Box file id map. Default trackers.json
  MAX_RELEASES_PER_RUN Per-file circuit breaker. Default 25.
  BOX_CLIENT_ID        Box app credentials (client credentials grant)
  BOX_CLIENT_SECRET
  BOX_SUBJECT_TYPE     "enterprise" or "user". Default enterprise.
  BOX_SUBJECT_ID
  BOX_DEVELOPER_TOKEN  Alternative to the above for local testing.
  HUB_RELEASE_URL      Optional. When set, the Hub is told about each release.
  HUB_API_KEY          Optional bearer token for that call.
"""

import datetime as dt
import io
import json
import os
import re
import sys
from copy import copy

import requests
from openpyxl import load_workbook
from openpyxl.styles import Font

# --- config -----------------------------------------------------------------

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
EXPIRY_DAYS = int(os.environ.get("EXPIRY_DAYS", "14"))
REGISTRY = os.environ.get("TRACKER_REGISTRY", "trackers.json")
MAX_RELEASES_PER_RUN = int(os.environ.get("MAX_RELEASES_PER_RUN", "25"))
REPORT_FILE = os.environ.get("REPORT_FILE", "claim_expiry_report.json")

HUB_RELEASE_URL = os.environ.get("HUB_RELEASE_URL", "").strip()
HUB_API_KEY = os.environ.get("HUB_API_KEY", "").strip()

# Only rows carrying this in Source of Lead are ever touched. Everything else
# in the tracker (Instagram Ad leads, referrals, manual entries) is off limits.
PERMIT_SOURCE = os.environ.get("PERMIT_SOURCE", "Permit tool").strip().lower()

SHEET_NAME = "Lead Log"
RELEASE_MARKER = "Released by permit tool"
STRIKE_COLOUR = "FF9B3B2E"  # the rust already used on the dashboard

# Header text -> internal key. Matching is case and whitespace insensitive.
COLUMNS = {
    "date": "date",
    "lead name": "name",
    "source of lead": "source",
    "responded?": "responded",
    "qualified?": "qualified",
    "next follow-up": "followup",
    "notes": "notes",
}
REQUIRED = {"date", "source", "notes"}

# Any of these carrying a value means a human touched the lead. Notes is
# deliberately NOT in this list: the Hub writes the permit details into Notes
# at claim time, so Notes is never empty on a permit row and would make every
# claim look worked.
CONTACT_FIELDS = ("responded", "qualified", "followup")


# --- Box --------------------------------------------------------------------

class Box:
    API = "https://api.box.com/2.0"
    UPLOAD = "https://upload.box.com/api/2.0"

    def __init__(self):
        self.token = self._auth()
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {self.token}"

    @staticmethod
    def _auth():
        tok = os.environ.get("BOX_DEVELOPER_TOKEN", "").strip()
        if tok:
            return tok
        cid = os.environ.get("BOX_CLIENT_ID", "").strip()
        sec = os.environ.get("BOX_CLIENT_SECRET", "").strip()
        sub_type = os.environ.get("BOX_SUBJECT_TYPE", "enterprise").strip()
        sub_id = os.environ.get("BOX_SUBJECT_ID", "").strip()
        if not (cid and sec and sub_id):
            raise SystemExit(
                "No Box credentials. Set BOX_CLIENT_ID, BOX_CLIENT_SECRET and "
                "BOX_SUBJECT_ID (or BOX_DEVELOPER_TOKEN for a local test)."
            )
        r = requests.post(
            "https://api.box.com/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": sec,
                "box_subject_type": sub_type,
                "box_subject_id": sub_id,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["access_token"]

    def download(self, file_id):
        r = self.s.get(f"{self.API}/files/{file_id}/content", timeout=120)
        r.raise_for_status()
        return r.content

    def upload_version(self, file_id, name, blob):
        r = self.s.post(
            f"{self.UPLOAD}/files/{file_id}/content",
            files={
                "attributes": (None, json.dumps({"name": name})),
                "file": (name, blob,
                         "application/vnd.openxmlformats-officedocument."
                         "spreadsheetml.sheet"),
            },
            timeout=180,
        )
        r.raise_for_status()
        return r.json()["entries"][0]["file_version"]["id"]


# --- workbook ---------------------------------------------------------------

def norm(v):
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def map_headers(ws):
    """Resolve column letters by header text rather than fixed positions, so
    a tracker with an extra column inserted still works."""
    found = {}
    for cell in ws[1]:
        key = COLUMNS.get(norm(cell.value))
        if key and key not in found:
            found[key] = cell.column
    return found


def as_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(str(v).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def permit_id_from(notes):
    m = re.search(r"Permit\s+([A-Z]{2,4}-[A-Za-z0-9-]+)", str(notes or ""))
    return m.group(1) if m else None


def strike_row(ws, row, last_col):
    for col in range(1, last_col + 1):
        c = ws.cell(row=row, column=col)
        f = copy(c.font)
        c.font = Font(
            name=f.name, size=f.size, bold=f.bold, italic=f.italic,
            underline=f.underline, vertAlign=f.vertAlign,
            strike=True, color=STRIKE_COLOUR,
        )


def scan(ws, today):
    """Return the rows that should be released. Pure: touches nothing."""
    cols = map_headers(ws)
    missing = REQUIRED - set(cols)
    if missing:
        raise ValueError(f"missing expected column(s): {sorted(missing)}")

    last_col = max(cols.values())
    for cell in ws[1]:
        if cell.value not in (None, ""):
            last_col = max(last_col, cell.column)

    hits = []
    for row in range(2, ws.max_row + 1):
        source = norm(ws.cell(row=row, column=cols["source"]).value)
        if source != PERMIT_SOURCE:
            continue

        notes = ws.cell(row=row, column=cols["notes"]).value
        if RELEASE_MARKER.lower() in norm(notes):
            continue  # already released, keep this run idempotent

        claimed = as_date(ws.cell(row=row, column=cols["date"]).value)
        if claimed is None:
            continue  # never guess at an unparseable date
        age = (today - claimed).days
        if age < EXPIRY_DAYS:
            continue

        worked = any(
            norm(ws.cell(row=row, column=cols[f]).value)
            for f in CONTACT_FIELDS if f in cols
        )
        if worked:
            continue

        hits.append({
            "row": row,
            "age_days": age,
            "claimed_on": claimed.isoformat(),
            "lead": ws.cell(row=row,
                            column=cols.get("name", 1)).value or "",
            "permit_id": permit_id_from(notes),
        })
    return hits, cols, last_col


def release_rows(ws, hits, cols, last_col, today):
    note_col = cols["notes"]
    for h in hits:
        strike_row(ws, h["row"], last_col)
        existing = ws.cell(row=h["row"], column=note_col).value or ""
        stamp = f"{RELEASE_MARKER} {today.isoformat()}: not contacted within {EXPIRY_DAYS} days."
        ws.cell(row=h["row"], column=note_col).value = (
            f"{existing} | {stamp}".strip(" |")
        )


# --- hub --------------------------------------------------------------------

def tell_hub(entries):
    """The Hub owns claim state. Until it exposes a release endpoint this is a
    no-op and the release only exists in the tracker."""
    if not HUB_RELEASE_URL:
        return "skipped: HUB_RELEASE_URL not set"
    headers = {"Content-Type": "application/json"}
    if HUB_API_KEY:
        headers["Authorization"] = f"Bearer {HUB_API_KEY}"
    ok, failed = 0, 0
    for e in entries:
        if not e.get("permit_id"):
            failed += 1
            continue
        try:
            r = requests.post(HUB_RELEASE_URL, headers=headers, timeout=30,
                              json={"permitId": e["permit_id"],
                                    "tracker": e["tracker"],
                                    "reason": "expired_uncontacted"})
            ok += 1 if r.ok else 0
            failed += 0 if r.ok else 1
        except requests.RequestException:
            failed += 1
    return f"released {ok}, failed {failed}"


# --- main -------------------------------------------------------------------

def main():
    today = dt.date.today()
    if not os.path.exists(REGISTRY):
        raise SystemExit(
            f"No tracker registry at {REGISTRY}. It maps each broker to their "
            f"Box file id. See SETUP-claim-expiry.md."
        )
    with open(REGISTRY, encoding="utf-8") as f:
        trackers = json.load(f)

    trackers = [t for t in trackers if t.get("enabled", True)]
    if not trackers:
        raise SystemExit("Tracker registry has no enabled entries.")

    box = Box()
    report = {"run_at": dt.datetime.now().isoformat(timespec="seconds"),
              "dry_run": DRY_RUN, "expiry_days": EXPIRY_DAYS, "trackers": []}
    total = 0

    for t in trackers:
        entry = {"broker": t.get("broker"), "file_id": t["file_id"],
                 "released": [], "status": "ok"}
        try:
            blob = box.download(t["file_id"])
            wb = load_workbook(io.BytesIO(blob))
            ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb[wb.sheetnames[0]]

            hits, cols, last_col = scan(ws, today)
            entry["released"] = hits

            if len(hits) > MAX_RELEASES_PER_RUN:
                entry["status"] = (
                    f"ABORTED: {len(hits)} rows would be released, over the "
                    f"limit of {MAX_RELEASES_PER_RUN}. Nothing written. Raise "
                    f"MAX_RELEASES_PER_RUN if this is genuinely correct."
                )
            elif not hits:
                entry["status"] = "nothing to release"
            elif DRY_RUN:
                entry["status"] = f"dry run, would release {len(hits)}"
            else:
                release_rows(ws, hits, cols, last_col, today)
                out = io.BytesIO()
                wb.save(out)
                vid = box.upload_version(t["file_id"], t["name"], out.getvalue())
                entry["status"] = f"released {len(hits)}, new Box version {vid}"
                total += len(hits)
                for h in hits:
                    h["tracker"] = t.get("name") or t["file_id"]
        except Exception as exc:  # one bad tracker must not stop the rest
            entry["status"] = f"ERROR: {type(exc).__name__}: {exc}"

        report["trackers"].append(entry)
        print(f"[{entry['broker']}] {entry['status']}")
        for h in entry["released"]:
            print(f"    row {h['row']:>4}  {h['age_days']:>3}d  "
                  f"{h['permit_id'] or '?':<18} {str(h['lead'])[:44]}")

    flat = [dict(h, tracker=e.get("broker"))
            for e in report["trackers"] for h in e["released"]]
    report["hub"] = tell_hub(flat) if (flat and not DRY_RUN) else "skipped"
    report["total_released"] = total

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n{'DRY RUN, nothing written. ' if DRY_RUN else ''}"
          f"{len(flat)} row(s) matched across {len(trackers)} tracker(s).")
    print(f"Hub: {report['hub']}")

    if any(e["status"].startswith(("ERROR", "ABORTED")) for e in report["trackers"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
