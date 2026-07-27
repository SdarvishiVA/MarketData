"""
Priority Lead Deep-Research Agent
For each priority lead, runs a multi-source investigation via the Claude API
with web search: developer announcements, council/borough decisions, urbanism
trackers, contractor (RBQ) and architect trails, LinkedIn/news mentions.
Also queries Agora MTL's public forum search directly for matching threads.
Returns a dossier per lead. Cached so each permit is researched once.
"""

import json
import os
import time
import requests

DATA_FILE = "docs/data.json"
CACHE_FILE = "enrichment_cache.json"

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"

AGORA_SEARCH_URL = "https://agoramtl.com/search.json"

PROMPT_TEMPLATE = """You are an investigative researcher for a commercial real estate lender. A construction permit was issued in Montreal. Your job: identify who is behind the project, using every public trail available.

PERMIT:
Address: {address}
Borough: {borough}
Building category: {category}
Nature of work: {nature}
Housing units: {logements}

INVESTIGATE THESE TRAILS (use multiple searches, in French AND English):
1. Direct: search the address + "projet" / "developpement" / "condos" / "logements" - developer announcements, project marketing sites, news.
2. Urbanism trackers: search the address or project on Agora MTL, Montreal urbanism forums, and local news (Journal Metro, La Presse, JDM).
3. Municipal decisions: search the address + "conseil d'arrondissement" / "PIIA" / "derogation" / "changement de zonage" - borough council decisions name applicants.
4. Professionals: search the address + "architecte" / "entrepreneur general" - architects and general contractors publicly attach themselves to projects (portfolios, RBQ licence mentions, press).
5. LinkedIn/social: search the address + "chantier" / "groundbreaking" - developers and PMs announce projects.

RULES:
- Names must come from sources explicitly tied to THIS address/project. Never guess or pattern-match from similar names.
- A general contractor or architect finding is valuable even if the owner isn't found - report them with their role.
- Prefer 1 confirmed name over 3 maybes.

Respond ONLY with a JSON object, no markdown fences:
{{
  "found": true or false,
  "owner_developer": "name or null",
  "general_contractor": "name or null",
  "architect": "name or null",
  "people": ["named individuals with roles, e.g. 'Jane Roy (VP, Groupe X)'"],
  "summary": "2-3 sentences on what you found and how confident you are",
  "next_step": "one concrete suggested action, e.g. 'Call GC Construction ABC (RBQ-licensed) and ask for owner intro' or 'Check borough council minutes of <date>'",
  "sources": ["urls, max 5"]
}}

If nothing reliable ties to this specific project, return found: false but still fill next_step with the best manual move."""


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def empty_intel():
    return {
        "found": False, "owner_developer": None, "general_contractor": None,
        "architect": None, "people": [], "summary": None, "next_step": None,
        "sources": [], "agora_threads": [],
    }


def search_agora(address):
    """Query Agora MTL's public Discourse search API for threads about this address.
    Returns a list of {title, url, excerpt} dicts, or [] on any failure."""
    query = " ".join(address.split()[:4])
    try:
        r = requests.get(
            AGORA_SEARCH_URL,
            params={"q": query},
            headers={"User-Agent": "VA-Capital-Research/1.0 (permit market research)"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        topics = data.get("topics", [])[:3]
        posts = {p.get("topic_id"): p.get("blurb", "") for p in data.get("posts", [])}
        return [
            {
                "title": t.get("title", ""),
                "url": f"https://agoramtl.com/t/{t.get('slug')}/{t.get('id')}",
                "excerpt": posts.get(t.get("id"), "")[:300],
            }
            for t in topics
        ]
    except Exception as e:
        print(f"  Agora search failed: {e}")
        return []


def enrich_lead(lead):
    agora_threads = search_agora(lead.get("emplacement", ""))
    agora_context = ""
    if agora_threads:
        agora_context = "\n\nAGORA MTL FORUM THREADS FOUND FOR THIS ADDRESS (read these pages first - they likely name the developer):\n"
        for t in agora_threads:
            agora_context += f"- {t['title']} - {t['url']}\n  Excerpt: {t['excerpt']}\n"

    prompt = PROMPT_TEMPLATE.format(
        address=lead.get("emplacement", ""),
        borough=lead.get("arrondissement", ""),
        category=lead.get("description_categorie_batiment", ""),
        nature=lead.get("nature_travaux", ""),
        logements=lead.get("nb_logements", ""),
    ) + agora_context

    response = requests.post(
        API_URL,
        headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
        },
        timeout=300,
    )
    response.raise_for_status()
    data = response.json()

    text = "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ).strip().replace("```json", "").replace("```", "").strip()

    result = empty_intel()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        try:
            parsed = json.loads(text[start:end + 1])
            result.update(parsed)
        except (json.JSONDecodeError, ValueError):
            pass

    result["agora_threads"] = agora_threads
    return result


def main():
    if not API_KEY:
        print("ANTHROPIC_API_KEY not set - skipping enrichment.")
        return

    if not os.path.exists(DATA_FILE):
        print(f"{DATA_FILE} not found - run fetch_permits.py first.")
        return

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    cache = load_cache()
    leads = data.get("priority_leads", [])
    researched = 0

    for lead in leads:
        permit_id = str(lead.get("id_permis"))

        if permit_id in cache:
            lead["intelligence"] = cache[permit_id]
            continue

        print(f"Investigating: {lead.get('emplacement')} ...")
        try:
            intel = enrich_lead(lead)
        except Exception as e:
            print(f"  Research failed: {e}")
            intel = empty_intel()

        lead["intelligence"] = intel
        cache[permit_id] = intel
        researched += 1

        who = intel.get("owner_developer") or intel.get("general_contractor") or intel.get("architect")
        print(f"  {'Found: ' + who if who else 'No direct hit - see next_step.'}")
        time.sleep(2)

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    save_cache(cache)
    print(f"Done. {researched} new investigation(s), {len(leads) - researched} from cache.")


if __name__ == "__main__":
    main()
