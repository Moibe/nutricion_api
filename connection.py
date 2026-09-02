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
    Abre la base de datos (se crea sola si no existe). El esquema se asegura
    UNA vez al arrancar la API (asegurar_schema(), llamada desde el lifespan
    de main.py) — antes corría aquí mismo, o sea en CADA request.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL: lecturas no bloquean escrituras (importa en cuanto haya más de un
    # usuario pegándole a la API a la vez). busy_timeout: si dos escrituras
    # coinciden, la segunda espera en vez de tronar con "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def asegurar_schema() -> None:
    """
    Crea las tablas base si no existen y corre las migraciones in-place
    chiquitas (agregar una columna, tirar una tabla vieja) — idempotente,
    se puede llamar de más sin romper nada. Se llama UNA vez al arrancar la
    API, no en cada conexión.

    Modelo: una `comida` (desayuno/comida/cena/colación) agrupa varios
    `consumos` (1:N). `comida_id` es nullable porque todavía no hay
    API/UI para asignarlo — los consumos guardados hasta ahora quedan
    sueltos (NULL), y así seguirá hasta que se conecte ese flujo.
    """
    conn = get_connection()
    try:
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
        # Favoritos: platillos que el usuario decide "recordar" con sus macros ya
        # calculados por la IA, para reusarlos después con un tap (POST directo a
        # /consumos) en vez de volver a describirlos y gastar otra llamada.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS favoritos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                kilocalorias REAL,
                proteinas REAL,
                carbohidratos REAL,
                grasas REAL,
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
        conn.commit()
    finally:
        conn.close()


def crear_comida(tipo: str, orden: int = 0, fecha: str | None = None) -> dict:
    """
    Crea una instancia de comida y la regresa. Sin `fecha` explícita, usa hoy
    en CDMX (botones de /hoy); con ella, crea directo en ese día (botones de
    /calendario cuando el día elegido está vacío, para no depender de crear
    hoy y luego mover la fecha en dos pasos).
    """
    from auth import uid

    usuario_id = uid()
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO comidas (usuario_id, tipo, fecha, orden) VALUES (?, ?, ?, ?)",
            (usuario_id, tipo, fecha or hoy_cdmx(), orden),
        )
        conn.commit()
        return obtener_comida(conn, cursor.lastrowid, usuario_id)
    finally:
        conn.close()


def actualizar_fecha_comida(comida_id: int, fecha: str) -> dict:
    """Cambia la fecha de una comida existente (botón de calendario del front)."""
    from auth import uid

    usuario_id = uid()
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE comidas SET fecha = ? WHERE id = ? AND usuario_id = ?", (fecha, comida_id, usuario_id)
        )
        if cursor.rowcount == 0:
            raise ValueError(f"No existe la comida {comida_id}")
        conn.commit()
        return obtener_comida(conn, comida_id, usuario_id)
    finally:
        conn.close()


