-- queue_entry: one row per (as-of date, queue number). Re-loading the same page
-- for the same as-of date updates rows in place; a new as-of date adds new rows,
-- so history accumulates one snapshot per published date.
CREATE TABLE queue_entry (
    as_of                      date        NOT NULL,
    queue_number               text        NOT NULL,
    source                     text        NOT NULL,
    cluster_label              text,
    cluster_area               text,
    -- Parents this request was carried over from (serial ids or earlier cluster
    -- ids). More than one parent is possible.
    former_queue_numbers       text[]      NOT NULL DEFAULT '{}',
    -- NOTE: the utility resets this when a request transfers to a new cluster.
    request_date               date        NOT NULL,
    -- Keep in step with STATUS in normalize.py.
    status                     text        NOT NULL
        CHECK (status IN ('withdrawn', 'in_progress', 'suspended', 'in_service')),
    status_explanation         text,
    -- Set when a "Withdrawn" row is really a transfer ("Request moved to T2 Cluster").
    moved_to_cluster           text,
    company_name               text,
    service_type               text,
    application_rules          text,
    qualifying_facility        boolean,
    summer_mw                  numeric,
    winter_mw                  numeric,
    county                     text,
    state                      text,
    region                     text,
    point_of_interconnection   text,
    requested_cod              date,
    agreed_cod                 date,
    fuel_type                  text,
    facilities_study_available boolean,
    schedule_deviation_flagged boolean,
    -- The page's own header -> cell text, untouched.
    raw                        jsonb       NOT NULL,
    -- sha256 of every column above; a changed hash means the row is rewritten.
    content_hash               text        NOT NULL,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    created_by                 text        NOT NULL DEFAULT current_user,
    PRIMARY KEY (as_of, queue_number)
);

-- History of one project across snapshots, and lineage lookups by parent id.
CREATE INDEX queue_entry_queue_number_idx ON queue_entry (queue_number, as_of);
CREATE INDEX queue_entry_former_idx ON queue_entry USING gin (former_queue_numbers);

-- source_load: one row per load run, for provenance.
CREATE TABLE source_load (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source             text        NOT NULL,
    source_url         text        NOT NULL,
    as_of              date        NOT NULL,
    content_sha256     text        NOT NULL,
    http_last_modified text,
    http_etag          text,
    fetched_at         timestamptz NOT NULL,
    rows_parsed        integer     NOT NULL,
    rows_inserted      integer     NOT NULL,
    rows_updated       integer     NOT NULL,
    rows_unchanged     integer     NOT NULL,
    rows_deleted       integer     NOT NULL,
    issues             integer     NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    created_by         text        NOT NULL DEFAULT current_user
);
CREATE INDEX source_load_source_as_of_idx ON source_load (source, as_of);
