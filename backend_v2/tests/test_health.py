from sqlalchemy import text

from app.core.db import SessionLocal


def test_health_reports_database_and_features(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["features"]["discovery"] is True


def test_initial_migration_created_the_corpus(client):
    with SessionLocal() as session:
        for table in ("tenders", "requirements", "lane_leases", "app_state"):
            exists = session.execute(
                text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}
            ).scalar()
            assert exists is not None, f"{table} was not created"
