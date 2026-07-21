"""
Montreal Building Permit Market Intelligence + Lead Scanner
Pulls the City of Montreal's open building permit dataset,
aggregates market intelligence across every available dimension,
flags NEW commercial/CRE-relevant permits, and emails a digest.
"""

import pandas as pd
import json
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

CSV_URL = "https://donnees.montreal.ca/dataset/d90eaf1b-2de8-43f0-923a-27a620ecdf41/resource/5232a72d-235a-48eb-ae20-bb9d501300ad/download/permis-construction.csv"
STATE_FILE = "seen_permits.json"
LEADS_FILE = "new_leads.csv"
DASHBOARD_DATA_FILE = "docs/data.json"

RELEVANT_PERMIT_TYPES = ["CO"]  # CO = Construction. Add "TR" for renovations if you want.

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

LEADS_LOOKBACK_DAYS = 14      # window for the actionable leads list
DASHBOARD_LOOKBACK_DAYS = 90  # window for market-intelligence aggregates
TREND_WEEKS = 12              # number of weeks shown in the trend chart

TYPE_LABELS = {"CO": "Construction", "TR": "Transformation", "DE": "Démolition", "CA": "Certificat d'autorisation"}

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT")


def load_seen_ids():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen_ids(ids):
    with open(STATE_FILE, "w") as f:
        json.dump(list(ids), f)


def fetch_permits():
    print("Downloading permit dataset (this can take a minute - file is ~175 MB)...")
    df = pd.read_csv(CSV_URL, low_memory=False)
    df["date_emission"] = pd.to_datetime(df["date_emission"], errors="coerce")
    return df


def build_priority_leads(df):
    cutoff = datetime.now() - timedelta(days=LEADS_LOOKBACK_DAYS)
    recent = df[df["date_emission"] >= cutoff]

    type_match = recent["code_type_base_demande"].isin(RELEVANT_PERMIT_TYPES)
    text_cols = (
        recent["description_categorie_batiment"].fillna("") + " " +
        recent["description_type_batiment"].fillna("") + " " +
        recent["nature_travaux"].fillna("")
    ).str.lower()
    keyword_match = text_cols.apply(lambda t: any(k.lower() in t for k in KEYWORDS_INCLUDE))

    return recent[type_match & keyword_match]


def build_all_leads(df):
    """Same time window and permit type as priority leads, but no keyword filter.
    This is the full, unfiltered picture of construction activity in the window."""
    cutoff = datetime.now() - timedelta(days=LEADS_LOOKBACK_DAYS)
    recent = df[df["date_emission"] >= cutoff]
    type_match = recent["code_type_base_demande"].isin(RELEVANT_PERMIT_TYPES)
    return recent[type_match].sort_values("date_emission", ascending=False).head(300)


def build_dashboard_data(df, priority_leads, all_leads):
    cutoff = datetime.now() - timedelta(days=DASHBOARD_LOOKBACK_DAYS)
    window = df[df["date_emission"] >= cutoff].copy()

    by_type = (
        window["code_type_base_demande"].map(TYPE_LABELS).fillna("Autre")
        .value_counts().to_dict()
    )

    by_borough = (
        window["arrondissement"].fillna("Non précisé")
        .value_counts().head(20).to_dict()
    )

    by_category = (
        window["description_categorie_batiment"].fillna("Non précisé")
        .value_counts().head(15).to_dict()
    )

    total_housing_units = int(window["nb_logements"].fillna(0).sum())

    window["week"] = window["date_emission"].dt.to_period("W").apply(lambda p: p.start_time.strftime("%Y-%m-%d"))
    trend = (
        window.groupby(["week", "code_type_base_demande"]).size()
        .unstack(fill_value=0).tail(TREND_WEEKS)
    )
    trend_weeks = trend.index.tolist()
    trend_series = {TYPE_LABELS.get(c, c): trend[c].tolist() for c in trend.columns}

    geo_points = (
        window.dropna(subset=["latitude", "longitude"])
        [["latitude", "longitude", "arrondissement", "description_categorie_batiment", "emplacement"]]
        .head(2000)
        .rename(columns={
            "latitude": "lat", "longitude": "lng",
            "arrondissement": "borough", "description_categorie_batiment": "category",
            "emplacement": "address"
        })
        .to_dict(orient="records")
    )

    cols = [
        "id_permis", "date_emission", "emplacement", "arrondissement",
        "description_type_demande", "description_categorie_batiment",
        "nature_travaux", "nb_logements"
    ]

    priority_out = priority_leads[cols].copy()
    priority_out["date_emission"] = priority_out["date_emission"].dt.strftime("%Y-%m-%d")

    all_out = all_leads[cols].copy()
    all_out["date_emission"] = all_out["date_emission"].dt.strftime("%Y-%m-%d")

    return {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "window_days": DASHBOARD_LOOKBACK_DAYS,
        "leads_window_days": LEADS_LOOKBACK_DAYS,
        "total_permits": int(len(window)),
        "total_housing_units": total_housing_units,
        "by_type": by_type,
        "by_borough": by_borough,
        "by_category": by_category,
        "trend_weeks": trend_weeks,
        "trend_series": trend_series,
        "geo_points": geo_points,
        "priority_leads": priority_out.to_dict(orient="records"),
        "all_leads": all_out.to_dict(orient="records"),
    }


def send_email(new_leads_df):
    if not (EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECIPIENT):
        print("Email credentials not set - skipping notification.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{len(new_leads_df)} new permit lead(s) - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT

    rows_html = ""
    for _, row in new_leads_df.iterrows():
        rows_html += f"""
        <tr>
            <td>{row['date_emission'].strftime('%Y-%m-%d')}</td>
            <td>{row['emplacement']}</td>
            <td>{row['arrondissement']}</td>
            <td>{row['description_categorie_batiment']}</td>
            <td>{row['nature_travaux']}</td>
        </tr>"""

    html = f"""
    <html><body>
    <h2>New Montreal Permit Leads</h2>
    <table border="1" cellpadding="6" cellspacing="0">
        <tr><th>Date Issued</th><th>Address</th><th>Borough</th><th>Category</th><th>Nature of Work</th></tr>
        {rows_html}
    </table>
    </body></html>
    """
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
    print("Email sent.")


def main():
    os.makedirs("docs", exist_ok=True)
    seen = load_seen_ids()
    df = fetch_permits()

    priority_leads = build_priority_leads(df)
    all_leads = build_all_leads(df)
    new_leads = priority_leads[~priority_leads["id_permis"].isin(seen)]

    dashboard_data = build_dashboard_data(df, priority_leads, all_leads)
    with open(DASHBOARD_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(dashboard_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"Dashboard data written to {DASHBOARD_DATA_FILE}")

    if not new_leads.empty:
        cols = [
            "id_permis", "date_emission", "emplacement", "arrondissement",
            "description_type_demande", "description_categorie_batiment",
            "nature_travaux", "nb_logements"
        ]
        new_leads[cols].to_csv(LEADS_FILE, index=False)
        print(f"{len(new_leads)} new priority lead(s) written to {LEADS_FILE}")
        send_email(new_leads)
    else:
        print("No new priority leads this run.")

    seen.update(priority_leads["id_permis"].tolist())
    save_seen_ids(seen)


if __name__ == "__main__":
    main()
