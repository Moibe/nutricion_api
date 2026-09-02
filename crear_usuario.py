"""
Da de alta un usuario nuevo. No hay auto-registro: el dueño corre esto desde
el droplet y comparte el código de acceso por el canal que sea (WhatsApp,
etc). Requiere que migraciones.py ya haya corrido (la tabla usuarios debe
existir).

Uso:
    python crear_usuario.py "Nombre"
    python crear_usuario.py "Moibe" --id 1        # el dueño -- el id debe
                                                    # coincidir con el 1 que
                                                    # usó migraciones.py para
                                                    # el historial existente
    python crear_usuario.py "Mamá" --codigo mi-codigo-elegido
"""

import argparse
import secrets

from connection import get_connection


def crear_usuario(nombre: str, usuario_id: int | None = None, codigo: str | None = None) -> dict:
    codigo = codigo or secrets.token_urlsafe(8)
    conn = get_connection()
    try:
        if usuario_id is not None:
            conn.execute(
                "INSERT INTO usuarios (id, nombre, codigo_acceso) VALUES (?, ?, ?)",
                (usuario_id, nombre, codigo),
            )
        else:
            conn.execute(
                "INSERT INTO usuarios (nombre, codigo_acceso) VALUES (?, ?)",
                (nombre, codigo),
            )
        conn.commit()
        fila = conn.execute(
            "SELECT id, nombre, codigo_acceso FROM usuarios WHERE codigo_acceso = ?", (codigo,)
        ).fetchone()
        return {"id": fila[0], "nombre": fila[1], "codigo_acceso": fila[2]}
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Da de alta un usuario de Kcal.")
    parser.add_argument("nombre")
    parser.add_argument("--id", type=int, default=None, help="Solo para el usuario dueño (id=1)")
    parser.add_argument("--codigo", default=None, help="Código de acceso explícito (si no, se genera)")
    args = parser.parse_args()

    usuario = crear_usuario(args.nombre, usuario_id=args.id, codigo=args.codigo)
    print(f"Usuario creado: {usuario['nombre']} (id={usuario['id']})")
    print(f"Código de acceso: {usuario['codigo_acceso']}")
