-- W-19 rollout config, prepared only. Save the pre-update value before execution.
-- Existing db.create_project stored JSON objects as a jsonb string; preserve that representation.
BEGIN;
SELECT id, owner_id, name, default_params
FROM autowriter.projects
WHERE id = '59d5ae92-4735-48a2-a376-40079dc733ac'
  AND owner_id = '85f5f888-a649-4843-8358-9c83c91282e6' AND brand = 'Hatherine';

UPDATE autowriter.projects
SET default_params = to_jsonb((
  CASE WHEN jsonb_typeof(default_params) = 'string'
       THEN (default_params #>> '{}')::jsonb
       ELSE coalesce(default_params, '{}'::jsonb) END
  || '{"isolation_terms":["痘痘贴","祛痘","去痘","抑痘","76片"]}'::jsonb
)::text)
WHERE id = '59d5ae92-4735-48a2-a376-40079dc733ac'
  AND owner_id = '85f5f888-a649-4843-8358-9c83c91282e6' AND brand = 'Hatherine'
RETURNING id, name, default_params;
COMMIT;
