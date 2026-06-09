"""
Conexión a MariaDB y persistencia de consumos.

Mismo patrón que tus proyectos `fastapi-mariadb*`: `mysql-connector-python`,
variables de entorno DB_HOST / DB_USER / mariadb_c (password) / DB_NAME / DB_PORT,
y `autocommit=True`.
"""

import os

import mysql.connector
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    """
    Establece una conexión con MariaDB. Retorna la conexión o None si falla
    (p. ej. credenciales ausentes en .env). Nunca lanza.
    """
    try:
        conn = mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("mariadb_c"),
            database=os.getenv("DB_NAME"),
            port=int(os.getenv("DB_PORT", 3306)),
            autocommit=True,
        )
        return conn
    except mysql.connector.Error as err:
        print(f"[WARN] No se pudo conectar a MariaDB: {err}")
        return None


def guardar_consumo(conversation_id: str, datos) -> None:
    """
    Persiste el resultado final (platillo + 4 macros) en la tabla `consumos`.
    `datos` es cualquier objeto con .platillo/.kilocalorias/.proteinas/
    .carbohidratos/.grasas (p. ej. el ConsumoIn que llega del front).

    Upsert por `conversation_id`: si el usuario refina y se recalcula la misma
    conversación, se ACTUALIZA la fila en vez de duplicar.

    LANZA si la DB no está disponible o la query falla. El endpoint /consumos
    traduce el error a un HTTP 503 para que el usuario reciba feedback del
    guardado (es una acción deliberada con botón, no un guardado automático).
    """
    conn = get_connection()
    if conn is None:
        raise RuntimeError("No hay conexión a MariaDB (revisa las credenciales en .env).")
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO consumos
                (conversation_id, platillo, kilocalorias, proteinas, carbohidratos, grasas)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                platillo = VALUES(platillo),
                kilocalorias = VALUES(kilocalorias),
                proteinas = VALUES(proteinas),
                carbohidratos = VALUES(carbohidratos),
                grasas = VALUES(grasas)
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
        cursor.close()
    finally:
        conn.close()