def eliminar_consumo(consumo_id: int) -> None:
    """
    Borra un consumo (botón de eliminar del Listado). Si era el último de su
    comida, la comida queda vacía y simplemente deja de aparecer en el listado
    (listar_comidas hace JOIN con consumos) — no se borra la fila `comidas`.
    """
    from auth import uid

    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM consumos WHERE id = ? AND usuario_id = ?", (consumo_id, uid())
        )
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

    Ambos DELETE llevan `AND usuario_id = ?` — sin eso, alguien que adivinara
    el id de una comida AJENA podría borrar los consumos de esa comida en el
    primer DELETE (que solo filtraba por comida_id) antes de que el segundo
    DELETE (sobre comidas) fallara por no encontrarla.
    """
    from auth import uid

    usuario_id = uid()
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM consumos WHERE comida_id = ? AND usuario_id = ?", (comida_id, usuario_id)
        )
        cursor = conn.execute(
            "DELETE FROM comidas WHERE id = ? AND usuario_id = ?", (comida_id, usuario_id)
        )
        if cursor.rowcount == 0:
            raise ValueError(f"No existe la comida {comida_id}")
        conn.commit()
    finally:
        conn.close()


def obtener_comida(conn: sqlite3.Connection, comida_id: int, usuario_id: int) -> dict:
    fila = conn.execute(
        "SELECT id, tipo, fecha, orden, created_at FROM comidas WHERE id = ? AND usuario_id = ?",
        (comida_id, usuario_id),
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
    from auth import uid

    usuario_id = uid()
    conn = get_connection()
    try:
        condiciones = ["c.usuario_id = ?"]
        params: list = [usuario_id]
        if desde is not None:
            condiciones.append("c.fecha >= ?")
            params.append(desde)
        if hasta is not None:
            condiciones.append("c.fecha <= ?")
            params.append(hasta)
        where = f"WHERE {' AND '.join(condiciones)}"
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
        # usuario_id = ? acá también: sin este filtro, la query trae TODOS
        # los consumos de TODOS los usuarios a Python, y el descarte de "no
        # es de ninguna comida mía" (comida = por_id.get(...) -- None si no
        # es mía) pasa recién después, en memoria — funciona hoy porque
        # por_id ya está acotado a mis comidas, pero es un filtro que debería
        # vivir en la query, no depender de que el descarte de después nunca
        # se le olvide a nadie que edite esta función.
        for f in conn.execute(
            """
            SELECT id, comida_id, conversation_id, platillo, kilocalorias, proteinas, carbohidratos, grasas
            FROM consumos
            WHERE comida_id IS NOT NULL AND usuario_id = ?
            ORDER BY id
            """,
            (usuario_id,),
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

    LANZA ValueError si `comida_id` no existe o no es del usuario en curso —
    la FK por sí sola solo garantiza que la comida EXISTE, no que sea tuya.
    LANZA (cualquier otra excepción) si la escritura falla (p. ej. permisos
    de archivo). El endpoint /consumos traduce ValueError a 404 y el resto a
    un HTTP 503 para que el usuario reciba feedback del guardado (es una
    acción deliberada con botón, no automática).
    """
    from auth import uid

    usuario_id = uid()
    conn = get_connection()
    try:
        if datos.comida_id is not None:
            propia = conn.execute(
                "SELECT 1 FROM comidas WHERE id = ? AND usuario_id = ?", (datos.comida_id, usuario_id)
            ).fetchone()
            if propia is None:
                raise ValueError(f"No existe la comida {datos.comida_id}")
        conn.execute(
            """
            INSERT INTO consumos
                (usuario_id, conversation_id, comida_id, platillo, kilocalorias, proteinas, carbohidratos, grasas)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(usuario_id, conversation_id) DO UPDATE SET
                comida_id = excluded.comida_id,
                platillo = excluded.platillo,
                kilocalorias = excluded.kilocalorias,
                proteinas = excluded.proteinas,
                carbohidratos = excluded.carbohidratos,
                grasas = excluded.grasas,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                usuario_id,
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
            "SELECT id, platillo, kilocalorias, proteinas, carbohidratos, grasas FROM consumos "
            "WHERE usuario_id = ? AND conversation_id = ?",
            (usuario_id, conversation_id),
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


# Estimado conservador de un turno típico de /chat (ver reservar_cupo_ia) --
# se corrige con el gasto real en cuanto OpenAI responde (completar_cupo_ia).
RESERVA_TOKENS_ESTIMADA = 1500


def reservar_cupo_ia(usuario_id: int, tope_tokens_dia: int) -> int | None:
    """
    Aparta cupo de la cuota diaria ANTES de llamar a OpenAI (que tarda
    segundos), no después. Sin esto, dos /chat concurrentes del mismo
    usuario leen el mismo total "de antes" y ambos pasan el tope — un
    check-then-act clásico, con la llamada lenta de por medio agrandando la
    ventana de la carrera. BEGIN IMMEDIATE adquiere el lock de escritura de
    una vez (en vez de esperar a la primera escritura real), así que una
    segunda reserva concurrente queda serializada detrás de esta — corre
    después, ya viendo el total actualizado (busy_timeout de get_connection
    hace que espere en vez de tronar "database is locked").

    Devuelve el id de la fila placeholder (para completar_cupo_ia), o None
    si ya no hay cupo.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fila = conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) "
            "FROM uso_ia WHERE usuario_id = ? AND fecha = ?",
            (usuario_id, hoy_cdmx()),
        ).fetchone()
        # Estricto: rechaza si ESTA reserva empujaría el total por encima del
        # tope (no solo si ya estaba pasado antes de esta llamada) — así el
        # tope acota el gasto real, no "el gasto real menos una reserva de
        # más que se dejó pasar".
        if fila[0] + RESERVA_TOKENS_ESTIMADA > tope_tokens_dia:
            conn.execute("ROLLBACK")
            return None
        cursor = conn.execute(
            """
            INSERT INTO uso_ia (usuario_id, conversation_id, modelo, input_tokens, output_tokens, fecha)
            VALUES (?, NULL, NULL, ?, 0, ?)
            """,
            (usuario_id, RESERVA_TOKENS_ESTIMADA, hoy_cdmx()),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def completar_cupo_ia(fila_id: int, conversation_id: str, modelo: str, input_tokens: int, output_tokens: int) -> None:
    """
    Corrige la reserva de reservar_cupo_ia() con el gasto REAL una vez que
    OpenAI ya respondió (el estimado de la reserva casi nunca es exacto).
    Si esto llega a fallar, la reserva (RESERVA_TOKENS_ESTIMADA) se queda
    contando tal cual — a diferencia del registrar_uso() de antes, que si
    fallaba dejaba el tope sin contar NADA para esa llamada.
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE uso_ia SET conversation_id = ?, modelo = ?, input_tokens = ?, output_tokens = ?
            WHERE id = ?
            """,
            (conversation_id, modelo, int(input_tokens or 0), int(output_tokens or 0), fila_id),
        )
        conn.commit()
    finally:
        conn.close()


