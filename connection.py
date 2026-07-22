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
            -- Posición en la secuencia del día (Desayuno=0, Colación 1=1,
            -- Comida=2, Colación 2=3, Cena=4). Separado de `tipo` porque las
            -- dos colaciones comparten tipo pero van en momentos distintos.
            orden INTEGER NOT NULL DEFAULT 0,
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
    # Migración in-place para bases creadas antes de que existiera orden.
    columnas_comidas = {fila[1] for fila in conn.execute("PRAGMA table_info(comidas)")}
    if "orden" not in columnas_comidas:
        conn.execute("ALTER TABLE comidas ADD COLUMN orden INTEGER NOT NULL DEFAULT 0")
    # Uso de tokens de OpenAI: una fila por llamada a /chat, para el monitor de gasto.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS uso_ia (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            modelo TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            fecha TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return conn


def crear_comida(tipo: str, orden: int = 0) -> dict:
    """Crea una instancia de comida (tipo + fecha de hoy en CDMX) y la regresa."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO comidas (tipo, fecha, orden) VALUES (?, ?, ?)",
            (tipo, hoy_cdmx(), orden),
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


def eliminar_consumo(consumo_id: int) -> None:
    """
    Borra un consumo (botón de eliminar del Listado). Si era el último de su
    comida, la comida queda vacía y simplemente deja de aparecer en el listado
    (listar_comidas hace JOIN con consumos) — no se borra la fila `comidas`.
    """
    conn = get_connection()
    try:
        cursor = conn.execute("DELETE FROM consumos WHERE id = ?", (consumo_id,))
        if cursor.rowcount == 0:
            raise ValueError(f"No existe el consumo {consumo_id}")
        conn.commit()
    finally:
        conn.close()


def eliminar_comida(comida_id: int) -> None:
    """
    Borra una comida completa junto con todos sus consumos (botón del bote en
    la tarjeta, a diferencia de eliminar_consumo que solo quita un consumo y
    puede dejar la comida vacía sin borrar su fila). Se borran los consumos
    primero porque la FK consumos.comida_id no tiene ON DELETE CASCADE.
    """
    conn = get_connection()
    try:
        conn.execute("DELETE FROM consumos WHERE comida_id = ?", (comida_id,))
        cursor = conn.execute("DELETE FROM comidas WHERE id = ?", (comida_id,))
        if cursor.rowcount == 0:
            raise ValueError(f"No existe la comida {comida_id}")
        conn.commit()
    finally:
        conn.close()


def obtener_comida(conn: sqlite3.Connection, comida_id: int) -> dict:
    fila = conn.execute(
        "SELECT id, tipo, fecha, orden, created_at FROM comidas WHERE id = ?", (comida_id,)
    ).fetchone()
    return {"id": fila[0], "tipo": fila[1], "fecha": fila[2], "orden": fila[3], "created_at": fila[4]}


def listar_comidas() -> list[dict]:
    """
    Lista las comidas GUARDADAS (con al menos un consumo asociado), cada una
    con sus consumos anidados. Las comidas vacías (se creó la instancia con el
    botón pero nunca se le guardó un consumo) se omiten — son cascarones sin
    información nutricional. Orden: día más reciente primero; dentro del
    mismo día, en la secuencia en que se comen (orden ASC — Desayuno,
    Colación 1, Comida, Colación 2, Cena), no por cuándo se guardaron.
    """
    conn = get_connection()
    try:
        comidas = [
            {
                "id": f[0],
                "tipo": f[1],
                "fecha": f[2],
                "orden": f[3],
                "created_at": f[4],
                "consumos": [],
            }
            for f in conn.execute(
                """
                SELECT DISTINCT c.id, c.tipo, c.fecha, c.orden, c.created_at
                FROM comidas c
                JOIN consumos x ON x.comida_id = c.id
                ORDER BY c.fecha DESC, c.orden ASC, c.id ASC
                """
            )
        ]
        por_id = {c["id"]: c for c in comidas}
        for f in conn.execute(
            """
            SELECT id, comida_id, conversation_id, platillo, kilocalorias, proteinas, carbohidratos, grasas
            FROM consumos
            WHERE comida_id IS NOT NULL
            ORDER BY id
            """
        ):
            comida = por_id.get(f[1])
            if comida:
                comida["consumos"].append(
                    {
                        "id": f[0],
                        "conversation_id": f[2],
                        "platillo": f[3],
                        "kilocalorias": f[4],
                        "proteinas": f[5],
                        "carbohidratos": f[6],
                        "grasas": f[7],
                    }
                )
        return comidas
    finally:
        conn.close()


def guardar_consumo(conversation_id: str, datos) -> dict:
    """
    Persiste el resultado final (platillo + 4 macros) en la tabla `consumos`.
    `datos` es cualquier objeto con .comida_id/.platillo/.kilocalorias/
    .proteinas/.carbohidratos/.grasas (p. ej. el ConsumoIn que llega del front).
    `comida_id` es opcional: null si el consumo se guarda suelto (fuera de
    una comida), o el id de la comida a la que pertenece.

    Upsert por `conversation_id`: si el usuario refina y se recalcula la misma
    conversación, se ACTUALIZA la fila en vez de duplicar. Este es también el
    mecanismo de "editar" un consumo ya guardado: el front reabre la MISMA
    conversación (mandando el conversation_id original) para seguir chateando
    con el asistente, y al guardar de nuevo esto actualiza esa fila en vez de
    crear una nueva. Regresa la fila resultante (con su id) para que el front
    la pueda usar sin recargar — no se puede confiar en cursor.lastrowid
    porque en la rama ON CONFLICT DO UPDATE no refleja el id de la fila
    actualizada.

    LANZA si la escritura falla (p. ej. permisos de archivo, o comida_id que
    no existe — la FK lo rechaza). El endpoint /consumos traduce el error a
    un HTTP 503 para que el usuario reciba feedback del guardado (es una
    acción deliberada con botón, no automática).
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO consumos
                (conversation_id, comida_id, platillo, kilocalorias, proteinas, carbohidratos, grasas)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                comida_id = excluded.comida_id,
                platillo = excluded.platillo,
                kilocalorias = excluded.kilocalorias,
                proteinas = excluded.proteinas,
                carbohidratos = excluded.carbohidratos,
                grasas = excluded.grasas,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                conversation_id,
                datos.comida_id,
                datos.platillo,
                datos.kilocalorias,
                datos.proteinas,
                datos.carbohidratos,
                datos.grasas,
            ),
        )
        conn.commit()
        fila = conn.execute(
            "SELECT id, platillo, kilocalorias, proteinas, carbohidratos, grasas FROM consumos WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return {
            "id": fila[0],
            "platillo": fila[1],
            "kilocalorias": fila[2],
            "proteinas": fila[3],
            "carbohidratos": fila[4],
            "grasas": fila[5],
        }
    finally:
        conn.close()


def registrar_uso(conversation_id, modelo: str, input_tokens: int, output_tokens: int) -> None:
    """Registra el uso de tokens de una llamada a OpenAI (monitor de gasto)."""
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO uso_ia (conversation_id, modelo, input_tokens, output_tokens, fecha)
            VALUES (?, ?, ?, ?, ?)
            """,
            (conversation_id, modelo, int(input_tokens or 0), int(output_tokens or 0), hoy_cdmx()),
        )
        conn.commit()
    finally:
        conn.close()


def resumen_uso() -> dict:
    """
    Totales de tokens (todo el histórico y solo hoy en CDMX). El costo se calcula
    en el endpoint con los precios configurables; aquí solo agregamos tokens.
    """
    conn = get_connection()
    try:

        def agrega(where: str = "", params: tuple = ()) -> dict:
            fila = conn.execute(
                f"""
                SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
                FROM uso_ia {where}
                """,
                params,
            ).fetchone()
            return {"llamadas": fila[0], "input_tokens": fila[1], "output_tokens": fila[2]}

        return {"total": agrega(), "hoy": agrega("WHERE fecha = ?", (hoy_cdmx(),))}
    finally:
        conn.close()
