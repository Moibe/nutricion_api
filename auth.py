"""
Autenticación multi-usuario.

Dos formas de llegar autenticado a la API (nunca directo desde el browser —
el browser le habla al proxy /api/* de SvelteKit, que valida la cookie de
sesión y reenvía con estos headers; ver Fase 4):

1. Proxy del front: X-Internal-Token (secreto compartido entre el proxy y
   esta API, prueba que la llamada viene de nuestro propio proxy y no de
   cualquiera en internet) + X-Usuario-Id (el usuario que el proxy ya
   resolvió a partir de su cookie) + X-Token-Version (opcional por ahora —
   Fase 4 empieza a mandarlo — para revocar sesiones individuales sin
   desactivar la cuenta entera).
2. Atajo de iOS: X-Api-Key = token_ios de un usuario concreto. No hay
   browser ni cookie de por medio, así que no aplica nada de lo anterior.

uid() expone el usuario_id de la request en curso vía ContextVar — así las
~19 funciones de connection.py lo pueden leer sin que cada llamada en
main.py se los tenga que pasar como parámetro.

GOTCHA que le da forma a este archivo: la dependency de FastAPI que hace
`_usuario_actual.set(...)` DEBE ser `async def`. La mayoría de los
endpoints de este proyecto son `def` (síncronos) y FastAPI los despacha en
un threadpool — Starlette/anyio SÍ copian el ContextVar actual hacia ese
hilo al invocar un `def`, pero solo porque quien lo seteó corrió en la
tarea async principal de la request. Si esta dependency también fuera
`def` (y por lo tanto despachada a su PROPIO hilo del pool), el set()
ocurriría en una copia de contexto distinta a la que luego se copia hacia
el endpoint, y el ContextVar se vería vacío ahí. Por la misma razón: nunca
usar BaseHTTPMiddleware para esto (corre en la tarea ASGI directamente, sin
forma limpia de que su estado llegue al thread del endpoint sync).
"""

import hmac
import os
from contextvars import ContextVar
from typing import Optional

from fastapi import Header, HTTPException

from connection import get_connection

INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "")

_usuario_actual: ContextVar[int] = ContextVar("usuario_actual")


def uid() -> int:
    """
    usuario_id de la request en curso. Lanza si se llama fuera de una
    request autenticada (fail-closed: nunca "0 filas silenciosas" por leer
    un ContextVar vacío como si fuera un usuario válido — un bug que
    llamara uid() en un contexto sin resolver debe tronar fuerte, no
    devolver datos de nadie o de todos).
    """
    try:
        return _usuario_actual.get()
    except LookupError as exc:
        raise RuntimeError("uid() llamado sin usuario resuelto en el contexto") from exc


async def resolver_usuario(
    x_internal_token: Optional[str] = Header(default=None),
    x_usuario_id: Optional[str] = Header(default=None),
    x_token_version: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> int:
    """
    Dependency global (ver main.py: router_protegido = APIRouter(dependencies=[...])).
    Resuelve el usuario de la request por UNA de dos vías y deja su id en el
    ContextVar para el resto de la request.
    """
    if x_api_key:
        usuario_id = _resolver_por_api_key(x_api_key)
    elif x_internal_token is not None:
        usuario_id = _resolver_por_proxy(x_internal_token, x_usuario_id, x_token_version)
    else:
        raise HTTPException(status_code=401, detail="No autenticado.")

    _usuario_actual.set(usuario_id)
    return usuario_id


def _resolver_por_api_key(x_api_key: str) -> int:
    conn = get_connection()
    try:
        fila = conn.execute(
            "SELECT id, activo FROM usuarios WHERE token_ios = ?", (x_api_key,)
        ).fetchone()
    finally:
        conn.close()
    if fila is None or not fila[1]:
        raise HTTPException(status_code=401, detail="X-Api-Key inválida.")
    return fila[0]


def _resolver_por_proxy(
    x_internal_token: str, x_usuario_id: Optional[str], x_token_version: Optional[str]
) -> int:
    # compare_digest en vez de == para no filtrar el secreto por timing.
    if not INTERNAL_TOKEN or not hmac.compare_digest(x_internal_token, INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="X-Internal-Token inválido.")
    if x_usuario_id is None:
        raise HTTPException(status_code=401, detail="Falta X-Usuario-Id.")
    try:
        candidato = int(x_usuario_id)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="X-Usuario-Id inválido.") from exc
    # Un entero válido pero fuera del rango de un INTEGER de 8 bytes de SQLite
    # (p. ej. un header manipulado a mano) truena en el SELECT de abajo con
    # OverflowError -- sin este chequeo eso se cuela como 500 en vez de 401.
    if not 0 < candidato < 2**63:
        raise HTTPException(status_code=401, detail="X-Usuario-Id inválido.")

    conn = get_connection()
    try:
        fila = conn.execute(
            "SELECT activo, token_version FROM usuarios WHERE id = ?", (candidato,)
        ).fetchone()
    finally:
        conn.close()
    if fila is None or not fila[0]:
        raise HTTPException(status_code=401, detail="Usuario inactivo o inexistente.")
    # Defensa en profundidad: el proxy YA debería haber checado esto contra
    # la cookie antes de reenviar, pero si un día tiene un bug, esta segunda
    # verificación (independiente, contra la DB) sigue cerrando la sesión
    # revocada. Opcional mientras Fase 4 no manda el header todavía.
    if x_token_version is not None and str(fila[1]) != x_token_version:
        raise HTTPException(status_code=401, detail="Sesión revocada.")
    return candidato
