#!/usr/bin/env python3
"""
Verifica/crea el archivo SQLite y la tabla `consumos`. No es un paso
obligatorio: get_connection() ya crea el esquema sola en el primer uso; este
script sirve para confirmar el estado o inspeccionar la estructura a mano.

    .venv/Scripts/python.exe init_db.py
"""

import sys

from connection import DB_PATH, get_connection


def init_database():
    try:
        conn = get_connection()
    except Exception as e:
        print(f"❌ No se pudo abrir la base de datos: {e}")
        sys.exit(1)

    print(f"✅ Base de datos lista en {DB_PATH}")

    cursor = conn.execute("PRAGMA table_info(consumos)")
    print("\n📋 Estructura de 'consumos':")
    print("-" * 60)
    for cid, name, col_type, notnull, default, pk in cursor.fetchall():
        flags = " ".join(f for f in ("PK" if pk else "", "NOT NULL" if notnull else "") if f)
        print(f"  {name:<18} {col_type:<10} {flags}")
    conn.close()


if __name__ == "__main__":
    print("🔨 Inicializando base de datos…")
    print("-" * 60)
    init_database()
    print("\n✅ Lista para usar")
