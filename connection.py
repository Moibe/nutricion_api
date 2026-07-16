"""
Conexión a SQLite y persistencia de consumos.

Un solo archivo de base de datos, sin servidor ni credenciales que administrar
(mismo espíritu que tus proyectos SvelteKit con Drizzle + better-sqlite3).
"""

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent / "nutricion.db"))


def get_connection() -> sqlite3.Connection:
    """
    Abre la base de datos (se crea sola si no existe) y asegura el esquema.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS consumos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL UNIQUE,
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
    return conn


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
