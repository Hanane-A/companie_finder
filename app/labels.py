from __future__ import annotations

STATUTS = (
    ("a_contacter", "À contacter"),
    ("contacte", "Contacté"),
    ("rdv", "Rendez-vous pris"),
    ("client", "Client"),
    ("refuse", "Refusé"),
)

SOURCES = (
    ("api_gouv", "API Gouv"),
    ("maps", "Maps"),
    ("reseaux", "Réseaux sociaux"),
)

OUTILS = (
    ("", "Non renseigné"),
    ("aucun", "Aucun outil repéré"),
    ("planity", "Planity"),
    ("treatwell", "Treatwell"),
    ("mindbody", "Mindbody"),
    ("booksy", "Booksy"),
    ("fresha", "Fresha"),
    ("kiute", "Kiute"),
    ("clicrdv", "ClicRDV"),
    ("reservio", "Reservio"),
    ("salonkee", "Salonkee"),
    ("google", "Google Réservation"),
    ("autre", "Autre"),
)

SITE_PRESENCE = (
    ("inconnu", "Non vérifié"),
    ("oui", "Oui, un site existe"),
    ("non", "Pas de site"),
)

NAF_LABELS = {
    "96.02A": "Coiffure & barbiers",
    "96.02B": "Soins de beauté & onglerie",
    "96.04Z": "Spas, hammams & bien-être",
}

SECTEURS = (
    {
        "id": "96.02A",
        "label": "Coiffure & barbiers",
        "code": "96.02A",
        "hint": "Salons de coiffure et barbershops",
    },
    {
        "id": "96.02B",
        "label": "Soins de beauté & onglerie",
        "code": "96.02B",
        "hint": "Instituts, esthétique, bars à ongles",
    },
    {
        "id": "96.04Z",
        "label": "Spas, hammams & bien-être",
        "code": "96.04Z",
        "hint": "Entretien corporel, spas et hammams",
    },
    {
        "id": "coworking",
        "label": "Coworking beauté",
        "code": "Mot-clé",
        "hint": "Salons partagés. Pas de code NAF dédié.",
    },
    {
        "id": "independant",
        "label": "Indépendants & freelances",
        "code": "EI",
        "hint": "Entrepreneurs individuels, souvent à domicile",
    },
)

METIERS = (
    ("96.02A", "Coiffure & barbiers"),
    ("96.02B", "Soins de beauté & onglerie"),
    ("96.04Z", "Spas, hammams & bien-être"),
    ("autre", "Autre / non déterminé"),
)

EFFECTIFS = {
    "00": "0 salarié",
    "01": "1 ou 2 salariés",
    "02": "3 à 5 salariés",
    "03": "6 à 9 salariés",
    "11": "10 à 19 salariés",
    "12": "20 à 49 salariés",
    "21": "50 à 99 salariés",
    "22": "100 à 199 salariés",
    "31": "200 à 249 salariés",
    "32": "250 à 499 salariés",
    "41": "500 à 999 salariés",
    "42": "1 000 à 1 999 salariés",
    "51": "2 000 à 4 999 salariés",
    "52": "5 000 à 9 999 salariés",
    "53": "10 000 salariés et plus",
}

STATUT_LABELS = dict(STATUTS)
SOURCE_LABELS = dict(SOURCES)
OUTIL_LABELS = dict(OUTILS)
SITE_LABELS = dict(SITE_PRESENCE)
SECTEUR_IDS = {item["id"] for item in SECTEURS}
STATUT_IDS = set(STATUT_LABELS)
SOURCE_IDS = set(SOURCE_LABELS)
OUTIL_IDS = set(OUTIL_LABELS)
SITE_IDS = set(SITE_LABELS)
METIER_IDS = {value for value, _label in METIERS}
NAF_IDS = set(NAF_LABELS)
DEFAULT_SECTEURS = ("96.02A", "96.02B", "96.04Z")
SANS_RUE = "Sans rue indiquée"
