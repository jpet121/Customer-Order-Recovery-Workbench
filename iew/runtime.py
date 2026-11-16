"""Separate operator-owned migrations from restricted application connections."""
from psycopg import sql

from .store import Database


def require_runtime(database: Database) -> None:
    with database.connect() as db:
        role = db.execute(
            "SELECT rolsuper,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=current_user").fetchone()
        owned = db.execute(
            """SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname=current_schema() AND c.relkind IN ('r','S')
                 AND pg_has_role(current_user,c.relowner,'MEMBER')) AS owned""").fetchone()
        writable_schema = db.execute(
            "SELECT has_schema_privilege(current_user,current_schema(),'CREATE') AS allowed").fetchone()
        if (not role or any(role.values()) or not owned or owned["owned"]
                or not writable_schema or writable_schema["allowed"]):
            raise ValueError("Use a restricted runtime database role, not the migration owner. Run grant-runtime as the operator first.")


def grant_runtime(database: Database, role: str) -> None:
    with database.connect() as db:
        user = db.execute("SELECT rolsuper,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
        if not user or any(user.values()):
            raise ValueError("Create a non-superuser runtime role without CREATEDB or CREATEROLE first.")
        schema = db.execute("SELECT current_schema() AS name").fetchone()
        assert schema is not None
        namespace, principal = sql.Identifier(schema["name"]), sql.Identifier(role)
        owner = db.execute(
            """SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname=current_schema() AND c.relkind IN ('r','S')
                 AND pg_has_role(%s,c.relowner,'MEMBER')) AS owned""", (role,)).fetchone()
        if owner and owner["owned"]:
            raise ValueError("Runtime role must not own, or be a member of the owner of, application objects.")
        db.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(namespace, principal))
        db.execute(sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(namespace, principal))
        db.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(namespace, principal))
        db.execute(sql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(namespace, principal))
        for table in ("audit", "delivery_attempts"):
            present = db.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()
            if present and present["name"]:
                db.execute(sql.SQL("REVOKE UPDATE,DELETE,TRUNCATE ON {}.{} FROM {}").format(namespace,sql.Identifier(table),principal))
        present = db.execute("SELECT to_regclass('iew_schema_version') AS name").fetchone()
        if present and present["name"]:
            db.execute(sql.SQL("REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON {}.iew_schema_version FROM {}").format(namespace,principal))
        present = db.execute("SELECT to_regprocedure('iew_expire_audit(text,timestamp with time zone)') AS name").fetchone()
        if present and present["name"]:
            db.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {}.iew_expire_audit(text,timestamptz) TO {}").format(namespace,principal))
