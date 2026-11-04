"""Read an Access database (.mdb / .accdb) through Windows' 32-bit Access ODBC driver.

This file runs on its own under a 32-bit Python (the Access driver on this PC is 32-bit, and a
64-bit process can't load it): standard library only, ctypes over odbc32.dll, no varsha imports.
varsha starts it once per request with a JSON request on stdin and reads JSON from stdout:

    {"file": "...", "op": "tables" | "columns" | "rows" | "query" | "csv", ...}

The connection is always read-only (ReadOnly=1 plus SQL_MODE_READ_ONLY), and varsha only ever
points it at its own snapshot copy of the user's file.
"""

from __future__ import annotations

import csv
import ctypes
import io
import json
import struct
import sys
from ctypes import byref, c_short, c_ushort, c_void_p, c_wchar_p, create_string_buffer, create_unicode_buffer
from typing import Any

SQLLEN = ctypes.c_ssize_t
SQL_HANDLE_ENV, SQL_HANDLE_DBC, SQL_HANDLE_STMT = 1, 2, 3
SQL_SUCCESS, SQL_SUCCESS_WITH_INFO, SQL_NO_DATA = 0, 1, 100
SQL_ATTR_ODBC_VERSION, SQL_OV_ODBC3 = 200, 3
SQL_ATTR_ACCESS_MODE, SQL_MODE_READ_ONLY = 101, 1
SQL_ATTR_LOGIN_TIMEOUT = 103
SQL_DRIVER_NOPROMPT = 0
SQL_NTS = -3
SQL_NULL_DATA, SQL_NO_TOTAL = -1, -4
SQL_C_WCHAR, SQL_C_BINARY = -8, -2
INTEGER_TYPES = {4, 5, -6, -7, -5}        # INTEGER, SMALLINT, TINYINT, BIT, BIGINT
REAL_TYPES = {2, 3, 6, 7, 8}              # NUMERIC, DECIMAL, FLOAT, REAL, DOUBLE
BINARY_TYPES = {-2, -3, -4}               # BINARY, VARBINARY, LONGVARBINARY (OLE objects)
TYPE_NAMES = {1: "CHAR", 2: "NUMERIC", 3: "DECIMAL", 4: "INTEGER", 5: "SMALLINT", 6: "FLOAT", 7: "REAL",
              8: "DOUBLE", 9: "DATE", 91: "DATE", 93: "DATETIME", 12: "VARCHAR", -1: "LONGTEXT", -2: "BINARY",
              -3: "VARBINARY", -4: "OLEOBJECT", -5: "BIGINT", -6: "BYTE", -7: "YESNO", -8: "WCHAR",
              -9: "TEXT", -10: "MEMO", -11: "GUID"}
TEXT_PREVIEW = 2000
MAX_ROWS = 1000
DRIVERS = ("Microsoft Access Driver (*.mdb, *.accdb)", "Microsoft Access Driver (*.mdb)")

odbc = ctypes.windll.odbc32
# Every ODBC call returns SQLRETURN, a 16-bit value: without this ctypes reads 32 bits and the
# upper half is garbage, so every call looks like it failed.
for _name in ("SQLAllocHandle", "SQLSetEnvAttr", "SQLSetConnectAttrW", "SQLDriverConnectW", "SQLDisconnect",
              "SQLFreeHandle", "SQLExecDirectW", "SQLTablesW", "SQLColumnsW", "SQLStatisticsW", "SQLNumResultCols",
              "SQLDescribeColW", "SQLFetch", "SQLGetData", "SQLGetDiagRecW"):
    getattr(odbc, _name).restype = c_short


class OdbcError(Exception):
    pass


def check(ret: int, handle_type: int, handle: c_void_p, what: str) -> int:
    if ret in (SQL_SUCCESS, SQL_SUCCESS_WITH_INFO, SQL_NO_DATA):
        return ret
    state, native = create_unicode_buffer(6), ctypes.c_int()
    message, length = create_unicode_buffer(1024), c_short()
    messages = []
    record = 1
    while odbc.SQLGetDiagRecW(handle_type, handle, record, state, byref(native), message, 1024, byref(length)) in (0, 1):
        text = message.value
        for prefix in ("[Microsoft]", "[ODBC Microsoft Access Driver]", "[ODBC Driver Manager]"):
            text = text.replace(prefix, "")
        messages.append(text.strip())
        record += 1
    raise OdbcError(f"{what}: {'; '.join(messages) or f'ODBC error {ret}'}")


