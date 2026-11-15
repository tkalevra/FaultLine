"""Static ORDER census of the migration corpus (gauntlet first-boot-migration-corpus, FB4).

WHAT IT ANSWERS. For every `.sql` migration, in the bytewise sort order the boot applies them:
does each table / column / constraint the file references ALREADY EXIST at that point, and does
every literal a file writes into a CHECK-constrained column satisfy the constraint AS IT STANDS
at that point? A migration that references an object a LATER file creates is rejected by
PostgreSQL on every fresh database — it is only ever "healed" by a second boot, and under the
boot ledger the healing boot re-runs the failed file ALONE, after everything it was supposed to
precede. Six files in this corpus were exactly that (003, 016, 060, 073, 089, 110); this census
reproduces all six from the pre-fix corpus and none from the fixed one.

HOW. `sqlparse` splits each file into statements and strips comments; each statement is then
classified by its leading keywords and its identifiers are read from the statement text
(schema-qualified `public.x` collapses to `x`; system catalogs are ignored). A `DO $$ ... $$`
block is opened and its body split into inner statements (dollar-quote and parenthesis aware;
`IF ... THEN <stmt>` yields `<stmt>`), and two things make a reference inside a block
CONDITIONAL rather than red:
  * a GUARD — a string literal naming the object inside an `EXISTS (SELECT ... FROM
    information_schema.* / pg_catalog.*)` test anywhere in the block (`column_name = 'x'`,
    `conname = 'x'`, `table_name = 'x'`), the shape every guarded migration in this corpus uses;
  * an EXCEPTION handler that catches the class the miss would raise (`WHEN OTHERS`,
    `undefined_table`, `undefined_column`, `undefined_object`).
READ-SIDE REFERENCES (round 2). Beyond the columns a statement WRITES, every column-shaped
identifier a statement READS is checked: `UPDATE ... SET <expr> ... WHERE <expr>`, `DELETE ...
WHERE`, `INSERT ... SELECT`, `ON CONFLICT DO UPDATE SET/WHERE`, `CREATE INDEX ... WHERE`,
subqueries at any depth (their FROM tables form an inner scope in front of the enclosing one,
so correlated references resolve), `SELECT ... INTO` / `FOR r IN SELECT` / `IF EXISTS (SELECT
...)` inside DO bodies. String literals are blanked first; function calls, casts, keywords,
aliases, plpgsql variables (DECLAREd names, `_x`, `v_x`, loop variables) and `$n` parameters are
not columns. A subquery over a system catalog or a derived / set-returning source is a wildcard.

DYNAMIC FAN-OUTS (round 2). `EXECUTE format('... %I.table ...', _schema)` inside a loop over
`faultline\\_%` resolves `table` against the TENANT template (src/provisioning/templates/
user_schema.sql, `{schema_name}.` stripped) plus whatever fan-out DDL the corpus itself adds; a
loop over `flagent\\_%` resolves against the cortex template. `%L` / `%s` / `$n` become opaque
literals. Findings on those statements are tagged `tenant:` / `cortex:`. That arm is what catches
084's `DELETE FROM %I.facts WHERE subject_id = user_id` — no `user_id` on a tenant's facts. What is
STILL skipped, counted in `dynamic_skipped` and itemised in `skipped_detail`: `EXECUTE <variable>`
(no template text), a `%I` / `%s` that substitutes a table, column or constraint NAME rather than a
schema qualifier (`ALTER TABLE %I ...`, `DROP CONSTRAINT %I`), a `'a' || <non-schema variable>`
concatenation, and loops over a schema family with no template (`seed_%`). Two more shapes are
MODELLED rather than skipped (round 3): `EXECUTE 'a' || _schema || '.table ...'` chains (literal
pieces joined, a schema variable becomes the %I qualifier) and `EXECUTE format('SET search_path
TO %I', _schema)`, after which the plain statements of that block are judged against the tenant
(or cortex) template until RESET / block end. Measured on the fixed corpus: 12 of ~330 dynamic
statements skipped, every one itemised in `skipped_detail`.

PL/pgSQL FUNCTION bodies are skipped on purpose: PostgreSQL does not resolve table references
in a plpgsql body at CREATE time (`check_function_bodies` validates syntax only), so an early
body naming a later table is not a boot error — 003's `promote_staged_fact` was created fine
while its two DO blocks failed.

This is a CENSUS, not a parser of SQL: it models the handful of DDL / DML shapes this corpus
uses (CREATE TABLE, ALTER TABLE ADD/DROP/RENAME COLUMN + ADD/DROP CONSTRAINT, CREATE INDEX /
TRIGGER, INSERT ... VALUES, UPDATE ... SET, DELETE, COMMENT ON) and reports anything it cannot
classify as `unmodelled` so a reviewer can see the boundary. Findings are (file, kind, object,
detail) tuples; `kind` ∈ {missing_table, missing_column, missing_constraint, check_violation}.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

import sqlparse

_SYSTEM_PREFIXES = ("pg_", "information_schema")
_HERE = os.path.dirname(os.path.abspath(__file__))
_TENANT_TEMPLATE = os.path.join(os.path.dirname(_HERE), "src", "provisioning", "templates", "user_schema.sql")
_CORTEX_TEMPLATE = os.path.join(os.path.dirname(_HERE), "src", "provisioning", "templates", "cortex_schema.sql")
# Objects that exist BEFORE the first migration runs on a fresh database.
_PRE_EXISTING_TABLES = {
    # created by src/provisioning/boot_migrations.py::_ensure_ledger before any file runs
    "schema_migration_status": {"schema_name", "migration_id", "status", "checksum",
                                "started_at", "applied_at", "error"},
}


@dataclass
class Column:
    allowed: Optional[FrozenSet[str]] = None  # literal values a CHECK admits; None = unknown/none


@dataclass
class Table:
    columns: Dict[str, Column] = field(default_factory=dict)
    constraints: Dict[str, Tuple[Optional[str], Optional[FrozenSet[str]]]] = field(default_factory=dict)


@dataclass
class Finding:
    file: str
    kind: str
    obj: str
    detail: str

    def as_tuple(self) -> Tuple[str, str, str]:
        return (self.file, self.kind, self.obj)


@dataclass
class CensusResult:
    findings: List[Finding] = field(default_factory=list)
    dynamic_skipped: int = 0
    skipped_detail: List[Tuple[str, str, str]] = field(default_factory=list)  # (file, why, text[:80])
    unmodelled: List[Tuple[str, str]] = field(default_factory=list)
    statements: int = 0
    files: int = 0

    @property
    def red_files(self) -> List[str]:
        return sorted({f.file for f in self.findings})


# ── text helpers ──────────────────────────────────────────────────────────────────────
_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)'
_QNAME = rf'(?:{_IDENT}\.)?{_IDENT}'


def _norm(name: str) -> str:
    name = name.strip().strip('"')
    if "." in name:
        schema, _, bare = name.rpartition(".")
        schema = schema.strip('"').lower()
        if schema in ("public", ""):
            return bare.strip('"').lower()
        if schema.startswith(_SYSTEM_PREFIXES):
            return "pg_catalog." + bare.strip('"').lower()
        return name.lower()  # foreign schema — reported as is
    return name.lower()


def _is_system(name: str) -> bool:
    return name.startswith("pg_catalog.") or name.startswith(_SYSTEM_PREFIXES)


def _strip_comments_quote_aware(text: str) -> str:
    """Drop `-- ...` comments outside quotes / dollar-quotes (a regex literal may contain `--`)."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(text[i:j + 1]); i = j + 1; continue
        if ch == "$":
            m = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", text[i:])
            if m:
                tag = m.group(0)
                j = text.find(tag, i + len(tag))
                if j == -1:
                    j = n
                out.append(text[i:j + len(tag)]); i = j + len(tag); continue
        if ch == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        out.append(ch); i += 1
    return "".join(out)


