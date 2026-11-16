CREATE TABLE IF NOT EXISTS users (
  name text PRIMARY KEY,
  org text NOT NULL,
  roles text[] NOT NULL,
  password text NOT NULL,
  version integer NOT NULL DEFAULT 1,
  active boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS sessions (
  token text PRIMARY KEY,
  username text NOT NULL REFERENCES users(name),
  user_version integer NOT NULL,
  csrf text NOT NULL,
  touched timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS org_controls (
  org text PRIMARY KEY,
  access_hold boolean NOT NULL DEFAULT false,
  delivery_hold boolean NOT NULL DEFAULT false,
  paused boolean NOT NULL DEFAULT false,
  hold_reason text,
  reference jsonb NOT NULL,
  policy integer NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS intake (
  id uuid PRIMARY KEY,
  org text NOT NULL,
  actor text NOT NULL,
  created timestamptz NOT NULL DEFAULT clock_timestamp(),
  name text NOT NULL,
  profile text NOT NULL,
  csv bytea,
  manifest bytea,
  csv_hash text NOT NULL,
  manifest_hash text NOT NULL,
  result jsonb NOT NULL,
  state text NOT NULL,
  revision integer NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cases (
  id uuid PRIMARY KEY,
  org text NOT NULL,
  profile text NOT NULL,
  kind text NOT NULL,
  business_key text NOT NULL,
  intake_id uuid NOT NULL REFERENCES intake(id),
  inserted timestamptz NOT NULL DEFAULT clock_timestamp(),
  deadline timestamptz,
  terminal timestamptz,
  data jsonb NOT NULL,
  UNIQUE(org, profile, kind, business_key)
);
CREATE INDEX IF NOT EXISTS cases_queue ON cases(org, deadline, inserted, id);
CREATE TABLE IF NOT EXISTS bundle_cases (
  bundle uuid NOT NULL REFERENCES intake(id),
  case_id uuid NOT NULL REFERENCES cases(id),
  PRIMARY KEY(bundle, case_id)
);
CREATE SEQUENCE IF NOT EXISTS application_audit_seq;
CREATE TABLE IF NOT EXISTS audit_heads (
  org text NOT NULL, scope text NOT NULL, hash text NOT NULL, PRIMARY KEY(org,scope)
);
CREATE TABLE IF NOT EXISTS audit (
  seq bigint PRIMARY KEY,
  org text NOT NULL,
  actor text NOT NULL,
  case_id uuid,
  event text NOT NULL,
  detail jsonb NOT NULL,
  recorded timestamptz NOT NULL,
  previous_hash text NOT NULL,
  hash text NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_org ON audit(org, seq);


CREATE OR REPLACE FUNCTION iew_audit_protect() RETURNS trigger
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
  IF TG_OP <> 'DELETE' THEN
    RAISE EXCEPTION 'Audit history is append-only';
  END IF;
  IF OLD.case_id IS NULL THEN
    IF OLD.recorded > statement_timestamp() - interval '180 days' THEN
      RAISE EXCEPTION 'Audit record is inside retention';
    END IF;
  ELSIF NOT EXISTS(SELECT 1 FROM cases WHERE id=OLD.case_id AND terminal <= statement_timestamp() - interval '180 days') THEN
    RAISE EXCEPTION 'Audit record is inside retention';
  END IF;
  RETURN OLD;
END;
$$;
CREATE OR REPLACE TRIGGER audit_row_protect BEFORE UPDATE OR DELETE ON audit
FOR EACH ROW EXECUTE FUNCTION iew_audit_protect();
CREATE OR REPLACE TRIGGER audit_truncate_protect BEFORE TRUNCATE ON audit
FOR EACH STATEMENT EXECUTE FUNCTION iew_audit_protect();

CREATE OR REPLACE FUNCTION iew_expire_audit(target_org text, reviewed_at timestamptz) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $$
BEGIN
  IF reviewed_at IS NULL OR reviewed_at > clock_timestamp() THEN
    RAISE EXCEPTION 'Audit expiry cannot use an unverified future clock';
  END IF;
  DELETE FROM audit WHERE org=target_org AND case_id IN (
    SELECT id FROM cases WHERE org=target_org AND terminal <= reviewed_at - interval '180 days');
  DELETE FROM audit WHERE org=target_org AND case_id IS NULL
    AND recorded <= reviewed_at - interval '180 days';
END;
$$;
REVOKE ALL ON FUNCTION iew_expire_audit(text,timestamptz) FROM PUBLIC;

CREATE OR REPLACE FUNCTION iew_case_terminal_clock() RETURNS trigger
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
DECLARE operator_role oid;
BEGIN
  SELECT relowner INTO operator_role FROM pg_catalog.pg_class WHERE oid=TG_RELID;
  IF pg_catalog.pg_has_role(current_user,operator_role,'MEMBER') THEN
    RETURN NEW;
  END IF;
  IF TG_OP='UPDATE' AND OLD.terminal IS NOT NULL THEN
    IF OLD.data->>'status'='Confirmed'
       AND (NEW.data->>'status' IS DISTINCT FROM 'Confirmed' OR NEW.terminal IS DISTINCT FROM OLD.terminal) THEN
      RAISE EXCEPTION 'Confirmed terminal evidence is immutable';
    END IF;
    IF NEW.terminal IS NOT NULL AND NEW.terminal IS DISTINCT FROM OLD.terminal THEN
      RAISE EXCEPTION 'Terminal resolution time cannot be rewritten';
    END IF;
  END IF;
  IF NEW.terminal IS NOT NULL AND (TG_OP='INSERT' OR OLD.terminal IS NULL) THEN
    NEW.terminal=statement_timestamp();
  END IF;
  RETURN NEW;
END;
$$;
CREATE OR REPLACE TRIGGER case_terminal_clock BEFORE INSERT OR UPDATE ON cases
FOR EACH ROW EXECUTE FUNCTION iew_case_terminal_clock();
