import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    """Copie d'une base créée avant l'ajout des contacts et du journal."""
    source = Path(__file__).resolve().parent / "fixtures" / "legacy.db"
    target = tmp_path / "legacy.db"
    shutil.copy(source, target)
    monkeypatch.setattr(db, "DB_PATH", target)
    return target


def manual_prospect(client, **extra):
    data = {
        "marque": "Atelier Nord",
        "ville": "Roubaix",
        "code_postal": "59100",
        "metier": "96.02A",
        "source": "maps",
        "statut": "a_contacter",
        "actif": "1",
        **extra,
    }
    response = client.post("/prospects", data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].split("/")[2].split("?")[0])