def _split_top_level(text: str, sep: str = ",") -> List[str]:
    """Split on `sep` at parenthesis depth 0, outside quotes and dollar-quotes."""
    parts, depth, cur, i, n = [], 0, [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            cur.append(text[i:j + 1]); i = j + 1; continue
        if ch == "$":
            m = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", text[i:])
            if m:
                tag = m.group(0)
                j = text.find(tag, i + len(tag))
                j = n if j == -1 else j
                cur.append(text[i:j + len(tag)]); i = j + len(tag); continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(ch)
        i += 1
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def _literals(text: str) -> List[str]:
    return [m.group(1).replace("''", "'") for m in re.finditer(r"'((?:[^']|'')*)'", text)]


def _parse_check_allowed(check_body: str) -> Tuple[Optional[str], Optional[FrozenSet[str]]]:
    """`col IN ('a','b')` / `col = ANY (ARRAY['a','b'])` / `col IS NULL OR col IN (...)` →
    (col, allowed). Anything else → (col-or-None, None) = unknown, never asserted."""
    body = check_body.strip()
    m = re.match(rf"^\(?\s*({_IDENT})\s+IS\s+NULL\s+OR\s+(.*)$", body, re.I | re.S)
    nullable = False
    if m:
        nullable = True
        body = m.group(2)
    m = re.match(rf"^\(?\s*({_IDENT})\s+IN\s*\((.*)\)\s*\)?\s*$", body, re.I | re.S)
    if not m:
        m = re.match(rf"^\(?\s*({_IDENT})\s*=\s*ANY\s*\(\s*ARRAY\s*\[(.*)\]\s*(?:::\s*TEXT\[\])?\s*\)\s*\)?\s*$",
                     body, re.I | re.S)
    if not m:
        # a bare column followed by an operator we do not model (`col > 0`, `col ~ '...'`) still
        # names the column; a function call (`jsonb_typeof(col) = 'object'`, `length(x) > 0`)
        # names nothing the model may treat as a column.
        cm = re.match(rf"^\(?\s*({_IDENT})\s*(?:[<>=!~]|IS\b|IN\b|NOT\b|BETWEEN\b|LIKE\b|SIMILAR\b|@)", body, re.I)
        return (cm.group(1).strip('"').lower() if cm else None), None
    col = m.group(1).strip('"').lower()
    vals = set(_literals(m.group(2)))
    if nullable:
        vals.add("\0NULL")
    return col, frozenset(vals)


_CTRL_START = re.compile(r"^(IF|ELSIF|ELSE|END|LOOP|FOR|WHILE|RAISE|PERFORM|RETURN|EXCEPTION|WHEN|CONTINUE|"
                         r"EXIT|GET\s+DIAGNOSTICS|DECLARE|BEGIN|NULL|COMMIT|ROLLBACK)\b", re.I)


# ── the model ─────────────────────────────────────────────────────────────────────────
class SchemaModel:
    def __init__(self, pre_existing: bool = True) -> None:
        self.tables: Dict[str, Table] = {}
        if pre_existing:  # the boot gate's ledger exists in PUBLIC before any file runs — nowhere else
            for t, cols in _PRE_EXISTING_TABLES.items():
                self.tables[t] = Table(columns={c: Column() for c in cols})

    # creation ---------------------------------------------------------------------
    def create_table(self, name: str, body: str, if_not_exists: bool) -> List[str]:
        """Returns tables the body REFERENCES (FKs)."""
        if name in self.tables and if_not_exists:
            return []
        table = Table()
        refs: List[str] = []
        for item in _split_top_level(body):
            item = item.strip()
            if not item:
                continue
            up = item.upper()
            m_ref = re.search(rf"REFERENCES\s+({_QNAME})\s*\(", item, re.I)
            if m_ref:
                refs.append(_norm(m_ref.group(1)))
            if up.startswith("CONSTRAINT"):
                m = re.match(rf"CONSTRAINT\s+({_IDENT})\s+(.*)$", item, re.I | re.S)
                cname = m.group(1).strip('"').lower()
                rest = m.group(2)
                if rest.upper().lstrip().startswith("CHECK"):
                    col, allowed = _parse_check_allowed(rest[rest.upper().find("CHECK") + 5:].strip())
                    table.constraints[cname] = (col, allowed)
                else:
                    table.constraints[cname] = (None, None)
                continue
            if up.startswith(("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK", "EXCLUDE", "LIKE")):
                if up.startswith("CHECK"):
                    col, allowed = _parse_check_allowed(item[5:].strip())
                    if col:
                        table.constraints[f"{name}_{col}_check"] = (col, allowed)
                continue
            m = re.match(rf"({_IDENT})\s+(.*)$", item, re.S)
            if not m:
                continue
            col = m.group(1).strip('"').lower()
            column = Column()
            mc = re.search(r"\bCHECK\s*\((.*)\)\s*$", m.group(2), re.I | re.S)
            if mc:
                _c, allowed = _parse_check_allowed(mc.group(1))
                column.allowed = allowed
                table.constraints[f"{name}_{col}_check"] = (col, allowed)
            table.columns[col] = column
        # table-level CHECKs bind to columns that exist (never mint a column from a constraint)
        for _cname, (ccol, allowed) in table.constraints.items():
            if ccol in table.columns and allowed is not None:
                table.columns[ccol].allowed = allowed
        self.tables[name] = table
        return refs

    def add_column(self, tname: str, col: str, rest: str) -> None:
        table = self.tables[tname]
        column = table.columns.get(col) or Column()
        mc = re.search(r"\bCHECK\s*\((.*)\)\s*$", rest, re.I | re.S)
        if mc:
            _c, allowed = _parse_check_allowed(mc.group(1))
            column.allowed = allowed
            table.constraints[f"{tname}_{col}_check"] = (col, allowed)
        table.columns[col] = column

    def add_constraint(self, tname: str, cname: str, rest: str) -> None:
        table = self.tables[tname]
        if rest.upper().lstrip().startswith("CHECK"):
            col, allowed = _parse_check_allowed(rest[rest.upper().find("CHECK") + 5:].strip())
            table.constraints[cname] = (col, allowed)
            if col in table.columns and allowed is not None:
                table.columns[col].allowed = allowed  # never mint a column from a constraint
        else:
            table.constraints[cname] = (None, None)

    def drop_constraint(self, tname: str, cname: str) -> None:
        table = self.tables[tname]
        col, _allowed = table.constraints.pop(cname, (None, None))
        if col and col in table.columns:
            # another named CHECK on the same column may still stand
            still = [a for (c, a) in table.constraints.values() if c == col and a is not None]
            table.columns[col].allowed = still[0] if still else None


# ── read-side expression census ─────────────────────────────────────────────────────
# Words that appear as bare identifiers in expressions but are never column names in this
# corpus's SQL dialect: reserved words, operators spelled as words, EXTRACT fields, literals.
_SQL_WORDS = frozenset("""
select from where and or not in is null true false unknown case when then else end exists between
like ilike similar to escape any all some array interval as on using join inner left right full outer
cross lateral natural union intersect except distinct order by asc desc nulls first last limit offset
group having returning with recursive values default excluded row over partition filter within
current_timestamp current_date current_time localtimestamp localtime now at time zone collate
cast coalesce nullif greatest least epoch year month day hour minute second dow doy week quarter
century decade millennium microseconds milliseconds timezone timezone_hour timezone_minute isodow isoyear
text int integer bigint smallint boolean bool float real numeric decimal uuid jsonb json date timestamp
timestamptz timetz varchar char bytea interval double precision serial bigserial regclass name oid
new old into set update delete insert only table do begin declare loop for foreach if elsif exception
raise notice perform return next query strict found diagnostics row_count get continue exit while
symmetric asymmetric leading trailing both placing overlaps isnull notnull ordinality
""".split())
_ID_RE = re.compile(r'(?<![\w"$.:])("?[A-Za-z_][A-Za-z0-9_]*"?(?:\s*\.\s*"?[A-Za-z_][A-Za-z0-9_]*"?)?)')


def _blank_literals(text: str) -> str:
    """Replace every string literal (incl. E'..'/dollar-quoted) with '' so its words are never
    read as identifiers, keeping the text length-stable enough for position work."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'" or (ch in "EeBbXxNn" and i + 1 < n and text[i + 1] == "'" and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_"))):
            escapes = ch in "Ee"
            j = i + 1 if ch == "'" else i + 2
            while j < n:
                if escapes and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append("''"); i = j + 1; continue
        if ch == "$":
            m = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", text[i:])
            if m:
                tag = m.group(0)
                j = text.find(tag, i + len(tag))
                j = n if j == -1 else j
                out.append("''"); i = j + len(tag); continue
        out.append(ch); i += 1
    return "".join(out)


_COLUMN_ALIASES: Set[str] = set()


def self_declared_add(name: str) -> None:
    _COLUMN_ALIASES.add(name)


def _from_tables(clause: str) -> List[Tuple[Optional[str], str]]:
    """`FROM a [AS] x JOIN b y ON ...` → [(x, a), (y, b)] — comma lists and JOINs, no subquery
    sources (those are skipped: an unmodelled source yields a wildcard scope, never a red)."""
    out: List[Tuple[Optional[str], str]] = []
    parts = re.split(r"\b(?:LEFT|RIGHT|FULL|INNER|CROSS|NATURAL|OUTER)?\s*(?:OUTER\s+)?JOIN\b", clause, flags=re.I)
    for part in parts:
        for item in _split_top_level(part):
            item = item.strip()
            item = re.split(r"\b(?:ON|USING|WHERE|GROUP|ORDER|LIMIT|HAVING|RETURNING|SET)\b", item, maxsplit=1, flags=re.I)[0].strip()
            if not item:
                continue
            if item.startswith("(") or "(" in item:
                # derived table / set-returning function source → wildcard, alias kept
                ma = re.search(rf"\)\s*(?:AS\s+)?({_IDENT})\s*(?:\(([^)]*)\))?\s*$", item, re.I)
                alias = ma.group(1).strip('"').lower() if ma else None
                out.append((alias, "*"))
                if ma and ma.group(2):  # `unnest(...) AS u(col)` — column aliases are variables
                    for c in ma.group(2).split(","):
                        self_declared_add(c.strip().strip('"').lower())
                continue
            m = re.match(rf"({_QNAME})(?:\s+(?:AS\s+)?({_IDENT}))?\s*$", item, re.I)
            if m:
                alias = m.group(2).strip('"').lower() if m.group(2) and m.group(2).upper() not in ("ONLY",) else None
                out.append((alias, _norm(m.group(1))))
    return out


# ── the census walk ───────────────────────────────────────────────────────────────────
class Census:
    def __init__(self, tenant_template: Optional[str] = None, cortex_template: Optional[str] = None) -> None:
        self.model = SchemaModel()
        self.public = self.model
        self.result = CensusResult()
        self._file = ""
        self.scope = "public"
        self._declared: Set[str] = set()  # plpgsql variables of the block being walked
        self._path_scope: Optional[str] = None  # after `EXECUTE format('SET search_path TO %I')` in a block
        # Tenant / cortex fan-outs (`EXECUTE format('... %I.table ...')` over faultline_% /
        # flagent_%) resolve against the TEMPLATE the schemas are minted from — plus whatever
        # fan-out DDL the corpus itself adds to them as it goes.
        self.tenant = self._load_template(tenant_template or _TENANT_TEMPLATE)
        self.cortex = self._load_template(cortex_template or _CORTEX_TEMPLATE)

    @staticmethod
    def _load_template(path: Optional[str]) -> Optional[SchemaModel]:
        if not path or not os.path.exists(path):
            return None
        sub = Census.__new__(Census)
        sub.model = SchemaModel(pre_existing=False); sub.public = sub.model; sub.result = CensusResult(); sub._file = "template"
        sub.scope = "public"; sub._declared = set(); sub.tenant = None; sub.cortex = None; sub._path_scope = None
        with open(path, encoding="utf-8") as fh:
            text = fh.read().replace("{schema_name}.", "")
        text = _strip_comments_quote_aware(text)
        for stmt in sqlparse.split(text):
            stmt = stmt.strip().rstrip(";").strip()
            if stmt:
                sub.statement(stmt, guard=set(), handlers=set())
        return sub.model

    # findings ------------------------------------------------------------------------
    def _red(self, kind: str, obj: str, detail: str, guard: Set[str], handlers: Set[str]) -> None:
        bare = obj.rsplit(".", 1)[-1]
        if f"any:{bare}" in guard or f"any:{obj}" in guard:
            return  # conditional reference — the block tests for it first
        if kind == "missing_table" and f"table:{obj}" in guard:
            return
        if kind == "missing_column" and (f"column:{obj}" in guard or f"column:*.{bare}" in guard):
            return
        if kind == "missing_constraint" and f"constraint:{bare}" in guard:
            return
        if "others" in handlers:
            return
        if kind == "missing_table" and "undefined_table" in handlers:
            return
        if kind == "missing_column" and "undefined_column" in handlers:
            return
        if kind == "missing_constraint" and "undefined_object" in handlers:
            return
        tag = "" if self.scope == "public" else f"{self.scope}:"
        self.result.findings.append(Finding(self._file, kind, tag + obj, detail))

    def _need_table(self, name: str, ctx: str, guard: Set[str], handlers: Set[str]) -> bool:
        name = _norm(name)
        if _is_system(name):
            return False
        if name not in self.model.tables:
            self._red("missing_table", name, ctx, guard, handlers)
            return False
        return True

    def _need_column(self, tname: str, col: str, ctx: str, guard: Set[str], handlers: Set[str]) -> bool:
        tname = _norm(tname)
        col = col.strip().strip('"').lower()
        table = self.model.tables.get(tname)
        if table is None:
            return False
        if col not in table.columns and "*" not in table.columns:
            self._red("missing_column", f"{tname}.{col}", ctx, guard, handlers)
            return False
        return True

    def _check_value(self, tname: str, col: str, value: str, ctx: str, guard: Set[str], handlers: Set[str]) -> None:
        table = self.model.tables.get(_norm(tname))
        if table is None:
            return
        column = table.columns.get(col.strip('"').lower())
        if column is None or column.allowed is None:
            return
        if value not in column.allowed:
            if "check_violation" in handlers or "others" in handlers:
                return
            tag = "" if self.scope == "public" else f"{self.scope}:"
            self.result.findings.append(Finding(
                self._file, "check_violation", tag + f"{_norm(tname)}.{col}",
                f"{ctx}: literal {value!r} is not in CHECK set {sorted(v for v in column.allowed if v != chr(0) + 'NULL')}"))

    # walk ----------------------------------------------------------------------------
    def run(self, migrations_dir: str, only_before: Optional[str] = None) -> CensusResult:
        for name in sorted(os.listdir(migrations_dir)):
            if not name.endswith(".sql"):
                continue
            if only_before and name >= only_before:
                break
            self._file = name[:-4]
            self.result.files += 1
            with open(os.path.join(migrations_dir, name), encoding="utf-8") as fh:
                text = fh.read()
            text = _strip_comments_quote_aware(text)
            for stmt in sqlparse.split(text):
                stmt = stmt.strip().rstrip(";").strip()
                if not stmt:
                    continue
                self.result.statements += 1
                self.statement(stmt, guard=set(), handlers=set())
        return self.result

    def statement(self, stmt: str, guard: Set[str], handlers: Set[str]) -> None:
        s = " ".join(stmt.split())
        up = s.upper()
        if up.startswith("DO "):
            return self._do_block(stmt)
        if up.startswith(("CREATE OR REPLACE FUNCTION", "CREATE FUNCTION", "CREATE EXTENSION", "CREATE TYPE",
                          "CREATE SEQUENCE", "CREATE SCHEMA", "BEGIN", "COMMIT", "SET ", "SELECT", "GRANT",
                          "REVOKE", "ANALYZE", "VACUUM", "DROP FUNCTION", "DROP TRIGGER", "DROP INDEX",
                          "DROP TYPE", "CREATE OR REPLACE VIEW", "CREATE VIEW", "DROP VIEW", "NOTIFY",
                          "ALTER SEQUENCE", "DROP SEQUENCE", "ALTER FUNCTION", "CREATE POLICY", "ALTER INDEX",
                          "DROP POLICY", "ALTER TYPE", "REFRESH", "CLUSTER", "REINDEX")):
            return
        if up.startswith("DROP TABLE"):
            m = re.match(rf"DROP TABLE\s+(IF EXISTS\s+)?({_QNAME})", s, re.I)
            if m:
                self.model.tables.pop(_norm(m.group(2)), None)
            return
        mt = re.match(rf"CREATE (?:TEMP|TEMPORARY)\s+TABLE\s+(?:IF NOT EXISTS\s+)?({_QNAME})\b(.*)$", s, re.I | re.S)
        if mt:
            # a session-scoped scratch table; columns come from its SELECT — register as wildcard
            self.model.tables[_norm(mt.group(1))] = Table(columns={"*": Column()})
            mf = re.search(rf"\bAS\s+SELECT\b.*?\bFROM\s+({_QNAME})", mt.group(2), re.I | re.S)
            if mf:
                self._need_table(mf.group(1), "CREATE TEMP TABLE AS SELECT FROM", guard, handlers)
            return
        if up.startswith("CREATE TABLE") or up.startswith("CREATE UNLOGGED TABLE"):
            m = re.match(rf"CREATE (?:UNLOGGED )?TABLE\s+(IF NOT EXISTS\s+)?({_QNAME})\s*\((.*)\)\s*(?:WITH|INHERITS|PARTITION|TABLESPACE|;|$)",
                         s, re.I | re.S)
            if not m:
                m2 = re.match(rf"CREATE (?:UNLOGGED )?TABLE\s+(IF NOT EXISTS\s+)?({_QNAME})\s*\((.*)\)\s*$", s, re.I | re.S)
                m = m2
            if not m:
                self.result.unmodelled.append((self._file, s[:80]))
                return
            name = _norm(m.group(2))
            if _is_system(name):
                return
            refs = self.model.create_table(name, m.group(3), bool(m.group(1)))
            for r in refs:
                self._need_table(r, f"REFERENCES from CREATE TABLE {name}", guard, handlers)
            return
        if up.startswith("ALTER TABLE"):
            return self._alter_table(s, guard, handlers)
        if up.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX")):
            m = re.match(rf"CREATE (?:UNIQUE )?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF NOT EXISTS\s+)?({_IDENT})\s+ON\s+(?:ONLY\s+)?({_QNAME})\s*(?:USING\s+\w+\s*)?\((.*?)\)\s*(?:INCLUDE\b|WITH\b|WHERE\b|TABLESPACE\b|$)", s, re.I | re.S)
            if not m:
                self.result.unmodelled.append((self._file, s[:80]))
                return
            tname = _norm(m.group(2))
            if self._need_table(tname, "CREATE INDEX", guard, handlers):
                for col in _split_top_level(m.group(3)):
                    col = col.strip().split()[0] if col.strip() else ""
                    if re.fullmatch(_IDENT, col):
                        self._need_column(tname, col, "CREATE INDEX", guard, handlers)
                mw = re.search(r"\bWHERE\b(.*)$", s, re.I | re.S)
                if mw:
                    self.check_expr(mw.group(1), [(None, tname)], "CREATE INDEX WHERE", guard, handlers)
            return
        if up.startswith("CREATE TRIGGER") or up.startswith("CREATE OR REPLACE TRIGGER"):
            m = re.search(rf"\bON\s+({_QNAME})\s", s, re.I)
            if m:
                self._need_table(m.group(1), "CREATE TRIGGER", guard, handlers)
            return
        if up.startswith("COMMENT ON COLUMN"):
            m = re.match(rf"COMMENT ON COLUMN\s+({_QNAME})\.({_IDENT})\s+IS", s, re.I)
            if m and self._need_table(m.group(1), "COMMENT ON COLUMN", guard, handlers):
                self._need_column(m.group(1), m.group(2), "COMMENT ON COLUMN", guard, handlers)
            return
        if up.startswith("COMMENT ON TABLE"):
            m = re.match(rf"COMMENT ON TABLE\s+({_QNAME})\s+IS", s, re.I)
            if m:
                self._need_table(m.group(1), "COMMENT ON TABLE", guard, handlers)
            return
        if up.startswith("COMMENT ON"):
            return
        if up.startswith("INSERT INTO"):
            return self._insert(s, guard, handlers)
        if up.startswith("UPDATE "):
            return self._update(s, guard, handlers)
        if up.startswith("DELETE FROM") or up.startswith("TRUNCATE"):
            m = re.match(rf"(?:DELETE FROM|TRUNCATE(?: TABLE)?)\s+(?:ONLY\s+)?({_QNAME})(?:\s+(?:AS\s+)?(?!USING\b|WHERE\b|RETURNING\b)({_IDENT}))?(.*)$", s, re.I | re.S)
            if m and self._need_table(m.group(1), "DELETE/TRUNCATE", guard, handlers):
                alias = m.group(2).strip('"').lower() if m.group(2) and m.group(2).upper() not in ("USING", "WHERE") else None
                scope = [(alias, _norm(m.group(1)))]
                rest = m.group(3) or ""
                mu = re.search(r"\bUSING\b(.*?)(?:\bWHERE\b|$)", rest, re.I | re.S)
                if mu:
                    scope += _from_tables(mu.group(1))
                mw = re.search(r"\bWHERE\b(.*)$", rest, re.I | re.S)
                if mw:
                    self.check_expr(mw.group(1), scope, "DELETE WHERE", guard, handlers)
            return
        if up.startswith("WITH "):
            return  # CTE-driven DML: tables under the CTE — not modelled, counted
        self.result.unmodelled.append((self._file, s[:80]))

    # read-side expressions ------------------------------------------------------------
    def _scope_has(self, scope: List[Tuple[Optional[str], str]], col: str) -> Optional[bool]:
        """True if some table in scope carries `col`; None if a wildcard/unknown table is in scope."""
        for _alias, t in scope:
            if t == "*":
                return None
            table = self.model.tables.get(t)
            if table is None:
                if _is_system(t):
                    return None
                continue
            if "*" in table.columns or col in table.columns:
                return True
        return False

    def _resolve_alias(self, scope: List[Tuple[Optional[str], str]], qual: str) -> Optional[str]:
        qual = qual.strip('"').lower()
        for alias, t in scope:
            if alias == qual or t == qual:
                return t
        return None

    def check_expr(self, text: str, scope: List[Tuple[Optional[str], str]], ctx: str,
                   guard: Set[str], handlers: Set[str]) -> None:
        """Every column-shaped identifier in `text` must exist on a table in `scope` (the first
        entry is the PRIMARY table a bare miss is reported against). Subqueries open their own
        scope (their FROM tables first, then the enclosing scope — correlated references);
        function calls, casts, keywords, aliases, plpgsql variables and positional parameters are
        not columns and are skipped. A subquery over a system catalog or an unmodelled source
        (derived table) is a wildcard: nothing inside it is judged."""
        text = _blank_literals(text)
        # subqueries first: `( SELECT ... )` at any depth, innermost-first, replaced by a marker
        while True:
            m = None
            for cand in re.finditer(r"\(\s*SELECT\b", text, re.I):
                # find the matching close paren
                depth, j = 0, cand.start()
                while j < len(text):
                    if text[j] == "(":
                        depth += 1
                    elif text[j] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                inner = text[cand.start() + 1:j]
                if re.search(r"\(\s*SELECT\b", inner[7:], re.I):
                    continue  # not innermost
                m = (cand.start(), j, inner)
                break
            if m is None:
                break
            start, end, inner = m
            self._check_select(inner, scope, ctx + " (subquery)", guard, handlers)
            text = text[:start] + " 0 " + text[end + 1:]
        self._check_idents(text, scope, ctx, guard, handlers)

    def _check_select(self, sel: str, outer: List[Tuple[Optional[str], str]], ctx: str,
                      guard: Set[str], handlers: Set[str]) -> None:
        """`SELECT <list> FROM <tables> [WHERE ...] ...` — its own scope in front of the outer one."""
        mf = re.search(r"\bFROM\b(.*?)(?:\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bHAVING\b|$)", sel, re.I | re.S)
        own: List[Tuple[Optional[str], str]] = _from_tables(mf.group(1)) if mf else []
        if any(_is_system(t) for _a, t in own):
            return  # catalog probe: its columns are not ours to model
        for _a, t in own:
            if t != "*":
                self._need_table(t, ctx + " FROM", guard, handlers)
        scope = own + outer
        self.check_expr(sel, scope, ctx, guard, handlers)

    def _check_idents(self, text: str, scope: List[Tuple[Optional[str], str]], ctx: str,
                      guard: Set[str], handlers: Set[str]) -> None:
        if not scope or self._scope_has(scope, "\0probe") is None and any(t == "*" for _a, t in scope):
            return
        primary = scope[0][1]
        skip_next = False
        tokens = list(_ID_RE.finditer(text))
        for i, m in enumerate(tokens):
            tok = m.group(1)
            after = text[m.end():m.end() + 2]
            before = text[max(0, m.start() - 3):m.start()]
            word = tok.replace(" ", "")
            low = word.lower()
            if skip_next:
                skip_next = False
                continue
            if low in _SQL_WORDS or low.startswith("?"):
                if low in ("from", "join", "into", "update", "table", "as", "only", "using", "exists", "over", "filter", "within"):
                    skip_next = True  # the next identifier is a table / alias / type, not a column
                if low in ("as",):
                    skip_next = True
                continue
            if re.match(r"\s*\(", text[m.end():]):
                continue  # function call
            if before.rstrip().endswith("::"):
                continue  # type cast
            if low.startswith("_") or low.startswith("v_") or low in self._declared or low in _COLUMN_ALIASES or low in ("i", "r", "rec"):
                continue  # plpgsql variable
            if "." in low:
                qual, col = low.split(".", 1)
                qual, col = qual.strip('"'), col.strip('"')
                if qual in ("new", "old", "excluded") and qual != "excluded":
                    continue
                if qual == "excluded":
                    t = primary
                else:
                    t = self._resolve_alias(scope, qual)
                    if t is None:
                        if qual in ("public",) or _is_system(qual) or qual.startswith("faultline") or qual.startswith("flagent"):
                            continue  # schema-qualified table token
                        continue  # unknown qualifier: not ours to judge
                if t == "*" or _is_system(t):
                    continue
                table = self.model.tables.get(t)
                if table is None:
                    continue
                if "*" not in table.columns and col not in table.columns:
                    self._red("missing_column", f"{t}.{col}", ctx, guard, handlers)
                continue
            col = low.strip('"')
            has = self._scope_has(scope, col)
            if has is None or has:
                continue
            # not on any table in scope — if it names a table in scope it is a table token
            if any(t == col or a == col for a, t in scope) or col in self.model.tables:
                continue
            self._red("missing_column", f"{primary}.{col}", ctx + " (read)", guard, handlers)

    # ALTER TABLE ---------------------------------------------------------------------
    def _alter_table(self, s: str, guard: Set[str], handlers: Set[str]) -> None:
        m = re.match(rf"ALTER TABLE\s+(IF EXISTS\s+)?(?:ONLY\s+)?({_QNAME})\s+(.*)$", s, re.I | re.S)
        if not m:
            self.result.unmodelled.append((self._file, s[:80]))
            return
        if_exists, tname, actions = bool(m.group(1)), _norm(m.group(2)), m.group(3)
        if _is_system(tname):
            return
        if tname not in self.model.tables:
            if not if_exists:
                self._red("missing_table", tname, "ALTER TABLE", guard, handlers)
            return
        for action in _split_top_level(actions):
            a = action.strip()
            au = a.upper()
            if au.startswith("ADD CONSTRAINT"):
                mm = re.match(rf"ADD CONSTRAINT\s+({_IDENT})\s+(.*)$", a, re.I | re.S)
                cname = mm.group(1).strip('"').lower()
                rest = mm.group(2)
                mr = re.search(rf"REFERENCES\s+({_QNAME})\s*\(\s*({_IDENT})", rest, re.I)
                if mr and self._need_table(mr.group(1), f"ADD CONSTRAINT {cname} REFERENCES", guard, handlers):
                    self._need_column(mr.group(1), mr.group(2), f"ADD CONSTRAINT {cname} REFERENCES", guard, handlers)
                mcols = re.match(r"(?:UNIQUE|PRIMARY KEY|FOREIGN KEY)\s*\(([^)]*)\)", rest, re.I)
                if mcols:
                    for col in mcols.group(1).split(","):
                        self._need_column(tname, col, f"ADD CONSTRAINT {cname}", guard, handlers)
                self.model.add_constraint(tname, cname, rest)
                col, allowed = self.model.tables[tname].constraints[cname]
                if col:
                    self._need_column(tname, col, f"ADD CONSTRAINT {cname} CHECK", guard, handlers)
                continue
            if au.startswith("ADD CHECK") or au.startswith("ADD UNIQUE") or au.startswith("ADD PRIMARY KEY") or au.startswith("ADD FOREIGN KEY"):
                continue
            if au.startswith("ADD"):
                mm = re.match(rf"ADD\s+(?:COLUMN\s+)?(IF NOT EXISTS\s+)?({_IDENT})\s*(.*)$", a, re.I | re.S)
                if not mm:
                    self.result.unmodelled.append((self._file, a[:80]))
                    continue
                col = mm.group(2).strip('"').lower()
                mr = re.search(rf"REFERENCES\s+({_QNAME})\s*\(", mm.group(3), re.I)
                if mr:
                    self._need_table(mr.group(1), f"ADD COLUMN {col} REFERENCES", guard, handlers)
                self.model.add_column(tname, col, mm.group(3))
                continue
            if au.startswith("DROP CONSTRAINT"):
                mm = re.match(rf"DROP CONSTRAINT\s+(IF EXISTS\s+)?({_IDENT})", a, re.I)
                cname = mm.group(2).strip('"').lower()
                if cname in self.model.tables[tname].constraints:
                    self.model.drop_constraint(tname, cname)
                elif not mm.group(1):
                    self._red("missing_constraint", f"{tname}.{cname}", "DROP CONSTRAINT", guard, handlers)
                continue
            if au.startswith("DROP COLUMN") or (au.startswith("DROP ") and not au.startswith("DROP NOT NULL")):
                mm = re.match(rf"DROP\s+(?:COLUMN\s+)?(IF EXISTS\s+)?({_IDENT})", a, re.I)
                col = mm.group(2).strip('"').lower()
                if col in self.model.tables[tname].columns:
                    del self.model.tables[tname].columns[col]
                elif not mm.group(1):
                    self._red("missing_column", f"{tname}.{col}", "DROP COLUMN", guard, handlers)
                continue
            if au.startswith("RENAME COLUMN"):
                mm = re.match(rf"RENAME COLUMN\s+({_IDENT})\s+TO\s+({_IDENT})", a, re.I)
                old, new = mm.group(1).strip('"').lower(), mm.group(2).strip('"').lower()
                if self._need_column(tname, old, "RENAME COLUMN", guard, handlers):
                    self.model.tables[tname].columns[new] = self.model.tables[tname].columns.pop(old)
                continue
            if au.startswith("RENAME TO"):
                mm = re.match(rf"RENAME TO\s+({_IDENT})", a, re.I)
                self.model.tables[_norm(mm.group(1))] = self.model.tables.pop(tname)
                return
            if au.startswith("ALTER COLUMN") or au.startswith("ALTER "):
                mm = re.match(rf"ALTER\s+(?:COLUMN\s+)?({_IDENT})\s+(.*)$", a, re.I | re.S)
                col = mm.group(1).strip('"').lower()
                self._need_column(tname, col, "ALTER COLUMN", guard, handlers)
                continue
            if au.startswith(("VALIDATE CONSTRAINT", "ENABLE", "DISABLE", "SET ", "OWNER", "CLUSTER", "RESET",
                              "REPLICA", "INHERIT", "NO INHERIT", "ATTACH", "DETACH", "OF ", "NOT OF")):
                continue
            self.result.unmodelled.append((self._file, a[:80]))

    # INSERT ---------------------------------------------------------------------------
    def _insert(self, s: str, guard: Set[str], handlers: Set[str]) -> None:
        m = re.match(rf"INSERT INTO\s+({_QNAME})\s*(?:AS\s+{_IDENT}\s*)?(\(([^)]*)\))?\s*(.*)$", s, re.I | re.S)
        if not m:
            self.result.unmodelled.append((self._file, s[:80]))
            return
        tname = _norm(m.group(1))
        if not self._need_table(tname, "INSERT INTO", guard, handlers):
            return
        cols = [c.strip().strip('"').lower() for c in m.group(3).split(",")] if m.group(2) else []
        for col in cols:
            self._need_column(tname, col, "INSERT column list", guard, handlers)
        rest = m.group(4)
        mv = re.match(r"VALUES\s*(.*)$", rest, re.I | re.S)
        if mv and cols:
            body = mv.group(1)
            # cut ON CONFLICT / RETURNING at depth 0
            for kw in (" ON CONFLICT", " RETURNING"):
                idx = self._find_top_level(body, kw)
                if idx is not None:
                    body = body[:idx]
            for row in self._value_rows(body):
                if len(row) != len(cols):
                    continue
                for col, val in zip(cols, row):
                    if val is not None:
                        self._check_value(tname, col, val, "INSERT VALUES", guard, handlers)
        # ON CONFLICT DO UPDATE SET col = 'literal'
        mo = re.search(r"ON CONFLICT.*?DO UPDATE SET\s+(.*?)(?:\s+WHERE\s+|$)", rest, re.I | re.S)
        if mo:
            for assign in _split_top_level(mo.group(1)):
                ma = re.match(rf"\s*({_IDENT})\s*=\s*'((?:[^']|'')*)'\s*$", assign, re.S)
                if ma:
                    self._check_value(tname, ma.group(1).strip('"').lower(), ma.group(2), "ON CONFLICT DO UPDATE SET",
                                      guard, handlers)
        if not mv:
            msel = re.match(r"\s*(SELECT\b.*)$", rest, re.I | re.S)
            if msel:
                sel = msel.group(1)
                for kw in (" ON CONFLICT", " RETURNING"):
                    idx = self._find_top_level(sel, kw)
                    if idx is not None:
                        sel = sel[:idx]
                self._check_select(sel, [], "INSERT ... SELECT", guard, handlers)
        else:
            body = mv.group(1)
            for kw in (" ON CONFLICT", " RETURNING"):
                idx = self._find_top_level(body, kw)
                if idx is not None:
                    body = body[:idx]
            self.check_expr(body, [(None, "*")], "INSERT VALUES", guard, handlers)  # subqueries only
        if mo:
            self.check_expr(mo.group(1), [("excluded", tname), (None, tname)], "ON CONFLICT DO UPDATE SET", guard, handlers)
            mw = re.search(r"ON CONFLICT.*?DO UPDATE SET\s+.*?\s+WHERE\s+(.*)$", rest, re.I | re.S)
            if mw:
                self.check_expr(mw.group(1), [("excluded", tname), (None, tname)], "ON CONFLICT ... WHERE", guard, handlers)

    @staticmethod
    def _find_top_level(text: str, kw: str) -> Optional[int]:
        depth, i, n, up = 0, 0, len(text), text.upper()
        while i < n:
            ch = text[i]
            if ch == "'":
                j = i + 1
                while j < n:
                    if text[j] == "'":
                        if j + 1 < n and text[j + 1] == "'":
                            j += 2
                            continue
                        break
                    j += 1
                i = j + 1; continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and up.startswith(kw, i):
                return i
            i += 1
        return None

    @staticmethod
    def _value_rows(body: str) -> List[List[Optional[str]]]:
        """Each `( ... )` tuple at depth 1 → list of scalar literals (None for non-literals)."""
        rows: List[List[Optional[str]]] = []
        depth, i, n, start = 0, 0, len(body), None
        while i < n:
            ch = body[i]
            if ch == "'":
                j = i + 1
                while j < n:
                    if body[j] == "'":
                        if j + 1 < n and body[j + 1] == "'":
                            j += 2
                            continue
                        break
                    j += 1
                i = j + 1; continue
            if ch == "(":
                depth += 1
                if depth == 1:
                    start = i + 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and start is not None:
                    row = []
                    for cell in _split_top_level(body[start:i]):
                        cell = cell.strip()
                        mm = re.fullmatch(r"'((?:[^']|'')*)'(?:::\w+)?", cell, re.S)
                        row.append(mm.group(1).replace("''", "'") if mm else None)
                    rows.append(row)
                    start = None
            i += 1
        return rows

    # UPDATE ---------------------------------------------------------------------------
    def _update(self, s: str, guard: Set[str], handlers: Set[str]) -> None:
        m = re.match(rf"UPDATE\s+(?:ONLY\s+)?({_QNAME})(?:\s+(?:AS\s+)?({_IDENT}))?\s+SET\s+(.*)$", s, re.I | re.S)
        if not m:
            self.result.unmodelled.append((self._file, s[:80]))
            return
        tname = _norm(m.group(1))
        if not self._need_table(tname, "UPDATE", guard, handlers):
            return
        rest = m.group(3)
        alias = m.group(2).strip('"').lower() if m.group(2) else None
        full = rest
        for kw in (" FROM ", " WHERE "):
            idx = self._find_top_level(rest, kw)
            if idx is not None:
                rest = rest[:idx]
        scope: List[Tuple[Optional[str], str]] = [(alias, tname)]
        fi = self._find_top_level(full, " FROM ")
        wi = self._find_top_level(full, " WHERE ")
        if fi is not None:
            scope += _from_tables(full[fi + 6: wi] if wi is not None and wi > fi else full[fi + 6:])
            for _a, t in scope[1:]:
                if t != "*":
                    self._need_table(t, "UPDATE ... FROM", guard, handlers)
        if wi is not None:
            self.check_expr(full[wi + 7:], scope, "UPDATE WHERE", guard, handlers)
        for assign in _split_top_level(rest):
            ma0 = re.match(rf"\s*({_IDENT})\s*=\s*(.*)$", assign, re.S)
            if ma0:
                self.check_expr(ma0.group(2), scope, "UPDATE SET expression", guard, handlers)
        for assign in _split_top_level(rest):
            ma = re.match(rf"\s*({_IDENT})\s*=\s*(.*)$", assign, re.S)
            if not ma:
                continue
            col = ma.group(1).strip('"').lower()
            if not self._need_column(tname, col, "UPDATE SET", guard, handlers):
                continue
            mv = re.fullmatch(r"\s*'((?:[^']|'')*)'\s*(?:::\w+)?\s*", ma.group(2), re.S)
            if mv:
                self._check_value(tname, col, mv.group(1).replace("''", "'"), "UPDATE SET", guard, handlers)

    # DO $$ ... $$ ---------------------------------------------------------------------
    def _do_block(self, stmt: str) -> None:
        m = re.match(r"DO\s+(?:LANGUAGE\s+\w+\s+)?(\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$)(.*)\1", stmt, re.S)
        if not m:
            self.result.unmodelled.append((self._file, stmt[:80]))
            return
        body = _strip_comments_quote_aware(m.group(2))
        guard = self._guards(body)
        handlers: Set[str] = set()
        for h in re.finditer(r"EXCEPTION\s+WHEN\s+([A-Za-z_]+(?:\s+OR\s+[A-Za-z_]+)*)", body, re.I):
            handlers.update(x.strip().lower() for x in re.split(r"\s+OR\s+", h.group(1), flags=re.I))
        # the executable region: after the first BEGIN; DECLAREd names are variables, never columns
        mb = re.search(r"\bBEGIN\b", body, re.I)
        exe = body[mb.end():] if mb else body
        decl = body[:mb.start()] if mb else ""
        saved_declared = self._declared
        self._declared = set(saved_declared)
        for dm in re.finditer(rf"^\s*({_IDENT})\s+(?:CONSTANT\s+)?[A-Za-z_]", decl, re.M):
            self._declared.add(dm.group(1).strip('"').lower())
        for dm in re.finditer(rf"\bFOR\s+({_IDENT})\s+IN\b", exe, re.I):
            self._declared.add(dm.group(1).strip('"').lower())
        offset = 0
        try:
            self._do_chunks(exe, guard, handlers)
        finally:
            self._declared = saved_declared
            self._path_scope = None

    @staticmethod
    def _fanout_scope(exe: str, pos: int) -> str:
        """Which schema family the nearest preceding loop header iterates: `LIKE 'faultline\\_%'`
        or a `public.user_provisioning.schema_name` source → tenant; `LIKE 'flagent\\_%'` → cortex;
        any other LIKE → that family (no template); no header at all → unknown."""
        hits = [(m.start(), m.group(1).replace("\\_", "_")) for m in re.finditer(r"LIKE\s+'([^']*)'", exe[:pos], re.I)]
        hits += [(m.start(), "faultline_") for m in re.finditer(r"\buser_provisioning\b", exe[:pos], re.I)]
        if not hits:
            return "other:unknown"
        pat = max(hits)[1]
        if pat.startswith("faultline_"):
            return "tenant"
        if pat.startswith("flagent_"):
            return "cortex"
        return "other:" + pat

    def _do_chunks(self, exe: str, guard: Set[str], handlers: Set[str]) -> None:
        offset = 0
        for chunk in _split_top_level(exe, ";"):
            pos = offset
            offset += len(chunk) + 1
            c = chunk.strip()
            if not c:
                continue
            scope_kind = self._fanout_scope(exe, pos + len(chunk))  # the loop header is in this chunk
            # control conditions are read-side references too: `IF EXISTS (SELECT 1 FROM t WHERE
            # col = ...)`, `FOR r IN SELECT ... FROM t WHERE ...`
            cu_raw = c.upper()
            if cu_raw.startswith(("IF ", "ELSIF ")):
                idx = self._first_kw(c, "THEN")
                if idx is not None:
                    self.check_expr(c[c.index(" "):idx], [(None, "*")], "IF condition", guard, handlers)
            mfor = re.match(rf"FOR\s+{_IDENT}\s+IN\s+(SELECT\b.*?)\s+LOOP\b", c, re.I | re.S)
            if mfor:
                self._check_select(mfor.group(1), [], "FOR ... IN SELECT", guard, handlers)
            # `IF cond THEN <stmt>` / `ELSIF cond THEN <stmt>` / `ELSE <stmt>` / `FOR .. LOOP <stmt>`
            # — peel the control prefix (FIRST keyword at depth 0; a CASE ... THEN inside the
            # statement itself stays with the statement) and keep the tail.
            while True:
                cu0 = c.upper()
                if cu0.startswith(("IF ", "ELSIF ", "WHEN ")):
                    idx = self._first_kw(c, "THEN")
                elif cu0.startswith(("FOR ", "FOREACH ", "WHILE ", "LOOP")):
                    idx = self._first_kw(c, "LOOP")
                elif cu0.startswith("ELSE "):
                    idx = 0
                else:
                    break
                if idx is None:
                    c = ""
                    break
                kw_len = 4 if cu0.startswith("ELSE ") else (4 if cu0.startswith(("IF ", "ELSIF ", "WHEN ")) else 4)
                c = c[idx + kw_len:].strip()
            if c.upper().startswith("RESET "):
                self._path_scope = None
                continue
            if not c or _CTRL_START.match(c) or re.match(rf"^{_IDENT}\s*:=", c):
                continue
            cu = c.upper()
            if cu.startswith("EXECUTE"):
                self._execute(c, guard, handlers, scope_kind)
                continue
            if cu.startswith("SELECT") or cu.startswith("PERFORM"):
                sel = re.sub(r"\bINTO\s+(?:STRICT\s+)?[^ ]+(?:\s*,\s*[^ ]+)*", " ", c, flags=re.I)
                sel = re.sub(r"^PERFORM\b", "SELECT", sel, flags=re.I)
                self._check_select(sel, [], "SELECT ... INTO", guard, handlers)
                continue
            self.result.statements += 1
            if self._path_scope in ("tenant", "cortex") and self.scope == "public":
                # the block bound search_path to a tenant / cortex schema: unqualified names in
                # the statements that follow live THERE, not in public
                model = self.tenant if self._path_scope == "tenant" else self.cortex
                if model is not None:
                    saved_model, saved_scope = self.model, self.scope
                    self.model, self.scope = model, self._path_scope
                    try:
                        self.statement(c, guard, handlers)
                    finally:
                        self.model, self.scope = saved_model, saved_scope
                    continue
            self.statement(c, guard, handlers)

    @staticmethod
    def _guards(body: str) -> Set[str]:
        """The objects a block TESTS FOR before it acts — typed, so a probe of column T.C never
        excuses a missing TABLE T (003's first DO block probes `column_name = 'confirmation_source'`
        on a table that does not exist yet; the ALTER inside it still fails).

        Entries: `table:T`, `column:T.C` (or `column:*.C` when the probe names no table),
        `constraint:X`, and `any:<literal>` for probe shapes the census does not know (kept
        permissive — an unknown probe is more likely a guard than not)."""
        guard: Set[str] = set()

        def probe_entries(text: str) -> None:
            low = text.lower()
            lits = [v.lower() for v in _literals(text)]
            tbl = re.search(r"\btable_name\s*=\s*'([^']*)'", text, re.I)
            col = re.search(r"\bcolumn_name\s*=\s*'([^']*)'", text, re.I)
            con = re.search(r"\b(?:conname|constraint_name)\s*=\s*'([^']*)'", text, re.I)
            reg = re.search(r"to_regclass\s*\(\s*'([^']*)'\s*\)", text, re.I)
            att = re.search(r"\battname\s*=\s*'([^']*)'", text, re.I)
            rel = re.search(r"'([^']*)'\s*::\s*regclass", text, re.I)
            typed = False
            if "information_schema.columns" in low or att:
                cname = (col or att)
                if cname:
                    t = (tbl or rel)
                    guard.add(f"column:{_norm(t.group(1)) if t else '*'}.{cname.group(1).lower()}")
                    typed = True
            if ("information_schema.tables" in low or "pg_tables" in low or "pg_class" in low) and tbl and not col:
                guard.add(f"table:{_norm(tbl.group(1))}"); typed = True
            if reg:
                guard.add(f"table:{_norm(reg.group(1))}"); typed = True
            if con:
                guard.add(f"constraint:{con.group(1).lower()}"); typed = True
            if not typed:
                guard.update(f"any:{v}" for v in lits)

        for probe in re.finditer(r"EXISTS\s*\((.*?)\)\s*(?:THEN|AND|OR|\)|;)", body, re.I | re.S):
            probe_entries(probe.group(1))
        for probe in re.finditer(r"to_regclass\s*\(\s*'([^']*)'\s*\)", body, re.I):
            guard.add(f"table:{_norm(probe.group(1))}")
        return guard

    @classmethod
    def _first_kw(cls, text: str, kw: str) -> Optional[int]:
        """Position of the FIRST `kw` token at parenthesis depth 0 outside quotes."""
        return cls._kw_positions(text, kw, first=True)

    @staticmethod
    def _kw_positions(text: str, kw: str, first: bool) -> Optional[int]:
        last, depth, i, n, up = None, 0, 0, len(text), text.upper()
        while i < n:
            ch = text[i]
            if ch == "'":
                j = i + 1
                while j < n:
                    if text[j] == "'":
                        if j + 1 < n and text[j + 1] == "'":
                            j += 2
                            continue
                        break
                    j += 1
                i = j + 1; continue
            if ch == "$":
                mm = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", text[i:])
                if mm:
                    tag = mm.group(0)
                    j = text.find(tag, i + len(tag))
                    j = n if j == -1 else j
                    i = j + len(tag); continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and up.startswith(kw, i) and (i == 0 or not (up[i - 1].isalnum() or up[i - 1] == "_")) \
                    and (i + len(kw) >= n or not (up[i + len(kw)].isalnum() or up[i + len(kw)] == "_")):
                if first:
                    return i
                last = i
            i += 1
        return last

    def _execute(self, c: str, guard: Set[str], handlers: Set[str], scope_kind: str = "public") -> None:
        """EXECUTE format($q$ ... $q$, args) / EXECUTE 'literal' / EXECUTE variable.

        A `%I.table` fan-out is resolved against the schema family the enclosing loop iterates
        (`faultline\\_%` → the tenant template, `flagent\\_%` → the cortex template); `%L` / `%s` /
        `$n` become opaque literals. Only a template with a `%I` that is NOT a schema qualifier
        (a substituted table or column NAME) or an EXECUTE of a variable is skipped and counted."""
        mm = re.match(r"EXECUTE\s+(?:format\s*\(\s*)?(\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$)(.*?)\1", c, re.I | re.S)
        template: Optional[str] = None
        args_text = ""
        if mm:
            template = mm.group(2)
            args_text = c[mm.end():]
        else:
            # `'a' 'b'` on successive lines is ONE literal (SQL string-constant continuation) —
            # take every adjacent fragment, or a `%I` in the second line hides a fan-out.
            mm2 = re.match(r"EXECUTE\s+(?:format\s*\(\s*)?((?:'(?:[^']|'')*'\s*)+)", c, re.I | re.S)
            mchain = re.match(r"EXECUTE\s+(.*)$", c, re.I | re.S)
            if mm2 and "||" not in c[mm2.end():mm2.end() + 3]:
                template = "".join(_literals(mm2.group(1)))
                args_text = c[mm2.end():]
            elif mchain and "||" in mchain.group(1) and not re.match(r"format\s*\(", mchain.group(1), re.I):
                # `EXECUTE 'a' || _schema || '.b' || 'c'`: literal pieces are joined; a piece that
                # is a schema VARIABLE becomes the %I qualifier; any other non-literal piece is
                # itemised and the statement skipped
                expr = re.split(r"\bUSING\b", mchain.group(1), maxsplit=1, flags=re.I)[0]
                pieces = [pc.strip() for pc in re.split(r"\|\|", _blank_literals(expr))]
                raw_pieces = [pc.strip() for pc in _split_top_level(expr.replace("||", "\0"), "\0")]
                built, bad = [], None
                for pc in raw_pieces:
                    if re.fullmatch(r"'(?:[^']|'')*'", pc, re.S):
                        built.append(pc[1:-1].replace("''", "'"))
                    elif re.fullmatch(r"(?:quote_ident\(\s*)?[A-Za-z_]*schema[A-Za-z_]*\s*\)?", pc, re.I):
                        built.append("%I")
                    else:
                        bad = pc
                        break
                if bad is not None:
                    self.result.dynamic_skipped += 1
                    self.result.skipped_detail.append((self._file, f"concatenation with a non-literal piece ({bad[:30]})", " ".join(c.split())[:80]))
                    return
                template = "".join(built)
                args_text = c[mchain.end():]
            else:
                self.result.dynamic_skipped += 1
                self.result.skipped_detail.append((self._file, "EXECUTE of a variable (no template text)", " ".join(c.split())[:80]))
                return
        # `SET search_path TO %I` binds the rest of the block to that schema family
        if re.match(r"\s*SET\s+search_path\s+TO\s+%(?:\d+\$)?I", template, re.I):
            self._path_scope = scope_kind if scope_kind in ("tenant", "cortex") else None
            self.result.statements += 1
            return
        t = re.sub(r"%(?:\d+\$)?I\s*\.", "", template)          # %I.table → table (schema stripped)
        if re.search(r"%(?:\d+\$)?I", t) or re.search(r"\w%(?:\d+\$)?[sL]|%(?:\d+\$)?[sL]\w", t):
            self.result.dynamic_skipped += 1  # an identifier the census cannot know (table/column NAME substituted)
            self.result.skipped_detail.append((self._file, "substituted identifier (%I / %s inside a name)", " ".join(t.split())[:80]))
            return
        t = re.sub(r"%(?:\d+\$)?[Ls]", "'?'", t)                  # values → opaque literals
        t = t.replace("%%", "%")
        t = re.sub(r"\$\d+", "'?'", t)                             # USING parameters
        t = re.sub(r"\{schema_name\}\s*\.", "", t)
        # the schema ARGUMENT can override the loop family (147 loops faultline_% but targets the
        # cortex schema it derives: `format(..., _cortex_schema)`)
        if "%I" in template and "cortex" in args_text.lower():
            scope_kind = "cortex"
        if "%I" not in template:
            model, tag = self.public, "public"
        elif scope_kind == "tenant":
            model, tag = self.tenant, "tenant"
        elif scope_kind == "cortex":
            model, tag = self.cortex, "cortex"
        else:
            model, tag = None, scope_kind
        if model is None:
            self.result.dynamic_skipped += 1  # a schema family with no template to resolve against
            self.result.skipped_detail.append((self._file, f"schema family {tag!r} has no template", " ".join(t.split())[:80]))
            return
        saved_model, saved_scope = self.model, self.scope
        self.model, self.scope = model, tag
        try:
            self.result.statements += 1
            self.statement(t.strip().rstrip(";"), guard, handlers)
        finally:
            self.model, self.scope = saved_model, saved_scope


def run_census(migrations_dir: str, only_before: Optional[str] = None) -> CensusResult:
    return Census().run(migrations_dir, only_before=only_before)


def format_findings(result: CensusResult) -> str:
    lines = [f"files={result.files} statements={result.statements} dynamic_skipped={result.dynamic_skipped} "
             f"unmodelled={len(result.unmodelled)} findings={len(result.findings)} red_files={result.red_files}"]
    for f in result.findings:
        lines.append(f"  {f.file}: {f.kind} {f.obj} — {f.detail}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover — ad-hoc driver
    import sys
    r = run_census(sys.argv[1] if len(sys.argv) > 1 else "migrations")
    print(format_findings(r))
    if "-v" in sys.argv:
        for f, s in r.unmodelled:
            print("  unmodelled:", f, s)
        for f, why, s in r.skipped_detail:
            print("  skipped:", f, "|", why, "|", s)
