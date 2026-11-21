from app import db
from tests.conftest import manual_prospect


def item_sirene(**overrides):
    item = {
        "siret": "12345678900011", "siren": "123456789", "nom": "SALON TEST", "enseigne": "Salon Test",
        "secteur": "Coiffure & barbiers", "code_naf": "96.02A", "adresse": "3 Rue de Gand 59200 Tourcoing",
        "numero_voie": "3", "type_voie": "RUE", "libelle_voie": "DE GAND", "code_postal": "59200",
        "ville": "Tourcoing", "quartier": "", "latitude": "50.72", "longitude": "3.16",
        "date_creation": "2020-01-01", "date_creation_unite": "2020-01-01", "actif": 1,
        "effectif": "", "dirigeant": "", "independant": 0, "coworking": 0,
        "created_at": db.now(), "updated_at": db.now(),
    }
    item.update(overrides)
    return item


def prospect_id(siret="12345678900011"):
    return db.siret_ids()[siret]


def test_reimport_garde_le_travail_de_terrain(client):
    db.upsert_imported([item_sirene()])
    pid = prospect_id()
    db.update_prospect(pid, {"telephone": "06 12 34 56 78", "statut": "interesse", "notes_terrain": "4 postes"}, official=True)
    db.upsert_imported([item_sirene(effectif="3 à 5 salariés")])
    p = db.get_prospect(pid)
    assert (p["telephone"], p["statut"], p["notes_terrain"]) == ("06 12 34 56 78", "interesse", "4 postes")
    assert p["effectif"] == "3 à 5 salariés"


def test_reimport_ne_ressuscite_pas_un_salon_ferme(client):
    db.upsert_imported([item_sirene()])
    pid = prospect_id()
    db.mark_ferme(pid)
    db.upsert_imported([item_sirene(actif=1)])
    assert db.get_prospect(pid)["actif"] == 0


def test_reimport_sans_coordonnees_garde_la_position(client):
    db.upsert_imported([item_sirene(latitude="", longitude="")])
    pid = prospect_id()
    db.set_coords(pid, 50.7, 3.17)
    db.upsert_imported([item_sirene(latitude="", longitude="")])
    assert db.get_prospect(pid)["latitude"].startswith("50.7")


def test_recherche_par_numero_sans_espaces(client):
    manual_prospect(client, marque="Joignable", telephone="06 12 34 56 78")
    rows = db.query_prospects({"q": "0612345678"}, apply_q=True)
    assert [row["nom_affiche"] for row in rows] == ["Joignable"]
