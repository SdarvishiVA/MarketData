"""
Source discovery helper.

Searches Donnees Quebec (CKAN) for municipal permit datasets, then probes each
candidate CSV for its real column headers and prints a ready-to-paste SOURCES
entry for fetch_permits.py.

Run it from the Actions tab ("Discover permit sources" workflow). It only reads
public metadata and the first few KB of each file - nothing is committed.
"""

import csv
import io
import re
import sys
import unicodedata

import requests

CKAN = "https://www.donneesquebec.ca/recherche/api/3/action"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json,text/csv,*/*",
}

QUERIES = [
    "permis construction",
    "permis de construction",
    "permis batiment",
    "permis delivres",
    "construction demolition permis",
]

# Fields we want, and the header names commonly used for each across cities.
FIELD_HINTS = {
    "id":            ["no_permis", "numero_permis", "id_permis", "no_dossier", "permis"],
    "date":          ["date_emission", "date_delivrance", "date_permis", "emission", "date"],
    "start_date":    ["date_debut", "debut_travaux"],
    "address":       ["adresse", "adresse_travaux", "emplacement", "localisation"],
    "sector":        ["arrondissement", "secteur", "quartier", "district"],
    "type_label":    ["type_permis", "type", "nature_permis", "categorie_permis"],
    "category":      ["domaine", "categorie", "categorie_batiment", "usage"],
    "building_type": ["type_batiment", "genre_batiment"],
    "nature":        ["raison", "nature_travaux", "description", "objet", "travaux"],
    "units":         ["nb_logements", "nombre_logements", "logements"],
    "contractor":    ["entrepreneur", "requerant", "demandeur", "constructeur"],
    "cost":          ["cout", "cout_permis", "valeur", "valeur_travaux", "cout_travaux"],
    "area":          ["superficie", "sup_ca", "surface"],
    "lat":           ["latitude", "lat"],
    "lng":           ["longitude", "long", "lon"],
}


def norm(t):
    t = unicodedata.normalize("NFKD", str(t))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return "".join(c for c in t.lower() if c.isalnum())


def search_packages():
    seen, found = set(), []
    for q in QUERIES:
        try:
            r = requests.get(f"{CKAN}/package_search",
                             params={"q": q, "rows": 100},
                             headers=HEADERS, timeout=60)
            r.raise_for_status()
            results = r.json().get("result", {}).get("results", [])
        except Exception as e:
            print(f"  search '{q}' failed: {e}")
            continue
        for pkg in results:
            if pkg["id"] in seen:
                continue
            seen.add(pkg["id"])
            found.append(pkg)
    return found


def looks_like_permits(pkg):
    text = norm(pkg.get("title", "") + " " + pkg.get("notes", "") or "")
    if "permis" not in text:
        return False
    # Exclude non-building permit registries that also use the word "permis"
    for bad in ["alcool", "vehicule", "chasse", "peche", "conduire", "stationnementresident"]:
        if bad in text:
            return False
    return True


def csv_resources(pkg):
    out = []
    for res in pkg.get("resources", []):
        fmt = (res.get("format") or "").upper()
        url = res.get("url") or ""
        if fmt == "CSV" or url.lower().endswith(".csv"):
            out.append(res)
    return out


def probe_headers(url, limit_bytes=200_000):
    """Download just enough of the file to read the header row."""
    try:
        with requests.get(url, headers=HEADERS, stream=True, timeout=120) as r:
            r.raise_for_status()
            buf = b""
            for chunk in r.iter_content(chunk_size=16384):
                buf += chunk
                if len(buf) >= limit_bytes or b"\n" in buf:
                    break
    except Exception as e:
        return None, f"download failed: {e}"

    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = buf.decode(enc, errors="strict")
            break
        except Exception:
            continue
    else:
        text = buf.decode("latin-1", errors="replace")

    first = text.splitlines()[0] if text.splitlines() else ""
    if not first:
        return None, "empty file"
    sep = max([",", ";", "\t"], key=lambda s: first.count(s))
    cols = next(csv.reader(io.StringIO(first), delimiter=sep), [])
    cols = [c.strip() for c in cols if c.strip()]
    return (cols, sep) if cols else (None, "no header row")


def suggest_mapping(cols):
    lookup = {norm(c): c for c in cols}
    mapping, unmatched = {}, []
    for field, hints in FIELD_HINTS.items():
        hit = None
        for h in hints:
            if norm(h) in lookup:
                hit = lookup[norm(h)]
                break
        if not hit:                       # fall back to substring match
            for key, original in lookup.items():
                if any(norm(h) in key for h in hints):
                    hit = original
                    break
        if hit:
            mapping[field] = hit
        else:
            unmatched.append(field)
    return mapping, unmatched


def main():
    print("Searching Donnees Quebec for municipal permit datasets...\n")
    packages = [p for p in search_packages() if looks_like_permits(p)]
    print(f"{len(packages)} candidate dataset(s) matched.\n")
    print("=" * 78)

    for pkg in packages:
        org = (pkg.get("organization") or {}).get("title", "unknown publisher")
        resources = csv_resources(pkg)
        if not resources:
            continue

        print(f"\nPUBLISHER : {org}")
        print(f"DATASET   : {pkg.get('title')}")
        print(f"PAGE      : https://www.donneesquebec.ca/recherche/dataset/{pkg.get('name')}")

        for res in resources[:2]:
            print(f"\n  resource_id : {res.get('id')}")
            print(f"  file        : {res.get('name')}")
            cols, note = probe_headers(res.get("url", ""))
            if not cols:
                print(f"  columns     : could not read ({note})")
                continue
            print(f"  separator   : '{note}'")
            print(f"  columns     : {cols}")

            mapping, unmatched = suggest_mapping(cols)
            city = re.sub(r"^(ville|municipalite|municipalité)\s+(de|d')\s*", "", org, flags=re.I).strip()

            print("\n  --- suggested SOURCES entry (verify before use) ---")
            print("    {")
            print(f'        "city": "{city}",')
            print('        "kind": "ckan",')
            print(f'        "resource_id": "{res.get("id")}",')
            print('        "fields": {')
            for field, col in mapping.items():
                print(f'            "{field}": ["{col}"],')
            print("        },")
            print("    },")
            if unmatched:
                print(f"  fields with no obvious column: {unmatched}")
        print("\n" + "-" * 78)

    print("\nDone. Copy a suggested entry into SOURCES in fetch_permits.py,")
    print("then run the permit scan and check the log for 'unmapped fields'.")


if __name__ == "__main__":
    sys.exit(main())
