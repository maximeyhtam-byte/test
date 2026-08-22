#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
betclic_stats.py
Liste les matchs de football du jour sur Betclic.fr qui proposent l'onglet
"Stats équipes et joueurs" (id interne de catégorie : ca_ftb_prp).

Sortie : un fichier HTML cliquable + un CSV.

Usage :
    pip install requests
    python betclic_stats.py                 # matchs d'aujourd'hui
    python betclic_stats.py --jours 2       # aujourd'hui + demain
    python betclic_stats.py --sport tennis-stennis --categorie ca_tns_prp
"""

import argparse
import csv
import urllib.parse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://www.betclic.fr"

# Serveur local lance par serveur_projection.py. Un navigateur ne peut pas
# executer un script local depuis un lien : on passe donc par une URL http
# qui pointe vers ta propre machine.
PORT_PROJECTION = 8765
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Les pages Betclic sont rendues côté serveur (Nuxt) : tout l'état est dans le HTML.
RE_MATCH_HREF = re.compile(r'href="(?:https://www\.betclic\.fr)?(/[a-z0-9-]+-s[a-z0-9]+/[^"?]*?-m\d+)"')
RE_COMPET_HREF = re.compile(r'href="(?:https://www\.betclic\.fr)?(/[a-z0-9-]+-s[a-z0-9]+/[^"?/]*?-[cp]\d+)"')
RE_MATCH_HEAD = re.compile(
    r'"match":\{"matchId":"(?P<id>\d+)",'
    r'"name":"(?P<name>(?:[^"\\]|\\.)*)",'
    r'"matchDateUtc":"(?P<date>[^"]+)"')
RE_CATEGORIES = re.compile(r'"categories":\[(?P<cats>[^\]]*)\]')
RE_CAT_ITEM = re.compile(r'\{"id":"(?P<id>[^"]+)","name":"(?P<name>(?:[^"\\]|\\.)*)"\}')


def unescape(s: str) -> str:
    """Décode les échappements \\u002F, \\u0027... présents dans l'état Nuxt."""
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s.replace("\\u002F", "/")


def get(session, url, tries=3):
    for i in range(tries):
        try:
            r = session.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 429):
                time.sleep(3 * (i + 1))
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    return ""


def collect_match_urls(session, sport_path, crawl_competitions=True):
    """Récupère les URLs de match depuis la page sport, puis (option) chaque compétition."""
    html = get(session, f"{BASE}/{sport_path}")
    urls = {unescape(u) for u in RE_MATCH_HREF.findall(html)}
    if crawl_competitions:
        compets = {unescape(u) for u in RE_COMPET_HREF.findall(html)}
        print(f"  {len(compets)} compétitions détectées, exploration…", file=sys.stderr)
        for c in sorted(compets):
            h = get(session, BASE + c)
            urls |= {unescape(u) for u in RE_MATCH_HREF.findall(h)}
            time.sleep(0.3)  # on reste poli avec le serveur
    return sorted(urls)