def resumen_uso() -> dict:
    """
    Totales de tokens del usuario en curso (todo el histórico, el mes y solo
    hoy, en CDMX). El costo se calcula en el endpoint con los precios
    configurables; aquí solo agregamos tokens.
    """
    from auth import uid

    usuario_id = uid()
    conn = get_connection()
    try:

        def agrega(condicion_extra: str = "", params: tuple = ()) -> dict:
            fila = conn.execute(
                f"""
                SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
                FROM uso_ia WHERE usuario_id = ? {condicion_extra}
                """,
                (usuario_id, *params),
            ).fetchone()
            return {"llamadas": fila[0], "input_tokens": fila[1], "output_tokens": fila[2]}

        return {
            "total": agrega(),
            # fecha es TEXT "YYYY-MM-DD" (hoy_cdmx()); los primeros 7 caracteres
            # son el mes, sin necesitar funciones de fecha de SQLite.
            "mes": agrega("AND substr(fecha, 1, 7) = ?", (mes_cdmx(),)),
            "hoy": agrega("AND fecha = ?", (hoy_cdmx(),)),
        }
    finally:
        conn.close()


def obtener_usuario_de_conversacion(conversation_id: str) -> int | None:
    """None si esta conversación nunca se registró como de nadie (conversation_id
    desconocido/ajeno) — /chat lo trata igual que "no es tuya"."""
    conn = get_connection()
    try:
        fila = conn.execute(
            "SELECT usuario_id FROM conversaciones WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        return fila[0] if fila else None
    finally:
        conn.close()


def crear_conversacion_si_falta(conversation_id: str, usuario_id: int) -> None:
    """Registra el dueño de una conversation_id nueva (idempotente)."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO conversaciones (conversation_id, usuario_id) VALUES (?, ?)",
            (conversation_id, usuario_id),
        )
        conn.commit()
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
    from auth import uid

    usuario_id = uid()
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO metricas_ios (usuario_id, fecha, tipo, valor, fuente, concepto)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(usuario_id, fecha, tipo) DO UPDATE SET
                valor = excluded.valor,
                fuente = excluded.fuente,
                concepto = COALESCE(excluded.concepto, metricas_ios.concepto),
                actualizado_at = CURRENT_TIMESTAMP
            """,
            (usuario_id, fecha, tipo, valor, fuente, concepto),
        )
        conn.commit()
        fila = conn.execute(
            "SELECT fecha, tipo, valor, fuente, concepto FROM metricas_ios WHERE usuario_id = ? AND fecha = ? AND tipo = ?",
            (usuario_id, fecha, tipo),
        ).fetchone()
        return {"fecha": fila[0], "tipo": fila[1], "valor": fila[2], "fuente": fila[3], "concepto": fila[4]}
    finally:
        conn.close()


def listar_metricas_ios(desde: str | None = None, hasta: str | None = None) -> list[dict]:
    """
    Todas las filas guardadas del usuario en curso — el front filtra por
    tipo y por día como ya hace con comidas. desde/hasta ("YYYY-MM-DD",
    opcionales, inclusivos): mismo acotado por rango que listar_comidas,
    para /registro-diario.
    """
    from auth import uid

    conn = get_connection()
    try:
        condiciones = ["usuario_id = ?"]
        params: list = [uid()]
        if desde is not None:
            condiciones.append("fecha >= ?")
            params.append(desde)
        if hasta is not None:
            condiciones.append("fecha <= ?")
            params.append(hasta)
        where = f"WHERE {' AND '.join(condiciones)}"
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
    from auth import uid

    usuario_id = uid()
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO ejercicios (usuario_id, fecha, concepto, kilocalorias) VALUES (?, ?, ?, ?)",
            (usuario_id, fecha, concepto, kilocalorias),
        )
        conn.commit()
        fila = conn.execute(
            "SELECT id, fecha, concepto, kilocalorias, created_at FROM ejercicios WHERE id = ? AND usuario_id = ?",
            (cursor.lastrowid, usuario_id),
        ).fetchone()
        return {"id": fila[0], "fecha": fila[1], "concepto": fila[2], "kilocalorias": fila[3], "created_at": fila[4]}
    finally:
        conn.close()