class Connection:
    def __init__(self, path: str, password: str = "") -> None:
        self.env, self.dbc = c_void_p(), c_void_p()
        check(odbc.SQLAllocHandle(SQL_HANDLE_ENV, None, byref(self.env)), SQL_HANDLE_ENV, self.env, "ODBC")
        check(odbc.SQLSetEnvAttr(self.env, SQL_ATTR_ODBC_VERSION, c_void_p(SQL_OV_ODBC3), 0), SQL_HANDLE_ENV, self.env, "ODBC")
        check(odbc.SQLAllocHandle(SQL_HANDLE_DBC, self.env, byref(self.dbc)), SQL_HANDLE_ENV, self.env, "ODBC")
        odbc.SQLSetConnectAttrW(self.dbc, SQL_ATTR_ACCESS_MODE, c_void_p(SQL_MODE_READ_ONLY), 0)
        odbc.SQLSetConnectAttrW(self.dbc, SQL_ATTR_LOGIN_TIMEOUT, c_void_p(15), 0)
        last: Exception | None = None
        for driver in DRIVERS if not path.lower().endswith(".accdb") else DRIVERS[:1]:
            conn = f"Driver={{{driver}}};Dbq={path};ReadOnly=1;Exclusive=0;" + (f"Pwd={password};" if password else "")
            out, out_len = create_unicode_buffer(1024), c_short()
            ret = odbc.SQLDriverConnectW(self.dbc, None, c_wchar_p(conn), SQL_NTS, out, 1024, byref(out_len), SQL_DRIVER_NOPROMPT)
            try:
                check(ret, SQL_HANDLE_DBC, self.dbc, "Opening the Access file")
                return
            except OdbcError as exc:
                last = exc
        raise last or OdbcError("No Access ODBC driver")

    def close(self) -> None:
        odbc.SQLDisconnect(self.dbc)
        odbc.SQLFreeHandle(SQL_HANDLE_DBC, self.dbc)
        odbc.SQLFreeHandle(SQL_HANDLE_ENV, self.env)

    def statement(self) -> Statement:
        return Statement(self)


class Statement:
    def __init__(self, conn: Connection) -> None:
        self.h = c_void_p()
        check(odbc.SQLAllocHandle(SQL_HANDLE_STMT, conn.dbc, byref(self.h)), SQL_HANDLE_DBC, conn.dbc, "ODBC")

    def close(self) -> None:
        odbc.SQLFreeHandle(SQL_HANDLE_STMT, self.h)

    def run(self, sql: str) -> None:
        check(odbc.SQLExecDirectW(self.h, c_wchar_p(sql), SQL_NTS), SQL_HANDLE_STMT, self.h, "Query")

    def tables(self) -> None:
        check(odbc.SQLTablesW(self.h, None, 0, None, 0, None, 0, c_wchar_p("TABLE,VIEW"), SQL_NTS),
              SQL_HANDLE_STMT, self.h, "Listing tables")

    def columns_of(self, table: str) -> None:
        check(odbc.SQLColumnsW(self.h, None, 0, None, 0, c_wchar_p(table), SQL_NTS, None, 0),
              SQL_HANDLE_STMT, self.h, "Listing columns")

    def statistics(self, table: str) -> None:
        check(odbc.SQLStatisticsW(self.h, None, 0, None, 0, c_wchar_p(table), SQL_NTS, c_ushort(1), c_ushort(0)),
              SQL_HANDLE_STMT, self.h, "Listing indexes")

    def describe(self) -> list[dict[str, Any]]:
        count = c_short()
        check(odbc.SQLNumResultCols(self.h, byref(count)), SQL_HANDLE_STMT, self.h, "Query")
        cols = []
        for i in range(1, count.value + 1):
            name, name_len = create_unicode_buffer(256), c_short()
            dtype, size, digits, nullable = c_short(), ctypes.c_size_t(), c_short(), c_short()
            check(odbc.SQLDescribeColW(self.h, c_ushort(i), name, 256, byref(name_len), byref(dtype), byref(size),
                                       byref(digits), byref(nullable)), SQL_HANDLE_STMT, self.h, "Query")
            cols.append({"name": name.value, "sqltype": dtype.value, "size": size.value})
        return cols

    def fetch(self) -> bool:
        ret = odbc.SQLFetch(self.h)
        if ret == SQL_NO_DATA:
            return False
        check(ret, SQL_HANDLE_STMT, self.h, "Reading rows")
        return True

    def value(self, index: int, sqltype: int) -> Any:
        """Column `index` (1-based) of the current row, as a JSON cell."""
        ind = SQLLEN()
        if sqltype in BINARY_TYPES:
            buf = create_string_buffer(8192)
            total, head = 0, b""
            while True:
                ret = odbc.SQLGetData(self.h, c_ushort(index), c_short(SQL_C_BINARY), buf, SQLLEN(8192), byref(ind))
                if ret == SQL_NO_DATA:
                    break
                check(ret, SQL_HANDLE_STMT, self.h, "Reading a value")
                if ind.value == SQL_NULL_DATA:
                    return {"t": "null"}
                got = 8192 if ind.value in (SQL_NO_TOTAL,) or ind.value > 8192 else ind.value
                if not head:
                    head = buf.raw[:min(got, 24)]
                total += got
                if ret == SQL_SUCCESS:
                    break
            return {"t": "blob", "size": total, "hex": head.hex()}
        parts: list[str] = []
        buf = create_unicode_buffer(4097)
        while True:
            ret = odbc.SQLGetData(self.h, c_ushort(index), c_short(SQL_C_WCHAR), buf, SQLLEN(4097 * 2), byref(ind))
            if ret == SQL_NO_DATA:
                break
            check(ret, SQL_HANDLE_STMT, self.h, "Reading a value")
            if ind.value == SQL_NULL_DATA:
                return {"t": "null"}
            parts.append(buf.value)
            if ret == SQL_SUCCESS:
                break
        text = "".join(parts)
        if sqltype in INTEGER_TYPES:
            try:
                number = int(text)
                return {"t": "int", "v": number} if abs(number) <= 2**53 else {"t": "int", "v": text, "big": True}
            except ValueError:
                pass
        if sqltype in REAL_TYPES:
            try:
                return {"t": "real", "v": float(text)}
            except ValueError:
                pass
        if len(text) > TEXT_PREVIEW:
            return {"t": "text", "v": text[:TEXT_PREVIEW], "more": len(text)}
        return {"t": "text", "v": text}

    def rows(self, cols: list[dict[str, Any]], skip: int = 0, limit: int = MAX_ROWS) -> tuple[list[list[Any]], bool]:
        out: list[list[Any]] = []
        seen = 0
        while self.fetch():
            seen += 1
            if seen <= skip:
                continue
            if len(out) >= limit:
                return out, True
            out.append([self.value(i + 1, c["sqltype"]) for i, c in enumerate(cols)])
        return out, False


