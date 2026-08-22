"""
APPLICATION WEB - projection a la mi-temps

Reutilise la logique de projection_live.py. A deployer sur Streamlit Community
Cloud (gratuit) pour y acceder depuis un telephone.

Fichiers necessaires a cote de ce script :
    projection_live.py          la logique (importee, pas dupliquee)
    regles_exploitables.csv     les regles
    bases_equipes.csv           les coefficients par equipe (optionnel)
    requirements.txt

Le gros fichier matchs_grandes_ligues.csv N'EST PAS necessaire en ligne :
il ne sert qu'a fabriquer les deux CSV ci-dessus, sur ta machine.

Lancer en local :   streamlit run app.py
"""

import os

import pandas as pd
import streamlit as st

import projection_live as pl

try:
    import betclic_stats as bs
    BETCLIC_OK = True
except Exception:
    BETCLIC_OK = False

st.set_page_config(page_title="Projection mi-temps", page_icon="⚽",
                   layout="centered", initial_sidebar_state="collapsed")

st.markdown("""<style>
.block-container{padding-top:2rem;padding-bottom:3rem;max-width:900px}
h1{font-size:1.6rem !important}
</style>""", unsafe_allow_html=True)


@st.cache_data(ttl=3600)
def charger_regles():
    if not os.path.exists(pl.FICHIER_REGLES):
        return None
    return pd.read_csv(pl.FICHIER_REGLES, encoding="utf-8-sig")


@st.cache_data(ttl=3600)
def charger_bases():
    return pl.charger_bases()


@st.cache_resource
def charger_validation():
    """
    Sans cet appel, projection_live reste sur ses valeurs ecrites en dur et
    les totaux ressortent en "non teste". cache_resource (et non cache_data)
    car la fonction modifie l'etat du module importe.
    """
    return pl.charger_validation(), pl.INFO_VALIDATION


@st.cache_data(ttl=300, show_spinner=False)
def lire_match(url):
    """Mise en cache 5 min : evite de re-solliciter le site a chaque clic."""
    return pl.stats_mi_temps(url)


@st.cache_data(ttl=300, show_spinner=False)
def chercher(terme):
    return pl.chercher_match(terme)


regles = charger_regles()
if regles is None:
    st.error(f"{pl.FICHIER_REGLES} introuvable. Depose-le a cote de app.py.")
    st.stop()
bases = charger_bases()
val_ok, val_info = charger_validation()

st.title("⚽ Projection de 2e mi-temps")
c1, c2 = st.columns(2)
c1.caption(f"{len(regles)} regles chargees")
c2.caption(f"{bases.equipe.nunique()} equipes en base" if bases is not None
           else "bases par equipe absentes")
if val_ok:
    st.caption(f"Validation des marches : {val_info}")
else:
    st.error(
        f"**{pl.FICHIER_VALIDATION} introuvable.** Aucun marche ne peut etre "
        f"valide : tout ressortira en NON TESTE et aucune mise ne sera "
        f"proposee.\n\n"
        f"Ce fichier est produit par `backtest_etoiles.py`. Depose-le a la "
        f"racine du depot, a cote de `app.py`, puis utilise le bouton "
        f"*Vider le cache* ci-dessous.")

with st.expander("Fichiers charges"):
    import os as _os
    for f in (pl.FICHIER_REGLES, pl.FICHIER_BASES, pl.FICHIER_VALIDATION):
        if _os.path.exists(f):
            import datetime as _dt
            t = _dt.datetime.fromtimestamp(_os.path.getmtime(f))
            st.write(f"{f} — modifie le {t:%d/%m/%Y a %H:%M}")
        else:
            st.write(f"{f} — ABSENT")
    if st.button("Vider le cache et relire les fichiers"):
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun()

choix_modes = ["Rechercher un match", "Saisie manuelle"]
if BETCLIC_OK:
    choix_modes.insert(0, "Matchs du jour")

# un clic sur un match de la liste bascule automatiquement en recherche
if "terme_auto" in st.session_state:
    defaut = choix_modes.index("Rechercher un match")
else:
    defaut = 0

mode = st.radio("Source des donnees", choix_modes, index=defaut,
                horizontal=True, label_visibility="collapsed")

infos = None


