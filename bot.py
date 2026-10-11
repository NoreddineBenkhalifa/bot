"""Surveille l'apparition de nouvelles séances et envoie une notification ntfy.

Configuration par variables d'environnement (ou config.json local, non versionné) :
  NTFY_TOPIC   canal ntfy.sh
  FILM_MATCH   regex sur le titre du film
  SITE_IDS     cinémas, séparés par des virgules
  ATTR_IDS     attributs de séance requis, séparés par des virgules
  EXCLUDE_IDS  attributs de séance exclus, séparés par des virgules

Usage : python bot.py         -> vérifie une fois et notifie si nouveau
        python bot.py --all   -> affiche toutes les séances correspondantes
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from curl_cffi import requests

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen.json"
CONFIG_FILE = HERE / "config.json"

SITE = "https://kinepolis.be/fr/"
API = "https://digital-api.kinepolis.be/ocapi/v1"


def load_config():
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    for k in ("NTFY_TOPIC", "FILM_MATCH", "SITE_IDS", "ATTR_IDS", "EXCLUDE_IDS"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    split = lambda v: {x.strip() for x in v.split(",") if x.strip()}
    return {
        "topic": cfg.get("NTFY_TOPIC", ""),
        "film": re.compile(cfg["FILM_MATCH"], re.I),
        "sites": sorted(split(cfg["SITE_IDS"])),
        "required": split(cfg.get("ATTR_IDS", "")),
        "excluded": split(cfg.get("EXCLUDE_IDS", "")),
    }


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


def fetch_showtimes(cfg):
    s = requests.Session(impersonate="chrome")
    html = s.get(SITE, timeout=30).text
    token = re.search(r'"authToken":"([^"]+)"', html).group(1)
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    def get(path, params=None):
        r = s.get(f"{API}/{path}", params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    films = {f["id"]: f["title"]["text"] for f in get("films")["films"] if cfg["film"].search(f["title"]["text"])}
    if not films:
        log("Attention : aucun film correspondant dans le catalogue.")
        return []

    def matches(attrs):
        attrs = set(attrs)
        return cfg["required"] <= attrs and not (cfg["excluded"] & attrs)

    dates = {}
    for day in get("film-screening-dates", [("siteIds", x) for x in cfg["sites"]])["filmScreeningDates"]:
        for fs in day["filmScreenings"]:
            if fs["filmId"] in films:
                for st in fs["sites"]:
                    if cfg["required"] <= set(st["showtimeAttributeIds"]):
                        dates.setdefault(day["businessDate"], set()).add(st["siteId"])

    found = []
    for date, date_sites in sorted(dates.items()):
        data = get(f"showtimes/by-business-date/{date}", [("siteIds", x) for x in sorted(date_sites)])
        site_names = {x["id"]: x["name"]["text"] for x in data["relatedData"].get("sites", [])}
        for st in data["showtimes"]:
            if st["filmId"] in films and matches(st["attributeIds"]):
                found.append({
                    "id": st["id"],
                    "film": films[st["filmId"]],
                    "cinema": site_names.get(st["siteId"], st["siteId"]),
                    "start": st["schedule"]["filmStartsAt"],
                })
    found.sort(key=lambda x: (x["start"], x["cinema"]))
    return found


def key(st):
    # Identifiants hachés : seen.json est public
    return hashlib.sha256(st["id"].encode()).hexdigest()[:16]


def fmt(st):
    return f"{datetime.fromisoformat(st['start']):%a %d/%m %H:%M} - {st['cinema']} ({st['film']})"


def notify(topic, title, body):
    if topic:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                      headers={"Title": title.encode("utf-8"), "Priority": "high", "Tags": "film_projector",
                               "Click": SITE}, timeout=30)


def main():
    cfg = load_config()
    try:
        found = fetch_showtimes(cfg)
    except Exception as e:
        log(f"Erreur : {type(e).__name__}")
        sys.exit(1)

    if "--all" in sys.argv:
        print(f"{len(found)} séance(s) :")
        for st in found:
            print("  " + fmt(st))
        return

    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    new = [st for st in found if key(st) not in seen]
    if new:
        lines = [fmt(st) for st in new]
        body = "\n".join(lines[:5]) + (f"\n... et {len(lines) - 5} autres" if len(lines) > 5 else "")
        notify(cfg["topic"], f"{len(new)} nouvelle(s) séance(s) !", body)
        SEEN_FILE.write_text(json.dumps(sorted(seen | {key(st) for st in found})))
        log(f"{len(new)} nouvelle(s) séance(s), notification envoyée.")
    else:
        log(f"Rien de nouveau ({len(found)} connue(s)).")


if __name__ == "__main__":
    main()
