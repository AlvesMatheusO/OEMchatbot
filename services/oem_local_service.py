import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DB_PATH = BASE_DIR / "data" / "autoflex_oem.db"


def get_connection():

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    return conn


def search_oem(oem):

    conn = get_connection()

    query = """
    SELECT *
    FROM oems
    WHERE codigo_oem = ?
    """

    row = conn.execute(
        query,
        (oem,)
    ).fetchone()

    conn.close()

    if not row:
        return None

    return dict(row)