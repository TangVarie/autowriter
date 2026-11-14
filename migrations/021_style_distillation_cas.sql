-- 2026-11-14 external audit F01: old-note reads must succeed before distillation.
-- Compare the task's revision, replace notes and mark only its edits in one transaction.
-- Run once, any time (before or after the deskcore deploy). Until it is applied the service
-- saves notes the way it did before 021 and logs one warning; afterwards it uses this function
-- without a redeploy. Rerunning is safe; no existing notes/edits are rewritten.

-- A revision must change even for two updates in one transaction, or after clock rollback.
CREATE OR REPLACE FUNCTION autowriter.deskcore_style_revision()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, autowriter
AS $$
BEGIN
    NEW.updated_at := GREATEST(clock_timestamp(), OLD.updated_at + INTERVAL '1 microsecond');
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS deskcore_user_calib_updated_at ON autowriter.user_calibration_notes;
CREATE TRIGGER deskcore_user_calib_updated_at
    BEFORE UPDATE ON autowriter.user_calibration_notes
    FOR EACH ROW EXECUTE FUNCTION autowriter.deskcore_style_revision();
REVOKE ALL ON FUNCTION autowriter.deskcore_style_revision() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_style_revision() TO service_role;

CREATE OR REPLACE FUNCTION autowriter.deskcore_save_style_cas(
    _project_id UUID,
    _user_id UUID,
    _notes TEXT,
    _expected_updated_at TIMESTAMPTZ,
    _edit_ids UUID[]
)
RETURNS TABLE(revision TIMESTAMPTZ, edits_absorbed INT)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, autowriter
AS $$
DECLARE
    current_revision TIMESTAMPTZ;
    note_exists BOOLEAN;
    wanted INT;
    available INT;
    absorbed INT;
    next_revision TIMESTAMPTZ;
BEGIN
    IF _notes IS NULL OR btrim(_notes) = '' THEN
        RAISE EXCEPTION 'notes must not be empty' USING ERRCODE = '22023';
    END IF;
    -- Also serializes first saves: SELECT FOR UPDATE alone cannot lock an absent row.
    PERFORM pg_advisory_xact_lock(hashtextextended(_project_id::TEXT || ':' || _user_id::TEXT, 0));
    SELECT n.updated_at INTO current_revision
      FROM autowriter.user_calibration_notes n
     WHERE n.project_id = _project_id AND n.user_id = _user_id
     FOR UPDATE;
    note_exists := FOUND;
    IF (note_exists AND current_revision IS DISTINCT FROM _expected_updated_at)
       OR (NOT note_exists AND _expected_updated_at IS NOT NULL) THEN
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM autowriter.projects p
                    WHERE p.id = _project_id AND p.owner_id = _user_id) THEN
        RAISE EXCEPTION 'project access denied' USING ERRCODE = '42501';
    END IF;
    SELECT count(DISTINCT e) INTO wanted FROM unnest(COALESCE(_edit_ids, ARRAY[]::UUID[])) e;
    PERFORM 1 FROM autowriter.style_edits e
     WHERE e.id = ANY(COALESCE(_edit_ids, ARRAY[]::UUID[]))
       AND e.project_id = _project_id AND e.user_id = _user_id AND NOT e.distilled
     FOR UPDATE;
    SELECT count(*) INTO available FROM autowriter.style_edits e
     WHERE e.id = ANY(COALESCE(_edit_ids, ARRAY[]::UUID[]))
       AND e.project_id = _project_id AND e.user_id = _user_id AND NOT e.distilled;
    IF available <> wanted THEN
        RETURN;
    END IF;
    IF note_exists THEN
        UPDATE autowriter.user_calibration_notes n SET notes = btrim(_notes)
         WHERE n.project_id = _project_id AND n.user_id = _user_id
         RETURNING n.updated_at INTO next_revision;
    ELSE
        -- A legacy writer does not take our advisory lock. Do not overwrite a first
        -- note it inserted after our missing-row read.
        INSERT INTO autowriter.user_calibration_notes (project_id, user_id, notes)
        VALUES (_project_id, _user_id, btrim(_notes))
        ON CONFLICT (project_id, user_id) DO NOTHING
        RETURNING updated_at INTO next_revision;
        IF NOT FOUND THEN
            RETURN;
        END IF;
    END IF;
    UPDATE autowriter.style_edits e SET distilled = TRUE
     WHERE e.id = ANY(COALESCE(_edit_ids, ARRAY[]::UUID[]))
       AND e.project_id = _project_id AND e.user_id = _user_id AND NOT e.distilled;
    GET DIAGNOSTICS absorbed = ROW_COUNT;
    RETURN QUERY SELECT next_revision, absorbed;
END;
$$;

REVOKE ALL ON FUNCTION autowriter.deskcore_save_style_cas(UUID, UUID, TEXT, TIMESTAMPTZ, UUID[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_save_style_cas(UUID, UUID, TEXT, TIMESTAMPTZ, UUID[]) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_save_style_cas(UUID, UUID, TEXT, TIMESTAMPTZ, UUID[]) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_save_style_cas(UUID, UUID, TEXT, TIMESTAMPTZ, UUID[]) TO service_role;
