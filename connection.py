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


def mes_cdmx() -> str:
    return datetime.now(ZONA_CDMX).strftime("%Y-%m")


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
    # Tabla anterior de un solo metric (solo calorías quemadas), reemplazada
    # por metricas_ios de abajo, genérica para varios tipos de dato de iOS
    # (calorías quemadas, peso, lo que se agregue después). Nunca llegó a
    # tener datos reales en producción, así que se puede tirar sin migrar nada.
    # Guardado con el mismo patrón que las demás migraciones in-place de este
    # archivo (solo corre de verdad una vez, no en cada apertura de conexión).
    if conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'calorias_quemadas'"
    ).fetchone():
        conn.execute("DROP TABLE calorias_quemadas")
    # Métricas que manda un Atajo de iOS (Salud → nuestra API): una fila por
    # (fecha, tipo) — upsert, porque el Atajo puede correr varias veces al día
    # sobre el mismo día y siempre debe reemplazar el valor, no sumarlo.
    # `tipo` distingue qué es `valor` (unidad implícita por tipo: kcal para
    # calorias_quemadas, kg para peso). `concepto` es opcional (nullable):
    # solo lo manda la captura manual de /ejercicio ("Correr 5km", "Pesas"...)
    # — el Atajo de iOS solo conoce el número, no una descripción, así que
    # nunca lo manda y no debe ser obligatorio o le rompería el POST.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metricas_ios (
            fecha TEXT NOT NULL,
            tipo TEXT NOT NULL CHECK (tipo IN ('calorias_quemadas', 'peso')),
            valor REAL NOT NULL,
            concepto TEXT,
            fuente TEXT NOT NULL DEFAULT 'atajo_ios',
            actualizado_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (fecha, tipo)
        )
        """
    )
    # Migración in-place para bases creadas antes de que existiera concepto.
    columnas_metricas = {fila[1] for fila in conn.execute("PRAGMA table_info(metricas_ios)")}
    if "concepto" not in columnas_metricas:
        conn.execute("ALTER TABLE metricas_ios ADD COLUMN concepto TEXT")
    # Ejercicio manual: BITÁCORA, no un solo valor por día — cada "Guardar" de
    # /ejercicio agrega una fila (mismo espíritu que comidas/consumos: varios
    # renglones que se suman a un total del día), a diferencia de
    # metricas_ios (upsert de un solo valor por fecha+tipo, que sigue siendo
    # exclusivo del Atajo de iOS). "kcal quemadas" mostradas en el resto de la
    # app = suma de esta tabla + el valor de metricas_ios, cuando exista.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ejercicios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            concepto TEXT NOT NULL,
            kilocalorias REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Perfil para calcular metabolismo basal (Mifflin-St Jeor): una sola fila
    # (id fijo en 1 — app de un solo usuario). fecha_nacimiento en vez de
    # "edad" porque la edad cambia con el tiempo y un número fijo se volvería
    # viejo; se calcula al vuelo cada vez que se necesita.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS perfil (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            fecha_nacimiento TEXT NOT NULL,
            estatura_cm REAL NOT NULL,
            sexo TEXT NOT NULL CHECK (sexo IN ('hombre', 'mujer')),
            actualizado_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return conn


def crear_comida(tipo: str, orden: int = 0, fecha: str | None = None) -> dict:
    """
    Crea una instancia de comida y la regresa. Sin `fecha` explícita, usa hoy
    en CDMX (botones de /hoy); con ella, crea directo en ese día (botones de
    /calendario cuando el día elegido está vacío, para no depender de crear
    hoy y luego mover la fecha en dos pasos).
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO comidas (tipo, fecha, orden) VALUES (?, ?, ?)",
            (tipo, fecha or hoy_cdmx(), orden),
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


