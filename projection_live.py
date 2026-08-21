"""
PROJECTION EN DIRECT

Tu donnes un match, le script va chercher les statistiques de 1re mi-temps sur
MatchEnDirect, applique les regles de regles_exploitables.csv, et te sort une
projection de 2e mi-temps a comparer aux lignes du bookmaker.

Prerequis :
    pip install requests beautifulsoup4 pandas lxml
    regles_exploitables.csv dans le meme dossier
      (produit par recherche_seuils.py puis regles_exploitables.py)

Usage :
    python projection_live.py
      -> colle une URL MatchEnDirect, ou tape un nom d'equipe

IMPORTANT : les regles sont REDONDANTES. Possession, passes et pourcentage de
passes mesurent la meme chose. Le script regroupe donc les regles par FAMILLE
et ne retient que la plus forte de chaque famille, au lieu de les additionner.
"""

import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta

import webbrowser

import numpy as np

import pandas as pd
import requests
from bs4 import BeautifulSoup

FICHIER_REGLES = "regles_exploitables.csv"
FICHIER_BASES = "bases_equipes.csv"

# =====================================================================
#  VALIDATION PAR LE BACKTEST
#  A mettre a jour depuis la section "Par marche" de backtest_etoiles.py
#  (bloc "un seul pari par match"). Le calcul d'EV suppose la projection
#  juste ; ces chiffres disent sur QUELS marches cette hypothese tient.
#  Valeurs du 20/08/2026, 5065 matchs, test en aveugle sur 2533.
# =====================================================================
FICHIER_VALIDATION = "validation_marches.csv"   # ecrit par backtest_etoiles.py
FICHIER_JOURNAL = "paris_journal.csv"           # ecrit par tracker.py
BANKROLL_INITIALE = 100.0                       # doit correspondre a tracker.py


def bankroll_courante():
    """
    Lit le journal du tracker pour pre-remplir la bankroll : les mises Kelly
    sont ainsi calculees sur le capital reel, pas sur une valeur figee.
    Renvoie (bankroll, engage, nb_regles, message).
    """
    if not os.path.exists(FICHIER_JOURNAL):
        return (BANKROLL_INITIALE, 0.0, 0,
                f"{FICHIER_JOURNAL} absent, bankroll par defaut")
    try:
        j = pd.read_csv(FICHIER_JOURNAL, encoding="utf-8-sig")
        if "gain" not in j.columns or "statut" not in j.columns:
            return BANKROLL_INITIALE, 0.0, 0, "journal illisible"
        regles = j[j.statut.astype(str) == "regle"]
        en_cours = j[j.statut.astype(str) == "en cours"]
        gains = float(pd.to_numeric(regles.gain, errors="coerce").fillna(0).sum())
        engage = float(pd.to_numeric(en_cours.mise, errors="coerce").fillna(0).sum())
        br = BANKROLL_INITIALE + gains
        msg = (f"lue depuis {FICHIER_JOURNAL} : {len(regles)} pari(s) regle(s), "
               f"{gains:+.2f} EUR")
        if engage:
            msg += f", {engage:.2f} EUR engages sur {len(en_cours)} pari(s)"
        return br, engage, len(regles), msg
    except Exception as e:
        return BANKROLL_INITIALE, 0.0, 0, f"journal illisible ({e})"

ETOILES_MIN = 3          # en dessous, l'ecart au temoin devient marginal

# AUCUNE valeur en dur ici. Des chiffres figes donnaient l'illusion d'une
# validation a jour et masquaient l'absence du fichier : on prefere afficher
# "non teste" partout plutot qu'un ecart peut-etre perime.
VALIDATION = {}          # (marche, cote) -> (ecart au temoin, nb de paris)

ECART_MINIMUM_VALIDE = 3.0   # points d'ecart au temoin exiges
INFO_VALIDATION = "AUCUNE validation chargee"
VALIDATION_CELLULE = {}      # (marche, cote, etoiles) -> (ecart, n)
N_MIN_CELLULE = 40           # en dessous, on retombe sur la moyenne du marche


def ecart_cellule(quoi, qui, etoiles):
    """
    Ecart au temoin de la CELLULE (marche x etoiles), qui est la seule
    quantite comparable d'une carte a l'autre. Repli sur la moyenne du
    marche si la cellule est trop peu fournie.
    """
    e_c = VALIDATION_CELLULE.get((quoi, qui, etoiles))
    if e_c and e_c[1] >= N_MIN_CELLULE:
        return e_c[0], e_c[1], "cellule"
    e_m, n_m = VALIDATION.get((quoi, qui), (None, 0))
    return e_m, n_m, "marche"


def charger_validation():
    """
    Relit validation_marches.csv s'il existe : les chiffres ci-dessus sont
    alors remplaces par ceux du dernier backtest. Le dataset grossit, les
    marches valides changent - autant que ce soit automatique.
    """
    global VALIDATION, VALIDATION_CELLULE, ETOILES_MIN, INFO_VALIDATION
    if not os.path.exists(FICHIER_VALIDATION):
        INFO_VALIDATION = (f"{FICHIER_VALIDATION} INTROUVABLE - aucun marche "
                           f"ne peut etre valide")
        return False
    try:
        v = pd.read_csv(FICHIER_VALIDATION, encoding="utf-8-sig")
        if not len(v):
            return False
        # deux niveaux : la cellule (marche, etoiles) et la moyenne du marche
        VALIDATION, VALIDATION_CELLULE = {}, {}
        for _, l in v.iterrows():
            cle = (str(l.quoi), str(l.qui))
            e, n = float(l.ecart_points), int(l.n_paris)
            niv = int(l.etoiles) if "etoiles" in v.columns else -1
            if niv < 0:
                VALIDATION[cle] = (e, n)
            else:
                VALIDATION_CELLULE[cle + (niv,)] = (e, n)
        if not VALIDATION:      # ancien format sans colonne etoiles
            for (q, w, _), (e, n) in VALIDATION_CELLULE.items():
                VALIDATION.setdefault((q, w), (e, n))
        if "etoiles_min" in v.columns:
            ETOILES_MIN = int(v.etoiles_min.iloc[0])
        p0 = v.iloc[0]
        INFO_VALIDATION = (
            f"backtest du {p0.get('genere_le', '?')} sur "
            f"{p0.get('n_matchs_dataset', '?')} matchs"
            + (f", test {p0['periode_test']}" if p0.get("periode_test") else "")
            + f", seuil {ETOILES_MIN} etoiles")
        return True
    except Exception as e:
        print(f"  ({FICHIER_VALIDATION} illisible : {e})")
        return False


def verdict_marche(quoi, qui, etoiles):
    """Faut-il faire confiance a l'EV calculee sur cette carte ?"""
    ecart, n, source = ecart_cellule(quoi, qui, etoiles)
    detail = (f"{etoiles} etoiles sur ce marche" if source == "cellule"
              else "moyenne du marche, cellule trop peu fournie")
    if qui == "total" and (ecart is None or n < 30):
        return ("prudence", "Marche total non teste par le backtest. Relance "
                "backtest_etoiles.py : il valide desormais les totaux.", None)
    # Zero etoile = aucune regle declenchee : la projection vaut la base de
    # l'affiche, sans signal de jeu. L'ecart du backtest porte sur des paris a
    # une etoile ou plus, il ne dit rien de ce cas. Categorie a part.
    if etoiles == 0:
        return ("aucun", "Aucune regle declenchee : la projection est la base "
                "de l'affiche, sans signal de jeu. Les mesures du backtest "
                "portent sur des paris a une etoile au moins et ne "
                "s'appliquent pas ici.", ecart)
    if ecart is None or n < 30:
        return ("rejet", f"Marche non valide : seulement {n} pari(s) testes, "
                f"echantillon insuffisant pour conclure.", ecart)
    # On distingue le PERDANT du POSITIF MAIS TROP FAIBLE : annoncer
    # "non rentable" a cote d'un +1.6 se contredisait tout seul.
    if ecart < 0:
        return ("rejet", f"PERDANT au backtest : {ecart:+.1f} points vs "
                f"strategie naive, sur {n} paris ({detail}). S'abstenir.", ecart)
    if ecart < ECART_MINIMUM_VALIDE:
        return ("insuffisant", f"Avantage reel mais trop mince : {ecart:+.1f} "
                f"points sur {n} paris ({detail}), alors qu'il en faut "
                f"{ECART_MINIMUM_VALIDE:.1f} pour couvrir la marge du "
                f"bookmaker.", ecart)
    if etoiles < ETOILES_MIN:
        return ("faible", f"{ecart:+.1f} pts sur {n} paris ({detail}), mais "
                f"{etoiles} etoile(s) : sous le seuil de {ETOILES_MIN}, "
                f"l'avantage devient marginal.", ecart)
    return ("ok", f"Valide par le backtest : {ecart:+.1f} points vs strategie "
            f"naive, sur {n} paris ({detail}).", ecart)
PARSER = "lxml"
BASE = "https://www.matchendirect.fr"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; projection-perso/1.0)"}

LIBELLES = {
    "possession": "poss", "tirs": "tirs", "tirs cadres": "cadres",
    "tirs non cadres": "non_cadres", "tirs bloques": "bloques",
    "poteau": "poteau", "occasions manquees": "occ_manquees",
    "xg (buts attendus)": "xg",
    "ballons touches dans la surface adverse": "touches_surface",
    "corners": "corners", "hors-jeu": "horsjeu",
    "rentree de touche": "touches_ligne", "fautes": "fautes",
    "cartons janues": "jaunes", "cartons jaunes": "jaunes",
    "cartons rouges": "rouges", "passes": "passes",
    "passes reussis": "passes_reussies", "passes reussis (%)": "pct_passes",
    "centres": "centres", "centres reussis": "centres_reussis",
    "duels reussis": "duels_gagnes", "tacles reussis": "tacles",
    "duels aeriens reussis": "duels_aeriens", "dribbles reussis": "dribbles",
    "interceptions": "interceptions", "degagements": "degagements",
}

# regroupement des variables par famille : une famille = une information
FAMILLES = {
    "Domination (ballon)": ["poss", "passes", "passes_reussies", "pct_passes",
                            "tirs_p100passes", "touchsurf_p100passes"],
    "Penetration (surface)": ["touches_surface", "centres", "centres_reussis",
                              "corners"],
    "Injustice (xG vs score)": ["injustice", "xg_ecart", "xg"],
    "Volume deja produit": ["tirs", "cadres", "non_cadres", "bloques"],
    "Defense / duels": ["degagements", "interceptions", "tacles", "duels_gagnes",
                        "fautes", "horsjeu", "touches_ligne"],
    "Contexte": ["affluence", "ecart", "score", "buts"],
}

LIBELLE_CIBLE = {
    "tirs 2eMT (total)": ("tirs", "total"),
    "tirs 2eMT (domicile)": ("tirs", "domicile"),
    "tirs 2eMT (exterieur)": ("tirs", "exterieur"),
    "tirs cadres 2eMT (total)": ("cadres", "total"),
    "tirs cadres 2eMT (domicile)": ("cadres", "domicile"),
    "tirs cadres 2eMT (exterieur)": ("cadres", "exterieur"),
    "buts 2eMT (total)": ("buts", "total"),
    "buts 2eMT (domicile)": ("buts", "domicile"),
    "buts 2eMT (exterieur)": ("buts", "exterieur"),
}
ORDRE = list(LIBELLE_CIBLE)


