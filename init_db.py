#!/usr/bin/env python3
"""
Crea la tabla `consumos` en MariaDB. Corre una vez (o cuando cambie el esquema):

    .venv/Scripts/python.exe init_db.py

Requiere las variables de DB en .env (DB_HOST, DB_USER, mariadb_c, DB_NAME, DB_PORT).
"""

import sys

from connection import get_connection


def init_database():
    conn = get_connection()
    if not conn:
        print("❌ No se pudo conectar a la base de datos (revisa .env)")
        sys.exit(1)

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS `consumos` (
                `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
                `conversation_id` VARCHAR(64) NOT NULL UNIQUE
                    COMMENT 'conversation_id de OpenAI; upsert por conversación',
                `platillo` TEXT COMMENT 'Nombre/descripción del platillo final',
                `kilocalorias` DOUBLE COMMENT 'Energía total (kcal)',
                `proteinas` DOUBLE COMMENT 'Proteínas (g)',
                `carbohidratos` DOUBLE COMMENT 'Carbohidratos (g)',
                `grasas` DOUBLE COMMENT 'Grasas (g)',
                `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY `idx_created_at` (`created_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            COMMENT='Consumos calculados (platillo + macros) por el Kilocalculator'
            """
        )
        conn.commit()
        print("✅ Tabla 'consumos' lista")

        cursor.execute("DESCRIBE `consumos`")
        print("\n📋 Estructura:")
        print("-" * 60)
        for col in cursor.fetchall():
            print(f"  {col[0]:<18} {col[1]:<28} {col[2]}")
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"❌ Error al crear la tabla: {e}")
        sys.exit(1)


if __name__ == "__main__":
    print("🔨 Inicializando base de datos…")
    print("-" * 60)
    init_database()
    print("\n✅ Lista para usar")
