"""
Quebec Building Permit Market Intelligence + Lead Scoring

Pulls open permit data from multiple Quebec municipalities, normalizes to a
common schema, reconstructs a per-property permit timeline (including
forward-dated milestones such as planned work start and occupancy), and scores
every lead on a weighted, explainable model.

Scoring is deliberately multi-factor. A single binary test (e.g. "is this a
demolition") produces a monotonous list; real financing opportunity is a
function of project stage, scale, recency, asset type, developer activity and
how reachable the party is.

Adding a city: append one entry to SOURCES.
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

LEADS_LOOKBACK_DAYS = 45
DEMO_LOOKBACK_DAYS = 300
DASHBOARD_LOOKBACK_DAYS = 90
TIMELINE_YEARS = 8
TREND_WEEKS = 12

MAX_LEADS = 400
MAX_TIMELINE_ENTRIES = 40
MAX_GROUP_SIZE = 250
GEO_POINTS_PER_CITY = 1200

CRE_KEYWORDS = [
    "commercial", "industriel", "institutionnel", "bureau", "office",
    "centre commercial", "retail", "mixte", "mixed", "multilogement",
    "condominium", "residentiel multiple", "apartment", "logements",
    "residence", "entrepot", "warehouse", "logistique", "usine",
    "manufacturing", "distribution", "storage", "entreposage",
    "hotel", "motel", "stationnement", "parking", "tour", "tower",
    "clinique", "clinic", "data centre", "data center", "ecole", "school",
]

SOURCES = [
    {
        "city": "Montréal",
        "kind": "direct",
        "type_is_authoritative": True,
        "url": "https://donnees.montreal.ca/dataset/d90eaf1b-2de8-43f0-923a-27a620ecdf41/resource/5232a72d-235a-48eb-ae20-bb9d501300ad/download/permis-construction.csv",
        "fields": {
            "id": ["id_permis"],
            "date": ["date_emission"],
            "start_date": ["date_debut"],
            "address": ["emplacement"],
            "sector": ["arrondissement"],
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
            "occupancy_start": ["OCCUPATION_DEBUT"],
            "occupancy_end": ["OCCUPATION_FIN"],
            "address": ["ADRESSE"],
            "sector": ["EXVILLE_DESCR"],
            "type_code": ["TYPE_PERMIS"],
            "type_label": ["TYPE_PERMIS_DESCR"],
            "category": ["CATEGORIE_BATIMENT"],
            "building_type": ["TYPE_BATIMENT"],
            "nature": ["TYPE_PERMIS_DESCR"],
            "units": ["NOMBRE_LOGEMENTS"],
            "storeys": ["NOMBRE_ETAGES"],
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
    "city", "id_permis", "date_emission", "date_debut", "occupancy_start",
    "occupancy_end", "emplacement", "address_key", "secteur", "type_label",
    "work_class", "categorie", "type_batiment", "nature", "nb_logements",
    "storeys", "entrepreneur", "cout", "superficie", "latitude", "longitude",
]

MONTREAL_CODE_LABELS = {
    "CO": "Construction", "TR": "Transformation",
    "DE": "Démolition", "CA": "Certificat d'autorisation",
}


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------
def json_safe(obj):
    """NaN/Infinity are valid Python floats but invalid JSON - JSON.parse
    rejects them outright, which silently breaks the dashboard."""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, (int, float)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else obj
    if hasattr(obj, "item"):
        try:
            return json_safe(obj.item())
        except Exception:
            return str(obj)
    try:
        if pd.isna(obj):
            return None
    except Exception:
        pass
    return obj


def norm(text):
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return "".join(ch for ch in text.lower() if ch.isalnum())


def soft(text):
    """Lowercase, accent-stripped, but keeps word spacing for keyword search."""
    text = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def address_key(address, city):
    a = soft(str(address).split(",")[0])
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
    t = norm(label)
    if trust_label:
        return _class_from_label(t)
    detail = norm(category) + " " + norm(nature)
    if "demol" in detail or "demol" in t:
        return "demolition"
    if "nouveaubatiment" in detail or "nouvelleconstruction" in detail:
        return "construction"
    if "transform" in detail or "agrandiss" in detail or "renov" in detail:
        return "transformation"
    return _class_from_label(t)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
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
                    print(f"  {label}: {len(df):,} rows x {df.shape[1]} cols")
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
    out["id_permis"] = f"{norm(city)[:3].upper()}-" + pick("id").astype(str)
    out["date_emission"] = pd.to_datetime(pick("date"), errors="coerce")
    out["date_debut"] = pd.to_datetime(pick("start_date", None), errors="coerce")
    out["occupancy_start"] = pd.to_datetime(pick("occupancy_start", None), errors="coerce")
    out["occupancy_end"] = pd.to_datetime(pick("occupancy_end", None), errors="coerce")
    out["emplacement"] = pick("address").fillna("").astype(str)
    out["secteur"] = pick("sector").fillna(city).astype(str).replace("", city)
    out["categorie"] = pick("category").fillna("Non précisé").astype(str).replace("", "Non précisé")
    out["type_batiment"] = pick("building_type").fillna("").astype(str)
    out["nature"] = pick("nature").fillna("").astype(str)
    out["nb_logements"] = pd.to_numeric(pick("units", 0), errors="coerce").fillna(0)
    out["storeys"] = pd.to_numeric(pick("storeys", None), errors="coerce")
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
        classify_work(l, c, n, trust_label=bool(a))
        for l, c, n, a in zip(out["type_label"], out["categorie"], out["nature"], authoritative)
    ]
    out["city"] = city
    out["address_key"] = [address_key(a, city) for a in out["emplacement"]]

    if missing:
        print(f"  {city}: unmapped fields -> {missing}")
    print(f"  {city}: newest {out['date_emission'].max()}, classes {out['work_class'].value_counts().to_dict()}")
    return out[COLUMNS]


def fetch_permits():
    frames = [load_source(s) for s in SOURCES]
    df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    df = df.dropna(subset=["date_emission"])
    print(f"Combined: {len(df):,} rows across {df['city'].nunique()} cities")
    return df


def latest_date(df):
    return df["date_emission"].max()


def _window(df, days):
    """Recent rows measured per city - publication lag varies widely, and a
    single global cutoff would silently exclude slower publishers."""
    parts = []
    for _, group in df.groupby("city"):
        newest = group["date_emission"].max()
        if pd.notna(newest):
            parts.append(group[group["date_emission"] >= newest - timedelta(days=days)])
    return pd.concat(parts) if parts else df.iloc[0:0]


# --------------------------------------------------------------------------
# Timelines
# --------------------------------------------------------------------------
def build_timelines(df, keys):
    keys = {k for k in keys if k}
    if not keys:
        return {}

    horizon = latest_date(df) - timedelta(days=365 * TIMELINE_YEARS)
    hist = df[(df["address_key"].isin(keys)) & (df["date_emission"] >= horizon)]
    hist = hist.sort_values("date_emission")

    timelines, skipped = {}, 0
    for key, group in hist.groupby("address_key"):
        if len(group) > MAX_GROUP_SIZE:
            skipped += 1
            continue
        events = []
        for r in group.tail(MAX_TIMELINE_ENTRIES).itertuples():
            events.append({
                "date": r.date_emission.strftime("%Y-%m-%d"),
                "kind": "permit",
                "work_class": r.work_class,
                "title": r.type_label,
                "detail": (r.nature or "")[:180],
                "units": int(r.nb_logements or 0),
                "cost": None if pd.isna(r.cout) else float(r.cout),
            })
            if pd.notna(r.date_debut):
                events.append({
                    "date": r.date_debut.strftime("%Y-%m-%d"),
                    "kind": "milestone",
                    "work_class": r.work_class,
                    "title": "Work scheduled to begin",
                    "detail": f"Declared start date for {r.type_label.lower()}",
                    "units": 0, "cost": None,
                })
            if pd.notna(r.occupancy_start):
                events.append({
                    "date": r.occupancy_start.strftime("%Y-%m-%d"),
                    "kind": "milestone",
                    "work_class": r.work_class,
                    "title": "Occupancy begins",
                    "detail": "Declared occupancy start",
                    "units": 0, "cost": None,
                })
        events.sort(key=lambda e: e["date"])
        timelines[key] = events

    if skipped:
        print(f"  Skipped {skipped} oversized address group(s)")
    return timelines


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def _days_between(a, b):
    return (b - a).days


def score_lead(row, timeline, reference_date):
    """Weighted, explainable score. Each contributing factor records the points
    it added and why, so the dashboard can show the reasoning rather than an
    unexplained number."""
    reasons = []
    score = 0

    demos = [e for e in timeline if e["kind"] == "permit" and e["work_class"] == "demolition"]
    builds = [e for e in timeline if e["kind"] == "permit" and e["work_class"] == "construction"]
    permits = [e for e in timeline if e["kind"] == "permit"]

    issued = datetime.strptime(row["date_emission"], "%Y-%m-%d")
    age_days = max(0, _days_between(issued, reference_date))

    # --- 1. Project stage (0-35) -----------------------------------------
    stage = None
    if demos:
        last_demo = max(e["date"] for e in demos)
        later_builds = [e for e in builds if e["date"] > last_demo]
        demo_age = _days_between(datetime.strptime(last_demo, "%Y-%m-%d"), reference_date)
        if not later_builds:
            if 20 <= demo_age <= 240:
                pts, stage = 35, "cleared_site"
                why = (f"Demolition permit issued {demo_age} days ago with no construction "
                       "permit filed since. The site is being cleared but the build is not "
                       "yet permitted — construction financing is very unlikely to be closed.")
            elif demo_age < 20:
                pts, stage = 26, "cleared_site"
                why = ("Demolition permit issued within the last three weeks. Very early — the "
                       "owner may still be arranging the redevelopment.")
            else:
                pts, stage = 12, "stalled_site"
                why = (f"Demolition was {demo_age} days ago with still no construction permit. "
                       "Either a long approval process or a stalled project — worth a call, "
                       "but lower confidence.")
        else:
            pts, stage = 24, "rebuilding"
            why = ("Demolition followed by a construction permit — a full redevelopment is "
                   "underway. Financing may exist, but redevelopments often need bridge or "
                   "mezzanine layers.")
    elif row["work_class"] == "construction":
        pts, stage = 22, "new_build"
        why = "New construction permit — the build is approved and capital is being deployed."
    elif row["work_class"] == "transformation":
        pts, stage = 14, "major_reno"
        why = ("Major transformation permit — repositioning an existing asset, which often "
               "triggers refinancing.")
    else:
        pts, stage = 6, "activity"
        why = "Recent permit activity at this property."
    score += pts
    reasons.append({"factor": "Project stage", "points": pts, "detail": why})

    # --- 2. Scale (0-25) --------------------------------------------------
    units = int(row.get("nb_logements") or 0)
    cost = row.get("cout")
    scale_pts, scale_why = 0, None
    if units >= 50:
        scale_pts, scale_why = 25, f"{units} residential units — institutional-scale project."
    elif units >= 20:
        scale_pts, scale_why = 21, f"{units} units — solidly in commercial mortgage territory."
    elif units >= 10:
        scale_pts, scale_why = 16, f"{units} units — multi-residential financing candidate."
    elif units >= 5:
        scale_pts, scale_why = 11, f"{units} units — small multi-residential."
    elif units >= 2:
        scale_pts, scale_why = 5, f"{units} units."
    if cost:
        if cost >= 5_000_000 and scale_pts < 25:
            scale_pts, scale_why = 25, f"Declared permit value of ${cost:,.0f}."
        elif cost >= 1_000_000 and scale_pts < 19:
            scale_pts, scale_why = 19, f"Declared permit value of ${cost:,.0f}."
        elif cost >= 400_000 and scale_pts < 12:
            scale_pts, scale_why = 12, f"Declared permit value of ${cost:,.0f}."
    if scale_pts:
        score += scale_pts
        reasons.append({"factor": "Project scale", "points": scale_pts, "detail": scale_why})

    # --- 3. Recency (0-15) ------------------------------------------------
    if age_days <= 14:
        r_pts, r_why = 15, f"Filed {age_days} days ago — you would be early in the conversation."
    elif age_days <= 30:
        r_pts, r_why = 11, f"Filed {age_days} days ago."
    elif age_days <= 75:
        r_pts, r_why = 6, f"Filed {age_days} days ago."
    else:
        r_pts, r_why = 2, f"Filed {age_days} days ago — cooling."
    score += r_pts
    reasons.append({"factor": "Recency", "points": r_pts, "detail": r_why})

    # --- 4. Asset type (0-15) --------------------------------------------
    haystack = soft(f"{row.get('categorie','')} {row.get('type_batiment','')} {row.get('nature','')}")
    hits = [k for k in CRE_KEYWORDS if k in haystack]
    if hits:
        a_pts = 15 if len(hits) > 1 else 11
        score += a_pts
        reasons.append({
            "factor": "Asset type",
            "points": a_pts,
            "detail": f"Commercial-relevant asset signals in the permit record: {', '.join(hits[:3])}.",
        })

    # --- 5. Developer activity (0-10) ------------------------------------
    if len(permits) >= 4:
        d_pts, d_why = 10, f"{len(permits)} permits on record at this property — an actively worked site."
    elif len(permits) == 3:
        d_pts, d_why = 7, "Three permits on record — sustained activity at this property."
    elif len(permits) == 2:
        d_pts, d_why = 4, "A second permit at this property — repeat activity."
    else:
        d_pts, d_why = 0, None
    if d_pts:
        score += d_pts
        reasons.append({"factor": "Property activity", "points": d_pts, "detail": d_why})

    # --- 6. Reachability (0-8) -------------------------------------------
    if row.get("entrepreneur"):
        score += 8
        reasons.append({
            "factor": "Reachability",
            "points": 8,
            "detail": f"Permit applicant named in the public record: {row['entrepreneur']}. "
                      "A direct route to whoever is running the project.",
        })

    # --- 7. Forward-dated work (0-12) ------------------------------------
    future = [e for e in timeline
              if e["kind"] == "milestone" and e["date"] > reference_date.strftime("%Y-%m-%d")]
    if future:
        nxt = min(future, key=lambda e: e["date"])
        days_out = _days_between(reference_date, datetime.strptime(nxt["date"], "%Y-%m-%d"))
        score += 12
        reasons.append({
            "factor": "Timing window",
            "points": 12,
            "detail": f"{nxt['title']} on {nxt['date']} — {days_out} days out. Work has not "
                      "started yet, so the financing decision is still open.",
        })

    score = min(100, score)
    if score >= 70:
        tier, tier_label = "hot", "Hot"
    elif score >= 52:
        tier, tier_label = "strong", "Strong"
    elif score >= 34:
        tier, tier_label = "moderate", "Moderate"
    else:
        tier, tier_label = "watch", "Watch"

    reasons.sort(key=lambda r: -r["points"])
    return {
        "score": score,
        "tier": tier,
        "tier_label": tier_label,
        "stage": stage,
        "headline": reasons[0]["detail"] if reasons else "",
        "reasons": reasons,
    }


STAGE_LABELS = {
    "cleared_site": "Cleared site, no rebuild filed",
    "stalled_site": "Cleared site, long gap",
    "rebuilding": "Demolished and rebuilding",
    "new_build": "New construction",
    "major_reno": "Major transformation",
    "activity": "Permit activity",
}


def build_leads(df):
    """Candidate pool is deliberately broad - demolitions, new construction and
    substantial transformations - so the ranking does the discriminating rather
    than the filter producing a single-flavour list."""
    recent = _window(df, LEADS_LOOKBACK_DAYS)
    demo_pool = _window(df, DEMO_LOOKBACK_DAYS)

    haystack = (recent["categorie"].fillna("") + " " +
                recent["type_batiment"].fillna("") + " " +
                recent["nature"].fillna("")).map(soft)
    cre = haystack.apply(lambda t: any(k in t for k in CRE_KEYWORDS))

    big = (recent["nb_logements"].fillna(0) >= 4) | (recent["cout"].fillna(0) >= 400_000)

    construction = recent[(recent["work_class"] == "construction") & (cre | big)]
    transformation = recent[(recent["work_class"] == "transformation") &
                            ((recent["nb_logements"].fillna(0) >= 8) |
                             (recent["cout"].fillna(0) >= 750_000))]
    demolition = demo_pool[demo_pool["work_class"] == "demolition"]

    print(f"Candidates -> construction {len(construction)}, "
          f"transformation {len(transformation)}, demolition {len(demolition)}")

    pool = pd.concat([construction, transformation, demolition])
    pool = pool[pool["address_key"] != ""]
    pool = pool.sort_values("date_emission", ascending=False)
    pool = pool.drop_duplicates(subset=["address_key"], keep="first")
    print(f"  Unique properties: {len(pool)}")

    timelines = build_timelines(df, set(pool["address_key"]))
    reference = latest_date(df).to_pydatetime()

    records = []
    for r in pool.itertuples():
        base = {
            "city": r.city,
            "id_permis": r.id_permis,
            "date_emission": r.date_emission.strftime("%Y-%m-%d"),
            "emplacement": r.emplacement,
            "address_key": r.address_key,
            "secteur": r.secteur,
            "categorie": r.categorie,
            "type_batiment": r.type_batiment,
            "nature": r.nature,
            "type_label": r.type_label,
            "work_class": r.work_class,
            "nb_logements": int(r.nb_logements or 0),
            "storeys": None if pd.isna(r.storeys) else int(r.storeys),
            "entrepreneur": r.entrepreneur or "",
            "cout": None if pd.isna(r.cout) else float(r.cout),
            "superficie": None if pd.isna(r.superficie) else float(r.superficie),
            "latitude": None if pd.isna(r.latitude) else float(r.latitude),
            "longitude": None if pd.isna(r.longitude) else float(r.longitude),
        }
        tl = timelines.get(r.address_key, [])
        scoring = score_lead(base, tl, reference)
        base["timeline"] = tl
        base["scoring"] = scoring
        base["stage_label"] = STAGE_LABELS.get(scoring["stage"], "Permit activity")
        records.append(base)

    records.sort(key=lambda x: -x["scoring"]["score"])
    if len(records) > MAX_LEADS:
        print(f"  Capping at {MAX_LEADS} (from {len(records)})")
        records = records[:MAX_LEADS]

    tiers = {}
    stages = {}
    for rec in records:
        tiers[rec["scoring"]["tier_label"]] = tiers.get(rec["scoring"]["tier_label"], 0) + 1
        stages[rec["stage_label"]] = stages.get(rec["stage_label"], 0) + 1
    print(f"Leads by tier: {tiers}")
    print(f"Leads by stage: {stages}")
    return records


def build_dashboard_data(df, leads):
    cutoff = latest_date(df) - timedelta(days=DASHBOARD_LOOKBACK_DAYS)
    window = df[df["date_emission"] >= cutoff].copy()
    print(f"Market window: {len(window):,} permits from {cutoff.date()}")

    window["week"] = window["date_emission"].dt.to_period("W").apply(
        lambda p: p.start_time.strftime("%Y-%m-%d"))
    trend = window.groupby(["week", "work_class"]).size().unstack(fill_value=0).tail(TREND_WEEKS)
    trend_series = {c: trend[c].tolist() for c in trend.columns}

    # Sample per city so a large city cannot crowd the others off the map.
    geo_frames = []
    for _, group in window.dropna(subset=["latitude", "longitude"]).groupby("city"):
        geo_frames.append(group.head(GEO_POINTS_PER_CITY))
    geo = pd.concat(geo_frames) if geo_frames else window.iloc[0:0]
    geo_points = (
        geo[["latitude", "longitude", "secteur", "categorie", "emplacement",
             "nb_logements", "city", "work_class"]]
        .rename(columns={"latitude": "lat", "longitude": "lng", "secteur": "borough",
                         "categorie": "category", "emplacement": "address"})
        .to_dict(orient="records")
    )
    print(f"Map points by city: {geo['city'].value_counts().to_dict() if len(geo) else {}}")

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
        "leads": leads,
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
    for r in sorted(new_leads, key=lambda x: -x["scoring"]["score"])[:40]:
        rows += (f"<tr><td>{r['scoring']['score']}</td><td>{r['scoring']['tier_label']}</td>"
                 f"<td>{r['date_emission']}</td><td>{r['city']}</td>"
                 f"<td>{r['emplacement']}</td><td>{r['stage_label']}</td></tr>")
    msg.attach(MIMEText(f"""<html><body><h2>New permit leads</h2>
      <table border="1" cellpadding="6" cellspacing="0">
      <tr><th>Score</th><th>Tier</th><th>Issued</th><th>City</th><th>Address</th><th>Stage</th></tr>
      {rows}</table></body></html>""", "html"))
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
    new_leads = [l for l in leads if l["id_permis"] not in seen]

    data = json_safe(build_dashboard_data(df, leads))
    with open(DASHBOARD_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str, allow_nan=False)
    with open(DASHBOARD_DATA_FILE, "r", encoding="utf-8") as f:
        json.load(f)
    size_mb = os.path.getsize(DASHBOARD_DATA_FILE) / 1048576
    print(f"data.json written and validated ({size_mb:.1f} MB)")
    if size_mb > 50:
        raise RuntimeError(f"data.json is {size_mb:.0f} MB - refusing to commit.")

    if new_leads:
        pd.DataFrame([{k: v for k, v in l.items() if k not in ("timeline", "scoring")}
                      for l in new_leads]).to_csv(LEADS_FILE, index=False)
        print(f"{len(new_leads)} new lead(s) written to {LEADS_FILE}")
        send_email(new_leads)
    else:
        print("No new leads this run.")

    seen.update(l["id_permis"] for l in leads)
    save_seen_ids(seen)


if __name__ == "__main__":
    main()
