from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models.paper_access_grant import PaperAccessGrant
from app.services.paper_access_service import (
    can_access_paper,
    grant_paper_access,
)


def test_private_and_shared_paper_access_are_distinct():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE papers (paper_id VARCHAR(64) PRIMARY KEY, title TEXT)"
        ))
        PaperAccessGrant.__table__.create(connection)
        connection.execute(text(
            "INSERT INTO papers (paper_id, title) VALUES "
            "('paper_private', 'Private upload'), "
            "('paper_shared', 'Shared catalog paper')"
        ))
    with Session(engine) as db:
        grant_paper_access(db, "paper_private", "molyte-user-a")
        grant_paper_access(db, "paper_shared", None)
        db.commit()

        assert can_access_paper(db, "paper_private", "molyte-user-a") is True
        assert can_access_paper(db, "paper_private", "molyte-user-b") is False
        assert can_access_paper(db, "paper_shared", "molyte-user-a") is True
        assert can_access_paper(db, "paper_shared", "molyte-user-b") is True
        assert can_access_paper(db, "paper_private", None) is True