def qui_total(cible):
    return "(total)" in cible

# correspondance libelle -> colonne, pour retrouver les bases par equipe
CIBLE_COLONNE = {
    "tirs 2eMT (total)": "smt_tirs_tot", "tirs 2eMT (domicile)": "smt_tirs_dom",
    "tirs 2eMT (exterieur)": "smt_tirs_ext",
    "tirs cadres 2eMT (total)": "smt_cadres_tot",
    "tirs cadres 2eMT (domicile)": "smt_cadres_dom",
    "tirs cadres 2eMT (exterieur)": "smt_cadres_ext",
    "buts 2eMT (total)": "smt_buts_tot", "buts 2eMT (domicile)": "smt_buts_dom",
    "buts 2eMT (exterieur)": "smt_buts_ext",
}


def libelle_condition(condition, dom, ext):
    """Rend la condition lisible : noms d'equipes au lieu de dom/ext."""
    c = str(condition)
    c = re.sub(r"\binjustice\b", f"injustice (en faveur de {dom})", c)
    c = re.sub(r"\bdom\b", dom, c)
    c = re.sub(r"\bext\b", ext, c)
    return c


def texte_injustice(v, dom, ext):
    if v is None:
        return ""
    if v > 0.3:
        return (f"+{v:.2f} : {dom} a cree plus que son score ne l'indique "
                f"(merite mieux)")
    if v < -0.3:
        return (f"{v:.2f} : {dom} a un score meilleur que son jeu "
                f"(c'est {ext} qui merite mieux)")
    return f"{v:+.2f} : score conforme au jeu produit"


def sans_accents(t):
    n = unicodedata.normalize("NFKD", t)
    return "".join(c for c in n if not unicodedata.combining(c)).lower().strip()


def nombre(t):
    t = t.replace("%", "").replace(",", ".").strip()
    if not re.fullmatch(r"-?\d+(\.\d+)?", t):
        return None
    return float(t) if "." in t else int(t)


# =====================================================================
#  Recuperation du match
# =====================================================================

# Abreviations courantes : le slug MED n'utilise pas toujours le nom usuel.
ALIAS = {
    "psg": "paris sg", "om": "marseille", "ol": "lyon", "asse": "saint etienne",
    "ogc": "nice", "losc": "lille", "rcsa": "strasbourg", "fcn": "nantes",
    "atleti": "atletico madrid", "barca": "barcelone", "fcb": "barcelone",
    "juve": "juventus", "milan ac": "milan", "inter milan": "inter",
    "mu": "manchester united", "man utd": "manchester united",
    "mufc": "manchester united", "mcfc": "manchester city",
    "spurs": "tottenham", "gunners": "arsenal", "bvb": "dortmund",
    "fcb munich": "bayern munich", "bayer": "leverkusen",
    "atm": "atletico madrid", "rm": "real madrid",
}


def deplier(terme):
    """Remplace une abreviation connue par le nom present dans les URLs."""
    t = sans_accents(terme)
    if t in ALIAS:
        return ALIAS[t]
    for court, long in ALIAS.items():
        if t.startswith(court + " ") or t == court:
            return t.replace(court, long, 1)
    return terme


def mots(t):
    """Decoupe en mots comparables, sans accents ni ponctuation."""
    return [x for x in re.split(r"[^a-z0-9]+", sans_accents(str(t))) if len(x) >= 2]


def correspond(terme, slug):
    """
    Chaque mot saisi doit correspondre au DEBUT d'un mot du slug.
    'atl madrid' trouve 'atletico-madrid-getafe' : atl -> atletico, madrid -> madrid.
    Une comparaison par sous-chaine echouerait, 'atl-madrid' n'y figurant pas.
    """
    tt, ts = mots(terme), mots(slug)
    if not tt or not ts:
        return False
    return all(any(s.startswith(t) or (len(t) >= 4 and t.startswith(s))
                   for s in ts) for t in tt)


def joli(slug):
    return " ".join(w.capitalize() for w in slug.split("-"))


def chercher_match(terme, jours=(0, -1, 1, -2, 2)):
    """Cherche une equipe dans les resultats des jours proches."""
    d = deplier(terme)
    if d != terme:
        print(f"  ('{terme}' interprete comme '{d}')")
        terme = d
    candidats, tous = [], []
    for delta in jours:
        jour = (datetime.now() + timedelta(days=delta)).strftime("%d-%m-%Y")
        try:
            html = requests.get(f"{BASE}/resultat-foot-{jour}/",
                                headers=HEADERS, timeout=30).text
        except Exception as e:
            print(f"  page du {jour} inaccessible : {e}")
            continue
        soup = BeautifulSoup(html, PARSER)
        vus = set()
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if "/live-score/" not in h and "/foot-score/" not in h:
                continue
            url = ((BASE + h) if h.startswith("/") else h).split("?")[0]
            if url in vus:
                continue
            vus.add(url)
            slug = url.split("/")[-1].split("_")[0].replace(".html", "")
            tous.append((url, jour, slug))
            if correspond(terme, slug) and url not in [c[0] for c in candidats]:
                candidats.append((url, jour, slug))
        time.sleep(1.0)

    # repli : au moins un mot en commun, pour proposer quelque chose
    if not candidats:
        partiels = []
        tt = mots(terme)
        for url, jour, slug in tous:
            ts = mots(slug)
            if any(any(s.startswith(t) for s in ts) for t in tt):
                partiels.append((url, jour, slug))
        if partiels:
            print(f"\n  Aucune correspondance exacte pour '{terme}'.")
            print(f"  {len(partiels)} match(s) contiennent un mot proche :")
            return partiels[:15]
        print(f"\n  Aucun match trouve pour '{terme}' sur {len(tous)} rencontres "
              f"des jours proches.")
        print("  Verifie l'orthographe, ou colle directement l'URL du match.")
    return candidats


def stats_mi_temps(url):
    """Recupere le bloc 1re mi-temps de la page ?p=stats."""
    if "?" in url:
        url = url.split("?")[0]
    html = requests.get(url + "?p=stats", headers=HEADERS, timeout=30).text
    soup = BeautifulSoup(html, PARSER)
    corps = soup.get_text("\n")

    infos = {"url": url}
    t = soup.find("title")
    if t:
        m = re.search(r"(.+?)\s*-\s*(.+?)\s+match en direct", t.text)
        if m:
            infos["dom"] = nettoyer_nom(m.group(1))
            infos["ext"] = nettoyer_nom(m.group(2))
    m = re.search(r"(\d+)\s*-\s*(\d+)\s*\(MT\s*(\d+)-(\d+)\)", corps)
    if m:
        infos["score_ft"] = (int(m.group(1)), int(m.group(2)))
        infos["score_mt"] = (int(m.group(3)), int(m.group(4)))
    else:
        m = re.search(r"(\d+)\s*-\s*(\d+)", corps)
        if m:
            infos["score_mt"] = (int(m.group(1)), int(m.group(2)))
    m = re.search(r"👥\s*([\d.\s]+)spectateurs", corps)
    if m:
        infos["affluence"] = nombre(m.group(1).replace(".", "").replace(" ", ""))

    # blocs : 0 = Tous, 1 = 1re MT, 2 = 2e MT. On veut le bloc 1.
    textes = [x.strip() for x in corps.split("\n") if x.strip()]
    stats = {}
    bloc, i = -1, 0
    while i < len(textes):
        cle = sans_accents(textes[i])
        if cle == "possession":
            bloc += 1
        if cle in LIBELLES and bloc == 1:
            v1 = nombre(textes[i + 1]) if i + 1 < len(textes) else None
            v2 = nombre(textes[i + 2]) if i + 2 < len(textes) else None
            if v1 is not None and v2 is not None:
                stats[f"mt_{LIBELLES[cle]}_dom"] = v1
                stats[f"mt_{LIBELLES[cle]}_ext"] = v2
                i += 2
        i += 1
    # si un seul bloc existe (match en cours a la pause), on reprend le bloc 0
    if not stats:
        bloc, i = -1, 0
        while i < len(textes):
            cle = sans_accents(textes[i])
            if cle == "possession":
                bloc += 1
            if cle in LIBELLES and bloc == 0:
                v1 = nombre(textes[i + 1]) if i + 1 < len(textes) else None
                v2 = nombre(textes[i + 2]) if i + 2 < len(textes) else None
                if v1 is not None and v2 is not None:
                    stats[f"mt_{LIBELLES[cle]}_dom"] = v1
                    stats[f"mt_{LIBELLES[cle]}_ext"] = v2
                    i += 2
            i += 1
    infos["stats"] = stats
    return infos


def derivees(infos):
    """Reconstruit les memes variables que dans le fichier de regles."""
    s = dict(infos["stats"])
    sc = infos.get("score_mt", (0, 0))
    s["mt_score_dom"], s["mt_score_ext"] = sc[0], sc[1]
    s["mt_ecart"] = sc[0] - sc[1]
    s["mt_ecart_abs"] = abs(s["mt_ecart"])
    s["mt_buts_tot"] = sc[0] + sc[1]
    if "affluence" in infos:
        s["affluence"] = infos["affluence"]
    if "mt_poss_dom" in s and "mt_poss_ext" not in s:
        s["mt_poss_ext"] = 100 - s["mt_poss_dom"]

    for base in ("tirs", "cadres", "xg", "corners", "touches_surface", "passes",
                 "passes_reussies", "centres", "centres_reussis", "fautes",
                 "jaunes", "rouges", "touches_ligne", "degagements",
                 "interceptions", "duels_gagnes", "tacles", "dribbles",
                 "horsjeu", "occ_manquees", "bloques", "non_cadres"):
        d, e = f"mt_{base}_dom", f"mt_{base}_ext"
        if d in s and e in s:
            s[f"mt_{base}_tot"] = s[d] + s[e]

    if "mt_xg_dom" in s and "mt_xg_ext" in s:
        s["mt_xg_ecart"] = s["mt_xg_dom"] - s["mt_xg_ext"]
        s["mt_injustice"] = s["mt_xg_ecart"] - s["mt_ecart"]
    for cote in ("dom", "ext"):
        p = s.get(f"mt_passes_{cote}")
        if p:
            if s.get(f"mt_tirs_{cote}") is not None:
                s[f"mt_tirs_p100passes_{cote}"] = s[f"mt_tirs_{cote}"] / p * 100
            if s.get(f"mt_touches_surface_{cote}") is not None:
                s[f"mt_touchsurf_p100passes_{cote}"] = \
                    s[f"mt_touches_surface_{cote}"] / p * 100
    return s


# =====================================================================
#  Application des regles
# =====================================================================

def nom_colonne(variable):
    v = str(variable).strip().replace(" ", "_")
    return v if v.startswith(("mt_", "affluence")) else f"mt_{v}"


def famille_de(variable):
    v = str(variable)
    for nom, motifs in FAMILLES.items():
        for m in motifs:
            if re.search(r"(?:^|_)" + m + r"(?:_|$)", v.replace(" ", "_")):
                return nom
    return "Autre"


def evaluer(regles, valeurs):
    lignes = []
    for _, r in regles.iterrows():
        col = nom_colonne(r.variable)
        if col not in valeurs or valeurs[col] is None:
            continue
        v = valeurs[col]
        actif = v > r.seuil if r.sens == ">" else v <= r.seuil
        if not actif:
            continue
        lignes.append({
            "cible": r.cible, "condition": r.condition,
            "famille": famille_de(r.variable),
            "valeur": v, "seuil": r.seuil,
            "ecart": r.ecart_moyenne,
            "moyenne": r.get("moyenne", float("nan")),
            "attendu": r.get("attendu", float("nan")),
            "frequence": r.frequence,
        })
    return pd.DataFrame(lignes)