def parse_match(html):
    """Extrait id, nom, date UTC et catégories (onglets) d'une page match."""
    m = RE_MATCH_HEAD.search(html)
    if not m:
        return None
    window = html[m.end():m.end() + 4000]
    cats = []
    cats_m = RE_CATEGORIES.search(window)
    if cats_m:
        cats = [(c.group("id"), unescape(c.group("name")))
                for c in RE_CAT_ITEM.finditer(cats_m.group("cats"))]
    return {
        "id": m.group("id"),
        "name": unescape(m.group("name")),
        "date_utc": m.group("date")[:19],
        "categories": cats,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="football-sfootball")
    ap.add_argument("--categorie", default="ca_ftb_prp",
                    help="id de l'onglet recherché (ca_ftb_prp = Stats équipes et joueurs)")
    ap.add_argument("--jours", type=int, default=1, help="1 = aujourd'hui seulement")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-competitions", action="store_true")
    ap.add_argument("--sortie", default="matchs_stats")
    args = ap.parse_args()

    session = requests.Session()
    print("Collecte des URLs de match…", file=sys.stderr)
    urls = collect_match_urls(session, args.sport, not args.no_competitions)
    print(f"  {len(urls)} matchs trouvés au total", file=sys.stderr)

    today = datetime.now(timezone.utc).astimezone()
    start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=args.jours)

    results = []

    def work(u):
        html = get(session, BASE + u)
        if not html:
            return None
        d = parse_match(html)
        if not d:
            return None
        d["url"] = BASE + u
        return d

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for d in ex.map(work, urls):
            if not d:
                continue
            dt = datetime.fromisoformat(d["date_utc"]).replace(tzinfo=timezone.utc).astimezone()
            if not (start <= dt < end):
                continue
            ids = [c[0] for c in d["categories"]]
            if args.categorie in ids:
                d["local_dt"] = dt
                results.append(d)

    results.sort(key=lambda x: x["local_dt"])
    print(f"  {len(results)} matchs avec l'onglet '{args.categorie}'", file=sys.stderr)

    with open(args.sortie + ".csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["date", "heure", "match", "url", "onglets"])
        for d in results:
            w.writerow([d["local_dt"].strftime("%Y-%m-%d"),
                        d["local_dt"].strftime("%H:%M"),
                        d["name"], d["url"],
                        " | ".join(n for _, n in d["categories"])])

    def ligne(d):
        proj = (f"http://127.0.0.1:{PORT_PROJECTION}/proj?match="
                + urllib.parse.quote(d["name"]))
        return (
            f'<tr><td>{d["local_dt"]:%d/%m %H:%M}</td>'
            f'<td><a href="{d["url"]}" target="_blank">{d["name"]}</a></td>'
            f'<td class="c">{len(d["categories"])} onglets</td>'
            f'<td class="act">'
            # Un navigateur n'autorise qu'UN seul window.open par clic : le
            # second est bloque silencieusement. On ouvre donc la projection
            # par script, et Betclic par la navigation normale du lien
            # (target=_blank), qui echappe au bloqueur de fenetres.
            f'<a class="deux" href="{d["url"]}" target="_blank" '
            f'onclick="window.open(\'{proj}\',\'_blank\')" '
            f'title="Ouvre Betclic et la projection">Betclic + projection</a>'
            f'<a class="seule" href="{proj}" target="_blank" '
            f'title="Projection seule">proj.</a></td></tr>')

    rows = "\n".join(ligne(d) for d in results)
    html_out = f"""<!doctype html><html lang="fr"><meta charset="utf-8">
<title>Matchs avec onglet Stats équipes et joueurs</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:820px;color:#111}}
h1{{font-size:1.25rem}} table{{border-collapse:collapse;width:100%}}
td,th{{padding:.5rem .6rem;border-bottom:1px solid #e5e5e5;text-align:left}}
a{{color:#0a58ca;text-decoration:none}} a:hover{{text-decoration:underline}}
.c{{color:#666;font-size:.85em}} .m{{color:#666;font-size:.85em;margin-bottom:1rem}}
td.act{{white-space:nowrap;text-align:right}}
a.deux{{display:inline-block;background:#0a58ca;color:#fff;padding:.25rem .7rem;
       border-radius:5px;font-size:.82em}}
a.deux:hover{{background:#0846a5;text-decoration:none}}
a.seule{{margin-left:.5rem;font-size:.82em;color:#666}}
.avert{{background:#fff6e5;border-left:3px solid #e0a800;padding:.7rem 1rem;
       border-radius:0 6px 6px 0;font-size:.88em;margin-bottom:1.2rem}}
</style>
<script>
// Rien a faire ici : l'ouverture double se joue dans l'attribut onclick du
// lien, pour contourner le bloqueur de fenetres.
</script>
<h1>Matchs avec l'onglet « Stats équipes et joueurs »</h1>
<p class="m">{len(results)} matchs — généré le {datetime.now():%d/%m/%Y %H:%M}</p>
<div class="avert"><b>Avant de cliquer sur « Betclic + projection »</b> :
lance <code>serveur_projection.py</code> et laisse sa fenêtre ouverte.
Sans lui, le lien de projection affichera une erreur de connexion.<br>
Si un seul des deux onglets s'ouvre, autorise les fenêtres surgissantes pour
cette page : l'icône apparaît à droite de la barre d'adresse.</div>
<table><tr><th>Coup d'envoi</th><th>Match</th><th></th><th></th></tr>
{rows}</table></html>"""
    with open(args.sortie + ".html", "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"→ {args.sortie}.html et {args.sortie}.csv générés", file=sys.stderr)


if __name__ == "__main__":
    main()