"""
Quebec Building Permit Market Intelligence + Lead Scanner
Pulls open building-permit data from multiple municipalities, normalizes it to a
common schema, aggregates market intelligence, flags commercial/CRE-relevant
permits as priority leads, and emails a digest.

Sources:
  - Ville de Montreal  (donnees.montreal.ca)   weekly refresh, has coordinates
  - Ville de Laval     (donneesquebec.ca)      daily refresh, has contractor name + cost
"""

import io
import json
import os
import smtplib
from datetime import datetime, timedelta

import pandas as pd
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

STATE_FILE = "seen_permits.json"
LEADS_FILE = "new_leads.csv"
DASHBOARD_DATA_FILE = "docs/data.json"

MONTREAL_CSV = "https://donnees.montreal.ca/dataset/d90eaf1b-2de8-43f0-923a-27a620ecdf41/resource/5232a72d-235a-48eb-ae20-bb9d501300ad/download/permis-construction.csv"
LAVAL_CSV = "https://www.donneesquebec.ca/recherche/dataset/c7808c42-e401-49f0-8049-df3c809d5982/resource/d4731ee2-b1e5-4a31-bc56-4e13115e74ef/download/permis-de-construction.csv"

# --- Tuning ---------------------------------------------------------------
MONTREAL_TYPES = ["CO"]          # CO = Construction. Add "TR" for transformations.
LAVAL_TYPE_KEYWORDS = ["construction", "nouveau", "nouvelle", "batiment", "bâtiment"]

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

LEADS_LOOKBACK_DAYS = 14
DASHBOARD_LOOKBACK_DAYS = 90
TREND_WEEKS = 12

TYPE_LABELS = {
    "CO": "Construction", "TR": "Transformation",
    "DE": "Démolition", "CA": "Certificat d'autorisation",
}

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT")

# Common normalized schema used across every city
COLUMNS = [
    "city", "id_permis", "date_emission", "emplacement", "secteur",
    "type_label", "categorie", "type_batiment", "nature",
    "nb_logements", "entrepreneur", "cout", "superficie",
    "latitude", "longitude", "is_construction",
]


# --- Loading --------------------------------------------------------------
def _download_csv(url, label):
    print(f"Downloading {label} dataset...")
    r = requests.get(url, timeout=600)
    r.raise_for_status()
    raw = r.content
    for enc in ("utf-8", "latin-1"):
        for sep in (",", ";"):
            try:
                df = pd.read_csv(io.BytesIO(raw), encoding=enc, sep=sep, low_memory=False)
                if df.shape[1] > 3:
                    print(f"  {label}: {len(df):,} rows, {df.shape[1]} columns (enc={enc}, sep='{sep}')")
                    return df
            except Exception:
                continue
    raise RuntimeError(f"Could not parse {label} CSV")