def afficher(res, valeurs, infos):
    if not len(res):
        print("\n  Aucune regle ne se declenche sur ce match.")
        print("  Projection = moyenne generale, aucun signal exploitable.")
        return

    dom = infos.get("dom", "domicile")
    ext = infos.get("ext", "exterieur")

    for cible in sorted(res.cible.unique()):
        sous = res[res.cible == cible]
        quoi, qui = LIBELLE_CIBLE.get(cible, (cible, ""))
        equipe = dom if qui == "domicile" else (ext if qui == "exterieur" else "les 2 equipes")
        moyenne = sous.moyenne.dropna()
        moyenne = float(moyenne.iloc[0]) if len(moyenne) else float("nan")

        print("\n" + "=" * 78)
        print(f"  {quoi.upper()} de 2e mi-temps  -  {equipe}")
        print("=" * 78)
        if pd.notna(moyenne):
            print(f"  moyenne de reference : {moyenne:.2f}")

        # une seule regle par famille : la plus forte
        best = (sous.reindex(sous.ecart.abs().sort_values(ascending=False).index)
                    .drop_duplicates(subset="famille"))
        print(f"\n  {'famille':<26}{'condition':<34}{'valeur':>8}{'ecart':>8}")
        print("  " + "-" * 74)
        for _, l in best.iterrows():
            print(f"  {l.famille:<26}{l.condition:<34}{l.valeur:>8.1f}"
                  f"{l.ecart:>+8.2f}")

        autres = len(sous) - len(best)
        if autres:
            print(f"  ({autres} autre(s) regle(s) redondante(s) ignoree(s))")

        if pd.notna(moyenne):
            # consensus : moyenne des familles, pas somme
            proj = moyenne + best.ecart.mean()
            mini = moyenne + best.ecart.min()
            maxi = moyenne + best.ecart.max()
            print(f"\n  PROJECTION 2e MI-TEMPS : {proj:.2f}"
                  f"   (fourchette selon les familles : {mini:.2f} a {maxi:.2f})")

            # total du match, pour comparer aux lignes "match entier"
            base = {"tirs": "mt_tirs", "cadres": "mt_cadres",
                    "buts": "mt_score"}.get(quoi)
            suffixe = {"domicile": "_dom", "exterieur": "_ext"}.get(qui)
            deja = None
            if base and suffixe:
                deja = valeurs.get(base + suffixe)
            elif base:
                d_, e_ = valeurs.get(base + "_dom"), valeurs.get(base + "_ext")
                deja = (d_ + e_) if (d_ is not None and e_ is not None) else None
            if deja is not None:
                print(f"  deja realise en 1re MT : {deja:.0f}")
                print(f"  -> TOTAL MATCH PROJETE : {deja + proj:.2f}")

            if len(best) >= 2:
                accord = (best.ecart > 0).all() or (best.ecart < 0).all()
                print("\n  " + ("Familles CONCORDANTES : signal le plus fiable."
                                if accord else
                                "Familles CONTRADICTOIRES : signal faible, s'abstenir."))
            else:
                print("\n  Une seule famille active : signal isole, prudence.")



# =====================================================================
#  Rapport HTML
# =====================================================================

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#0f1115;
     color:#e6e8ec;padding:28px;line-height:1.5}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:26px;font-weight:650;margin-bottom:4px}
