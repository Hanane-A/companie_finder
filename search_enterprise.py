import requests
import streamlit as st
import pandas as pd

# Configuration de la page Streamlit
st.set_page_config(
    page_title="COMPANIE FINDER", page_icon="✂️", layout="wide"
)

st.title("COMPANIE FINDER - Extracteur de salons et d'indépendants")
st.markdown(
    "Croisez les données officielles (SIRENE) par zone géographique et secteur d'activité."
)

# Dictionnaire des domaines et codes NAF associés
ACTIVITES = {
    "Tous les domaines beauté / bien-être": [
        "96.02A",
        "96.02B",
        "96.04Z",
    ],  # Coiffure, Beauté, Spa
    "Coiffure & Barbiers (96.02A)": ["96.02A"],
    "Soins de beauté & Esthétique / Onglerie (96.02B)": ["96.02B"],
    "Spas, Hammams & Bien-être (96.04Z)": ["96.04Z"],
}

# --- BARRE LATERALE (Filtres) ---
st.sidebar.header("📍 Paramètres de Recherche")

code_postal_input = st.sidebar.text_input(
    "Code Postal ou Ville (ex: 59100 ou Roubaix)", value="59100"
)
domaine_selection = st.sidebar.selectbox(
    "Secteur d'activité", list(ACTIVITES.keys())
)
securise_actif = st.sidebar.checkbox(
    "Filtrer uniquement sur les entreprises actives", value=True
)


def fetch_entreprises(postal_code, codes_naf):
    url = "https://recherche-entreprises.api.gouv.fr/search"
    results = []

    for code in codes_naf:
        params = {
            "code_postal": postal_code,
            "activite_principale": code,
            "per_page": 25,  # Maximum par requête standard de l'API
            "etat_administratif": "A" if securise_actif else None,
        }
        try:
            response = requests.get(url, params=params)
            if response.status_code == 200:
                data = response.json().get("results", [])
                for item in data:
                    # Récupération propre de l'adresse principale
                    siege = item.get("siege", {})
                    adresse = (
                        f"{siege.get('numero_voie', '')} "
                        f"{siege.get('type_voie', '')} "
                        f"{siege.get('libelle_voie', '')}, "
                        f"{siege.get('code_postal', '')} "
                        f"{siege.get('libelle_commune', '')}"
                    ).strip()

                    results.append({
                        "Nom / Enseigne": item.get("nom_raison_sociale")
                        or item.get("nom_complet"),
                        "SIRET": siege.get("siret"),
                        "Code NAF": code,
                        "Adresse": adresse,
                        "Date de Création": item.get("date_creation"),
                        "Source": "API Gouv (SIRENE)",
                    })
        except Exception as e:
            st.error(f"Erreur lors de la requête API pour le code {code} : {e}")

    return pd.DataFrame(results)


# --- ACTION DE RECHERCHE ---
if st.sidebar.button("Lancer la recherche"):
    with st.spinner("Interrogation de la base officielle en cours..."):
        codes_a_chercher = ACTIVITES[domaine_selection]
        df = fetch_entreprises(code_postal_input, codes_a_chercher)

        if not df.empty:
            st.success(
                f"🎉 {len(df)} établissements trouvés pour '{code_postal_input}' !"
            )

            # Filtres dynamiques supplémentaires sur l'interface
            st.subheader("🔍 Filtrer les résultats")
            recherche_texte = st.text_input(
                "Filtrer par mot-clé (nom de salon, rue...)"
            )

            if recherche_texte:
                df = df[
                    df["Nom / Enseigne"].str.contains(
                        recherche_texte, case=False, na=False
                    )
                    | df["Adresse"].str.contains(
                        recherche_texte, case=False, na=False
                    )
                ]

            # Affichage du tableau interactif
            st.dataframe(df, use_container_width=True)

            # Option d'export CSV
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="📥 Télécharger le tableau (CSV)",
                data=csv,
                file_name=f"prospection_{code_postal_input}.csv",
                mime="text/csv",
            )
        else:
            st.warning(
                "Aucun résultat trouvé pour ces critères. Essayez un autre code postal ou élargissez le secteur."
            )