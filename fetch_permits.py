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
    # Commercial / office
    "commercial", "bureau", "office", "centre commercial", "shopping centre",
    "shopping mall", "retail", "commerce de detail",
    # Industrial
    "industriel", "industrial", "usine", "factory", "plant",
    "entrepot", "warehouse", "logistique", "logistics",
    "manufacturing", "distribution", "storage", "entreposage",
    # Institutional
    "institutionnel", "institutional", "clinique", "clinic",
    "ecole", "school", "data centre", "data center",
    # Mixed-use / multi-residential
    "mixte", "mixed", "multilogement", "multi-unit", "multifamily",
    "multi-residential", "residentiel multiple", "condominium",
    "apartment", "logements", "residence",
    # Hospitality / parking
    "hotel", "motel", "stationnement", "parking", "tour", "tower",
]
# Note: accent-stripping (see soft()/norm() below) already matches most
# French/English pairs that differ only by accent (e.g. "residence" ==
# "résidence" once normalized) - the entries above cover the pairs that
# differ by more than accents.

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
    {
        # Application-stage data - filed BEFORE a permit is issued. This is
        # upstream of everything else in the pipeline: a property showing up
        # here with no matching issued permit yet is the earliest financing
        # signal we have access to.
        "city": "Montréal",
        "kind": "ckan",
        "resource_id": "02ef21a5-3a21-4112-a134-4d7d22348e44",
        "id_prefix": "MTLAPP",
        "record_kind": "application",
        "fields": {
            "id": ["Numéro de demande"],
            "date": ["Date d'ouverture de la demande"],
            "address": ["Adresse"],
            "sector": ["Arrondissement"],
            "building_type": ["Type de bâtiment"],
            "nature": ["Description du permis"],
            "units": ["Nombre unités de logements"],
        },
    },
    {
        # Signed inclusionary-housing agreements ("metropole mixte"). Every row
        # is a confirmed residential development with a committed unit count -
        # signed around the same stage as, sometimes before, the permit itself.
        "city": "Montréal",
        "kind": "ckan",
        "resource_id": "1b5a181d-11d4-4491-b3fa-b6e5264a2f47",
        "id_prefix": "MTLENT",
        "record_kind": "agreement",
        "force_work_class": "agreement",
        "fields": {
            "id": ["id_entente"],
            "date": ["date_signature_sys"],
            "address": ["adr_emplacement"],
            "sector": ["arrondissement"],
            "units": ["nb_log_ajout"],
        },
    },
    {
        # Longueuil issued construction permits - live ArcGIS FeatureServer,
        # not a CKAN dataset. Includes a contractor name field, which neither
        # Montreal nor Quebec City publish.
        "city": "Longueuil",
        "kind": "arcgis",
        "base_url": "https://gociteweb.longueuil.quebec/arcgis/rest/services/Urbanisme/GPI/FeatureServer/43",
        "fields": {
            "id": ["NO_PERMIS"],
            "date": ["DATE_EMISSION"],
            "address": ["NO_CIV", "VOIE_CIRC"],
            "sector": ["ANCIENNE_VILLE"],
            "type_label": ["TYPE_PERMIS"],
            "category": ["TYPE_DEMANDE"],
            "nature": ["NATURE_TRAVAUX", "DESCRIPTION"],
            "contractor": ["REQ_NOM_ENTREPRISE"],
            "cost": ["VALEUR_TRAVAUX"],
            "lat": ["_GEOM_LAT"],
            "lng": ["_GEOM_LNG"],
        },
    },
    {
        # Longueuil's own pre-permit signal: applications still being
        # processed, not yet issued. Their direct equivalent of the Montreal
        # "harmonised delays" application-stage dataset above.
        "city": "Longueuil",
        "kind": "arcgis",
        "base_url": "https://gociteweb.longueuil.quebec/arcgis/rest/services/Urbanisme/GPI/FeatureServer/44",
        "id_prefix": "LONGAPP",
        "record_kind": "application",
        "fields": {
            "id": ["NO_PERMIS", "OBJECTID"],
            "date": ["DATE_CREATION"],
            "address": ["NO_CIV", "VOIE_CIRC"],
            "sector": ["ANCIENNE_VILLE"],
            "type_label": ["TYPE_PERMIS"],
            "category": ["TYPE_DEMANDE"],
            "nature": ["NATURE_TRAVAUX", "DESCRIPTION"],
            "contractor": ["REQ_NOM_ENTREPRISE"],
            "cost": ["VALEUR_TRAVAUX"],
            "lat": ["_GEOM_LAT"],
            "lng": ["_GEOM_LNG"],
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
    "work_class", "record_kind", "categorie", "type_batiment", "nature",
    "nb_logements", "storeys", "entrepreneur", "cout", "superficie",
    "latitude", "longitude",
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


def download_arcgis_layer(base_url, label, max_pages=250):
    """Query an ArcGIS FeatureServer/MapServer layer with pagination and pull
    geometry back reprojected to WGS84, so lat/lng arrive alongside the
    attribute table exactly like the CKAN sources do."""
    print(f"Downloading {label} (ArcGIS)...")
    params = {
        "where": "1=1", "outFields": "*", "f": "json",
        "outSR": 4326, "resultRecordCount": 1000, "resultOffset": 0,
    }
    features = []
    for _ in range(max_pages):
        r = requests.get(f"{base_url}/query", params=params, headers=HTTP_HEADERS, timeout=120)
        r.raise_for_status()
        payload = r.json()
        if "error" in payload:
            raise RuntimeError(payload["error"].get("message", "ArcGIS query error"))
        batch = payload.get("features", [])
        features.extend(batch)
        if not payload.get("exceededTransferLimit") and len(batch) < params["resultRecordCount"]:
            break
        params["resultOffset"] += params["resultRecordCount"]
    else:
        print(f"  {label}: hit the {max_pages}-page safety cap, data may be truncated")

    print(f"  {label}: {len(features):,} features")
    if not features:
        return pd.DataFrame()

    df = pd.DataFrame(f.get("attributes", {}) for f in features)
    df["_GEOM_LNG"] = [(f.get("geometry") or {}).get("x") for f in features]
    df["_GEOM_LAT"] = [(f.get("geometry") or {}).get("y") for f in features]
    return df


def load_source(spec):
    city = spec["city"]
    try:
        if spec["kind"] == "arcgis":
            df = download_arcgis_layer(spec["base_url"], f"{city} ({spec['base_url'].rsplit('/', 1)[-1]})")
        else:
            url = spec["url"] if spec["kind"] == "direct" else resolve_ckan_url(spec["resource_id"], city)
            df = download_csv(url, city)
    except Exception as e:
        print(f"  {city}: SKIPPED ({e})")
        return pd.DataFrame(columns=COLUMNS)

    if df.empty:
        print(f"  {city}: no rows returned")
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

    def pick_date(key, fill=None):
        for candidate in spec["fields"].get(key, []):
            col = lookup.get(norm(candidate))
            if col is not None:
                series = df[col]
                if spec["kind"] == "arcgis":
                    # ArcGIS date fields come back as epoch milliseconds.
                    return pd.to_datetime(series, unit="ms", errors="coerce")
                return pd.to_datetime(series, errors="coerce")
        missing.append(key)
        return pd.Series([pd.NaT] * len(df), index=df.index)

    out = pd.DataFrame()
    prefix = spec.get("id_prefix", norm(city)[:3].upper())
    out["id_permis"] = f"{prefix}-" + pick("id").astype(str)
    out["date_emission"] = pick_date("date")
    out["date_debut"] = pick_date("start_date")
    out["occupancy_start"] = pick_date("occupancy_start")
    out["occupancy_end"] = pick_date("occupancy_end")

    address_fields = spec["fields"].get("address", [])
    if len(address_fields) > 1:
        # Some sources (Longueuil) split the address across a civic-number
        # column and a street-name column rather than publishing one field.
        parts = [df[lookup[norm(c)]].fillna("").astype(str) for c in address_fields if norm(c) in lookup]
        out["emplacement"] = parts[0].str.cat(parts[1:], sep=" ").str.strip() if parts else ""
        if len(parts) < len(address_fields):
            missing.append("address")
    else:
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

    if spec.get("force_work_class"):
        # This source isn't classifiable by permit-type text (e.g. a signed
        # agreement has no "construction/demolition" wording) - the record
        # kind itself is the signal.
        out["work_class"] = spec["force_work_class"]
    else:
        out["work_class"] = [
            classify_work(l, c, n, trust_label=bool(a))
            for l, c, n, a in zip(out["type_label"], out["categorie"], out["nature"], authoritative)
        ]
    out["record_kind"] = spec.get("record_kind", "permit")
    out["city"] = city
    out["address_key"] = [address_key(a, city) for a in out["emplacement"]]

    if missing:
        print(f"  {city}: unmapped fields -> {missing}")
    print(f"  {city} [{out['record_kind'].iloc[0] if len(out) else spec.get('record_kind','permit')}]: "
          f"newest {out['date_emission'].max()}, classes {out['work_class'].value_counts().to_dict()}")
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
            title = r.type_label
            if r.record_kind == "application":
                title = f"Application filed: {r.type_label}" if r.type_label != "Autre" else "Application filed"
            elif r.record_kind == "agreement":
                title = "Inclusionary housing agreement signed"
            events.append({
                "date": r.date_emission.strftime("%Y-%m-%d"),
                "kind": "permit",
                "record_kind": r.record_kind,
                "work_class": r.work_class,
                "title": title,
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
# Classification
# --------------------------------------------------------------------------
def _days_between(a, b):
    return (b - a).days


CLASS_LABELS = {
    "demolition_rebuild": "Demolition + Construction",
    "demolition_only": "Demolition, No Construction Filed",
    "transformation": "Transformation",
    "construction": "Construction",
    "application": "Application Filed (Pre-Permit)",
    "agreement": "Agreement Signed (Pre-Permit)",
}


def classify_lead(row, timeline):
    """Sort a lead into one bucket rather than scoring it. The property's own
    permit history decides the bucket - not just the single most recent row -
    so a construction permit that followed a demolition is still classified
    as a demolition-rebuild rather than a plain new build."""
    permits_hist = [e for e in timeline
                    if e["kind"] == "permit" and e.get("record_kind", "permit") == "permit"]
    demos = [e for e in permits_hist if e["work_class"] == "demolition"]
    builds = [e for e in permits_hist if e["work_class"] == "construction"]

    record_kind = row.get("record_kind", "permit")

    if record_kind == "agreement":
        u = int(row.get("nb_logements") or 0)
        return {
            "code": "agreement",
            "label": CLASS_LABELS["agreement"],
            "why": f"Signed inclusionary-housing agreement confirming {u} residential units at "
                   "this address — a formally committed development, often signed before or "
                   "alongside the permit itself.",
        }

    if record_kind == "application" and not permits_hist:
        return {
            "code": "application",
            "label": CLASS_LABELS["application"],
            "why": "A permit application has been opened for this property, but no permit has "
                   "been issued yet and none appears in the historical record.",
        }

    if demos:
        last_demo = max(e["date"] for e in demos)
        later_builds = [e for e in builds if e["date"] > last_demo]
        if later_builds:
            return {
                "code": "demolition_rebuild",
                "label": CLASS_LABELS["demolition_rebuild"],
                "why": "Demolition permit followed by a construction permit — a full "
                       "redevelopment is underway at this property.",
            }
        return {
            "code": "demolition_only",
            "label": CLASS_LABELS["demolition_only"],
            "why": f"Demolition permit issued on {last_demo} with no construction permit "
                   "filed since — the site is cleared but the rebuild is not yet permitted.",
        }

    if row.get("work_class") == "construction":
        return {
            "code": "construction",
            "label": CLASS_LABELS["construction"],
            "why": "New construction permit, with no demolition on record at this property.",
        }

    if row.get("work_class") == "transformation":
        return {
            "code": "transformation",
            "label": CLASS_LABELS["transformation"],
            "why": "Major transformation/renovation permit at this property.",
        }

    return {
        "code": "construction",
        "label": CLASS_LABELS["construction"],
        "why": "Recent permit activity at this property.",
    }


def build_leads(df):
    """Candidate pool is deliberately broad - demolitions, new construction,
    substantial transformations, plus pre-permit applications and signed
    agreements - so the ranking does the discriminating rather than the filter
    producing a single-flavour list."""
    is_permit = df["record_kind"] == "permit"
    recent = _window(df[is_permit], LEADS_LOOKBACK_DAYS)
    demo_pool = _window(df[is_permit], DEMO_LOOKBACK_DAYS)

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

    # Pre-permit signals get no size filter - being upstream of the permit
    # itself is the entire point, regardless of scale.
    applications = _window(df[df["record_kind"] == "application"], LEADS_LOOKBACK_DAYS)
    agreements = _window(df[df["record_kind"] == "agreement"], LEADS_LOOKBACK_DAYS * 3)

    print(f"Candidates -> construction {len(construction)}, "
          f"transformation {len(transformation)}, demolition {len(demolition)}, "
          f"applications {len(applications)}, agreements {len(agreements)}")

    pool = pd.concat([construction, transformation, demolition, applications, agreements])
    pool = pool[pool["address_key"] != ""]
    pool = pool.sort_values("date_emission", ascending=False)
    pool = pool.drop_duplicates(subset=["address_key"], keep="first")
    print(f"  Unique properties: {len(pool)}")

    timelines = build_timelines(df, set(pool["address_key"]))

    records = []
    for r in pool.itertuples():
        base = {
            "city": r.city,
            "id_permis": r.id_permis,
            "record_kind": r.record_kind,
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
        base["timeline"] = tl
        base["classification"] = classify_lead(base, tl)
        records.append(base)

    # Sort by recency - most recent activity at the property first - rather
    # than by any weighted score.
    records.sort(key=lambda x: x["date_emission"], reverse=True)
    if len(records) > MAX_LEADS:
        print(f"  Capping at {MAX_LEADS} (from {len(records)}), most recent kept")
        records = records[:MAX_LEADS]

    counts = {}
    for rec in records:
        label = rec["classification"]["label"]
        counts[label] = counts.get(label, 0) + 1
    print(f"Leads by classification: {counts}")
    return records


def build_dashboard_data(df, leads):
    cutoff = latest_date(df) - timedelta(days=DASHBOARD_LOOKBACK_DAYS)
    # Market charts represent issued-permit activity. Applications and signed
    # agreements are a different lifecycle stage and would muddy the "by type"
    # and "by category" breakdowns - they still surface fully in the leads list.
    # Market charts represent activity we actually act on. Certificate-class
    # permits (signage, tree removal, minor site changes in Montreal - and a
    # catch-all bucket in Quebec City for anything not clearly demolition or
    # construction) never become leads, so they're excluded here too rather
    # than padding out the volume charts with noise.
    window = df[(df["date_emission"] >= cutoff) &
                (df["record_kind"] == "permit") &
                (df["work_class"] != "certificate")].copy()
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
    for r in sorted(new_leads, key=lambda x: x["date_emission"], reverse=True)[:40]:
        rows += (f"<tr><td>{r['classification']['label']}</td>"
                 f"<td>{r['date_emission']}</td><td>{r['city']}</td>"
                 f"<td>{r['emplacement']}</td></tr>")
    msg.attach(MIMEText(f"""<html><body><h2>New permit leads</h2>
      <table border="1" cellpadding="6" cellspacing="0">
      <tr><th>Classification</th><th>Issued</th><th>City</th><th>Address</th></tr>
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
        pd.DataFrame([
            {**{k: v for k, v in l.items() if k not in ("timeline", "classification")},
             "classification": l["classification"]["label"]}
            for l in new_leads
        ]).to_csv(LEADS_FILE, index=False)
        print(f"{len(new_leads)} new lead(s) written to {LEADS_FILE}")
        send_email(new_leads)
    else:
        print("No new leads this run.")

    seen.update(l["id_permis"] for l in leads)
    save_seen_ids(seen)


if __name__ == "__main__":
    main()