def bracket(name: str) -> str:
    if "]" in name or "[" in name:
        raise OdbcError(f"Unsupported name {name!r}")
    return f"[{name}]"


def list_tables(conn: Connection) -> list[dict[str, Any]]:
    st = conn.statement()
    try:
        st.tables()
        cols = st.describe()
        found = []
        while st.fetch():
            name = st.value(3, cols[2]["sqltype"])["v"]
            kind = st.value(4, cols[3]["sqltype"])["v"]
            if not name.startswith("MSys") and not name.startswith("~"):
                found.append({"name": name, "type": "view" if kind == "VIEW" else "table"})
    finally:
        st.close()
    for table in found:
        table["rows"] = None
        if table["type"] == "table":
            st = conn.statement()
            try:
                st.run(f"SELECT COUNT(*) FROM {bracket(table['name'])}")
                if st.fetch():
                    table["rows"] = st.value(1, 4)["v"]
            except OdbcError:
                pass
            finally:
                st.close()
    found.sort(key=lambda t: (t["type"] != "table", t["name"].lower()))
    return found


def columns(conn: Connection, table: str) -> list[dict[str, Any]]:
    st = conn.statement()
    try:
        st.columns_of(table)
        meta = st.describe()
        out = []
        while st.fetch():
            row = {m["name"].upper(): st.value(i + 1, m["sqltype"]) for i, m in enumerate(meta)}
            dtype = row.get("DATA_TYPE", {}).get("v")
            out.append({"cid": len(out), "name": row["COLUMN_NAME"]["v"],
                        "type": row.get("TYPE_NAME", {}).get("v") or TYPE_NAMES.get(dtype, ""),
                        "notnull": row.get("NULLABLE", {}).get("v") == 0,
                        "default": row.get("COLUMN_DEF", {}).get("v"), "pk": 0, "sqltype": dtype})
    finally:
        st.close()
    primary = next((i for i in index_list(conn, table) if i["name"] == "PrimaryKey"), None)
    if primary:
        for position, name in enumerate(primary["columns"], 1):
            for col in out:
                if col["name"] == name:
                    col["pk"] = position
    return out


