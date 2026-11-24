#!/usr/bin/env bash
# Run with two libpq service names. The first connection only reads registration metadata.
set -euo pipefail
if [[ $# != 2 ]]; then
  echo "usage: $0 KNOWBENCH_READ_SERVICE KNOWMART_MIGRATION_SERVICE" >&2
  exit 2
fi
umask 077
snapshot=$(mktemp)
trap 'rm -f "$snapshot"' EXIT
PGSERVICE="$1" psql -X -q -v ON_ERROR_STOP=1 > "$snapshot" <<'SQL'
COPY (
    SELECT document->>'owner_subject', method_id, version, digest,
           CASE document#>>'{draft,definition,procedure,kind}'
               WHEN 'direct' THEN 'tool' WHEN 'orchestration' THEN 'expert' END
    FROM workbench_formal_methods
    WHERE caller = 'knowlab'
      AND document#>>'{reference,digest}' = digest
      AND document#>>'{reference,method_id}' = method_id::text
      AND document#>>'{reference,version}' = version::text
) TO STDOUT;
SQL
{
  cat <<'SQL'
BEGIN;
CREATE TEMP TABLE frozen_kinds (
    owner_subject text NOT NULL, method_id uuid NOT NULL, version integer NOT NULL,
    digest text NOT NULL, method_kind text NOT NULL CHECK (method_kind IN ('tool','expert')),
    PRIMARY KEY (owner_subject,method_id,version,digest)
) ON COMMIT DROP;
COPY frozen_kinds FROM STDIN;
SQL
  cat "$snapshot"
  cat <<'SQL'
\.
LOCK TABLE formal_method_listing IN SHARE ROW EXCLUSIVE MODE;
DO $$
BEGIN
  IF EXISTS (
    SELECT FROM formal_method_listing l LEFT JOIN frozen_kinds f
      USING (owner_subject,method_id,version,digest)
    WHERE f.method_id IS NULL OR (l.method_kind IS NOT NULL AND l.method_kind <> f.method_kind)
  ) THEN
    RAISE EXCEPTION 'A market version has no matching frozen registration or has conflicting kinds. Repeat export after in-flight registrations finish.';
  END IF;
END $$;
UPDATE formal_method_listing l SET method_kind=f.method_kind
FROM frozen_kinds f
WHERE (l.owner_subject,l.method_id,l.version,l.digest)=(f.owner_subject,f.method_id,f.version,f.digest)
  AND l.method_kind IS NULL;
COMMIT;
SQL
} | PGSERVICE="$2" psql -X -v ON_ERROR_STOP=1