def load_montreal():
    df = _download_csv(MONTREAL_CSV, "Montreal")
    out = pd.DataFrame()
    out["id_permis"] = "MTL-" + df["id_permis"].astype(str)
    out["date_emission"] = pd.to_datetime(df["date_emission"], errors="coerce")
    out["emplacement"] = df["emplacement"].fillna("")
    out["secteur"] = df["arrondissement"].fillna("Non précisé")
    out["type_label"] = df["code_type_base_demande"].map(TYPE_LABELS).fillna("Autre")
    out["categorie"] = df["description_categorie_batiment"].fillna("Non précisé")
    out["type_batiment"] = df["description_type_batiment"].fillna("")
    out["nature"] = df["nature_travaux"].fillna("")
    out["nb_logements"] = pd.to_numeric(df["nb_logements"], errors="coerce").fillna(0)
    out["entrepreneur"] = ""
    out["cout"] = pd.NA
    out["superficie"] = pd.NA
    out["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    out["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    out["is_construction"] = df["code_type_base_demande"].isin(MONTREAL_TYPES)
    out["city"] = "Montréal"
    print(f"  Montreal newest permit: {out['date_emission'].max()}")
    return out[COLUMNS]


def load_laval():
    try:
        df = _download_csv(LAVAL_CSV, "Laval")
    except Exception as e:
        print(f"  Laval load failed ({e}) - continuing with Montreal only.")
        return pd.DataFrame(columns=COLUMNS)

    cols = {c.lower().strip(): c for c in df.columns}

    def col(name, default=None):
        return cols.get(name.lower())

    def series(name, fill=""):
        c = col(name)
        return df[c] if c else pd.Series([fill] * len(df), index=df.index)

    type_desc = series("Type_Permis_Description").fillna("").astype(str)

    out = pd.DataFrame()
    out["id_permis"] = "LAV-" + series("No_Identifiant", "").astype(str)
    out["date_emission"] = pd.to_datetime(series("Date_Emission"), errors="coerce")
    addr = series("Adresse").fillna("").astype(str)
    out["emplacement"] = addr
    out["secteur"] = series("ExVille_Descr").fillna("Laval").astype(str).replace("", "Laval")
    out["type_label"] = type_desc.replace("", "Autre")
    out["categorie"] = series("Categorie_Batiment").fillna("Non précisé").astype(str).replace("", "Non précisé")
    out["type_batiment"] = series("Type_Batiment").fillna("").astype(str)
    out["nature"] = type_desc
    out["nb_logements"] = pd.to_numeric(series("Nombre_Logements"), errors="coerce").fillna(0)
    out["entrepreneur"] = series("Entrepreneur").fillna("").astype(str)
    out["cout"] = pd.to_numeric(series("Cout_Permis"), errors="coerce")
    out["superficie"] = pd.to_numeric(series("Superficie_Pi_Carre"), errors="coerce")
    out["latitude"] = pd.NA        # Laval publishes no coordinates
    out["longitude"] = pd.NA
    out["is_construction"] = type_desc.str.lower().apply(
        lambda t: any(k in t for k in LAVAL_TYPE_KEYWORDS)
    )
    out["city"] = "Laval"

    print(f"  Laval newest permit: {out['date_emission'].max()}")
    print(f"  Laval permit types (top 12): {type_desc.value_counts().head(12).to_dict()}")
    print(f"  Laval rows flagged as construction: {int(out['is_construction'].sum()):,}")
    return out[COLUMNS]


def fetch_permits():
    frames = [load_montreal(), load_laval()]
    df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    print(f"Combined dataset: {len(df):,} rows across {df['city'].nunique()} cities")
    return df


def latest_date(df):
    """Anchor lookbacks to the newest permit in the data, not today - the
    published files lag behind real time."""
    return df["date_emission"].max()


# --- Lead building --------------------------------------------------------
def build_priority_leads(df):
    cutoff = latest_date(df) - timedelta(days=LEADS_LOOKBACK_DAYS)
    recent = df[(df["date_emission"] >= cutoff) & (df["is_construction"])]

    text = (
        recent["categorie"].fillna("") + " " +
        recent["type_batiment"].fillna("") + " " +
        recent["nature"].fillna("")
    ).str.lower()
    match = text.apply(lambda t: any(k.lower() in t for k in KEYWORDS_INCLUDE))

    result = recent[match]
    print(f"Priority leads: {len(result)} (window from {cutoff.date()})")
    print(f"  By city: {result['city'].value_counts().to_dict()}")
    return result


def build_all_leads(df):
    cutoff = latest_date(df) - timedelta(days=LEADS_LOOKBACK_DAYS)
    recent = df[(df["date_emission"] >= cutoff) & (df["is_construction"])]
    result = recent.sort_values("date_emission", ascending=False).head(400)
    print(f"All construction permits in window: {len(result)}")
    print(f"  By city: {result['city'].value_counts().to_dict()}")
    return result


def _records(frame):
    out = frame.copy()
    out["date_emission"] = out["date_emission"].dt.strftime("%Y-%m-%d")
    out = out.where(pd.notna(out), None)
    return out[[
        "city", "id_permis", "date_emission", "emplacement", "secteur",
        "categorie", "nature", "nb_logements", "entrepreneur", "cout", "superficie",
    ]].to_dict(orient="records")


def build_dashboard_data(df, priority_leads, all_leads):
    cutoff = latest_date(df) - timedelta(days=DASHBOARD_LOOKBACK_DAYS)
    window = df[df["date_emission"] >= cutoff].copy()
    print(f"Market window: {len(window):,} permits from {cutoff.date()}")

    window["week"] = window["date_emission"].dt.to_period("W").apply(
        lambda p: p.start_time.strftime("%Y-%m-%d"))
    trend = window.groupby(["week", "type_label"]).size().unstack(fill_value=0).tail(TREND_WEEKS)
    top_types = window["type_label"].value_counts().head(4).index.tolist()
    trend_series = {t: trend[t].tolist() for t in top_types if t in trend.columns}

    geo = window.dropna(subset=["latitude", "longitude"])
    geo_points = (
        geo[["latitude", "longitude", "secteur", "categorie", "emplacement", "nb_logements", "city"]]
        .head(2500)
        .rename(columns={
            "latitude": "lat", "longitude": "lng",
            "secteur": "borough", "categorie": "category", "emplacement": "address",
        })
        .to_dict(orient="records")
    )

    return {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_through": latest_date(df).strftime("%Y-%m-%d"),
        "window_days": DASHBOARD_LOOKBACK_DAYS,
        "leads_window_days": LEADS_LOOKBACK_DAYS,
        "cities": sorted(window["city"].dropna().unique().tolist()),
        "total_permits": int(len(window)),
        "total_housing_units": int(window["nb_logements"].fillna(0).sum()),
        "by_city": window["city"].value_counts().to_dict(),
        "by_type": window["type_label"].value_counts().head(8).to_dict(),
        "by_borough": window["secteur"].value_counts().head(20).to_dict(),
        "by_category": window["categorie"].value_counts().head(15).to_dict(),
        "trend_weeks": trend.index.tolist(),
        "trend_series": trend_series,
        "geo_points": geo_points,
        "geo_note": "Coordinates are published by Montréal only; Laval permits appear in the tables but not on the map.",
        "priority_leads": _records(priority_leads),
        "all_leads": _records(all_leads),
    }


# --- State + email --------------------------------------------------------
def load_seen_ids():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen_ids(ids):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(ids), f)