def listar_comidas(desde: str | None = None, hasta: str | None = None) -> list[dict]:
    """
    Lista las comidas GUARDADAS (con al menos un consumo asociado), cada una
    con sus consumos anidados. Las comidas vacías (se creó la instancia con el
    botón pero nunca se le guardó un consumo) se omiten — son cascarones sin
    información nutricional. Orden: día más reciente primero; dentro del
    mismo día, en la secuencia en que se comen (orden ASC — Desayuno,
    Colación 1, Comida, Colación 2, Cena), no por cuándo se guardaron.

    desde/hasta ("YYYY-MM-DD", opcionales, inclusivos): acotan por c.fecha —
    usado por /registro-diario para pedir solo un mes en vez de todo el
    historial en cada carga. None = sin ese límite (comportamiento de siempre).
    """
    conn = get_connection()
    try:
        condiciones = []
        params: list = []
        if desde is not None:
            condiciones.append("c.fecha >= ?")
            params.append(desde)
        if hasta is not None:
            condiciones.append("c.fecha <= ?")
            params.append(hasta)
        where = f"WHERE {' AND '.join(condiciones)}" if condiciones else ""
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
                f"""
                SELECT DISTINCT c.id, c.tipo, c.fecha, c.orden, c.created_at
                FROM comidas c
                JOIN consumos x ON x.comida_id = c.id
                {where}
                ORDER BY c.fecha DESC, c.orden ASC, c.id ASC
                """,
                params,
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
    Totales de tokens (todo el histórico, el mes y solo hoy, en CDMX). El costo
    se calcula en el endpoint con los precios configurables; aquí solo
    agregamos tokens.
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

        return {
            "total": agrega(),
            # fecha es TEXT "YYYY-MM-DD" (hoy_cdmx()); los primeros 7 caracteres
            # son el mes, sin necesitar funciones de fecha de SQLite.
            "mes": agrega("WHERE substr(fecha, 1, 7) = ?", (mes_cdmx(),)),
            "hoy": agrega("WHERE fecha = ?", (hoy_cdmx(),)),
        }
    finally:
        conn.close()


def guardar_metrica_ios(
    tipo: str, fecha: str, valor: float, fuente: str = "atajo_ios", concepto: str | None = None
) -> dict:
    """
    Upsert por (fecha, tipo): el Atajo de iOS puede correr varias veces sobre
    el mismo día (reintentos, o correrlo manual para probar) y cada corrida
    ya trae el valor del día completo hasta ese momento — reemplaza, no suma.

    `concepto` usa COALESCE en el UPDATE: si esta llamada no lo manda (el
    Atajo de iOS nunca lo hace, solo la captura manual), se conserva el que
    ya hubiera en vez de borrarlo — así el Atajo actualizando el número no
    pisa una descripción que ya habías escrito a mano.
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO metricas_ios (fecha, tipo, valor, fuente, concepto)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(fecha, tipo) DO UPDATE SET
                valor = excluded.valor,
                fuente = excluded.fuente,
                concepto = COALESCE(excluded.concepto, metricas_ios.concepto),
                actualizado_at = CURRENT_TIMESTAMP
            """,
            (fecha, tipo, valor, fuente, concepto),
        )
        conn.commit()
        fila = conn.execute(
            "SELECT fecha, tipo, valor, fuente, concepto FROM metricas_ios WHERE fecha = ? AND tipo = ?",
            (fecha, tipo),
        ).fetchone()
        return {"fecha": fila[0], "tipo": fila[1], "valor": fila[2], "fuente": fila[3], "concepto": fila[4]}
    finally:
        conn.close()


