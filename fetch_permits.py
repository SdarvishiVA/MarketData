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
            "id": ["No_Identifiant", "No_Permis"],
            "date": ["Date_Emission"],
            "address": ["Adresse"],
            "sector": ["ExVille_Descr", "ExVille_Code"],
            "type_code": ["Type_Permis"],
            "type_label": ["Type_Permis_Description", "Type_Permis_Desc"],
            "category": ["Categorie_Batiment"],
            "building_type": ["Type_Batiment"],
            "nature": ["Type_Permis_Description", "Structure"],
            "units": ["Nombre_Logements"],
            "contractor": ["Entrepreneur"],
            "cost": ["Cout_Permis"],
            "area": ["Superficie_Pi_Carre"],
        },
    },
    {
        "city": "Québec",
        "kind": "ckan",
        "resource_id": "9555031e-cfc5-4b78-bec9-4ab84b549f67",
        "fields": {
            "id": ["no_permis", "numero_permis", "numero", "id_permis", "no_dossier", "id"],
            "date": ["date_emission", "date_delivrance", "date_permis", "date"],
            "address": ["adresse", "adresse_complete", "adresse_civique", "localisation"],
            "sector": ["arrondissement", "nom_arrondissement", "quartier", "secteur"],
            "type_label": ["type_permis", "type", "nature_permis"],
            "category": ["categorie", "categorie_travaux", "nature_travaux", "type_travaux"],
            "nature": ["description", "description_travaux", "objet", "raison", "travaux"],
            "units": ["nb_logements", "nombre_logements", "logements"],
            "cost": ["cout", "valeur_travaux", "cout_travaux", "cout_estime"],
            "lat": ["latitude", "lat", "y"],
            "lng": ["longitude", "long", "lon", "x"],
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


def norm(text):
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return "".join(ch for ch in text.lower() if ch.isalnum())


def address_key(address, city):
    """Loose property key so permits at the same address group together."""
    a = unicodedata.normalize("NFKD", str(address).split(",")[0].lower())
    a = "".join(ch for ch in a if not unicodedata.combining(ch))
    a = re.sub(r"\b(rue|avenue|av|boulevard|boul|blvd|chemin|ch|place|croissant|montee|cote|terrasse|impasse|route|rang)\b", " ", a)
    a = re.sub(r"[^a-z0-9]+", " ", a).strip()
    return f"{norm(city)}|{a}"


def classify_work(label, category="", nature=""):
    """City-agnostic work classification.

    Cities disagree about where the real work type lives. Montreal encodes it in
    the permit type; Quebec City files a demolition as a 'Certificat
    d'autorisation' with the actual work in the category and description. So we
    check the descriptive fields first for the two classes we care about, then
    fall back to the permit type.
    """
    detail = norm(category) + " " + norm(nature)
    t = norm(label)

    if "demol" in detail or "demol" in t:
        return "demolition"
    if "nouveaubatiment" in detail or "nouvelleconstruction" in detail or "nouveaubatiment" in t:
        return "construction"
    if "transform" in detail or "agrandiss" in detail or "renov" in detail:
        return "transformation"
    if "construction" in t:
        return "construction"
    if "transform" in t or "renov" in t or "agrandiss" in t or "modif" in t:
        return "transformation"
    if "certificat" in t or "autorisation" in t:
        return "certificate"
    return "other"


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
    if city == "Montréal":
        codes = pick("type_code").fillna("").astype(str)
        mapped = codes.map(MONTREAL_CODE_LABELS)
        label = mapped.where(mapped.notna(), label)
    out["type_label"] = label.replace("", "Autre")
    out["work_class"] = [
        classify_work(lbl, cat, nat)
        for lbl, cat, nat in zip(out["type_label"], out["categorie"], out["nature"])
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
    horizon = latest_date(df) - timedelta(days=365 * TIMELINE_YEARS)
    hist = df[(df["address_key"].isin(keys)) & (df["date_emission"] >= horizon)]
    hist = hist.sort_values("date_emission")

    timelines = {}
    for key, group in hist.groupby("address_key"):
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


def build_leads(df):
    newest = latest_date(df)
    recent = df[df["date_emission"] >= newest - timedelta(days=LEADS_LOOKBACK_DAYS)]

    text = (recent["categorie"].fillna("") + " " +
            recent["type_batiment"].fillna("") + " " +
            recent["nature"].fillna("")).str.lower()
    cre = text.apply(lambda t: any(k.lower() in t for k in KEYWORDS_INCLUDE))

    construction_leads = recent[(recent["work_class"] == "construction") & cre]

    demo_window = df[df["date_emission"] >= newest - timedelta(days=DEMO_LOOKBACK_DAYS)]
    demolition_leads = demo_window[demo_window["work_class"] == "demolition"]

    leads = pd.concat([construction_leads, demolition_leads]).drop_duplicates(subset=["id_permis"])
    print(f"Lead candidates: {len(construction_leads)} construction + {len(demolition_leads)} demolition")

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

    records.sort(key=lambda x: (x["signal"]["rank"], x["date_emission"]))
    by_signal = {}
    for rec in records:
        by_signal[rec["signal"]["label"]] = by_signal.get(rec["signal"]["label"], 0) + 1
    print(f"Leads by signal: {by_signal}")
    return records


def build_all_permits(df):
    newest = latest_date(df)
    recent = df[(df["date_emission"] >= newest - timedelta(days=LEADS_LOOKBACK_DAYS)) &
                (df["work_class"].isin(["construction", "demolition"]))]
    recent = recent.sort_values("date_emission", ascending=False).head(500)
    print(f"All construction/demolition permits in window: {len(recent)}")
    out = recent.copy()
    out["date_emission"] = out["date_emission"].dt.strftime("%Y-%m-%d")
    out = out.where(pd.notna(out), None)
    return out[["city", "id_permis", "date_emission", "emplacement", "secteur",
                "categorie", "nature", "type_label", "work_class",
                "nb_logements", "entrepreneur", "cout"]].to_dict(orient="records")


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

    data = build_dashboard_data(df, leads, all_permits)
    with open(DASHBOARD_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"Dashboard data written to {DASHBOARD_DATA_FILE}")

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
