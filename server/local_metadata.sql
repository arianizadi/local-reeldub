CREATE UNIQUE INDEX IF NOT EXISTS job_request ON reeldub.jobs ((doc->'owner'),(doc->'request_id'));
CREATE INDEX IF NOT EXISTS manual_queue_order ON reeldub.jobs ((doc->'state'),(doc->'queue_front') DESC NULLS LAST,(doc->'recovery_rank') DESC NULLS LAST,(doc->'priority') DESC NULLS LAST,(doc->'queue_seq'),(doc->'created'),id);
CREATE INDEX IF NOT EXISTS owner_activity ON reeldub.jobs ((doc->'owner'),(coalesce(doc->'last_requested',doc->'created')) DESC,id DESC);
CREATE INDEX IF NOT EXISTS owner_video_digest ON reeldub.jobs ((doc->'owner'),(md5(doc->>'url')),(doc->'target_language'));
CREATE INDEX IF NOT EXISTS sources_identity_digest ON reeldub.sources ((doc->'owner'),(md5(doc->>'identity')));
CREATE UNIQUE INDEX IF NOT EXISTS source_slot ON reeldub.sources ((doc->'owner'),(doc->'slot')) WHERE doc ? 'slot';
CREATE UNIQUE INDEX IF NOT EXISTS fleet_slot_running ON reeldub.jobs ((doc->>'fleet_slot'))
 WHERE doc->>'state'='running' AND doc->>'fleet_slot' IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_requests ON reeldub.jobs USING gin ((doc->'request_keys'));
CREATE TABLE IF NOT EXISTS reeldub.request_keys (
    owner text NOT NULL, request text NOT NULL, job jsonb NOT NULL REFERENCES reeldub.jobs(id) ON DELETE CASCADE,
    PRIMARY KEY(owner,request)
);
CREATE INDEX IF NOT EXISTS requests_job ON reeldub.request_keys(job);
CREATE OR REPLACE FUNCTION reeldub.local_job_keys() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,reeldub AS $$
BEGIN
    IF TG_OP='INSERT' OR OLD.doc->'request_keys' IS DISTINCT FROM NEW.doc->'request_keys'
       OR OLD.doc->'owner' IS DISTINCT FROM NEW.doc->'owner' THEN
        DELETE FROM reeldub.request_keys WHERE job=NEW.id;
        INSERT INTO reeldub.request_keys
            SELECT DISTINCT NEW.doc->>'owner',value,NEW.id FROM jsonb_array_elements_text(NEW.doc->'request_keys');
    END IF;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION reeldub.local_job_keys() FROM PUBLIC;
CREATE OR REPLACE TRIGGER job_keys AFTER INSERT OR UPDATE ON reeldub.jobs FOR EACH ROW EXECUTE FUNCTION reeldub.local_job_keys();
