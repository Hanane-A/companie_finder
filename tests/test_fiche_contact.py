import sqlite3

from app import db
from app.text import clean_email, clean_instagram, clean_phone
from tests.conftest import manual_prospect


def test_numero_francais_normalise():
    assert clean_phone("0612345678") == ("06 12 34 56 78", "")
    assert clean_phone("+33 6 12 34 56 78") == ("06 12 34 56 78", "")
    assert clean_phone("06.12.34.56.78") == ("06 12 34 56 78", "")
    assert clean_phone("")[1] == ""
    assert clean_phone("0612")[1]


def test_email_et_instagram():
    assert clean_email("Contact@Salon.FR") == ("contact@salon.fr", "")
    assert clean_email("pas-un-email")[1]
    assert clean_instagram("https://www.instagram.com/atelier.nord/") == "atelier.nord"
    assert clean_instagram("@atelier.nord") == "atelier.nord"


def test_migration_garde_les_fiches_existantes(legacy_db):
    before = sqlite3.connect(legacy_db).execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
    db.init_db()
    conn = sqlite3.connect(legacy_db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(prospects)")}
    assert {"telephone", "email", "prochaine_action_at", "dernier_contact_at"} <= columns
    assert conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == before
    db.init_db()  # une deuxième migration ne casse rien


def test_creation_avec_contact(client):
    pid = manual_prospect(client, telephone="0612345678", email="hello@nord.fr", instagram="@atelier.nord")
    prospect = db.get_prospect(pid)
    assert prospect["telephone"] == "06 12 34 56 78"
    page = client.get(f"/prospects/{pid}").text
    assert 'href="tel:0612345678"' in page
    assert "https://wa.me/33612345678" in page


def test_telephone_invalide_refuse(client):
    response = client.post(
        "/prospects",
        data={"marque": "X", "ville": "Lille", "telephone": "123"},
    )
    assert response.status_code == 200
    assert "10 chiffres" in response.text
    assert db.count_all() == 0


def test_journal_change_statut_et_relance(client):
    pid = manual_prospect(client)
    response = client.post(
        f"/prospects/{pid}/journal",
        data={
            "type": "passage",
            "notes": "Gérante absente, repasser jeudi",
            "statut": "visite",
            "prochaine_action_at": "2026-11-19",
            "prochaine_action": "Repasser",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    prospect = db.get_prospect(pid)
    assert prospect["statut"] == "visite"
    assert prospect["prochaine_action_at"] == "2026-11-19"
    assert prospect["dernier_contact_at"]
    journal = db.list_interactions(pid)
    assert journal[0]["type"] == "passage"
    assert journal[0]["statut_avant"] == "a_contacter"
    assert journal[0]["statut_apres"] == "visite"


def test_journal_sans_date_garde_la_relance(client):
    pid = manual_prospect(client, prochaine_action_at="2026-12-01", prochaine_action="Rappeler")
    client.post(f"/prospects/{pid}/journal", data={"type": "appel", "notes": "Messagerie"})
    assert db.get_prospect(pid)["prochaine_action_at"] == "2026-12-01"


def test_client_ou_refus_efface_la_relance(client):
    pid = manual_prospect(client, prochaine_action_at="2026-12-01", prochaine_action="Rappeler")
    client.post(f"/prospects/{pid}/journal", data={"type": "demo", "statut": "client"})
    prospect = db.get_prospect(pid)
    assert prospect["statut"] == "client"
    assert prospect["prochaine_action_at"] == ""


def test_type_inconnu_refuse(client):
    pid = manual_prospect(client)
    response = client.post(f"/prospects/{pid}/journal", data={"type": "pirate"}, follow_redirects=False)
    assert "erreur_journal=type" in response.headers["location"]
    assert db.list_interactions(pid) == []


def test_changement_de_statut_rapide_journalise(client):
    pid = manual_prospect(client)
    assert client.post(f"/prospects/{pid}/statut", data={"statut": "interesse"}).json() == {"ok": True}
    assert client.post(f"/prospects/{pid}/statut", data={"statut": "interesse"}).json() == {"ok": True}
    journal = db.list_interactions(pid)
    assert len(journal) == 1  # remettre le même statut ne crée pas de doublon
    assert journal[0]["type"] == "statut"


def test_suppression_efface_le_journal(client):
    pid = manual_prospect(client)
    client.post(f"/prospects/{pid}/journal", data={"type": "appel"})
    client.post(f"/prospects/{pid}/supprimer")
    assert db.list_interactions(pid) == []


def test_export_contient_les_contacts(client):
    manual_prospect(client, telephone="0612345678")
    csv_text = client.get("/export.csv").content.decode("utf-8-sig")
    assert "Téléphone" in csv_text.splitlines()[0]
    assert "06 12 34 56 78" in csv_text


def test_sauvegarde_avant_migration(legacy_db):
    db.init_db()
    backups = list(legacy_db.parent.glob("legacy.avant-migration-*.db"))
    assert len(backups) == 1
    db.init_db()  # base déjà migrée : pas de nouvelle copie
    assert len(list(legacy_db.parent.glob("legacy.avant-migration-*.db"))) == 1
