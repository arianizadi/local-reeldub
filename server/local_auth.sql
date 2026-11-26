CREATE SCHEMA IF NOT EXISTS reeldub;
CREATE TABLE IF NOT EXISTS reeldub.access_keys (
    library_id uuid PRIMARY KEY,
    label text NOT NULL UNIQUE CHECK (length(label) BETWEEN 1 AND 80),
    token_hash bytea UNIQUE CHECK (octet_length(token_hash) = 32),
    generation uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