def listar_ejercicios(desde: str | None = None, hasta: str | None = None) -> list[dict]:
    """
    Todas las entradas de ejercicio del usuario en curso, día más reciente
    primero. desde/hasta ("YYYY-MM-DD", opcionales, inclusivos): mismo
    acotado por rango que listar_comidas/listar_metricas_ios, para
    /registro-diario.
    """
    from auth import uid

    conn = get_connection()
    try:
        condiciones = ["usuario_id = ?"]
        params: list = [uid()]
        if desde is not None:
            condiciones.append("fecha >= ?")
            params.append(desde)
        if hasta is not None:
            condiciones.append("fecha <= ?")
            params.append(hasta)
        where = f"WHERE {' AND '.join(condiciones)}"
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
    from auth import uid

    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM ejercicios WHERE id = ? AND usuario_id = ?", (ejercicio_id, uid())
        )
        if cursor.rowcount == 0:
            raise ValueError(f"No existe el ejercicio {ejercicio_id}")
        conn.commit()
    finally:
        conn.close()


def crear_favorito(
    nombre: str,
    kilocalorias: float | None,
    proteinas: float | None,
    carbohidratos: float | None,
    grasas: float | None,
) -> dict:
    """Guarda un platillo (con sus macros ya calculados) para reusar sin IA."""
    from auth import uid

    usuario_id = uid()
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO favoritos (usuario_id, nombre, kilocalorias, proteinas, carbohidratos, grasas) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (usuario_id, nombre, kilocalorias, proteinas, carbohidratos, grasas),
        )
        conn.commit()
        fila = conn.execute(
            "SELECT id, nombre, kilocalorias, proteinas, carbohidratos, grasas, created_at "
            "FROM favoritos WHERE id = ? AND usuario_id = ?",
            (cursor.lastrowid, usuario_id),
        ).fetchone()
        return {
            "id": fila[0],
            "nombre": fila[1],
            "kilocalorias": fila[2],
            "proteinas": fila[3],
            "carbohidratos": fila[4],
            "grasas": fila[5],
            "created_at": fila[6],
        }
    finally:
        conn.close()


def listar_favoritos() -> list[dict]:
    """Todos los favoritos del usuario en curso, más reciente primero."""
    from auth import uid

    conn = get_connection()
    try:
        return [
            {
                "id": f[0],
                "nombre": f[1],
                "kilocalorias": f[2],
                "proteinas": f[3],
                "carbohidratos": f[4],
                "grasas": f[5],
                "created_at": f[6],
            }
            for f in conn.execute(
                "SELECT id, nombre, kilocalorias, proteinas, carbohidratos, grasas, created_at "
                "FROM favoritos WHERE usuario_id = ? ORDER BY id DESC",
                (uid(),),
            )
        ]
    finally:
        conn.close()


def eliminar_favorito(favorito_id: int) -> None:
    """Borra un favorito (ya no aparece en la lista rápida del chat)."""
    from auth import uid

    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM favoritos WHERE id = ? AND usuario_id = ?", (favorito_id, uid())
        )
        if cursor.rowcount == 0:
            raise ValueError(f"No existe el favorito {favorito_id}")
        conn.commit()
    finally:
        conn.close()


def guardar_perfil(fecha_nacimiento: str, estatura_cm: float, sexo: str) -> dict:
    """Upsert del perfil del usuario en curso (una fila por usuario; usuario_id es la PK)."""
    from auth import uid

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO perfil (usuario_id, fecha_nacimiento, estatura_cm, sexo)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(usuario_id) DO UPDATE SET
                fecha_nacimiento = excluded.fecha_nacimiento,
                estatura_cm = excluded.estatura_cm,
                sexo = excluded.sexo,
                actualizado_at = CURRENT_TIMESTAMP
            """,
            (uid(), fecha_nacimiento, estatura_cm, sexo),
        )
        conn.commit()
        return {"fecha_nacimiento": fecha_nacimiento, "estatura_cm": estatura_cm, "sexo": sexo}
    finally:
        conn.close()


def obtener_perfil() -> dict | None:
    """None si el usuario en curso todavía no ha capturado su perfil."""
    from auth import uid

    conn = get_connection()
    try:
        fila = conn.execute(
            "SELECT fecha_nacimiento, estatura_cm, sexo FROM perfil WHERE usuario_id = ?", (uid(),)
        ).fetchone()
        if fila is None:
            return None
        return {"fecha_nacimiento": fila[0], "estatura_cm": fila[1], "sexo": fila[2]}
    finally:
        conn.close()