def send_email(new_leads):
    if not (EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECIPIENT):
        print("Email credentials not set - skipping notification.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{len(new_leads)} new permit lead(s) - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT

    rows = ""
    for _, r in new_leads.iterrows():
        rows += (
            f"<tr><td>{r['date_emission'].strftime('%Y-%m-%d')}</td>"
            f"<td>{r['city']}</td><td>{r['emplacement']}</td>"
            f"<td>{r['categorie']}</td><td>{r['nature']}</td>"
            f"<td>{r['entrepreneur'] or '-'}</td></tr>"
        )

    msg.attach(MIMEText(f"""
    <html><body><h2>New Quebec Permit Leads</h2>
    <table border="1" cellpadding="6" cellspacing="0">
      <tr><th>Issued</th><th>City</th><th>Address</th><th>Category</th><th>Nature</th><th>Contractor</th></tr>
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

    priority = build_priority_leads(df)
    all_leads = build_all_leads(df)
    new_leads = priority[~priority["id_permis"].isin(seen)]

    data = build_dashboard_data(df, priority, all_leads)
    with open(DASHBOARD_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"Dashboard data written to {DASHBOARD_DATA_FILE}")

    if not new_leads.empty:
        new_leads.to_csv(LEADS_FILE, index=False)
        print(f"{len(new_leads)} new priority lead(s) written to {LEADS_FILE}")
        send_email(new_leads)
    else:
        print("No new priority leads this run.")

    seen.update(priority["id_permis"].tolist())
    save_seen_ids(seen)


if __name__ == "__main__":
    main()
