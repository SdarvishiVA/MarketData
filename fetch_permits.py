"""
Quebec Building Permit Market Intelligence + Lead Scanner

Pulls open permit data from multiple Quebec municipalities, normalizes to a
common schema, builds a per-property permit TIMELINE, and scores leads by signal
strength. A demolition permit with no follow-on construction permit is treated
as the strongest signal: the owner is clearing a site and has very likely not
arranged construction financing yet.

Adding a city: append one entry to SOURCES. Most Quebec cities publish through
Donnees Quebec (CKAN), so usually only a resource ID and column candidates are
needed.
"""

import json
import math
import os
import re
import smtplib
import unicodedata
from datetime import datetime, timedelta

import pandas as pd
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

STATE_FILE = "seen_permits.json"
LEADS_FILE = "new_leads.csv"
DASHBOARD_DATA_FILE = "docs/data.json"

CKAN_API = "https://www.donneesquebec.ca/recherche/api/3/action/resource_show"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,application/csv,application/json,*/*",
}

LEADS_LOOKBACK_DAYS = 30        # how recent a permit must be to become a lead
DEMO_LOOKBACK_DAYS = 270        # how far back to hunt unresolved demolitions
DASHBOARD_LOOKBACK_DAYS = 90    # market-intelligence charts
TIMELINE_YEARS = 6              # history depth for per-property timelines
TREND_WEEKS = 12

# Guards. Permits with unusable addresses collapse into one key, so a property
# "history" can otherwise balloon to the size of the whole dataset.
MAX_LEADS = 300                 # hard cap on leads written to the dashboard
MAX_TIMELINE_ENTRIES = 40       # most recent N permits per property
MAX_GROUP_SIZE = 250            # above this, the key is junk, not a property

KEYWORDS_INCLUDE = [
    "commercial", "industriel", "institutionnel", "bureau", "office building",
    "immeuble à bureaux", "centre commercial", "shopping centre", "retail",
    "mixte", "mixed-use", "mixed use",
    "multilogement", "condominium", "résidentiel multiple", "apartment building",
    "student housing", "résidence étudiante", "résidence pour personnes âgées",
    "seniors residence", "seniors home",
    "entrepôt", "warehouse", "logistique", "logistics", "usine",
    "manufacturing plant", "zone industrielle", "distribution centre",
    "self-storage", "entreposage libre-service",
    "hôtel", "hotel", "motel",
    "stationnement étagé", "parking garage", "tour", "tower",
    "clinique privée", "private clinic", "data centre", "data center",
]

SOURCES = [
    {
        "city": "Montréal",
        "kind": "direct",
        # Montreal encodes the work type in a reliable code column, so the type
        # wins over descriptive text (which often mentions partial demolition
        # inside what is really a renovation permit).
        "type_is_authoritative": True,
        "url": "https://donnees.montreal.ca/dataset/d90eaf1b-2de8-43f0-923a-27a620ecdf41/resource/5232a72d-235a-48eb-ae20-bb9d501300ad/download/permis-construction.csv",
        "fields": {
            "id": ["id_permis", "numero_permis"],
            "date": ["date_emission"],
            "address": ["emplacement", "adresse"],
            "sector": ["arrondissement", "nom_arrondissement"],
            "type_code": ["code_type_base_demande"],
            "type_label": ["description_type_demande"],
            "category": ["description_categorie_batiment"],
            "building_type": ["description_type_batiment"],
            "nature": ["nature_travaux"],
            "units": ["nb_logements"],
            "lat": ["latitude"],
            "lng": ["longitude"],
        },
    },
    {
        "city": "Laval",
        "kind": "ckan",
        "resource_id": "d4731ee2-b1e5-4a31-bc56-4e13115e74ef",
        "fields": {
            "id": ["NO_PERMIS"],
            "date": ["DATE_EMISSION"],
            "address": ["ADRESSE"],
            "sector": ["EXVILLE_DESCR", "EXVILLE_CODE"],
            "type_code": ["TYPE_PERMIS"],
            "type_label": ["TYPE_PERMIS_DESCR"],
            "category": ["CATEGORIE_BATIMENT"],
            "building_type": ["TYPE_BATIMENT"],
            "nature": ["TYPE_PERMIS_DESCR"],
            "units": ["NOMBRE_LOGEMENTS"],
            "contractor": ["ENTREPRENEUR"],
            "cost": ["COUT_PERMIS"],
            "area": ["SUP_CA"],
        },
    },
    {
        "city": "Québec",
        "kind": "ckan",
        "resource_id": "9555031e-cfc5-4b78-bec9-4ab84b549f67",
        "fields": {
            "id": ["NUMERO_PERMIS"],
            "date": ["DATE_DELIVRANCE"],
            "address": ["ADRESSE_TRAVAUX"],
            "sector": ["ARRONDISSEMENT"],
            "type_label": ["TYPE_PERMIS"],
            "category": ["DOMAINE"],
            "nature": ["RAISON"],
            "lat": ["LATITUDE"],
            "lng": ["LONGITUDE"],
        },
    },
]

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT")

COLUMNS = [
    "city", "id_permis", "date_emission", "emplacement", "address_key", "secteur",
    "type_label", "work_class", "categorie", "type_batiment", "nature",
    "nb_logements", "entrepreneur", "cout", "superficie", "latitude", "longitude",
]

MONTREAL_CODE_LABELS = {
    "CO": "Construction", "TR": "Transformation",
    "DE": "Démolition", "CA": "Certificat d'autorisation",
}


def json_safe(obj):
    """Convert values that json.dump would emit as NaN/Infinity - which are
    valid Python but invalid JSON, and rejected outright by JSON.parse."""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, (int, float)):
        f = float(obj)
        if math.isnan(f) or math.isinf(f):
            return None
        return obj
    if hasattr(obj, "item"):          # numpy scalar
        try:
            return json_safe(obj.item())
        except Exception:
            return str(obj)
    if pd.isna(obj) is True:
        return None
    return obj


def norm(text):
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return "".join(ch for ch in text.lower() if ch.isalnum())


def address_key(address, city):
    """Loose property key so permits at the same address group together.
    Returns "" when the address is unusable - blank, or with no civic number -
    so those rows never group with each other."""
    a = unicodedata.normalize("NFKD", str(address).split(",")[0].lower())
    a = "".join(ch for ch in a if not unicodedata.combining(ch))
    a = re.sub(r"\b(rue|avenue|av|boulevard|boul|blvd|chemin|ch|place|croissant|montee|cote|terrasse|impasse|route|rang)\b", " ", a)
    a = re.sub(r"[^a-z0-9]+", " ", a).strip()
    if len(a) < 5 or not any(ch.isdigit() for ch in a):
        return ""
    return f"{norm(city)}|{a}"


def _class_from_label(t):
    if "demol" in t:
        return "demolition"
    if "construction" in t:
        return "construction"
    if "transform" in t or "renov" in t or "agrandiss" in t or "modif" in t:
        return "transformation"
    if "certificat" in t or "autorisation" in t:
        return "certificate"
    return "other"


def classify_work(label, category="", nature="", trust_label=False):
    """City-agnostic work classification.

    Cities disagree about where the real work type lives. Montreal encodes it in
    a reliable code, so that code decides - important because Montreal
    renovation permits often mention partial demolition in the work description,
    which would otherwise be misread as a building demolition. Quebec City has no
    such code and files demolitions as a 'Certificat d'autorisation' with the
    real work in the category and description, so there we read those fields
    first.
    """
    t = norm(label)
    if trust_label:
        return _class_from_label(t)

    detail = norm(category) + " " + norm(nature)
    if "demol" in detail or "demol" in t:
        return "demolition"
    if "nouveaubatiment" in detail or "nouvelleconstruction" in detail or "nouveaubatiment" in t:
        return "construction"
    if "transform" in detail or "agrandiss" in detail or "renov" in detail:
        return "transformation"
    return _class_from_label(t)


def resolve_ckan_url(resource_id, label):
    r = requests.get(CKAN_API, params={"id": resource_id}, headers=HTTP_HEADERS, timeout=60)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("success"):
        raise RuntimeError(f"CKAN lookup failed for {label}")
    return payload["result"]["url"]


def download_csv(url, label):
    print(f"Downloading {label}...")
    tmp = f"/tmp/{norm(label)}_permits.csv"
    with requests.get(url, headers=HTTP_HEADERS, stream=True, timeout=900) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
    print(f"  {label}: {os.path.getsize(tmp) / 1048576:.1f} MB")

    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        for sep in (",", ";", "\t"):
            try:
                df = pd.read_csv(tmp, encoding=enc, sep=sep, low_memory=False)
                if df.shape[1] > 3:
                    print(f"  {label}: {len(df):,} rows x {df.shape[1]} cols (enc={enc}, sep='{sep}')")
                    os.remove(tmp)
                    return df
            except Exception:
                continue
    os.remove(tmp)
    raise RuntimeError(f"Could not parse {label} CSV")


def load_source(spec):
    city = spec["city"]
    try:
        url = spec["url"] if spec["kind"] == "direct" else resolve_ckan_url(spec["resource_id"], city)
        df = download_csv(url, city)
    except Exception as e:
        print(f"  {city}: SKIPPED ({e})")
        return pd.DataFrame(columns=COLUMNS)

    print(f"  {city} columns: {list(df.columns)}")
    lookup = {norm(c): c for c in df.columns}
    missing = []

    def pick(key, fill=""):
        for candidate in spec["fields"].get(key, []):
            col = lookup.get(norm(candidate))
            if col is not None:
                return df[col]
        missing.append(key)
        return pd.Series([fill] * len(df), index=df.index)

    out = pd.DataFrame()
    prefix = norm(city)[:3].upper()
    out["id_permis"] = f"{prefix}-" + pick("id").astype(str)
    out["date_emission"] = pd.to_datetime(pick("date"), errors="coerce")
    out["emplacement"] = pick("address").fillna("").astype(str)
    out["secteur"] = pick("sector").fillna(city).astype(str).replace("", city)
    out["categorie"] = pick("category").fillna("Non précisé").astype(str).replace("", "Non précisé")
    out["type_batiment"] = pick("building_type").fillna("").astype(str)
    out["nature"] = pick("nature").fillna("").astype(str)
    out["nb_logements"] = pd.to_numeric(pick("units", 0), errors="coerce").fillna(0)
    out["entrepreneur"] = pick("contractor").fillna("").astype(str)
    out["cout"] = pd.to_numeric(pick("cost", None), errors="coerce")
    out["superficie"] = pd.to_numeric(pick("area", None), errors="coerce")
    out["latitude"] = pd.to_numeric(pick("lat", None), errors="coerce")
    out["longitude"] = pd.to_numeric(pick("lng", None), errors="coerce")

    label = pick("type_label").fillna("").astype(str)
    authoritative = pd.Series([False] * len(df), index=df.index)
    if spec.get("type_is_authoritative"):
        codes = pick("type_code").fillna("").astype(str)
        mapped = codes.map(MONTREAL_CODE_LABELS)
        label = mapped.where(mapped.notna(), label)
        authoritative = mapped.notna()

    out["type_label"] = label.replace("", "Autre")
    out["work_class"] = [
        classify_work(lbl, cat, nat, trust_label=bool(auth))
        for lbl, cat, nat, auth in zip(out["type_label"], out["categorie"],
                                       out["nature"], authoritative)
    ]
    out["city"] = city
    out["address_key"] = [address_key(a, city) for a in out["emplacement"]]

    if missing:
        print(f"  {city}: fields NOT matched -> {missing}")
    print(f"  {city}: newest permit {out['date_emission'].max()}")
    print(f"  {city}: work classes {out['work_class'].value_counts().to_dict()}")
    return out[COLUMNS]


def fetch_permits():
    frames = [load_source(s) for s in SOURCES]
    df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    df = df.dropna(subset=["date_emission"])
    print(f"Combined: {len(df):,} rows across {df['city'].nunique()} cities")
    return df


def latest_date(df):
    return df["date_emission"].max()


def build_timelines(df, keys):
    """Full permit history for the given property keys, oldest first."""
    keys = {k for k in keys if k}
    if not keys:
        return {}

    horizon = latest_date(df) - timedelta(days=365 * TIMELINE_YEARS)
    hist = df[(df["address_key"].isin(keys)) & (df["date_emission"] >= horizon)]
    hist = hist.sort_values("date_emission")

    timelines = {}
    skipped = 0
    for key, group in hist.groupby("address_key"):
        if len(group) > MAX_GROUP_SIZE:
            skipped += 1
            continue
        group = group.tail(MAX_TIMELINE_ENTRIES)
        timelines[key] = [
            {
                "date": r.date_emission.strftime("%Y-%m-%d"),
                "type": r.type_label,
                "work_class": r.work_class,
                "nature": (r.nature or "")[:160],
                "units": int(r.nb_logements or 0),
                "cost": None if pd.isna(r.cout) else float(r.cout),
            }
            for r in group.itertuples()
        ]
    if skipped:
        print(f"  Skipped {skipped} oversized address group(s) - likely non-specific addresses")
    return timelines


def signal_for(timeline, lead_class):
    """Rank the opportunity. Demolition with nothing built after it is the
    earliest point at which an owner needs construction financing."""
    demos = [e for e in timeline if e["work_class"] == "demolition"]
    builds = [e for e in timeline if e["work_class"] == "construction"]

    if demos:
        last_demo = max(e["date"] for e in demos)
        later_builds = [e for e in builds if e["date"] > last_demo]
        if not later_builds:
            return {
                "code": "demo_no_rebuild",
                "rank": 1,
                "label": "Demolition, no rebuild filed",
                "why": "Site is being cleared with no construction permit filed yet — "
                       "construction financing is very unlikely to be in place.",
            }
        return {
            "code": "rebuild_active",
            "rank": 2,
            "label": "Demolished and rebuilding",
            "why": "Demolition followed by a construction permit — a full redevelopment in progress.",
        }

    if lead_class == "construction":
        return {
            "code": "new_construction",
            "rank": 3,
            "label": "New construction",
            "why": "New build matching commercial criteria.",
        }

    return {
        "code": "activity",
        "rank": 4,
        "label": "Permit activity",
        "why": "Recent permit activity at this property.",
    }


def _date_ord(iso):
    return datetime.strptime(iso, "%Y-%m-%d").toordinal()


def _window(df, days):
    """Recent rows, measured per city.

    Cities publish with very different lags - Quebec City is days behind while
    Laval can be months. Anchoring every city to the single freshest date would
    silently exclude the slower publishers entirely.
    """
    parts = []
    for city, group in df.groupby("city"):
        newest = group["date_emission"].max()
        if pd.isna(newest):
            continue
        parts.append(group[group["date_emission"] >= newest - timedelta(days=days)])
    return pd.concat(parts) if parts else df.iloc[0:0]


def build_leads(df):
    recent = _window(df, LEADS_LOOKBACK_DAYS)

    text = (recent["categorie"].fillna("") + " " +
            recent["type_batiment"].fillna("") + " " +
            recent["nature"].fillna("")).str.lower()
    cre = text.apply(lambda t: any(k.lower() in t for k in KEYWORDS_INCLUDE))

    construction_leads = recent[(recent["work_class"] == "construction") & cre]

    demo_window = _window(df, DEMO_LOOKBACK_DAYS)
    demolition_leads = demo_window[demo_window["work_class"] == "demolition"]

    leads = pd.concat([construction_leads, demolition_leads])
    leads = leads[leads["address_key"] != ""]
    leads = leads.sort_values("date_emission", ascending=False)
    leads = leads.drop_duplicates(subset=["address_key"], keep="first")
    print(f"Lead candidates: {len(construction_leads)} construction + {len(demolition_leads)} demolition")
    print(f"  After address cleanup and one-row-per-property: {len(leads)}")

    timelines = build_timelines(df, set(leads["address_key"]))

    records = []
    for r in leads.itertuples():
        tl = timelines.get(r.address_key, [])
        sig = signal_for(tl, r.work_class)
        records.append({
            "city": r.city,
            "id_permis": r.id_permis,
            "date_emission": r.date_emission.strftime("%Y-%m-%d"),
            "emplacement": r.emplacement,
            "address_key": r.address_key,
            "secteur": r.secteur,
            "categorie": r.categorie,
            "nature": r.nature,
            "type_label": r.type_label,
            "work_class": r.work_class,
            "nb_logements": int(r.nb_logements or 0),
            "entrepreneur": r.entrepreneur or "",
            "cout": None if pd.isna(r.cout) else float(r.cout),
            "superficie": None if pd.isna(r.superficie) else float(r.superficie),
            "signal": sig,
            "timeline": tl,
        })

    records.sort(key=lambda x: (x["signal"]["rank"], -_date_ord(x["date_emission"])))
    if len(records) > MAX_LEADS:
        print(f"  Capping leads at {MAX_LEADS} (from {len(records)}), strongest signals kept")
        records = records[:MAX_LEADS]
    by_signal = {}
    for rec in records:
        by_signal[rec["signal"]["label"]] = by_signal.get(rec["signal"]["label"], 0) + 1
    print(f"Leads by signal: {by_signal}")
    return records


def build_all_permits(df):
    recent = _window(df, LEADS_LOOKBACK_DAYS)
    recent = recent[recent["work_class"].isin(["construction", "demolition"])]
    recent = recent.sort_values("date_emission", ascending=False).head(500)
    print(f"All construction/demolition permits in window: {len(recent)}")
    return [
        {
            "city": r.city,
            "id_permis": r.id_permis,
            "date_emission": r.date_emission.strftime("%Y-%m-%d"),
            "emplacement": r.emplacement,
            "secteur": r.secteur,
            "categorie": r.categorie,
            "nature": r.nature,
            "type_label": r.type_label,
            "work_class": r.work_class,
            "nb_logements": int(r.nb_logements) if pd.notna(r.nb_logements) else 0,
            "entrepreneur": r.entrepreneur or "",
            "cout": None if pd.isna(r.cout) else float(r.cout),
        }
        for r in recent.itertuples()
    ]


def build_dashboard_data(df, leads, all_permits):
    cutoff = latest_date(df) - timedelta(days=DASHBOARD_LOOKBACK_DAYS)
    window = df[df["date_emission"] >= cutoff].copy()
    print(f"Market window: {len(window):,} permits from {cutoff.date()}")

    window["week"] = window["date_emission"].dt.to_period("W").apply(
        lambda p: p.start_time.strftime("%Y-%m-%d"))
    trend = window.groupby(["week", "work_class"]).size().unstack(fill_value=0).tail(TREND_WEEKS)
    trend_series = {c: trend[c].tolist() for c in trend.columns}

    geo = window.dropna(subset=["latitude", "longitude"])
    geo_points = (
        geo[["latitude", "longitude", "secteur", "categorie", "emplacement",
             "nb_logements", "city", "work_class"]]
        .head(2500)
        .rename(columns={"latitude": "lat", "longitude": "lng", "secteur": "borough",
                         "categorie": "category", "emplacement": "address"})
        .to_dict(orient="records")
    )

    return {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_through": latest_date(df).strftime("%Y-%m-%d"),
        "window_days": DASHBOARD_LOOKBACK_DAYS,
        "leads_window_days": LEADS_LOOKBACK_DAYS,
        "demo_window_days": DEMO_LOOKBACK_DAYS,
        "cities": sorted(window["city"].dropna().unique().tolist()),
        "city_freshness": {
            c: g["date_emission"].max().strftime("%Y-%m-%d")
            for c, g in df.groupby("city") if pd.notna(g["date_emission"].max())
        },
        "total_permits": int(len(window)),
        "total_housing_units": int(window["nb_logements"].fillna(0).sum()),
        "by_city": window["city"].value_counts().to_dict(),
        "by_type": window["type_label"].value_counts().head(8).to_dict(),
        "by_borough": window["secteur"].value_counts().head(20).to_dict(),
        "by_category": window["categorie"].value_counts().head(15).to_dict(),
        "trend_weeks": trend.index.tolist(),
        "trend_series": trend_series,
        "geo_points": geo_points,
        "priority_leads": leads,
        "all_leads": all_permits,
    }


def load_seen_ids():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return {str(x) for x in json.load(f)}
    return set()


def save_seen_ids(ids):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(str(x) for x in ids), f)


def send_email(new_leads):
    if not (EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECIPIENT):
        print("Email credentials not set - skipping notification.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{len(new_leads)} new permit lead(s) - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT

    rows = ""
    for r in new_leads:
        rows += (f"<tr><td>{r['signal']['label']}</td><td>{r['date_emission']}</td>"
                 f"<td>{r['city']}</td><td>{r['emplacement']}</td>"
                 f"<td>{r['categorie']}</td><td>{r['entrepreneur'] or '-'}</td></tr>")

    msg.attach(MIMEText(f"""
    <html><body><h2>New Quebec Permit Leads</h2>
    <table border="1" cellpadding="6" cellspacing="0">
      <tr><th>Signal</th><th>Issued</th><th>City</th><th>Address</th><th>Category</th><th>Contractor</th></tr>
      {rows}
    </table></body></html>""", "html"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
    print("Email sent.")


def main():
    os.makedirs("docs", exist_ok=True)
    seen = load_seen_ids()
    df = fetch_permits()

    leads = build_leads(df)
    all_permits = build_all_permits(df)
    new_leads = [l for l in leads if l["id_permis"] not in seen]

    data = json_safe(build_dashboard_data(df, leads, all_permits))
    with open(DASHBOARD_DATA_FILE, "w", encoding="utf-8") as f:
        # allow_nan=False makes any remaining NaN a hard error here rather than
        # invalid JSON that only fails later in the browser.
        json.dump(data, f, ensure_ascii=False, indent=2, default=str, allow_nan=False)

    with open(DASHBOARD_DATA_FILE, "r", encoding="utf-8") as f:
        json.load(f)   # parse-back check: guarantees the file is valid JSON
    print("data.json validated as parseable JSON")
    size_mb = os.path.getsize(DASHBOARD_DATA_FILE) / 1048576
    print(f"Dashboard data written to {DASHBOARD_DATA_FILE} ({size_mb:.1f} MB)")
    if size_mb > 50:
        raise RuntimeError(
            f"data.json is {size_mb:.0f} MB - refusing to commit. "
            "Something is generating far too many records."
        )

    if new_leads:
        pd.DataFrame([{k: v for k, v in l.items() if k not in ("timeline", "signal")}
                      for l in new_leads]).to_csv(LEADS_FILE, index=False)
        print(f"{len(new_leads)} new lead(s) written to {LEADS_FILE}")
        send_email(new_leads)
    else:
        print("No new leads this run.")

    seen.update(l["id_permis"] for l in leads)
    save_seen_ids(seen)


if __name__ == "__main__":
    main()
