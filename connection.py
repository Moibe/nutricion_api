"""
Conexión a SQLite y persistencia de consumos.

Un solo archivo de base de datos, sin servidor ni credenciales que administrar
(mismo espíritu que tus proyectos SvelteKit con Drizzle + better-sqlite3).
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent / "nutricion.db"))

# Fecha de una comida = la de Ciudad de México, no la del servidor (el droplet
# corre en UTC; cerca de medianoche CDMX eso desfasaría el día por hasta 6h).
ZONA_CDMX = ZoneInfo("America/Mexico_City")


def hoy_cdmx() -> str:
    return datetime.now(ZONA_CDMX).date().isoformat()


def get_connection() -> sqlite3.Connection:
    """
    Abre la base de datos (se crea sola si no existe) y asegura el esquema.

    Modelo: una `comida` (desayuno/comida/cena/colación) agrupa varios
    `consumos` (1:N). `comida_id` es nullable porque todavía no hay
    API/UI para asignarlo — los consumos guardados hasta ahora quedan
    sueltos (NULL), y así seguirá hasta que se conecte ese flujo.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS comidas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL CHECK (tipo IN ('desayuno', 'comida', 'cena', 'colacion')),
            fecha TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS consumos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL UNIQUE,
            comida_id INTEGER REFERENCES comidas(id),
            platillo TEXT,
            kilocalorias REAL,
            proteinas REAL,
            carbohidratos REAL,
            grasas REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Migración in-place para bases creadas antes de que existiera comida_id.
    columnas = {fila[1] for fila in conn.execute("PRAGMA table_info(consumos)")}
    if "comida_id" not in columnas:
        conn.execute("ALTER TABLE consumos ADD COLUMN comida_id INTEGER REFERENCES comidas(id)")
    return conn


def crear_comida(tipo: str) -> dict:
    """Crea una instancia de comida (tipo + fecha de hoy en CDMX) y la regresa."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO comidas (tipo, fecha) VALUES (?, ?)",
            (tipo, hoy_cdmx()),
        )
        conn.commit()
        return obtener_comida(conn, cursor.lastrowid)
    finally:
        conn.close()


def actualizar_fecha_comida(comida_id: int, fecha: str) -> dict:
    """Cambia la fecha de una comida existente (botón de calendario del front)."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE comidas SET fecha = ? WHERE id = ?", (fecha, comida_id)
        )
        if cursor.rowcount == 0:
            raise ValueError(f"No existe la comida {comida_id}")
        conn.commit()
        return obtener_comida(conn, comida_id)
    finally:
        conn.close()


def obtener_comida(conn: sqlite3.Connection, comida_id: int) -> dict:
    fila = conn.execute(
        "SELECT id, tipo, fecha, created_at FROM comidas WHERE id = ?", (comida_id,)
    ).fetchone()
    return {"id": fila[0], "tipo": fila[1], "fecha": fila[2], "created_at": fila[3]}


def guardar_consumo(conversation_id: str, datos) -> None:
    """
    Persiste el resultado final (platillo + 4 macros) en la tabla `consumos`.
    `datos` es cualquier objeto con .platillo/.kilocalorias/.proteinas/
    .carbohidratos/.grasas (p. ej. el ConsumoIn que llega del front).

    Upsert por `conversation_id`: si el usuario refina y se recalcula la misma
    conversación, se ACTUALIZA la fila en vez de duplicar.

    LANZA si la escritura falla (p. ej. permisos de archivo). El endpoint
    /consumos traduce el error a un HTTP 503 para que el usuario reciba
    feedback del guardado (es una acción deliberada con botón, no automática).
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO consumos
                (conversation_id, platillo, kilocalorias, proteinas, carbohidratos, grasas)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                platillo = excluded.platillo,
                kilocalorias = excluded.kilocalorias,
                proteinas = excluded.proteinas,
                carbohidratos = excluded.carbohidratos,
                grasas = excluded.grasas,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                conversation_id,
                datos.platillo,
                datos.kilocalorias,
                datos.proteinas,
                datos.carbohidratos,
                datos.grasas,
            ),
        )
        conn.commit()
    finally:
        conn.close()
