"""Corpus intelligence: observations, classification, verdicts and feedback.

The corpus is global, so three different kinds of fact live beside a tender:

* ``tender_observations`` - the append-only trail of where a tender was seen.
  It answers "how did we find this, and when did we last see it there". The
  href and session are kept as evidence, but the durable keys (organisation,
  portal tender id, reference) are what a later run re-enters by, because a
  portal notice address is bound to the session that opened it.
* ``tender_classifications`` - what kind of work the tender is. This does not
  depend on any company, so it is computed once per tender and reused by every
  company's matching. Stamped with the rules version and the tender content
  hash, so improving the classifier invalidates it.
* ``company_tender_verdicts`` and ``company_relevance_feedback`` - the
  per-company judgement and the human signal behind it. A verdict is stamped
  with the hash of every input that produced it (profile, evidence, feedback,
  rules), so a profile edit or a new "not relevant" invalidates it instead of
  leaving a stale decision in place.

Revision ID: 0005_corpus_intelligence
Revises: 0004_tender_cycle
Create Date: 2026-11-16
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005_corpus_intelligence"
down_revision: Union[str, None] = "0004_tender_cycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A contains-search cannot use a btree index. Trigram keeps the read-time
    # gate and the search box an index lookup instead of a table scan.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tender_observations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tender_id uuid REFERENCES tenders(id) ON DELETE SET NULL,
            run_id uuid REFERENCES runs(id) ON DELETE SET NULL,
            portal_code text NOT NULL DEFAULT 'cppp',
            organisation text,
            listing_url text,
            list_page integer,
            row_position integer,
            portal_tender_id text,
            reference text,
            route_key text NOT NULL,
            href text,
            session_ref text,
            outcome text NOT NULL,
            reason text,
            observed_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_tender_observations_outcome CHECK (outcome IN
                ('stored','updated','rejected','missing','terminated','unreadable'))
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tender_observations_tender "
        "ON tender_observations (tender_id, observed_at DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tender_observations_run "
        "ON tender_observations (run_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tender_observations_route "
        "ON tender_observations (route_key, observed_at DESC);"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tender_classifications (
            tender_id uuid PRIMARY KEY REFERENCES tenders(id) ON DELETE CASCADE,
            primary_work_type text,
            work_types text[] NOT NULL DEFAULT '{}'::text[],
            confidence numeric(4,3),
            method text NOT NULL DEFAULT 'rules',
            rules_version text NOT NULL,
            input_hash text NOT NULL,
            reason text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_tender_classifications_method CHECK (method IN
                ('rules','model'))
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tender_classifications_types "
        "ON tender_classifications USING GIN (work_types);"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS company_tender_verdicts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
            bucket text NOT NULL,
            score numeric(6,4) NOT NULL DEFAULT 0,
            matched_terms text[] NOT NULL DEFAULT '{}'::text[],
            reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
            source text NOT NULL DEFAULT 'rules',
            input_hash text NOT NULL,
            rules_version text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_company_tender_verdicts UNIQUE (company_id, tender_id),
            CONSTRAINT ck_company_tender_verdicts_bucket CHECK (bucket IN
                ('strong','possible','hidden')),
            CONSTRAINT ck_company_tender_verdicts_source CHECK (source IN
                ('rules','model'))
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_company_tender_verdicts_company "
        "ON company_tender_verdicts (company_id, bucket);"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS company_relevance_feedback (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            tender_id uuid NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
            verdict text NOT NULL,
            work_types text[] NOT NULL DEFAULT '{}'::text[],
            matched_terms text[] NOT NULL DEFAULT '{}'::text[],
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_company_relevance_feedback UNIQUE (company_id, tender_id),
            CONSTRAINT ck_company_relevance_feedback_verdict CHECK (verdict IN
                ('relevant','not_relevant'))
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_company_relevance_feedback_company "
        "ON company_relevance_feedback (company_id, verdict, created_at DESC);"
    )

    # Corpus indexes the list, gate and audit actually read.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenders_work_title_trgm "
        "ON tenders USING GIN (work_title gin_trgm_ops);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenders_closing_at "
        "ON tenders (closing_at);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenders_created_at "
        "ON tenders (created_at DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_company_tenders_saved "
        "ON company_tenders (company_id, saved_at) WHERE saved_at IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_company_tenders_bidding "
        "ON company_tenders (company_id, bidding_at) WHERE bidding_at IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_discovery_candidates_run "
        "ON discovery_candidates (run_id, kind, outcome);"
    )


def downgrade() -> None:
    for index in (
        "ix_discovery_candidates_run",
        "ix_company_tenders_bidding",
        "ix_company_tenders_saved",
        "ix_tenders_created_at",
        "ix_tenders_closing_at",
        "ix_tenders_work_title_trgm",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index};")
    op.execute("DROP TABLE IF EXISTS company_relevance_feedback;")
    op.execute("DROP TABLE IF EXISTS company_tender_verdicts;")
    op.execute("DROP TABLE IF EXISTS tender_classifications;")
    op.execute("DROP TABLE IF EXISTS tender_observations;")
