"""
Migración one-shot: agrega soporte multi-usuario al schema existente.

Se corre A MANO (`python migraciones.py`), NO en cada arranque de la API —
a diferencia de las migraciones chiquitas de asegurar_schema() (agregar una
columna, tirar una tabla), esto reconstruye tablas completas (rebuild con
copia de filas) y no queremos que corra sola en cada `pm2 restart`.

Guardada por PRAGMA user_version: correrla dos veces no hace nada la
segunda vez.

Todo el historial existente queda asignado a usuario_id=1 (el dueño actual)
sin un solo UPDATE explícito — las columnas nuevas se agregan con
DEFAULT 1. Después de correr esto, falta dar de alta esa fila en `usuarios`
con crear_usuario.py "Nombre" --id 1 (el id debe coincidir con el 1 usado
aquí).
"""

from connection import asegurar_schema, get_connection

VERSION_OBJETIVO = 1


def migrar() -> None:
    asegurar_schema()  # el schema base debe existir antes de evolucionarlo

    conn = get_connection()
    try:
        version_actual = conn.execute("PRAGMA user_version").fetchone()[0]
        if version_actual >= VERSION_OBJETIVO:
            print(f"Ya migrado (user_version={version_actual}). Nada que hacer.")
            return

        print("Migrando a schema multi-usuario...")

        # --- usuarios ------------------------------------------------------
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                codigo_acceso TEXT NOT NULL UNIQUE,
                token_ios TEXT UNIQUE,
                activo INTEGER NOT NULL DEFAULT 1,
                -- Se sube (+1) para revocar TODAS las sesiones de este
                -- usuario sin tocar a los demás: la cookie trae la versión
                -- que tenía al hacer login, y un mismatch la invalida.
                token_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # --- conversaciones (ownership de conversation_id para /chat) ------
        # Sin esto, cualquiera podría mandar el conversation_id de otra
        # persona a /chat y seguir leyendo/escribiendo su conversación.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversaciones (
                conversation_id TEXT PRIMARY KEY,
                usuario_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Se siembra desde consumos (conversaciones que ya se guardaron) Y
        # uso_ia (TODAS las llamadas a /chat, incluidas las que nunca se
        # guardaron) -- consumos solo no basta: una conversación abandonada
        # a medias quedaría sin dueño y /chat la rechazaría al reabrirla.
        conn.execute(
            """
            INSERT OR IGNORE INTO conversaciones (conversation_id, usuario_id)
            SELECT DISTINCT conversation_id, 1 FROM consumos WHERE conversation_id IS NOT NULL
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO conversaciones (conversation_id, usuario_id)
            SELECT DISTINCT conversation_id, 1 FROM uso_ia WHERE conversation_id IS NOT NULL
            """
        )

        # --- usuario_id por ADD COLUMN (tablas que solo necesitan filtrar) --
        # Sin REFERENCES usuarios(id) a propósito: SQLite rechaza un ALTER
        # TABLE ADD COLUMN con FK + DEFAULT no-NULL, y de cualquier forma el
        # aislamiento real lo da el filtro `WHERE usuario_id = ?` en cada
        # query (Fase 3), no una constraint de la DB.
        for tabla in ("comidas", "ejercicios", "favoritos", "uso_ia"):
            columnas = {fila[1] for fila in conn.execute(f"PRAGMA table_info({tabla})")}
            if "usuario_id" not in columnas:
                conn.execute(f"ALTER TABLE {tabla} ADD COLUMN usuario_id INTEGER NOT NULL DEFAULT 1")

        # --- perfil: rebuild (usuario_id pasa a ser la PK, se va el --------
        # --- CHECK (id = 1) que asumía un solo usuario) ---------------------
        columnas_perfil = {fila[1] for fila in conn.execute("PRAGMA table_info(perfil)")}
        if "usuario_id" not in columnas_perfil:
            conn.execute(
                """
                CREATE TABLE perfil_nuevo (
                    usuario_id INTEGER PRIMARY KEY,
                    fecha_nacimiento TEXT NOT NULL,
                    estatura_cm REAL NOT NULL,
                    sexo TEXT NOT NULL CHECK (sexo IN ('hombre', 'mujer')),
                    actualizado_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                INSERT INTO perfil_nuevo (usuario_id, fecha_nacimiento, estatura_cm, sexo, actualizado_at)
                SELECT 1, fecha_nacimiento, estatura_cm, sexo, actualizado_at FROM perfil
                """
            )
            conn.execute("DROP TABLE perfil")
            conn.execute("ALTER TABLE perfil_nuevo RENAME TO perfil")

        # --- metricas_ios: rebuild (la PK pasa de (fecha, tipo) a ----------
        # --- (usuario_id, fecha, tipo), si no cada usuario pisaría el ------
        # --- mismo renglón del que llegó primero) ---------------------------
        columnas_metricas = {fila[1] for fila in conn.execute("PRAGMA table_info(metricas_ios)")}
        if "usuario_id" not in columnas_metricas:
            conn.execute(
                """
                CREATE TABLE metricas_ios_nuevo (
                    usuario_id INTEGER NOT NULL DEFAULT 1,
                    fecha TEXT NOT NULL,
                    tipo TEXT NOT NULL CHECK (tipo IN ('calorias_quemadas', 'peso')),
                    valor REAL NOT NULL,
                    concepto TEXT,
                    fuente TEXT NOT NULL DEFAULT 'atajo_ios',
                    actualizado_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (usuario_id, fecha, tipo)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO metricas_ios_nuevo (usuario_id, fecha, tipo, valor, concepto, fuente, actualizado_at)
                SELECT 1, fecha, tipo, valor, concepto, fuente, actualizado_at FROM metricas_ios
                """
            )
            conn.execute("DROP TABLE metricas_ios")
            conn.execute("ALTER TABLE metricas_ios_nuevo RENAME TO metricas_ios")

        # --- consumos: rebuild (conversation_id deja de ser UNIQUE global, -
        # --- pasa a UNIQUE(usuario_id, conversation_id) -- dos usuarios ----
        # --- nunca comparten conversation_id en la práctica, pero el ------
        # --- upsert de guardar_consumo() debe quedar acotado por usuario) --
        columnas_consumos = {fila[1] for fila in conn.execute("PRAGMA table_info(consumos)")}
        if "usuario_id" not in columnas_consumos:
            conn.execute(
                """
                CREATE TABLE consumos_nuevo (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    usuario_id INTEGER NOT NULL DEFAULT 1,
                    conversation_id TEXT NOT NULL,
                    comida_id INTEGER REFERENCES comidas(id),
                    platillo TEXT,
                    kilocalorias REAL,
                    proteinas REAL,
                    carbohidratos REAL,
                    grasas REAL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (usuario_id, conversation_id)
                )
                """
            )
            # Se copian los id explícitos -- ningún id de consumo cambia, y
            # sqlite_sequence de la tabla renombrada sigue avanzando desde
            # el máximo real (SQLite lo actualiza en cada INSERT, incluso
            # con id explícito, mientras la tabla sea AUTOINCREMENT).
            conn.execute(
                """
                INSERT INTO consumos_nuevo
                    (id, usuario_id, conversation_id, comida_id, platillo, kilocalorias,
                     proteinas, carbohidratos, grasas, created_at, updated_at)
                SELECT id, 1, conversation_id, comida_id, platillo, kilocalorias,
                       proteinas, carbohidratos, grasas, created_at, updated_at
                FROM consumos
                """
            )
            conn.execute("DROP TABLE consumos")
            conn.execute("ALTER TABLE consumos_nuevo RENAME TO consumos")

        # --- índices ---------------------------------------------------------
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comidas_usuario_fecha ON comidas(usuario_id, fecha)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ejercicios_usuario_fecha ON ejercicios(usuario_id, fecha)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_uso_ia_usuario_fecha ON uso_ia(usuario_id, fecha)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_consumos_usuario ON consumos(usuario_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_favoritos_usuario ON favoritos(usuario_id)")
        # metricas_ios no necesita índice aparte: su PK ya arranca en usuario_id.

        conn.execute(f"PRAGMA user_version = {VERSION_OBJETIVO}")
        conn.commit()
        print(f"Migración completa (user_version={VERSION_OBJETIVO}).")
    finally:
        conn.close()


if __name__ == "__main__":
    migrar()