@st.cache_data(ttl=900, show_spinner=False)
def matchs_du_jour(jours, explorer):
    """
    Matchs Betclic proposant l'onglet statistiques. Mise en cache 15 min :
    la collecte parcourt des dizaines de pages, inutile de la refaire a
    chaque interaction.
    """
    import requests
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timedelta, timezone

    session = requests.Session()
    urls = bs.collect_match_urls(session, "football-sfootball", explorer)
    n_urls = len(urls)
    echecs = 0

    debut = (datetime.now(timezone.utc).astimezone()
             .replace(hour=0, minute=0, second=0, microsecond=0))
    fin = debut + timedelta(days=jours)

    def lire(u):
        h = bs.get(session, bs.BASE + u)
        if not h:
            return "echec"
        d = bs.parse_match(h)
        if not d:
            return None
        d["url"] = bs.BASE + u
        return d

    sortie = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for d in ex.map(lire, urls):
            if d == "echec":
                echecs += 1
                continue
            if not d:
                continue
            dt = (datetime.fromisoformat(d["date_utc"])
                  .replace(tzinfo=timezone.utc).astimezone())
            if not (debut <= dt < fin):
                continue
            if "ca_ftb_prp" in [c[0] for c in d["categories"]]:
                sortie.append({"heure": dt.strftime("%d/%m %H:%M"),
                               "quand": dt.isoformat(),
                               "nom": d["name"], "url": d["url"],
                               "onglets": len(d["categories"])})
    return sorted(sortie, key=lambda x: x["quand"]), n_urls, echecs


# ---------------------------------------------------------------------
if mode == "Matchs du jour":
    c1, c2, c3 = st.columns([1, 1, 2])
    jours = c1.selectbox("Periode", [1, 2, 3],
                         format_func=lambda j: f"{j} jour{'s' if j > 1 else ''}")
    # Le script local explore les competitions par defaut : sans cela, seule
    # la page d'accueil football est lue et il manque la plupart des matchs.
    explorer = c2.checkbox("Explorer toutes les competitions", value=True,
                           help="Coche par defaut, comme le script local. "
                                "Decoche pour aller plus vite, au prix de "
                                "beaucoup de matchs manquants.")
    if c3.button("Actualiser la liste", use_container_width=True):
        matchs_du_jour.clear()

    with st.spinner("Lecture de Betclic (une a deux minutes si l exploration "
                    "des competitions est activee)..."):
        try:
            liste, n_urls, echecs = matchs_du_jour(jours, explorer)
        except Exception as e:
            liste = None
            st.error(f"Betclic inaccessible depuis ce serveur : {e}")
            st.info("Les hebergeurs cloud sont souvent bloques par les "
                    "bookmakers. Utilise *Rechercher un match* ou *Saisie "
                    "manuelle*, ou lance l application en local.")

    if liste is not None:
        st.caption(f"{n_urls} match(s) reperes sur Betclic, {echecs} page(s) "
                   f"illisible(s), {len(liste)} retenu(s) sur la periode avec "
                   f"l onglet statistiques.")
        if echecs > max(n_urls * 0.2, 5):
            st.warning(f"{echecs} pages n ont pas pu etre lues : Betclic "
                       f"limite probablement les requetes depuis ce serveur. "
                       f"La liste est incomplete.")
        if not liste:
            st.warning("Aucun match avec l onglet statistiques sur cette "
                       "periode.")
        else:
            st.caption(f"{len(liste)} match(s) avec statistiques. "
                       f"*Betclic* ouvre un nouvel onglet, *Projection* "
                       f"calcule ici meme.")
            for i, m in enumerate(liste):
                a, b, c, d = st.columns([1.1, 3.4, 1.2, 1.4])
                a.write(m["heure"])
                b.write(m["nom"])
                c.link_button("Betclic", m["url"], use_container_width=True)
                if d.button("Projection", key=f"m{i}",
                            use_container_width=True):
                    st.session_state["terme_auto"] = m["nom"]
                    st.session_state["url_betclic"] = m["url"]
                    st.rerun()
    st.stop()

