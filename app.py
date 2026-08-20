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

st.title("⚽ Projection de 2e mi-temps")
c1, c2 = st.columns(2)
c1.caption(f"{len(regles)} regles chargees")
c2.caption(f"{bases.equipe.nunique()} equipes en base" if bases is not None
           else "bases par equipe absentes")

mode = st.radio("Source des donnees", ["Rechercher un match", "Saisie manuelle"],
                horizontal=True, label_visibility="collapsed")

infos = None

# ---------------------------------------------------------------------
if mode == "Rechercher un match":
    saisie = st.text_input("URL MatchEnDirect ou nom d'equipe",
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
st.subheader(f"{infos.get('dom','?')} {sc[0]} – {sc[1]} {infos.get('ext','?')}")

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