def index_list(conn: Connection, table: str) -> list[dict[str, Any]]:
    st = conn.statement()
    try:  # the Access driver has no SQLPrimaryKeys, but its primary key is an index named PrimaryKey
        st.statistics(table)
        meta = st.describe()
        indexes: dict[str, dict[str, Any]] = {}
        while st.fetch():
            row = {m["name"].upper(): st.value(i + 1, m["sqltype"]) for i, m in enumerate(meta)}
            iname = row.get("INDEX_NAME", {}).get("v")
            if not iname:
                continue
            index = indexes.setdefault(iname, {"name": iname, "unique": row.get("NON_UNIQUE", {}).get("v") in (0, "0"),
                                               "origin": "", "columns": []})
            index["columns"].append(row.get("COLUMN_NAME", {}).get("v"))
    except OdbcError:
        indexes = {}
    finally:
        st.close()
    return list(indexes.values())


def select_page(conn: Connection, req: dict[str, Any]) -> dict[str, Any]:
    table = req["table"]
    cols = columns(conn, table)
    names = [c["name"] for c in cols]
    where = ""
    search = str(req.get("search") or "")
    if search:
        wanted = [c for c in cols if c["sqltype"] not in BINARY_TYPES and (not req.get("column") or c["name"] == req["column"])]
        if not wanted:
            raise OdbcError(f"No column named {req.get('column')!r}")
        literal = "'%" + search.replace("'", "''").replace("[", "[[]") + "%'"
        where = " WHERE " + " OR ".join(f"({bracket(c['name'])} & '') LIKE {literal}" for c in wanted)
    order = req.get("order") or ""
    if order and order not in names:
        raise OdbcError(f"No column named {order!r}")
    order_sql = f" ORDER BY {bracket(order)} {'DESC' if req.get('desc') else 'ASC'}" if order else ""
    offset, limit = int(req.get("offset") or 0), int(req.get("limit") or 100)
    st = conn.statement()
    try:
        st.run(f"SELECT COUNT(*) FROM {bracket(table)}{where}")
        total = st.value(1, 4)["v"] if st.fetch() else 0
    finally:
        st.close()
    st = conn.statement()
    try:  # Access SQL has no OFFSET: take TOP offset+limit and skip the first rows here
        st.run(f"SELECT TOP {offset + limit} * FROM {bracket(table)}{where}{order_sql}")
        meta = st.describe()
        rows, _more = st.rows(meta, skip=offset, limit=limit)
    finally:
        st.close()
    return {"columns": cols, "rows": rows, "total": total}


def run_query(conn: Connection, sql: str) -> dict[str, Any]:
    first = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if first not in ("SELECT", "TRANSFORM", "WITH"):
        raise OdbcError("Access files are read-only here: only SELECT queries can run")
    st = conn.statement()
    try:
        st.run(sql.strip().rstrip(";"))
        meta = st.describe()
        rows, truncated = st.rows(meta, limit=MAX_ROWS)
        return {"columns": [m["name"] for m in meta], "rows": rows, "truncated": truncated}
    finally:
        st.close()


def export_csv(conn: Connection, table: str) -> str:
    st = conn.statement()
    try:
        st.run(f"SELECT * FROM {bracket(table)}")
        meta = st.describe()
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow([m["name"] for m in meta])
        while st.fetch():
            row = []
            for i, m in enumerate(meta):
                cell = st.value(i + 1, m["sqltype"])
                row.append("" if cell["t"] == "null" else cell.get("hex", "") if cell["t"] == "blob" else cell["v"])
            writer.writerow(row)
        return out.getvalue()
    finally:
        st.close()


def handle(req: dict[str, Any]) -> Any:
    if struct.calcsize("P") != 4:
        raise OdbcError("This helper must run under a 32-bit Python")
    conn = Connection(req["file"], req.get("password") or "")
    try:
        op = req["op"]
        if op == "tables":
            return {"tables": list_tables(conn)}
        if op == "columns":
            return {"columns": columns(conn, req["table"]), "indexes": index_list(conn, req["table"])}
        if op == "rows":
            return select_page(conn, req)
        if op == "query":
            return run_query(conn, req["sql"])
        if op == "csv":
            return {"csv": export_csv(conn, req["table"])}
        raise OdbcError(f"unknown op {op}")
    finally:
        conn.close()


def main() -> int:
    try:
        req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        result = {"ok": True, "result": handle(req)}
    except OdbcError as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception as exc:  # report everything as JSON; varsha shows the message
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.buffer.write(json.dumps(result).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
