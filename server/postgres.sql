CREATE UNIQUE INDEX IF NOT EXISTS job_request ON reeldub.jobs ((doc->'owner'),(doc->'request_id'));
CREATE UNIQUE INDEX IF NOT EXISTS job_slot ON reeldub.jobs ((doc->'owner'),(doc->'day'),(doc->'slot')) WHERE doc ? 'slot';
CREATE UNIQUE INDEX IF NOT EXISTS job_extra ON reeldub.jobs ((doc->'extra_ticket')) WHERE doc ? 'extra_ticket';
CREATE UNIQUE INDEX IF NOT EXISTS job_skip ON reeldub.jobs ((doc->'skip_ticket')) WHERE doc ? 'skip_ticket';
CREATE UNIQUE INDEX IF NOT EXISTS job_render ON reeldub.jobs ((doc->'render_key')) WHERE doc ? 'render_key';
CREATE UNIQUE INDEX IF NOT EXISTS source_slot ON reeldub.sources ((doc->'owner'),(doc->'slot')) WHERE doc ? 'slot';
CREATE INDEX IF NOT EXISTS beta_owner ON reeldub.beta_allowances ((doc->'owner')) WHERE doc ? 'owner';
CREATE UNIQUE INDEX IF NOT EXISTS store_token ON reeldub.accounts ((doc->'store_token')) WHERE doc ? 'store_token';
CREATE UNIQUE INDEX IF NOT EXISTS binding_token ON reeldub.billing_bindings ((doc->'token'));
CREATE INDEX IF NOT EXISTS queue_order ON reeldub.jobs ((doc->'state'),(doc->'priority') DESC,(doc->'queue_seq'),(doc->'created'));
CREATE INDEX IF NOT EXISTS owner_url ON reeldub.jobs ((doc->'owner'),(doc->'url'),(doc->'target_language'));
CREATE INDEX IF NOT EXISTS owner_activity ON reeldub.jobs ((doc->'owner'),(coalesce(doc->'last_requested',doc->'created')) DESC,id DESC);
CREATE INDEX IF NOT EXISTS purchases_owner ON reeldub.purchases ((doc->'owner'),(doc->'environment'));
CREATE INDEX IF NOT EXISTS sources_owner ON reeldub.sources ((doc->'owner'),(doc->'identity'));
CREATE INDEX IF NOT EXISTS jobs_minutes ON reeldub.jobs USING gin ((doc->'minute_grants'));
CREATE INDEX IF NOT EXISTS jobs_requests ON reeldub.jobs USING gin ((doc->'request_keys'));
CREATE TABLE IF NOT EXISTS reeldub.spend_keys (unit text PRIMARY KEY, job jsonb NOT NULL REFERENCES reeldub.jobs(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS reeldub.request_keys (owner text NOT NULL, request text NOT NULL, job jsonb NOT NULL REFERENCES reeldub.jobs(id) ON DELETE CASCADE, PRIMARY KEY(owner,request));
CREATE INDEX IF NOT EXISTS spend_job ON reeldub.spend_keys(job);
CREATE INDEX IF NOT EXISTS requests_job ON reeldub.request_keys(job);
CREATE OR REPLACE FUNCTION reeldub.job_keys() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,reeldub AS $$
BEGIN
 IF TG_OP='INSERT' OR OLD.doc->'minute_units' IS DISTINCT FROM NEW.doc->'minute_units' THEN
   DELETE FROM reeldub.spend_keys WHERE job=NEW.id;
   INSERT INTO reeldub.spend_keys SELECT DISTINCT value,NEW.id FROM jsonb_array_elements_text(NEW.doc->'minute_units');
 END IF;
 IF TG_OP='INSERT' OR OLD.doc->'request_keys' IS DISTINCT FROM NEW.doc->'request_keys' OR OLD.doc->'owner' IS DISTINCT FROM NEW.doc->'owner' THEN
   DELETE FROM reeldub.request_keys WHERE job=NEW.id;
   INSERT INTO reeldub.request_keys SELECT DISTINCT NEW.doc->>'owner',value,NEW.id FROM jsonb_array_elements_text(NEW.doc->'request_keys');
 END IF;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION reeldub.job_keys() FROM PUBLIC;
CREATE OR REPLACE TRIGGER job_keys AFTER INSERT OR UPDATE ON reeldub.jobs FOR EACH ROW EXECUTE FUNCTION reeldub.job_keys();
