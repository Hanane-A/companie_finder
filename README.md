# COMPANIE FINDER

Outil local de prospection commerciale B2B pour une suite logicielle de gestion de salons : coiffure, barbiers, instituts de beauté, onglerie, spas, hammams, espaces de coworking beauté et praticiens indépendants.

COMPANIE FINDER interroge le fichier public SIRENE, puis garde vos prospects, vos notes de terrain et vos statuts sur votre machine.

## Fonctions

- Recherche par code postal, ville ou quartier, et par secteur d'activité.
- Secteurs couverts : coiffure (`96.02A`), soins de beauté et onglerie (`96.02B`), spas, hammams et bien-être (`96.04Z`), coworking beauté, indépendants et freelances.
- Adresse de l'établissement local, et non celle du siège social quand il est dans une autre ville.
- Tableau de prospects : enseigne, secteur, adresse, SIRET, source, statut.
- Recherche texte instantanée et filtres par secteur, source, code postal et ville.
- Fiche de qualification : site web, outil de réservation, notes de terrain, notes Google Maps, notes Instagram.
- Ajout manuel d'un salon repéré sur Maps ou les réseaux sociaux.
- Tournée groupée par rue, prête à imprimer.
- Export CSV de la sélection, ouvrable dans Excel (séparateur `;`, UTF-8 avec BOM).

## Prérequis

- Python 3.11 ou plus récent
- Une connexion internet pour interroger les API publiques

Aucune clé API n'est nécessaire.

## Installation

```bash
python -m venv .venv
```

Windows :

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python run.py
```

macOS et Linux :

```bash
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Puis ouvrir [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Utilisation

1. **Recherche** - saisir un code postal, une ville ou un quartier, choisir les secteurs, puis interroger SIRENE.
2. Cocher les établissements à suivre et les ajouter au tableau.
3. **Prospects** - changer le statut directement dans la ligne : À contacter, Contacté, Rendez-vous pris, Client, Refusé.
4. Ouvrir une fiche pour qualifier le salon : site, Planity, Treatwell, Mindbody ou un autre outil, et notes de passage.
5. **Tournée** - filtrer par code postal pour lister les arrêts par rue.
6. Exporter le CSV depuis le tableau ou la tournée.

Les prospects ajoutés à la main (Maps, réseaux sociaux) se gèrent dans **Ajouter**.

## Sources de données

| Source | Rôle |
| --- | --- |
| [API Recherche d'entreprises](https://recherche-entreprises.api.gouv.fr) | Nom, SIRET, adresse, date de création, état administratif, code NAF |
| [API Découpage administratif](https://geo.api.gouv.fr) | Résolution d'une ville vers ses codes postaux |
| [Base Adresse Nationale](https://adresse.data.gouv.fr) | Repérage d'un quartier ou d'une rue |

Le coworking beauté n'a pas de code NAF dédié : la recherche passe par des mots-clés (coworking, salon partagé). Les indépendants sont les entrepreneurs individuels des métiers beauté. Un praticien présent seulement sur Instagram s'ajoute à la main.

L'API répond par pages de 25. Sur une grande ville, la liste peut être plafonnée : précisez un code postal ou une rue. Le filtre de quartier ne garde que les adresses qui contiennent le mot saisi, car le quartier est rarement écrit dans SIRENE.

## Données locales

Statuts, notes et fiches sont enregistrés dans `data/companie.db`. Ce fichier n'est pas versionné. Ne le publiez pas s'il contient des notes de prospection.

## Structure

```text
run.py                 Lancement
app/main.py            Pages et actions
app/sirene.py          Client API Recherche d'entreprises
app/geo.py             Ville, code postal, quartier
app/db.py              Base SQLite
app/exporting.py       Export CSV
app/labels.py          Secteurs, statuts, sources
templates/             Interface
static/                Styles et scripts
search_cli.py          Script d'export CSV, sans interface
search_enterprise.py   Prototype Streamlit, optionnel
```

`search_cli.py` et `search_enterprise.py` sont des essais antérieurs. L'application à utiliser est `run.py`.

## Stack

Python, FastAPI, Jinja2, Tailwind CSS, Alpine.js, SQLite.
