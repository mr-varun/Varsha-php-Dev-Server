"""Create a small Access .mdb for the tests (run with a 32-bit Python: python make_access_fixture.py OUT.mdb).

Uses the Access ODBC driver's own CREATE_DB, then plain Jet SQL over a writable connection.
"""

import ctypes
import sys
from ctypes import byref, c_short, c_void_p, c_wchar_p, create_unicode_buffer

DRIVER = "Microsoft Access Driver (*.mdb)"
odbc = ctypes.windll.odbc32
odbccp = ctypes.windll.odbccp32
for _name in ("SQLAllocHandle", "SQLSetEnvAttr", "SQLDriverConnectW", "SQLExecDirectW", "SQLFreeHandle", "SQLDisconnect"):
    getattr(odbc, _name).restype = c_short  # SQLRETURN is 16-bit


def main(path: str) -> None:
    global DRIVER
    create = "CREATE_DB"
    if path.lower().endswith(".accdb"):
        DRIVER = "Microsoft Access Driver (*.mdb, *.accdb)"  # ACE (Access 2007+)
        create = "CREATE_DBV12"  # plain CREATE_DB always writes the old Jet (.mdb) format
    if not odbccp.SQLConfigDataSourceW(None, 1, c_wchar_p(DRIVER), c_wchar_p(f'{create}="{path}" General\0\0')):
        raise SystemExit("CREATE_DB failed")
    env, dbc, st = c_void_p(), c_void_p(), c_void_p()
    odbc.SQLAllocHandle(1, None, byref(env))
    odbc.SQLSetEnvAttr(env, 200, c_void_p(3), 0)
    odbc.SQLAllocHandle(2, env, byref(dbc))
    out, out_len = create_unicode_buffer(1024), c_short()
    if odbc.SQLDriverConnectW(dbc, None, c_wchar_p(f"Driver={{{DRIVER}}};Dbq={path};"), -3, out, 1024, byref(out_len), 0) not in (0, 1):
        raise SystemExit("connect failed")
    statements = [
        "CREATE TABLE members (id COUNTER CONSTRAINT PrimaryKey PRIMARY KEY, name TEXT(60) NOT NULL, "
        "branch TEXT(30), balance CURRENCY, joined DATETIME, active BIT, notes MEMO, photo LONGBINARY)",
        "CREATE TABLE loans (loan_no TEXT(12) CONSTRAINT PrimaryKey PRIMARY KEY, member_id INTEGER, amount DOUBLE)",
        "CREATE INDEX loans_member ON loans (member_id)",
    ]
    names = ["Asha", "Ravi", "Meena", "Kiran", "Suma", "Prakash"]
    for i in range(1, 31):
        name = names[i % 6] + f" {i}"
        branch = "Hubli" if i % 2 else "Dharwad"
        balance = "NULL" if i % 7 == 0 else f"{100 + i * 12.5}"
        notes = "'" + ("long note " * 300 if i == 3 else "o''clock") + "'"
        statements.append(f"INSERT INTO members (name, branch, balance, joined, active, notes) "
                          f"VALUES ('{name}', '{branch}', {balance}, #2026-0{1 + i % 9}-1{i % 10}#, {i % 2}, {notes})")
    for i in range(1, 11):
        statements.append(f"INSERT INTO loans (loan_no, member_id, amount) VALUES ('L{i:04d}', {i}, {i * 1000.5})")
    for sql in statements:
        odbc.SQLAllocHandle(3, dbc, byref(st))
        ret = odbc.SQLExecDirectW(st, c_wchar_p(sql), -3)
        if ret not in (0, 1):
            raise SystemExit(f"failed: {sql[:80]}")
        odbc.SQLFreeHandle(3, st)
    odbc.SQLDisconnect(dbc)
    print("ok", path)


if __name__ == "__main__":
    main(sys.argv[1])