.sub{color:#8b93a7;font-size:13px;margin-bottom:24px}
.score{background:#171a21;border:1px solid #262b36;border-radius:12px;
       padding:18px 22px;margin-bottom:14px}
.score .teams{font-size:22px;font-weight:650;margin-bottom:14px}
.score .teams .n{color:#5b9dff}
table.mini{width:100%;border-collapse:collapse;font-size:13px}
table.mini td{padding:5px 8px;border-bottom:1px solid #21262f}
table.mini td:first-child{color:#8b93a7;width:46%}
table.mini td.v{text-align:right;width:27%;font-variant-numeric:tabular-nums}
.note{background:#1a1d26;border-left:3px solid #5b9dff;padding:10px 14px;
      border-radius:0 8px 8px 0;font-size:13px;color:#b8c0d0;margin-top:12px}
.card{background:#171a21;border:1px solid #262b36;border-radius:12px;
      padding:20px 22px;margin-bottom:16px}
.card.total{border-color:#5b9dff;background:#151b28}
.card.s3,.card.s4,.card.s5{border-color:#3ddc84;background:#111d18}
.card.s0{opacity:.72}
.hd{display:flex;justify-content:space-between;align-items:center;
    flex-wrap:wrap;gap:10px;margin-bottom:14px}
.card h2{font-size:15px;font-weight:600;color:#8b93a7;text-transform:uppercase;
         letter-spacing:.6px}
.card.total h2{color:#5b9dff}
.stars{font-size:20px;letter-spacing:3px;white-space:nowrap}
.stars .on{color:#ffd166}.stars .off{color:#333a48}
.stars .lab{font-size:11px;color:#8b93a7;letter-spacing:.4px;margin-left:8px;
            text-transform:uppercase}
.duo{display:flex;gap:14px;flex-wrap:wrap;margin:8px 0 12px}
.proj{flex:1;min-width:210px;background:#0d1017;border:1px solid #262b36;
      border-radius:10px;padding:14px 16px}
.proj.primaire{border-color:#3a4a6b;background:#101724}
.ptit{font-size:11px;text-transform:uppercase;letter-spacing:.6px;
      color:#8b93a7;margin-bottom:8px;display:flex;align-items:center;gap:8px}
.info{background:#232937;color:#7ee2ac;padding:1px 7px;border-radius:4px;
      font-size:10px;cursor:help;letter-spacing:0;text-transform:none}
.proj .num{font-size:44px;font-weight:750;line-height:1;
           font-variant-numeric:tabular-nums}
.proj .num2{font-size:34px;font-weight:650;line-height:1;color:#a8b2c6;
            font-variant-numeric:tabular-nums}
.num.up{color:#3ddc84}.num.down{color:#ff6b6b}.num.flat{color:#e6e8ec}
.num2.up{color:#7ee2ac}.num2.down{color:#ffa0a0}.num2.flat{color:#a8b2c6}
.pdelta{font-size:12px;margin-top:6px}
.pdelta.up{color:#3ddc84}.pdelta.down{color:#ff6b6b}
.pnote{font-size:11px;color:#6b7386;margin-top:3px}
.range{font-size:12px;color:#8b93a7;margin-bottom:10px}
.tot{background:#0d1017;border:1px dashed #39404f;border-radius:9px;
     padding:12px 16px;margin:14px 0;font-size:14px}
.tot b{font-size:26px;color:#ffd166;font-variant-numeric:tabular-nums}
.charts{display:flex;gap:16px;flex-wrap:wrap;margin:14px 0}
.chart{flex:1;min-width:300px;background:#0d1017;border:1px solid #232937;
       border-radius:10px;padding:14px 16px}
.chart h3{font-size:11px;text-transform:uppercase;letter-spacing:.6px;
          color:#8b93a7;font-weight:500;margin-bottom:10px}
table.rules{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
table.rules th{text-align:left;color:#8b93a7;font-weight:500;padding:7px 8px;
               border-bottom:1px solid #262b36;font-size:11px;
               text-transform:uppercase;letter-spacing:.5px}
table.rules td{padding:8px;border-bottom:1px solid #1c212a}
td.e{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
td.e.up{color:#3ddc84}td.e.down{color:#ff6b6b}
.fam{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;
     background:#232937;color:#a8b2c6}
.tag{background:#123524;color:#7ee2ac;padding:1px 6px;border-radius:4px;
     font-size:10px;margin-left:6px}
.tag.red{background:#2a2320;color:#c9a87e}
.verdict{margin-top:14px;padding:11px 14px;border-radius:8px;font-size:13px}
.verdict.ok{background:#123524;color:#7ee2ac}
.verdict.bad{background:#3a2a12;color:#ffc978}
.verdict.none{background:#1c212a;color:#8b93a7}
.recap{background:#171a21;border:1px solid #262b36;border-radius:12px;
       padding:18px 22px;margin-bottom:16px}
.recap table{width:100%;border-collapse:collapse;font-size:13px}
.recap th{text-align:left;color:#8b93a7;font-size:11px;font-weight:500;
          text-transform:uppercase;letter-spacing:.5px;padding:7px 8px;
          border-bottom:1px solid #262b36}
.recap td{padding:9px 8px;border-bottom:1px solid #1c212a}
.recap td.n{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.synth .stit{font-size:12px;text-transform:uppercase;letter-spacing:.6px;
             color:#5b9dff;font-weight:600;margin-bottom:10px}
.pills{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.pill{border:1px solid #2c3444;border-radius:20px;padding:3px 12px;
      font-size:11px;color:#8b93a7}
.pill b{color:#e6e8ec}
.bar{display:inline-block;width:74px;height:7px;background:#232937;
     border-radius:4px;overflow:hidden;vertical-align:middle}
.bar .fill{display:block;height:100%}
.bv{font-size:11px;color:#a8b2c6;margin-left:7px;
    font-variant-numeric:tabular-nums}
.sub2{font-size:10px;color:#6b7386;margin-left:5px}
.inc{font-size:11px;color:#6b7386;font-style:italic}
.part{font-size:9px;background:#2a2320;color:#c9a87e;padding:1px 5px;
      border-radius:3px;margin-left:5px;font-weight:400}
.sub3{font-size:11px;color:#6b7386;margin-top:11px;line-height:1.5}
.cotes{background:#0d1017;border:1px solid #2c3444;border-radius:10px;
       padding:14px 16px;margin:14px 0}
.cotes h4{font-size:11px;text-transform:uppercase;letter-spacing:.6px;
          color:#5b9dff;font-weight:600;margin-bottom:10px}
.champs{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.champ{display:flex;flex-direction:column;gap:4px}
.champ label{font-size:10px;color:#8b93a7;text-transform:uppercase;
             letter-spacing:.4px}
.champ input{width:92px;background:#171a21;border:1px solid #2c3444;
             border-radius:7px;padding:7px 10px;color:#e6e8ec;font-size:14px;
             font-variant-numeric:tabular-nums}
.champ input:focus{outline:none;border-color:#5b9dff}
.res{margin-top:12px;font-size:13px}
.res table{width:100%;border-collapse:collapse;margin-top:8px}
.res th{text-align:left;font-size:10px;color:#8b93a7;text-transform:uppercase;
        letter-spacing:.4px;padding:5px 8px;border-bottom:1px solid #262b36}
.res td{padding:7px 8px;border-bottom:1px solid #1c212a;
        font-variant-numeric:tabular-nums}
.res td.g{color:#3ddc84;font-weight:600}.res td.r{color:#ff6b6b}
.badge{display:inline-block;padding:3px 10px;border-radius:6px;font-size:12px;
       font-weight:600}
.badge.ok{background:#123524;color:#3ddc84}
.badge.no{background:#2a1c1c;color:#ff8080}
.badge.neutre{background:#1c212a;color:#8b93a7}
.mise{font-size:22px;font-weight:750;color:#ffd166}
.aide{font-size:11px;color:#6b7386;margin-top:8px;line-height:1.5}
.grille{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px}
.grille th{font-size:10px;color:#8b93a7;padding:4px 6px;text-align:right;
           border-bottom:1px solid #262b36}
.grille th:first-child{text-align:left}
.grille td{padding:5px 6px;text-align:right;border-bottom:1px solid #1c212a;
           font-variant-numeric:tabular-nums}
.grille td:first-child{text-align:left;color:#8b93a7}
.grille tr.cur{background:#141b28}
.reglages{background:#171a21;border:1px solid #262b36;border-radius:12px;
          padding:16px 20px;margin-bottom:16px;display:flex;gap:18px;
          flex-wrap:wrap;align-items:flex-end}
.reglages{background:#171a21;border:1px solid #262b36;border-radius:12px;
          padding:14px 20px;margin-bottom:16px;display:flex;gap:22px;
          align-items:center;flex-wrap:wrap;font-size:13px}
.reglages input,.reglages select{background:#0d1017;color:#e6e8ec;
  border:1px solid #39404f;border-radius:6px;padding:5px 9px;margin-left:6px}
.reglages input{width:90px}
.rnote{font-size:11px;color:#6b7386;flex:1;min-width:220px}
.cotes{background:#0d1017;border:1px solid #2c3444;border-radius:10px;
       padding:14px 16px;margin:14px 0}
.ctit{font-size:11px;text-transform:uppercase;letter-spacing:.6px;
      color:#5b9dff;margin-bottom:10px;font-weight:600}
.ctit2{font-size:11px;text-transform:uppercase;letter-spacing:.5px;
       color:#8b93a7;margin:14px 0 4px;font-weight:600}
.copt{text-transform:none;letter-spacing:0;color:#6b7386;font-weight:400;
      margin-left:8px;font-size:10px}
.crow{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.crow label{font-size:11px;color:#8b93a7;display:flex;flex-direction:column;
            gap:4px}
.crow input,.crow select{background:#171a21;color:#e6e8ec;border:1px solid #39404f;
  border-radius:6px;padding:6px 9px;font-size:13px}
.crow input{width:95px}
.crow button{background:#5b9dff;color:#0d1017;border:0;border-radius:6px;
  padding:7px 18px;font-weight:600;cursor:pointer;font-size:13px}
.crow button:hover{background:#7db2ff}
.cres{margin-top:12px}
table.ctab{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px}
table.ctab th{text-align:left;color:#8b93a7;font-weight:500;padding:6px;
  border-bottom:1px solid #262b36;font-size:10px;text-transform:uppercase}
table.ctab td{padding:6px;border-bottom:1px solid #1c212a;
  font-variant-numeric:tabular-nums}
table.ctab tr.ok td{background:#0f1d16}
table.ctab tr.cur td{background:#141a26;font-weight:600}
table.ctab td.g{color:#3ddc84;font-weight:600}
table.ctab td.r{color:#ff6b6b}
.cnote{font-size:11px;color:#8b93a7;margin-top:8px;line-height:1.6}
.cnote b.g{color:#3ddc84}
.champ select{background:#171a21;color:#e6e8ec;border:1px solid #39404f;
  border-radius:6px;padding:6px 9px;font-size:13px}
.coller{display:flex;gap:10px;margin-top:10px;align-items:stretch}
.coller textarea{flex:1;background:#171a21;color:#e6e8ec;border:1px solid #2c3444;
  border-radius:6px;padding:7px 10px;font-size:12px;font-family:inherit;
  resize:vertical}
.coller textarea:focus{outline:none;border-color:#5b9dff}
.btn2{background:#232937;color:#a8b2c6;border:1px solid #39404f;border-radius:6px;
  padding:0 16px;font-size:12px;cursor:pointer;white-space:nowrap}
.btn2:hover{background:#2c3444;color:#e6e8ec}
.pli{margin-bottom:10px;border:1px solid #262b36;border-radius:12px;
     overflow:hidden;background:#12151b}
.pli>summary{list-style:none;cursor:pointer;padding:12px 16px;display:flex;
  align-items:center;gap:12px;flex-wrap:wrap;background:#171a21}
.pli>summary::-webkit-details-marker{display:none}
.pli>summary::before{content:"▸";color:#5b9dff;font-size:12px;
  transition:transform .15s}
.pli[open]>summary::before{transform:rotate(90deg)}
.pli>summary:hover{background:#1c212a}
.pli .card{margin:0;border:0;border-radius:0}
.sst{font-size:15px;letter-spacing:2px;color:#ffd166}
.snom{font-weight:600;font-size:13.5px;min-width:190px}
.sproj{font-size:19px;font-weight:750;font-variant-numeric:tabular-nums}
.stm{font-size:11px;color:#ffd166}
.sbt{font-size:11px;color:#8b93a7}
.sbadge{margin-left:auto;font-size:10px;font-weight:700;letter-spacing:.5px;
        padding:3px 9px;border-radius:5px}
.plibar{display:flex;gap:10px;align-items:center;margin-bottom:12px;
        flex-wrap:wrap}
.plibar button{background:#232937;color:#a8b2c6;border:1px solid #2c3444;
  border-radius:6px;padding:6px 14px;font-size:12px;cursor:pointer}
.plibar button:hover{background:#2c3444;color:#e6e8ec}
.valinfo{font-size:11px;color:#6b7386;margin-bottom:10px;
  border-left:2px solid #2c3444;padding-left:10px}
.verdictbt{border-radius:8px;padding:11px 14px;font-size:12.5px;
  margin:12px 0;line-height:1.5}
.verdictbt b{letter-spacing:.5px;font-size:11px}
.cwarn{font-size:11px;color:#c9a87e;margin-top:10px;border-top:1px solid #262b36;
       padding-top:8px}
.warn{background:#241a1a;border:1px solid #4a2a2a;border-radius:10px;
      padding:16px 20px;font-size:13px;color:#e0a8a8;margin-top:22px}
"""


def etoiles(best):
    """
    Note de 0 a 5 selon la CONVERGENCE des familles.
      etoiles = familles allant dans le sens majoritaire
                MOINS familles allant dans le sens contraire
    Trois familles concordantes -> 3 etoiles.
    Une pour, une contre -> 0 etoile.
    """
    if best is None or not len(best):
        return 0, 0, 0
    signes = np.sign(best.ecart.values)
    pos, neg = int((signes > 0).sum()), int((signes < 0).sum())
    pour, contre = max(pos, neg), min(pos, neg)
    return max(0, min(5, pour - contre)), pour, contre


LABELS_ETOILES = {
    0: "signal nul", 1: "signal faible", 2: "signal correct",
    3: "signal fort", 4: "signal tres fort", 5: "signal maximal",
}


def html_etoiles(n, pour, contre):
    pleins = "".join('<span class="on">&#9733;</span>' for _ in range(n))
    vides = "".join('<span class="off">&#9734;</span>' for _ in range(5 - n))
    detail = f"{pour} pour"
    if contre:
        detail += f" / {contre} contre"
    return (f'<div class="stars">{pleins}{vides}'
            f'<span class="lab">{LABELS_ETOILES[n]} &middot; {detail}</span></div>')


def svg_familles(best, dom, ext):
    """Barres divergentes : contribution de chaque famille, en tirs."""
    if best is None or not len(best):
        return ""
    h_l, n = 30, len(best)
    H = n * h_l + 26
    maxi = max(0.5, float(best.ecart.abs().max()) * 1.15)
    cx, larg = 178, 150
    barres = ""
    for i, (_, l) in enumerate(best.iterrows()):
        y = 20 + i * h_l
        w = abs(l.ecart) / maxi * larg
        pos = l.ecart > 0
        x = cx if pos else cx - w
        col = "#3ddc84" if pos else "#ff6b6b"
        barres += (
            f'<text x="{cx-10}" y="{y+13}" text-anchor="end" fill="#a8b2c6" '
            f'font-size="11">{l.famille}</text>'
            f'<rect x="{x}" y="{y+2}" width="{max(w,1.5):.1f}" height="15" '
            f'rx="3" fill="{col}" opacity=".85"/>'
            f'<text x="{(x+w+7) if pos else (x-7)}" y="{y+14}" '
            f'text-anchor="{"start" if pos else "end"}" fill="{col}" '
            f'font-size="11" font-weight="600">{l.ecart:+.2f}</text>')
    return (f'<svg viewBox="0 0 360 {H}" width="100%" height="{H}">'
            f'<line x1="{cx}" y1="12" x2="{cx}" y2="{H-8}" stroke="#39404f" '
            f'stroke-width="1"/>{barres}</svg>')


def svg_echelle(moyenne, proj, sd, deja=None):
    """Position de la projection sur la distribution habituelle."""
    if not sd or sd <= 0:
        sd = max(moyenne * 0.3, 1.0)
    lo, hi = moyenne - 2.2 * sd, moyenne + 2.2 * sd
    W, H = 360, 92

    def px(v):
        return 14 + (v - lo) / (hi - lo) * (W - 28)

    x1, x2 = px(moyenne - sd), px(moyenne + sd)
    xm, xp = px(moyenne), px(min(max(proj, lo), hi))
    col = "#3ddc84" if proj > moyenne else ("#ff6b6b" if proj < moyenne else "#e6e8ec")
    return (
        f'<svg viewBox="0 0 {W} {H}" width="100%" height="{H}">'
        f'<rect x="14" y="30" width="{W-28}" height="16" rx="8" fill="#1c212a"/>'
        f'<rect x="{x1:.1f}" y="30" width="{x2-x1:.1f}" height="16" rx="8" '
        f'fill="#262f3f"/>'
        f'<line x1="{xm:.1f}" y1="24" x2="{xm:.1f}" y2="52" stroke="#8b93a7" '
        f'stroke-width="2"/>'
        f'<text x="{xm:.1f}" y="18" text-anchor="middle" fill="#8b93a7" '
        f'font-size="10">moyenne {moyenne:.1f}</text>'
        f'<circle cx="{xp:.1f}" cy="38" r="8" fill="{col}"/>'
        f'<text x="{xp:.1f}" y="70" text-anchor="middle" fill="{col}" '
        f'font-size="14" font-weight="700">{proj:.2f}</text>'
        f'<text x="{xp:.1f}" y="84" text-anchor="middle" fill="#6b7386" '
        f'font-size="9">projection</text>'
        f'<text x="14" y="{H-4}" fill="#4a5162" font-size="9">{lo:.0f}</text>'
        f'<text x="{W-14}" y="{H-4}" text-anchor="end" fill="#4a5162" '
        f'font-size="9">{hi:.0f}</text></svg>')



NIVEAUX = {
    0: ("Ne pas jouer", "Les signaux se neutralisent : autant de familles "
        "pointent dans un sens que dans l autre.", "n0"),
    1: ("Signal isole", "Une seule famille porte l information. "
        "Insuffisant pour miser seul.", "n1"),
    2: ("Signal correct", "Deux familles independantes concordent. "
        "A confronter a la ligne du book.", "n2"),
    3: ("Signal fort", "Trois familles independantes concordent. "
        "C est la configuration la plus fiable du modele.", "n3"),
    4: ("Signal tres fort", "Quatre familles concordent. Rare : "
        "verifie qu il ne s agit pas d un match atypique.", "n4"),
    5: ("Signal maximal", "Cinq familles concordent. Tres rare. "
        "Verifie les donnees avant de conclure.", "n5"),
}


def charger_bases():
    """Coefficients par equipe produits par bases_equipes.py, si presents."""
    if not os.path.exists(FICHIER_BASES):
        return None
    try:
        return pd.read_csv(FICHIER_BASES, encoding="utf-8-sig")
    except Exception:
        return None


def nettoyer_nom(nom):
    """
    Retire ce que MED ajoute au titre d'un match en direct :
    emojis, mentions LIVE / TERMINE, scores, parentheses.
    Sans ca, l'equipe exterieure s'appelle 'Malaga LIVE' et n'est
    retrouvee dans aucune base.
    """
    n = str(nom)
    n = re.sub(r"[^\w\s.'\-]", " ", n)                      # emojis, symboles
    n = re.sub(r"\b(live|direct|termine|fini|mi.?temps|ht|ft)\b", " ",
               n, flags=re.I)
    n = re.sub(r"\b\d+\s*[-:]\s*\d+\b", " ", n)             # scores
    n = re.sub(r"\s+", " ", n).strip(" .-")
    return n


def normaliser(nom):
    return sans_accents(nettoyer_nom(nom)).replace(".", "").replace("-", " ").strip()


def trouver_equipe(index, nom):
    """
    Retrouve une equipe dans les bases malgre les variantes d'ecriture.
      1. correspondance exacte apres normalisation
      2. sinon correspondance par mots ('atl madrid' -> 'atletico madrid')
      3. sinon rien, plutot qu'un mauvais appariement
    """
    cle = normaliser(nom)
    if cle in index:
        return index[cle]
    proches = [v for k, v in index.items()
               if correspond(cle, k.replace(" ", "-"))
               or correspond(k, cle.replace(" ", "-"))]
    return proches[0] if len(proches) == 1 else None


def base_equipe(bases, col_cible, dom, ext, defaut):
    """
    Base propre a l'affiche : moyenne_ligue x attaque(producteur)
    x defense(adverse). Retombe sur la moyenne generale si une equipe est
    inconnue.
    """
    if bases is None or col_cible not in set(bases.cible):
        return defaut, None
    b = bases[bases.cible == col_cible]
    moy = float(b.moyenne_ligue.iloc[0])
    cote = str(b.cote.iloc[0])
    producteur, adverse = (dom, ext) if cote == "dom" else (ext, dom)

    idx = {normaliser(e): e for e in b.equipe}
    ka, kd = trouver_equipe(idx, producteur), trouver_equipe(idx, adverse)

    # Une equipe connue vaut mieux que zero : on utilise le coefficient
    # disponible et on met 1.0 (= neutre) pour celle qui manque, au lieu de
    # tout annuler. On ne retombe sur la moyenne que si AUCUNE n'est connue.
    if ka is None and kd is None:
        return defaut, None
    att = float(b[b.equipe == ka].attaque.iloc[0]) if ka is not None else 1.0
    dfn = float(b[b.equipe == kd].defense.iloc[0]) if kd is not None else 1.0
    val = moy * att * dfn

    bouts = []
    bouts.append(f"{producteur} attaque x{att:.2f}" if ka is not None
                 else f"{producteur} inconnu (neutre)")
    bouts.append(f"{adverse} defense x{dfn:.2f}" if kd is not None
                 else f"{adverse} inconnu (neutre)")
    detail = ", ".join(bouts) + f" sur une moyenne de {moy:.2f}"
    if ka is None or kd is None:
        detail += " - base partielle"
    return val, detail


def bloc_synthese(bases, dom, ext):
    """Carte de synthese : dataset, moyennes de ligue, profils des 2 equipes."""
    if bases is None or not len(bases):
        return ('<div class="recap"><h3 style="font-size:12px;color:#ffc978;'
                'text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px">'
                'Bases par equipe absentes</h3>'
                '<div style="font-size:13px;color:#8b93a7">Lance '
                '<b>bases_equipes.py</b> pour generer bases_equipes.csv. '
                'Les projections partent pour l instant de la moyenne generale '
                'du championnat, identique pour toutes les equipes.</div></div>')

    p = bases.iloc[0]
    meta = []
    for cle, lib in (("n_matchs", "matchs"), ("n_equipes", "equipes"),
                     ("periode", "periode"), ("ligues", "championnats"),
                     ("lissage", "lissage"), ("demi_vie", "demi-vie")):
        if cle in bases.columns and pd.notna(p.get(cle)):
            v = p[cle]
            v = f"{int(v)}" if isinstance(v, (int, float)) and float(v).is_integer() else v
            meta.append(f'<span class="pill"><b>{v}</b> {lib}</span>')
    gain = p.get("gain_valide_pct")
    if pd.notna(gain):
        col = "#3ddc84" if gain > 1 else ("#ffc978" if gain > 0 else "#ff6b6b")
        meta.append(f'<span class="pill" style="border-color:{col}">'
                    f'<b style="color:{col}">{gain:+.1f}%</b> gain valide</span>')

    # moyennes de ligue par cible
    noms = {"smt_tirs_dom": ("Tirs", "domicile"), "smt_tirs_ext": ("Tirs", "exterieur"),
            "smt_cadres_dom": ("Cadres", "domicile"),
            "smt_cadres_ext": ("Cadres", "exterieur"),
            "smt_buts_dom": ("Buts", "domicile"), "smt_buts_ext": ("Buts", "exterieur")}
    idx = {normaliser(e): e for e in bases.equipe}
    kd, ke = trouver_equipe(idx, dom), trouver_equipe(idx, ext)

    lignes = ""
    for col, (quoi, cote) in noms.items():
        b = bases[bases.cible == col]
        if not len(b):
            continue
        moy = float(b.moyenne_ligue.iloc[0])
        equipe = dom if cote == "domicile" else ext
        adverse = ext if cote == "domicile" else dom
        k_att = kd if cote == "domicile" else ke
        k_def = ke if cote == "domicile" else kd
        # Un coefficient manquant vaut 1.0 (neutre), pas l'abandon de la ligne :
        # si Atl. Madrid est connu et son adversaire non, on garde quand meme
        # l'information sur Atl. Madrid.
        att = dfn = None
        if k_att is not None and (b.equipe == k_att).any():
            att = float(b[b.equipe == k_att].attaque.iloc[0])
        if k_def is not None and (b.equipe == k_def).any():
            dfn = float(b[b.equipe == k_def].defense.iloc[0])
        if att is None and dfn is None:
            lignes += (f'<tr><td>{quoi} &mdash; {equipe}</td>'
                       f'<td class="n">{moy:.2f}</td><td colspan="3" '
                       f'style="color:#6b7386">aucune des deux equipes connue, '
                       f'moyenne utilisee</td></tr>')
            continue
        att_connu, def_connu = att is not None, dfn is not None
        att = att if att_connu else 1.0
        dfn = dfn if def_connu else 1.0
        base = moy * att * dfn

        def barre(x, couleur):
            larg = min(max((x - 0.6) / 0.8, 0), 1) * 100
            return (f'<span class="bar"><span class="fill" '
                    f'style="width:{larg:.0f}%;background:{couleur}"></span></span>'
                    f'<span class="bv">x{x:.2f}</span>')

        ca = "#3ddc84" if att > 1 else "#ff6b6b"
        cd = "#ff6b6b" if dfn > 1 else "#3ddc84"
        cb = "#3ddc84" if base > moy else "#ff6b6b"
        cell_att = (barre(att, ca) if att_connu else
                    '<span class="inc">inconnue, neutre</span>')
        cell_def = ((barre(dfn, cd) + f'<span class="sub2"> {adverse}</span>')
                    if def_connu else
                    f'<span class="inc">{adverse} inconnu, neutre</span>')
        etoile = "" if (att_connu and def_connu) else ' <span class="part">partielle</span>'
        lignes += (f'<tr><td>{quoi} &mdash; <b>{equipe}</b></td>'
                   f'<td class="n" style="color:#8b93a7">{moy:.2f}</td>'
                   f'<td>{cell_att}</td><td>{cell_def}</td>'
                   f'<td class="n" style="color:{cb};font-size:15px">{base:.2f}'
                   f'{etoile}</td></tr>')

    return (f'<div class="recap synth">'
            f'<h3 class="stit">Synthese &mdash; bases calculees sur l historique'
            f'</h3><div class="pills">{"".join(meta)}</div>'
            f'<table><tr><th>marche</th><th style="text-align:right">moyenne '
            f'championnat</th><th>attaque</th><th>defense adverse</th>'
            f'<th style="text-align:right">base de ce match</th></tr>'
            f'{lignes}</table>'
            f'<div class="sub3">Un coefficient d attaque superieur a 1 signifie '
            f'que l equipe produit plus que la moyenne ; un coefficient de '
            f'defense superieur a 1 que l adversaire encaisse plus que la '
            f'moyenne. La base du match est le produit des deux.</div></div>')


def bandeau_verdict(quoi, qui, etoiles):
    niveau, texte, ecart = verdict_marche(quoi, qui, etoiles)
    couleurs = {"ok": ("#123524", "#7ee2ac", "JOUABLE"),
                "faible": ("#2a2416", "#ffc978", "SIGNAL FAIBLE"),
                "insuffisant": ("#241f16", "#c9a87e", "TROP MINCE"),
                "prudence": ("#1c212a", "#8b93a7", "NON TESTE"),
                "aucun": ("#16181d", "#6b7386", "AUCUN SIGNAL"),
                "rejet": ("#2a1818", "#ff9d9d", "A EVITER")}
    fond, texte_col, titre = couleurs[niveau]
    return (f'<div class="verdictbt" style="background:{fond};color:{texte_col}">'
            f'<b>{titre}</b> &mdash; {texte}</div>'), niveau


def bloc_cotes(cid, proj, deja, quoi, niveau="prudence"):
    """Zone de saisie des cotes : tout est recalcule dans le navigateur."""
    total = (deja + proj) if deja is not None else proj
    defaut = float(np.floor(total) + 0.5)
    d = (f'data-proj="{proj:.4f}" '
         f'data-deja="{deja if deja is not None else 0}" '
         f'data-niveau="{niveau}"')
    return (f'<div class="cotes" id="c{cid}" {d}>'
            f'<h4>Cotes du bookmaker &mdash; {quoi}</h4>'
            f'<div class="champs">'
            f'<div class="champ"><label>porte sur</label>'
            f'<select data-r="portee" onchange="calc({cid})">'
            f'<option value="match">match entier</option>'
            f'<option value="smt">2e mi-temps</option>'
            f'</select></div>'
            f'<div class="champ"><label>ligne</label>'
            f'<input type="number" step="0.5" value="{defaut:g}" '
            f'data-r="ligne" oninput="calc({cid})"></div>'
            f'<div class="champ"><label>cote over</label>'
            f'<input type="number" step="0.01" placeholder="ex 1.80" '
            f'data-r="over" oninput="calc({cid})"></div>'
            f'<div class="champ"><label>cote under</label>'
            f'<input type="number" step="0.01" placeholder="ex 1.90" '
            f'data-r="under" oninput="calc({cid})"></div>'
            f'<button class="btn" onclick="calc({cid})">Calculer</button>'
            f'</div>'
            f'<div class="coller">'
            f'<textarea data-r="colle" rows="2" placeholder="ou colle ici la zone '
            f'de cotes copiee depuis ton bookmaker "></textarea>'
            f'<button class="btn2" onclick="analyser({cid})">Extraire</button>'
            f'</div>'
            f'<div class="res" id="r{cid}"></div></div>')


JS_CALCUL = r"""
<script>
function plier(ouvrir){
  document.querySelectorAll('details.pli').forEach(d => d.open = ouvrir);
}
const BR = () => parseFloat(document.getElementById('bankroll').value) || 0;
const FK = () => parseFloat(document.getElementById('fkelly').value) || 0.25;

function pcdf(k, lam){                       // P(X <= k), loi de Poisson
  if (lam <= 0) return 1;
  if (k < 0) return 0;
  let t = Math.exp(-lam), s = t;
  for (let i = 1; i <= k; i++){ t *= lam / i; s += t; }
  return Math.min(s, 1);
}
const pOver = (lam, L) => 1 - pcdf(Math.floor(L), lam);

function lamDepuisP(p, L){                   // lambda tel que P(X > L) = p
  let lo = 0.001, hi = Math.max(4 * (L + 1), 8);
  for (let i = 0; i < 80; i++){
    const m = (lo + hi) / 2;
    if (pOver(m, L) < p) lo = m; else hi = m;
  }
  return (lo + hi) / 2;
}
const kelly = (p, c) => (c > 1 ? (p * c - 1) / (c - 1) : 0);

function ligneRes(nom, p, cote, valide){
  if (!cote || cote <= 1) return '';
  const ev = p * cote - 1, k = kelly(p, cote);
  // Sans validation du backtest, l'EV repose sur une projection dont on ignore
  // si elle est fiable sur ce marche : on n'affiche aucune mise.
  const kf = valide ? Math.max(0, k) * FK() : 0, mise = BR() * kf;
  return `<tr class="${ev > 0 ? 'ok' : ''}">
    <td>${nom}</td><td>${(p*100).toFixed(1)}%</td><td>${cote.toFixed(2)}</td>
    <td>${(100/cote).toFixed(1)}%</td>
    <td class="${ev>0?'g':'r'}">${ev>=0?'+':''}${(ev*100).toFixed(1)}%</td>
    <td>${k>0?(k*100).toFixed(1)+'%':'—'}</td>
    <td>${kf>0?(kf*100).toFixed(2)+'%':'—'}</td>
    <td>${mise>0?mise.toFixed(2)+' EUR':'—'}</td></tr>`;
}

function analyser(cid){
  /*
    Extrait ligne et cotes d un texte copie depuis un bookmaker.
    Pas de scraping : c est TOI qui copies depuis ta propre session. Aucune
    requete automatisee, donc aucun risque de fermeture de compte.
    Formats reconnus : "Plus de 22.5  1.80", "Over 22,5 1.80", "+22.5 1.80",
    et la plupart des mises en page en colonnes.
  */
  const box = document.getElementById('c' + cid);
  const zone = box.querySelector('[data-r="colle"]');
  const out = document.getElementById('r' + cid);
  let t = (zone.value || '').replace(/,/g, '.').replace(/\s+/g, ' ');
  if (!t.trim()){ return; }

  const nb = '(\\d{1,3}(?:\\.\\d+)?)';
  const cote = '([1-9]\\d?\\.\\d{1,2})';
  let ligne = null, over = null, under = null;

  let m = t.match(new RegExp('(?:plus de|over|sup(?:erieur)?|\\+)\\s*' + nb +
                             '[^\\d]{0,25}' + cote, 'i'));
  if (m){ ligne = parseFloat(m[1]); over = parseFloat(m[2]); }
  m = t.match(new RegExp('(?:moins de|under|inf(?:erieur)?|\\-)\\s*' + nb +
                         '[^\\d]{0,25}' + cote, 'i'));
  if (m){ if (ligne === null) ligne = parseFloat(m[1]); under = parseFloat(m[2]); }

  // repli : une ligne en .5 puis les deux premieres cotes plausibles
  if (ligne === null || (over === null && under === null)){
    const l2 = t.match(/\b(\d{1,3}\.5)\b/);
    if (l2 && ligne === null) ligne = parseFloat(l2[1]);
    const cs = (t.match(/\b[1-9]\d?\.\d{1,2}\b/g) || [])
                 .map(parseFloat).filter(x => x >= 1.01 && x <= 30);
    if (over === null && cs.length) over = cs[0];
    if (under === null && cs.length > 1) under = cs[1];
  }

  const set = (r, v) => {
    const el = box.querySelector('[data-r="' + r + '"]');
    if (el && v !== null && !isNaN(v)) el.value = v;
  };
  set('ligne', ligne); set('over', over); set('under', under);

  if (ligne === null && over === null){
    out.innerHTML = '<div class="cnote">Rien de reconnu dans le texte colle. ' +
      'Saisis les champs a la main.</div>';
    return;
  }
  calc(cid);
}

function calc(cid){
  const box = document.getElementById('c' + cid);
  const out = document.getElementById('r' + cid);
  if (!box || !out) return;
  const val = (r) => {
    const el = box.querySelector('[data-r="' + r + '"]');
    return el ? el.value : '';
  };
  const lam  = parseFloat(box.dataset.proj);      // projection 2e mi-temps
  const deja = parseFloat(box.dataset.deja) || 0; // deja realise en 1re MT
  const L    = parseFloat(val('ligne'));
  const co   = parseFloat(val('over'));
  const cu   = parseFloat(val('under'));
  const portee = val('portee') || 'match';
  const niveau = box.dataset.niveau || 'prudence';
  const valide = (niveau === 'ok');

  if (!L || (!co && !cu)){ out.innerHTML = ''; return; }

  // La 1re mi-temps est DEJA JOUEE : son incertitude est nulle. Sur une ligne
  // de match entier, seule la 2e mi-temps reste aleatoire. On ramene donc la
  // ligne au reste a jouer avant d'appliquer la loi de Poisson.
  const Leff = (portee === 'match') ? L - deja : L;
  if (Leff < 0){
    out.innerHTML = '<div class="cnote">Ligne deja depassee en 1re mi-temps : ' +
      'l over est acquis.</div>';
    return;
  }
  const p = pOver(lam, Leff);

  let h = `<table class="ctab"><tr><th>pari</th><th>ma proba</th><th>cote</th>
    <th>proba book</th><th>EV</th><th>Kelly</th>
    <th>Kelly/${(1/FK()).toFixed(0)}</th><th>mise</th></tr>`;
  h += ligneRes('OVER ' + L, p, co, valide);
  h += ligneRes('UNDER ' + L, 1 - p, cu, valide);
  h += '</table>';
  if (!valide){
    h += `<div class="cnote" style="color:#ffc978">Aucune mise proposee : ce
      marche n est pas valide par le backtest, ou le signal est trop faible.
      L EV affichee suppose une projection fiable, ce qui n est pas etabli ici.
      </div>`;
  }

  if (co && cu){
    const marge = 1/co + 1/cu;
    const pBook = (1/co) / marge;
    const lamBook = lamDepuisP(pBook, Leff);
    const ecart = lam - lamBook;
    const fort = Math.abs(ecart) > 0.5;
    h += `<div class="cnote">Marge du book <b>${((marge-1)*100).toFixed(1)}%</b>
      &nbsp;·&nbsp; sa projection <b>${(lamBook + (portee==='match'?deja:0)).toFixed(2)}</b>
      &nbsp;·&nbsp; la mienne <b>${(lam + (portee==='match'?deja:0)).toFixed(2)}</b>
      &nbsp;·&nbsp; ecart <b class="${fort?'g':''}">${ecart>=0?'+':''}${ecart.toFixed(2)}</b>
      ${fort ? '' : ' — le book voit la meme chose que toi'}</div>`;

    h += `<div class="ctit2">Cotes estimees sur les lignes voisines</div>
      <div class="cnote">Deduites de SA projection, avec la meme marge.</div>
      <table class="ctab"><tr><th>ligne</th><th>proba over</th><th>cote over</th>
      <th>cote under</th><th>mon EV over</th><th>mon EV under</th></tr>`;
    for (let d = -2; d <= 2; d++){
      const L2 = L + d, L2eff = Leff + d;
      if (L2 <= 0 || L2eff < 0) continue;
      const pb = pOver(lamBook, L2eff);
      if (pb <= 0.001 || pb >= 0.999) continue;
      const c2o = 1/(pb*marge), c2u = 1/((1-pb)*marge);
      const pm = pOver(lam, L2eff);
      const evo = pm*c2o - 1, evu = (1-pm)*c2u - 1;
      h += `<tr class="${d===0?'cur':''}"><td>${L2.toFixed(1)}${d===0?' (affichee)':''}</td>
        <td>${(pb*100).toFixed(1)}%</td><td>${c2o.toFixed(2)}</td>
        <td>${c2u.toFixed(2)}</td>
        <td class="${evo>0?'g':'r'}">${evo>=0?'+':''}${(evo*100).toFixed(1)}%</td>
        <td class="${evu>0?'g':'r'}">${evu>=0?'+':''}${(evu*100).toFixed(1)}%</td></tr>`;
    }
    h += '</table>';
  } else {
    h += `<div class="cnote">Renseigne les DEUX cotes pour estimer la projection
      du book et les lignes voisines.</div>`;
  }
  h += `<div class="cwarn">Ces EV supposent ma projection exacte. Le backtest
    montre un exces de confiance au-dela de 60% : mefie-toi des EV elevees et
    reste sur une fraction de Kelly.</div>`;
  out.innerHTML = h;
}
</script>
"""


def barre_reglages():
    br, engage, n, msg = bankroll_courante()
    dispo = max(br - engage, 0)
    detail = (f'<span class="rnote">Bankroll {msg}.'
              + (f' Disponible hors paris en cours : {dispo:.2f} EUR.'
                 if engage else "")
              + '</span>')
    return f"""
<div class="reglages">
  <label>Bankroll <input type="number" id="bankroll" value="{br:.2f}"
     step="10"> EUR</label>
  <label>Fraction de Kelly
    <select id="fkelly">
      <option value="0.125">1/8 (tres prudent)</option>
      <option value="0.25" selected>1/4 (recommande)</option>
      <option value="0.5">1/2 (agressif)</option>
      <option value="1">Kelly complet (deconseille)</option>
    </select>
  </label>
  {detail}
  <span class="rnote">Kelly complet suppose des probabilites exactes. Les
  tiennes ne le sont pas : une fraction protege contre l exces de confiance.
  </span>
</div>
"""


def bloc_etoiles(n, accord, contre, direction):
    pleines = "&#9733;" * n
    vides = "&#9734;" * (5 - n)
    titre, texte, cls = NIVEAUX[n]
    detail = f"{len(accord)} famille(s) dans le sens du consensus"
    if contre:
        detail += f", {len(contre)} a contre-courant ({', '.join(contre)})"
    sens = "a la hausse" if direction > 0 else "a la baisse"
    return (f'<div class="etoiles {cls}">'
            f'<div class="stars">{pleines}<span class="vide">{vides}</span></div>'
            f'<div class="etxt"><b>{titre}</b> &mdash; projection {sens}<br>'
            f'<span class="edet">{detail}. {texte}</span></div></div>')


_CID = [0]


def bloc_html(cible, sous, valeurs, infos, moyenne, sd=None,
              detail_base=None):
    quoi, qui = LIBELLE_CIBLE.get(cible, (cible, ""))
    dom = infos.get("dom", "Domicile")
    ext = infos.get("ext", "Exterieur")
    equipe = {"domicile": dom, "exterieur": ext}.get(qui, "les deux equipes")
    est_total = qui == "total"

    if sous is None or not len(sous):
        # meme sans regle, la projection (base par equipe ou moyenne) reste
        # exploitable : on propose donc quand meme la saisie des cotes.
        base_col = {"tirs": "mt_tirs", "cadres": "mt_cadres",
                    "buts": "mt_score"}.get(quoi)
        suf0 = {"domicile": "_dom", "exterieur": "_ext"}.get(qui)
        deja0 = None
        if base_col and suf0:
            deja0 = valeurs.get(base_col + suf0)
        elif base_col:
            a0, b0 = valeurs.get(base_col + "_dom"), valeurs.get(base_col + "_ext")
            deja0 = (a0 + b0) if (a0 is not None and b0 is not None) else None
        _CID[0] += 1
        html = (f'<div class="card s0{" total" if est_total else ""}">'
                f'<div class="hd"><h2>{quoi} 2e mi-temps &mdash; {equipe}</h2></div>'
                f'{bloc_etoiles(0, [], [], 1)}'
                f'<div class="proj"><div class="num flat">{moyenne:.2f}</div>'
                f'<div class="pnote">aucune regle declenchee &mdash; projection '
                f'= base de l affiche</div></div>'
                + (f'<div class="tot">deja realise en 1re MT : '
                   f'<b style="font-size:18px;color:#e6e8ec">{deja0:.0f}</b>'
                   f' &nbsp;&rarr;&nbsp; total match projete : '
                   f'<b>{deja0 + moyenne:.2f}</b></div>'
                   if deja0 is not None else "")
                + bandeau_verdict(quoi, qui, 0)[0]
                + bloc_cotes(_CID[0], moyenne, deja0, quoi,
                             bandeau_verdict(quoi, qui, 0)[1])
                + '</div>')
        niv0, _, ec0 = verdict_marche(quoi, qui, 0)
        return html, {"cible": cible, "quoi": quoi, "equipe": equipe,
                      "proj": moyenne, "moyenne": moyenne, "etoiles": 0,
                      # deja0 a bien ete calcule plus haut : sans cette ligne,
                      # les cartes sans regle perdaient leur total match
                      "total_match": (deja0 + moyenne) if deja0 is not None
                                     else None,
                      "best": None, "delta": 0.0,
                      "cote": qui, "niveau": niv0, "ecart_bt": ec0,
                      "html": html}

    best = (sous.reindex(sous.ecart.abs().sort_values(ascending=False).index)
                .drop_duplicates(subset="famille"))
    delta = best.ecart.mean()                  # une regle par famille
    delta_all = sous.ecart.mean()              # toutes les regles
    proj = moyenne + delta
    proj_all = moyenne + delta_all
    mini, maxi = moyenne + best.ecart.min(), moyenne + best.ecart.max()
    sens = "up" if delta > 0.15 else ("down" if delta < -0.15 else "flat")
    sens_all = "up" if delta_all > 0.15 else ("down" if delta_all < -0.15 else "flat")

    # toutes les regles, triees par famille puis par ampleur
    sorted_all = sous.reindex(
        sous.assign(_a=sous.ecart.abs()).sort_values(
            ["famille", "_a"], ascending=[True, False]).index)
    lignes, familles_vues = "", set()
    for _, l in sorted_all.iterrows():
        cls = "up" if l.ecart > 0 else "down"
        premiere = l.famille not in familles_vues
        familles_vues.add(l.famille)
        marque = ('<span class="tag">retenue</span>' if premiere
                  else '<span class="tag red">redondante</span>')
        style = "" if premiere else ' style="opacity:.55"'
        lignes += (f'<tr{style}><td><span class="fam">{l.famille}</span></td>'
                   f'<td>{libelle_condition(l.condition, dom, ext)} {marque}</td>'
                   f'<td class="e">{l.valeur:.1f}</td>'
                   f'<td class="e {cls}">{l.ecart:+.2f}</td></tr>')

    n_fam, n_tot = len(best), len(sous)
    note_txt = (f'<div class="range">{n_tot} regles declenchees, '
                f'{n_fam} famille(s) distincte(s)</div>')

    base = {"tirs": "mt_tirs", "cadres": "mt_cadres", "buts": "mt_score"}.get(quoi)
    suf = {"domicile": "_dom", "exterieur": "_ext"}.get(qui)
    deja = None
    if base and suf:
        deja = valeurs.get(base + suf)
    elif base:
        a, b = valeurs.get(base + "_dom"), valeurs.get(base + "_ext")
        deja = (a + b) if (a is not None and b is not None) else None
    bloc_total = ""
    if deja is not None:
        bloc_total = (f'<div class="tot">deja realise en 1re MT : '
                      f'<b style="font-size:18px;color:#e6e8ec">{deja:.0f}</b>'
                      f' &nbsp;&rarr;&nbsp; total match projete : '
                      f'<b>{deja + proj:.2f}</b></div>')

    # --- notation en etoiles
    # une etoile par famille qui va dans le sens du consensus,
    # moins une par famille qui le contredit. Plancher a 0, plafond a 5.
    direction = 1 if delta > 0 else -1
    accord = [f for f, e in zip(best.famille, best.ecart)
              if (1 if e > 0 else -1) == direction]
    contre = [f for f, e in zip(best.famille, best.ecart)
              if (1 if e > 0 else -1) != direction]
    note = max(0, min(5, len(accord) - len(contre)))
    v = bloc_etoiles(note, accord, contre, direction)

    duo = (f'<div class="duo">'
           f'<div class="proj primaire"><div class="ptit">1 regle par famille'
           f'<span class="info" title="Possession, passes et pourcentage de '
           f'passes mesurent la meme chose. Ne compter qu une regle par famille '
           f'evite de compter 4 fois la meme information.">recommande</span>'
           f'</div>'
           f'<div class="num {sens}">{proj:.2f}</div>'
           f'<div class="pdelta {"up" if delta>0 else "down"}">{delta:+.2f} '
           f'vs moyenne {moyenne:.2f}</div>'
           f'<div class="pnote">{n_fam} famille(s)</div></div>'
           f'<div class="proj"><div class="ptit">toutes les regles'
           f'<span class="info" title="Moyenne des {n_tot} regles declenchees, '
           f'redondances comprises. Sur-pondere la famille la plus representee.">'
           f'indicatif</span></div>'
           f'<div class="num2 {sens_all}">{proj_all:.2f}</div>'
           f'<div class="pdelta {"up" if delta_all>0 else "down"}">'
           f'{delta_all:+.2f} vs moyenne</div>'
           f'<div class="pnote">{n_tot} regles</div></div></div>')

    ecart_proj = abs(proj - proj_all)
    alerte = ""
    if ecart_proj > 0.4:
        alerte = (f'<div class="range" style="color:#ffc978">Les deux methodes '
                  f'divergent de {ecart_proj:.2f} : une famille est '
                  f'sur-representee parmi les regles declenchees.</div>')

    graphes = (f'<div class="charts">'
               f'<div class="chart"><h3>contribution par famille (en {quoi})</h3>'
               f'{svg_familles(best, dom, ext)}</div>'
               f'<div class="chart"><h3>position dans la distribution</h3>'
               f'{svg_echelle(moyenne, proj, sd)}</div></div>')

    bandeau, niveau = bandeau_verdict(quoi, qui, note)
    _CID[0] += 1
    cid = _CID[0]
    classe = f"card s{note}" + (" total" if est_total else "")
    html = (f'<div class="{classe}">'
            f'<div class="hd"><h2>{quoi} 2e mi-temps &mdash; {equipe}</h2></div>'
            f'{v}'
            f'{duo}{alerte}'
            + f'<div class="range">fourchette selon les familles : '
            f'{mini:.2f} a {maxi:.2f}</div>'
            f'{graphes}'
            f'{bloc_total}'
            f'{bandeau}'
            f'{bloc_cotes(cid, proj, deja, quoi, niveau)}'
            f'<table class="rules"><tr><th>famille</th><th>condition</th>'
            f'<th style="text-align:right">valeur</th>'
            f'<th style="text-align:right">ecart</th></tr>{lignes}</table>'
            f'{note_txt}</div>')
    _, _, ecart_bt = verdict_marche(quoi, qui, note)
    return html, {"cible": cible, "quoi": quoi, "equipe": equipe,
                  "proj": proj, "moyenne": moyenne, "etoiles": note,
                  "total_match": (deja + proj) if deja is not None else None,
                  "best": best, "delta": delta, "cote": qui,
                  "niveau": niveau, "ecart_bt": ecart_bt, "html": html}


def rapport_html(res, valeurs, infos, regles, chemin="projection.html"):
    dom = infos.get("dom", "Domicile")
    ext = infos.get("ext", "Exterieur")
    sc = infos.get("score_mt", (0, 0))

    apercu = ""
    for lib, k in (("Possession %", "poss"), ("Tirs", "tirs"),
                   ("Tirs cadres", "cadres"), ("Passes", "passes"),
                   ("Touches surface", "touches_surface"),
                   ("Centres", "centres"), ("Corners", "corners"), ("xG", "xg")):
        a, b = valeurs.get(f"mt_{k}_dom"), valeurs.get(f"mt_{k}_ext")
        if a is not None and b is not None:
            apercu += (f'<tr><td>{lib}</td><td class="v">{a:g}</td>'
                       f'<td class="v">{b:g}</td></tr>')

    inj = valeurs.get("mt_injustice")
    note_inj = (f'<div class="note"><b>Injustice {inj:+.2f}</b> &mdash; '
                f'{texte_injustice(inj, dom, ext)}<br>'
                f'<span style="color:#8b93a7">Calcul : (xG {dom} &minus; xG {ext}) '
                f'&minus; (buts {dom} &minus; buts {ext}). '
                f'Toujours du point de vue de l equipe a domicile.</span></div>'
                if inj is not None else "")

    _CID[0] = 0
    bases = charger_bases()
    blocs, resume, par_cote = "", [], {}
    moyennes, sds = {}, {}
    for _, r in regles.iterrows():
        if pd.notna(r.get("moyenne")):
            moyennes[r.cible] = float(r["moyenne"])
        if pd.notna(r.get("sd_cible")):
            sds[r.cible] = float(r["sd_cible"])
    # Un total sans aucune regle n'a pas de moyenne dans regles_exploitables.csv,
    # donc sa carte n'etait jamais creee. On la reconstruit en sommant les deux
    # camps : c'est souvent le seul marche propose par le bookmaker.
    for cle_tot in [c for c in LIBELLE_CIBLE if c.endswith("(total)")]:
        if cle_tot in moyennes:
            continue
        # les libelles ne sont pas reguliers ("tirs cadres 2eMT (total)") :
        # on derive les clefs par substitution plutot que par reconstruction
        d_ = cle_tot.replace("(total)", "(domicile)")
        e_ = cle_tot.replace("(total)", "(exterieur)")
        if d_ in moyennes and e_ in moyennes:
            moyennes[cle_tot] = moyennes[d_] + moyennes[e_]
            if d_ in sds and e_ in sds:
                # variances additives : les deux camps sont peu correles
                sds[cle_tot] = (sds[d_] ** 2 + sds[e_] ** 2) ** 0.5

    cibles = [c for c in ORDRE if c in moyennes or (len(res) and c in set(res.cible))]
    for cible in cibles:
        sous = res[res.cible == cible] if len(res) else None
        moy = moyennes.get(cible)
        if moy is None and sous is not None and len(sous):
            m = sous.moyenne.dropna()
            moy = float(m.iloc[0]) if len(m) else None
        if moy is None:
            continue
        # On n'escamote plus les totaux sans regle : leur projection de base
        # (moyenne du championnat ou base par equipe) reste exploitable, et
        # c'est souvent le seul marche propose par le bookmaker. La carte
        # s'affiche donc a zero etoile, avec son formulaire de cotes.
        col_cible = CIBLE_COLONNE.get(cible)
        detail_base = None
        if col_cible:
            moy_eq, detail_base = base_equipe(bases, col_cible, dom, ext, moy)
            moy = moy_eq
        h, info_bloc = bloc_html(cible, sous, valeurs, infos, moy,
                                 sds.get(cible), detail_base)
        resume.append(info_bloc)
        par_cote[(info_bloc["quoi"], info_bloc.get("cote"))] = info_bloc

    # Une seule carte par marche. Le total affiche est celui produit par les
    # regles portant DIRECTEMENT sur le total : c'est cette cible que le
    # backtest mesure, donc la seule dont l'ecart au temoin soit etabli.
    RANG = {"ok": 0, "faible": 1, "insuffisant": 2, "prudence": 3,
            "aucun": 4, "rejet": 5}

    resume.sort(key=lambda r: (RANG.get(r.get("niveau"), 9),
                               -(r.get("ecart_bt") if r.get("ecart_bt")
                                 is not None else -999),
                               -r["etoiles"]))

    BADGE = {"ok": ("#123524", "#7ee2ac", "JOUABLE"),
             "faible": ("#2a2416", "#ffc978", "FAIBLE"),
             "insuffisant": ("#241f16", "#c9a87e", "TROP MINCE"),
             "prudence": ("#1c212a", "#8b93a7", "NON TESTE"),
             "aucun": ("#16181d", "#6b7386", "AUCUN SIGNAL"),
             "rejet": ("#2a1818", "#ff9d9d", "A EVITER")}
    blocs, premier = "", True
    for r in resume:
        if not r.get("html"):
            continue
        niv = r.get("niveau", "prudence")
        fond, col, titre = BADGE[niv]
        ec = r.get("ecart_bt")
        ec_txt = (f'<span class="sbt">backtest {ec:+.1f} pts</span>'
                  if ec is not None else "")
        etoiles = ("&#9733;" * r["etoiles"]
                   + f'<span style="color:#333a48">'
                     f'{"&#9734;" * (5 - r["etoiles"])}</span>')
        d = r["proj"] - r["moyenne"]
        cd = "#3ddc84" if d > 0.05 else ("#ff6b6b" if d < -0.05 else "#8b93a7")
        tm = (f'<span class="stm">match {r["total_match"]:.2f}</span>'
              if r.get("total_match") is not None else "")
        # marches jouables deplies, plus le premier de la liste : la page ne
        # s'ouvre jamais entierement repliee
        ouvert = " open" if (niv == "ok" or premier) else ""
        premier = False
        blocs += (
            f'<details class="pli"{ouvert}><summary>'
            f'<span class="sst">{etoiles}</span>'
            f'<span class="snom">{r["quoi"]} &mdash; {r["equipe"]}</span>'
            f'<span class="sproj" style="color:{cd}">{r["proj"]:.2f}</span>{tm}'
            f'{ec_txt}'
            f'<span class="sbadge" style="background:{fond};color:{col}">'
            f'{titre}</span></summary>{r["html"]}</details>')

    resume.sort(key=lambda r: (-r["etoiles"], -abs(r["proj"] - r["moyenne"])))
    rec = ""
    for r in resume:
        st = ("".join('<span style="color:#ffd166">&#9733;</span>'
                      for _ in range(r["etoiles"]))
              + "".join('<span style="color:#333a48">&#9734;</span>'
                        for _ in range(5 - r["etoiles"])))
        d = r["proj"] - r["moyenne"]
        col = "#3ddc84" if d > 0.05 else ("#ff6b6b" if d < -0.05 else "#8b93a7")
        tm = (f'{r["total_match"]:.2f}' if r["total_match"] is not None else "&mdash;")
        rec += (f'<tr><td>{st}</td><td>{r["quoi"]} &mdash; {r["equipe"]}</td>'
                f'<td class="n" style="color:{col}">{r["proj"]:.2f}</td>'
                f'<td class="n" style="color:#8b93a7">{r["moyenne"]:.2f}</td>'
                f'<td class="n" style="color:#ffd166">{tm}</td></tr>')
    recap = (f'<div class="recap"><table><tr><th>signal</th><th>marche</th>'
             f'<th style="text-align:right">projection 2e MT</th>'
             f'<th style="text-align:right">moyenne</th>'
             f'<th style="text-align:right">total match</th></tr>'
             f'{rec}</table></div>') if rec else ""

    html = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Projection {dom} - {ext}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>{dom} <span style="color:#8b93a7">vs</span> {ext}</h1>
<div class="sub">{"Bases par equipe actives" if bases is not None else
"Bases par equipe absentes (lance bases_equipes.py)"} &mdash;
Projection de 2e mi-temps &mdash; genere le
{datetime.now():%d/%m/%Y a %H:%M} &mdash; {len(regles)} regles chargees</div>
<div class="score"><div class="teams">{dom} <span class="n">{sc[0]}</span>
&ndash; <span class="n">{sc[1]}</span> {ext}
<span style="font-size:13px;color:#8b93a7">&nbsp;score a la pause</span></div>
<table class="mini"><tr><th></th><th></th><th></th></tr>{apercu}</table>
{note_inj}</div>
<div class="valinfo" style="{'border-left-color:#ff6b6b;color:#ff9d9d'
   if not VALIDATION else ''}">Validation des marches : {INFO_VALIDATION}
{'&nbsp;&mdash; depose validation_marches.csv a cote de app.py, sinon tous les '
 'marches restent en NON TESTE et aucune mise ne sera proposee.'
 if not VALIDATION else ''}</div>
{barre_reglages()}
{bloc_synthese(bases, dom, ext)}
{recap}
<div class="plibar">
  <button onclick="plier(true)">Tout deplier</button>
  <button onclick="plier(false)">Tout replier</button>
  <span class="rnote">Les marches jouables sont deplies, les autres replies.</span>
</div>
{blocs}
<div class="warn"><b>Avant de miser</b><br>
Le bookmaker voit la meme mi-temps que toi : sa ligne n est pas placee a la
moyenne generale, elle est deja ajustee. Ton avantage vaut la difference entre
ces projections et SA ligne, pas l ecart affiche ici. Note ses lignes sur
20 matchs sans parier avant de conclure quoi que ce soit.</div>
</div>{JS_CALCUL}</body></html>"""

    with open(chemin, "w", encoding="utf-8") as f:
        f.write(html)
    return os.path.abspath(chemin)


def main():
    if not os.path.exists(FICHIER_REGLES):
        print(f"{FICHIER_REGLES} introuvable.")
        print("Lance d'abord recherche_seuils.py puis regles_exploitables.py.")
        sys.exit(1)
    regles = pd.read_csv(FICHIER_REGLES, encoding="utf-8-sig")
    auto = charger_validation()
    print("=" * 78)
    print(f"PROJECTION EN DIRECT   -   {len(regles)} regles chargees")
    print("=" * 78)
    print(f"\n  Validation des marches : {INFO_VALIDATION}")
    if not auto:
        print(f"  ({FICHIER_VALIDATION} absent : lance backtest_etoiles.py pour")
        print("   que les marches valides se mettent a jour automatiquement.)")
    else:
        for (q, w), (e, n) in sorted(VALIDATION.items(),
                                     key=lambda x: -(x[1][0] or -99)):
            etat = "valide" if (e is not None and e >= ECART_MINIMUM_VALIDE
                                and n >= 30) else "ecarte"
            print(f"    {q + ' ' + w:<20}{e:>+7.1f} pts sur {n:>5} paris   {etat}")

    entree = input("\nURL MatchEnDirect, ou nom d'une equipe : ").strip()
    if not entree:
        return

    if entree.startswith("http"):
        url = entree
    else:
        print(f"\nRecherche de '{entree}'...")
        cands = chercher_match(entree)
        if not cands:
            print("  Aucun match trouve. Colle plutot l'URL du match.")
            return
        print(f"\n  {len(cands)} match(s) trouve(s) :")
        for i, (u, jour, slug) in enumerate(cands, 1):
            print(f"  {i:>2}. [{jour}]  {joli(slug)}")
        if len(cands) == 1:
            url = cands[0][0]
            print(f"      -> selectionne automatiquement")
        else:
            c = input("\nNumero du match : ").strip()
            if not c.isdigit() or not 1 <= int(c) <= len(cands):
                return
            url = cands[int(c) - 1][0]

    print(f"\nLecture de {url} ...")
    infos = stats_mi_temps(url)
    if not infos.get("stats"):
        print("  Aucune statistique disponible pour ce match.")
        return

    valeurs = derivees(infos)
    sc = infos.get("score_mt", (0, 0))
    print(f"\n  {infos.get('dom','?')} {sc[0]} - {sc[1]} {infos.get('ext','?')}"
          f"   (score a la pause)")
    apercu = [("possession", "poss"), ("tirs", "tirs"), ("cadres", "cadres"),
              ("passes", "passes"), ("touches surface", "touches_surface"),
              ("centres", "centres"), ("xG", "xg")]
    for lib, k in apercu:
        d_, e_ = valeurs.get(f"mt_{k}_dom"), valeurs.get(f"mt_{k}_ext")
        if d_ is not None and e_ is not None:
            print(f"    {lib:<18}{d_:>7.1f}  -  {e_:<7.1f}")
    if "mt_injustice" in valeurs:
        print(f"    {'injustice':<18}{valeurs['mt_injustice']:>+7.2f}")

    bases = charger_bases()
    if bases is None:
        print("\n  bases_equipes.csv absent : projections basees sur la moyenne")
        print("  generale du championnat. Lance bases_equipes.py pour les bases")
        print("  par equipe.")
    else:
        idx = {normaliser(e): e for e in bases.equipe}
        print(f"\n  Bases par equipe ({bases.equipe.nunique()} equipes connues) :")
        for role, nom in (("domicile", infos.get("dom", "?")),
                          ("exterieur", infos.get("ext", "?"))):
            k = trouver_equipe(idx, nom)
            if k:
                print(f"    {role:<11}{nom:<24} -> trouvee sous '{k}'")
            else:
                cible = normaliser(nom)
                proches = [v for kk, v in idx.items()
                           if any(m in kk for m in mots(cible))][:5]
                print(f"    {role:<11}{nom:<24} -> INCONNUE")
                if proches:
                    print(f"                {'':<24}    noms proches dans les "
                          f"bases : {', '.join(proches)}")
                else:
                    print(f"                {'':<24}    aucun nom proche : cette "
                          f"equipe n'est pas dans ton historique")

    res = evaluer(regles, valeurs)
    afficher(res, valeurs, infos)

    try:
        chemin = rapport_html(res, valeurs, infos, regles)
        print(f"\n  Rapport HTML : {chemin}")
        webbrowser.open("file://" + chemin)
    except Exception as e:
        print(f"\n  (rapport HTML non genere : {e})")

    print("\n" + "=" * 78)
    print("  RAPPEL : le bookmaker voit la meme mi-temps que toi. Sa ligne n'est")
    print("  pas placee a la moyenne generale. Ton avantage vaut la difference")
    print("  entre cette projection et SA ligne, pas l'ecart affiche ici.")
    print("  Note ses lignes sur 20 matchs sans parier avant de conclure.")
    print("=" * 78)


if __name__ == "__main__":
    main()