def listar_metricas_ios(desde: str | None = None, hasta: str | None = None) -> list[dict]:
    """
    Todas las filas guardadas — el front filtra por tipo y por día como ya
    hace con comidas. desde/hasta ("YYYY-MM-DD", opcionales, inclusivos):
    mismo acotado por rango que listar_comidas, para /registro-diario.
    """
    conn = get_connection()
    try:
        condiciones = []
        params: list = []
        if desde is not None:
            condiciones.append("fecha >= ?")
            params.append(desde)
        if hasta is not None:
            condiciones.append("fecha <= ?")
            params.append(hasta)
        where = f"WHERE {' AND '.join(condiciones)}" if condiciones else ""
        return [
            {"fecha": f[0], "tipo": f[1], "valor": f[2], "fuente": f[3], "concepto": f[4]}
            for f in conn.execute(
                f"SELECT fecha, tipo, valor, fuente, concepto FROM metricas_ios {where} ORDER BY fecha DESC",
                params,
            )
        ]
    finally:
        conn.close()


def crear_ejercicio(fecha: str, concepto: str, kilocalorias: float) -> dict:
    """Agrega una entrada de ejercicio (bitácora — no reemplaza las anteriores del día)."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO ejercicios (fecha, concepto, kilocalorias) VALUES (?, ?, ?)",
            (fecha, concepto, kilocalorias),
        )
        conn.commit()
        fila = conn.execute(
            "SELECT id, fecha, concepto, kilocalorias, created_at FROM ejercicios WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return {"id": fila[0], "fecha": fila[1], "concepto": fila[2], "kilocalorias": fila[3], "created_at": fila[4]}
    finally:
        conn.close()


def listar_ejercicios(desde: str | None = None, hasta: str | None = None) -> list[dict]:
    """
    Todas las entradas de ejercicio guardadas, día más reciente primero.
    desde/hasta ("YYYY-MM-DD", opcionales, inclusivos): mismo acotado por
    rango que listar_comidas/listar_metricas_ios, para /registro-diario.
    """
    conn = get_connection()
    try:
        condiciones = []
        params: list = []
        if desde is not None:
            condiciones.append("fecha >= ?")
            params.append(desde)
        if hasta is not None:
            condiciones.append("fecha <= ?")
            params.append(hasta)
        where = f"WHERE {' AND '.join(condiciones)}" if condiciones else ""
        return [
            {"id": f[0], "fecha": f[1], "concepto": f[2], "kilocalorias": f[3], "created_at": f[4]}
            for f in conn.execute(
                f"SELECT id, fecha, concepto, kilocalorias, created_at FROM ejercicios {where} ORDER BY fecha DESC, id ASC",
                params,
            )
        ]
    finally:
        conn.close()


def eliminar_ejercicio(ejercicio_id: int) -> None:
    """Borra una entrada de ejercicio (botón de eliminar de la bitácora)."""
    conn = get_connection()
    try:
        cursor = conn.execute("DELETE FROM ejercicios WHERE id = ?", (ejercicio_id,))
        if cursor.rowcount == 0:
            raise ValueError(f"No existe el ejercicio {ejercicio_id}")
        conn.commit()
    finally:
        conn.close()


def guardar_perfil(fecha_nacimiento: str, estatura_cm: float, sexo: str) -> dict:
    """Upsert de la única fila de perfil (id=1) — app de un solo usuario."""
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO perfil (id, fecha_nacimiento, estatura_cm, sexo)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                fecha_nacimiento = excluded.fecha_nacimiento,
                estatura_cm = excluded.estatura_cm,
                sexo = excluded.sexo,
                actualizado_at = CURRENT_TIMESTAMP
            """,
            (fecha_nacimiento, estatura_cm, sexo),
        )
        conn.commit()
        return {"fecha_nacimiento": fecha_nacimiento, "estatura_cm": estatura_cm, "sexo": sexo}
    finally:
        conn.close()


def obtener_perfil() -> dict | None:
    """None si todavía no se ha capturado el perfil (primera vez)."""
    conn = get_connection()
    try:
        fila = conn.execute(
            "SELECT fecha_nacimiento, estatura_cm, sexo FROM perfil WHERE id = 1"
        ).fetchone()
        if fila is None:
            return None
        return {"fecha_nacimiento": fila[0], "estatura_cm": fila[1], "sexo": fila[2]}
    finally:
        conn.close()
