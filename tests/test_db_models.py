"""Focused tests for the SQLAlchemy schema."""

import unittest

from app.db.models import Base, Company, JobPosting, JobType, StatusLog
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session


class DatabaseModelsTestCase(unittest.TestCase):
    """Exercise model metadata and relationships with an in-memory database."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_schema_contains_expected_tables_and_indexes(self) -> None:
        inspector = inspect(self.engine)

        self.assertEqual(
            set(inspector.get_table_names()), {"companies", "job_postings", "status_logs"}
        )
        index_names = {index["name"] for index in inspector.get_indexes("job_postings")}
        self.assertIn("ix_job_postings_discovery", index_names)
        self.assertIn("ix_job_postings_content_hash", index_names)

    def test_models_persist_with_relationships(self) -> None:
        company = Company(name="Example", domain="example.com")
        posting = JobPosting(
            company=company,
            title="Software Engineer Intern",
            base_hash="a" * 64,
            content_hash="b" * 64,
            apply_url="https://example.com/apply",
            location="New York, NY",
            season=2027,
            job_type=JobType.INTERNSHIP,
        )
        posting.status_logs.append(StatusLog(previous_state=None, new_state="new_role"))

        with Session(self.engine) as session:
            session.add(posting)
            session.commit()
            session.refresh(posting)

            self.assertFalse(posting.is_closed)
            self.assertEqual(posting.company.name, "Example")
            self.assertEqual(posting.status_logs[0].new_state, "new_role")
            self.assertIsNotNone(posting.created_at)


if __name__ == "__main__":
    unittest.main()
