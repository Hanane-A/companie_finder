import pandas as pd
import requests

POSTAL_CODE = "59100"  # Roubaix par exemple
NAF_CODES = ["96.02A", "96.02B", "96.04Z"]

print(f"🔎 Recherche pour le code postal {POSTAL_CODE}...")
results = []

for code in NAF_CODES:
    url = "https://recherche-entreprises.api.gouv.fr/search"
    params = {
        "code_postal": POSTAL_CODE,
        "activite_principale": code,
        "per_page": 25,
        "etat_administratif": "A",
    }
    r = requests.get(url, params=params)
    if r.status_code == 200:
        data = r.json().get("results", [])
        for item in data:
            siege = item.get("siege", {})
            adresse = f"{siege.get('numero_voie', '')} {siege.get('type_voie', '')} {siege.get('libelle_voie', '')}, {siege.get('code_postal', '')} {siege.get('libelle_commune', '')}".strip()
            results.append({
                "Nom": item.get("nom_raison_sociale")
                or item.get("nom_complet"),
                "SIRET": siege.get("siret"),
                "Code NAF": code,
                "Adresse": adresse,
                "Date de Création": item.get("date_creation"),
            })

df = pd.DataFrame(results)
output_file = f"prospects_{POSTAL_CODE}.csv"
df.to_csv(output_file, index=False, encoding="utf-8-sig")
print(
    f"✅ Terminé ! {len(df)} résultats enregistrés dans '{output_file}'"
)