# ---------------------------------------------------------------------
if mode == "Rechercher un match":
    auto = st.session_state.pop("terme_auto", "")
    saisie = st.text_input("URL MatchEnDirect ou nom d'equipe", value=auto,
                           placeholder="atl madrid, psg, ou colle une URL")
    if saisie:
        if saisie.startswith("http"):
            url = saisie
        else:
            with st.spinner("Recherche..."):
                cands = chercher(saisie)
            if not cands:
                st.warning("Aucun match trouve. Colle plutot l'URL du match.")
                st.stop()
            options = {f"[{j}] {pl.joli(s)}": u for u, j, s in cands}
            choix = st.selectbox("Match", list(options))
            url = options[choix]
        with st.spinner("Lecture des statistiques..."):
            try:
                infos = lire_match(url)
            except Exception as e:
                st.error(f"Lecture impossible : {e}")
                st.info("Le site peut bloquer les serveurs distants. "
                        "Utilise la saisie manuelle ci-dessus.")
                st.stop()
        if not infos.get("stats"):
            st.warning("Aucune statistique disponible pour ce match. "
                       "Bascule en saisie manuelle.")
            st.stop()

# ---------------------------------------------------------------------
else:
    st.caption("Recopie les statistiques de 1re mi-temps depuis ton application.")
    n1, n2 = st.columns(2)
    dom = n1.text_input("Equipe a domicile", "")
    ext = n2.text_input("Equipe a l'exterieur", "")

    champs = [("Score", "score", 0, 20, 1),
              ("Possession %", "poss", 0, 100, 1),
              ("Tirs", "tirs", 0, 40, 1),
              ("Tirs cadres", "cadres", 0, 25, 1),
              ("Passes", "passes", 0, 800, 5),
              ("Passes reussies", "passes_reussies", 0, 800, 5),
              ("Touches surface adverse", "touches_surface", 0, 60, 1),
              ("Centres", "centres", 0, 40, 1),
              ("Corners", "corners", 0, 20, 1),
              ("xG", "xg", 0.0, 6.0, 0.01)]

    stats, score = {}, [0, 0]
    with st.form("saisie"):
        for lib, cle, mini, maxi, pas in champs:
            a, b = st.columns(2)
            v1 = a.number_input(f"{lib} — {dom or 'domicile'}", mini, maxi,
                                value=mini, step=pas, key=f"{cle}_d")
            v2 = b.number_input(f"{lib} — {ext or 'exterieur'}", mini, maxi,
                                value=mini, step=pas, key=f"{cle}_e")
            if cle == "score":
                score = [int(v1), int(v2)]
            else:
                stats[f"mt_{cle}_dom"], stats[f"mt_{cle}_ext"] = v1, v2
        envoye = st.form_submit_button("Calculer la projection",
                                       use_container_width=True)
    if not envoye:
        st.stop()
    if not dom or not ext:
        st.warning("Renseigne le nom des deux equipes : ils servent a retrouver "
                   "les coefficients en base.")
        st.stop()
    infos = {"dom": dom, "ext": ext, "score_mt": tuple(score), "stats": stats}

# ---------------------------------------------------------------------
if infos is None:
    st.stop()

valeurs = pl.derivees(infos)
sc = infos.get("score_mt", (0, 0))

t1, t2 = st.columns([3, 1])
t1.subheader(f"{infos.get('dom','?')} {sc[0]} – {sc[1]} {infos.get('ext','?')}")
# Le lien vers Betclic reste accessible pendant qu'on lit la projection :
# Streamlit ne peut pas ouvrir deux onglets d'un seul clic, mais il peut
# garder la cote a portee de main.
if st.session_state.get("url_betclic"):
    t2.link_button("Ouvrir sur Betclic", st.session_state["url_betclic"],
                   use_container_width=True)

# reconnaissance des equipes
if bases is not None:
    idx = {pl.normaliser(e): e for e in bases.equipe}
    cols = st.columns(2)
    for col, (role, nom) in zip(cols, (("Domicile", infos.get("dom", "")),
                                       ("Exterieur", infos.get("ext", "")))):
        k = pl.trouver_equipe(idx, nom)
        col.caption(f"**{role}** · {'en base : ' + k if k else 'inconnue en base'}")

res = pl.evaluer(regles, valeurs)
if not len(res):
    st.info("Aucune regle ne se declenche : projection = valeurs moyennes.")

chemin = pl.rapport_html(res, valeurs, infos, regles,
                         chemin="/tmp/projection.html")
with open(chemin, encoding="utf-8") as f:
    html = f.read()
st.components.v1.html(html, height=2600, scrolling=True)

st.download_button("Telecharger le rapport", html,
                   file_name=f"projection_{infos.get('dom','match')}.html",
                   mime="text/html", use_container_width=True)

st.caption("Le bookmaker voit la meme mi-temps que toi : sa ligne est deja "
           "ajustee. Ton avantage vaut la difference entre ces projections et "
           "SA ligne, pas l'ecart affiche.")
