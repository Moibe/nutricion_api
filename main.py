"""
Prueba de concepto: migración del asistente "Kilocalculator" desde la
Assistants API (deprecada, sunset 26-ago-2026) hacia Responses API + Conversations API.

Arranca con:        uvicorn main:app --reload
Docs interactivas:  http://127.0.0.1:8000/docs
"""

import os
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal, Optional

import hmac

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ValidationInfo, field_validator

from asistente import INSTRUCCIONES, MODELO, crear_cliente
from auth import INTERNAL_TOKEN, requerir_admin, resolver_usuario, uid
from connection import (
    actualizar_activo,
    actualizar_fecha_comida,
    asegurar_schema,
    completar_cupo_ia,
    crear_comida,
    crear_conversacion_si_falta,
    crear_ejercicio,
    crear_favorito,
    crear_usuario_admin,
    eliminar_comida,
    eliminar_consumo,
    eliminar_ejercicio,
    eliminar_favorito,
    get_connection,
    guardar_consumo,
    guardar_metrica_ios,
    guardar_perfil,
    hoy_cdmx,
    listar_comidas,
    listar_ejercicios,
    listar_favoritos,
    listar_metricas_ios,
    listar_usuarios,
    obtener_perfil,
    obtener_usuario_de_conversacion,
    regenerar_codigo,
    reservar_cupo_ia,
    resumen_uso,
    revocar_sesiones,
)
from schema import RespuestaKilocalculator

client = crear_cliente()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Antes esto corría dentro de get_connection(), o sea en CADA request —
    # ~15 CREATE TABLE IF NOT EXISTS + varios PRAGMA table_info de más por
    # llamada a la API. Ahora corre una sola vez, al arrancar.
    asegurar_schema()
    yield


app = FastAPI(title="Kilocalculator — Responses API PoC", version="0.0.1", lifespan=lifespan)

# Todo lo que necesita saber DE QUIÉN son los datos vive acá, no en `app`
# directo -- así un endpoint nuevo nace protegido por default con solo
# agregarse a este router, sin tener que acordarse de poner
# Depends(resolver_usuario) uno por uno. Lo único fuera de este router es
# /health (lo pegan GitHub Actions/monitoring, sin sesión) y /auth/login
# (todavía no hay usuario que resolver -- es COMO se consigue uno).
router_protegido = APIRouter(dependencies=[Depends(resolver_usuario)])


# --- CORS ---------------------------------------------------------------------
# El frontend (SvelteKit + Vite) corre en el navegador y pega directo a esta API
# cross-origin. localhost y 127.0.0.1 son orígenes distintos para el browser, y
# Vite puede subir de puerto si 5173 está ocupado — por eso la lista es
# configurable por env var (coma-separada). En prod se agrega el dominio sin
# tocar código: CORS_ORIGINS="https://mi-front.com".
_default_origins = "http://localhost:5173,http://127.0.0.1:5173"
origins = [o.strip() for o in os.getenv("CORS_ORIGINS", _default_origins).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,  # el front no manda cookies ni Authorization
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# --- Modelos de la API HTTP ---------------------------------------------------
# Tope al tamaño del data URI (base64) de una foto — generoso para una foto de
# celular ya comprimida por el navegador, pero evita payloads descomunales.
TOPE_IMAGEN_BASE64_CHARS = 12_000_000  # ~9 MB decodificados


class ChatRequest(BaseModel):
    mensaje: str = ""
    # En el primer turno se omite; luego se reenvía el de la respuesta anterior
    # para mantener el hilo (equivale al thread_id de la Assistants API).
    conversation_id: Optional[str] = None
    # Solo al EDITAR un consumo ya guardado: descripción del consumo actual
    # (platillo + macros). Se inyecta en el primer turno para que el asistente
    # sepa qué está editando aunque el hilo de OpenAI ya no tenga ese contexto.
    contexto: Optional[str] = None
    # Foto del platillo, como data URI (data:image/jpeg;base64,...) — opcional,
    # se puede mandar sola o junto con mensaje.
    imagen_base64: Optional[str] = None

    @field_validator("imagen_base64")
    @classmethod
    def _imagen_valida(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.startswith("data:image/"):
            raise ValueError("imagen_base64 debe ser un data URI (data:image/...)")
        if len(v) > TOPE_IMAGEN_BASE64_CHARS:
            raise ValueError("Imagen demasiado grande")
        return v


class ChatResponse(BaseModel):
    conversation_id: str
    respuesta: RespuestaKilocalculator


# Precios de gpt-4.1 por 1M de tokens (USD). Configurables por env por si
# cambian o se usa otro modelo. El costo del monitor es una ESTIMACIÓN con estos.
PRECIO_INPUT_USD_POR_1M = float(os.getenv("PRECIO_INPUT_USD_POR_1M", "2.0"))
PRECIO_OUTPUT_USD_POR_1M = float(os.getenv("PRECIO_OUTPUT_USD_POR_1M", "8.0"))

# Tope de tokens/día por usuario en /chat — para que una sola cuenta (bug de
# cliente en loop, o alguien de mala fe) no se vuelva un problema de factura
# cuando ya no hay un solo dueño vigilando el monitor de gasto.
TOPE_TOKENS_DIA_USUARIO = int(os.getenv("TOPE_TOKENS_DIA_USUARIO", "200000"))


# --- Endpoints ----------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True}


@router_protegido.get("/uso")
def uso():
    """Monitor de gasto: tokens usados (total, mes y hoy) + costo estimado en USD."""
    try:
        datos = resumen_uso()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo leer el uso: {exc}") from exc

    def con_costo(bloque: dict) -> dict:
        costo = (
            bloque["input_tokens"] / 1_000_000 * PRECIO_INPUT_USD_POR_1M
            + bloque["output_tokens"] / 1_000_000 * PRECIO_OUTPUT_USD_POR_1M
        )
        return {**bloque, "costo_usd": round(costo, 4)}

    return {
        "modelo": MODELO,
        "precio_input_usd_por_1m": PRECIO_INPUT_USD_POR_1M,
        "precio_output_usd_por_1m": PRECIO_OUTPUT_USD_POR_1M,
        "total": con_costo(datos["total"]),
        "mes": con_costo(datos["mes"]),
        "hoy": con_costo(datos["hoy"]),
    }


@router_protegido.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """
    Un turno de conversación.

    1) Si viene conversation_id, DEBE ser del usuario en curso — si no, 404
       (mismo patrón que ya usa el resto de la API para "tocar lo ajeno": ni
       siquiera se distingue "existe pero no es tuya" de "no existe"). Sin
       esto, cualquiera podría mandar el conversation_id de otra persona y
       seguir leyendo/escribiendo su conversación.
    2) Si no hay conversation_id, crea una Conversation nueva y la registra
       como propia ANTES de llamar a OpenAI — si la llamada de abajo falla,
       un reintento con ese mismo id ya la encuentra bien atribuida.
    3) Tope diario de tokens por usuario (ver TOPE_TOKENS_DIA_USUARIO).
    4) Llama a responses.parse con el modelo, las instrucciones, el mensaje del
       usuario y el schema estructurado. Al pasar `conversation`, OpenAI guarda
       e incluye automáticamente el historial — no hay que reenviar mensajes.
    """
    if not req.mensaje.strip() and not req.imagen_base64:
        raise HTTPException(status_code=422, detail="Falta mensaje o imagen.")

    usuario_id = uid()

    conversation_id = req.conversation_id
    if conversation_id is not None:
        dueño = obtener_usuario_de_conversacion(conversation_id)
        if dueño != usuario_id:
            raise HTTPException(status_code=404, detail="Conversación no encontrada.")
    else:
        conversation = client.conversations.create()
        conversation_id = conversation.id
        crear_conversacion_si_falta(conversation_id, usuario_id)

    # Reserva ANTES de llamar a OpenAI (no "cuenta el uso y luego revisa"):
    # ver reservar_cupo_ia para el porqué (check-then-act con una llamada
    # lenta en medio es una carrera real entre requests concurrentes).
    reserva_id = reservar_cupo_ia(usuario_id, TOPE_TOKENS_DIA_USUARIO)
    if reserva_id is None:
        raise HTTPException(
            status_code=429,
            detail=f"Ya usaste tu tope de tokens de hoy ({TOPE_TOKENS_DIA_USUARIO}).",
        )

    entrada = req.mensaje
    if req.contexto:
        entrada = (
            "El usuario está EDITANDO un consumo que ya había calculado antes:\n"
            f"{req.contexto}\n\n"
            f"Su indicación para modificarlo es: {req.mensaje}\n\n"
            "Recalcula el platillo completo tomando en cuenta esta modificación. "
            "Si necesitas más datos para el nuevo cálculo, pregunta; si no, "
            "entrega el resultado final actualizado."
        )

    # Con foto: input multimodal (Responses API) — texto opcional + imagen.
    # Sin foto: se manda el string plano de siempre.
    entrada_final: object = entrada
    if req.imagen_base64:
        contenido: list[dict] = []
        if entrada.strip():
            contenido.append({"type": "input_text", "text": entrada})
        contenido.append(
            {"type": "input_image", "image_url": req.imagen_base64, "detail": "auto"}
        )
        entrada_final = [{"role": "user", "content": contenido}]

    try:
        response = client.responses.parse(
            model=MODELO,
            conversation=conversation_id,
            instructions=INSTRUCCIONES,
            input=entrada_final,
            text_format=RespuestaKilocalculator,
            temperature=1.0,
        )
    except Exception as exc:  # noqa: BLE001 — en PoC propagamos el detalle
        raise HTTPException(status_code=502, detail=f"Error de OpenAI: {exc}") from exc

    # Corrige la reserva (RESERVA_TOKENS_ESTIMADA) con el gasto REAL. Si esto
    # falla, la reserva se queda contando tal cual para el tope diario — a
    # propósito: un "no se pudo actualizar" nunca debe dejar la cuota en
    # blanco (ver completar_cupo_ia).
    try:
        usage = getattr(response, "usage", None)
        if usage is not None:
            completar_cupo_ia(
                reserva_id,
                conversation_id,
                MODELO,
                getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0,
            )
    except Exception:  # noqa: BLE001 — el conteo de tokens nunca debe tumbar el chat
        pass

    return ChatResponse(conversation_id=conversation_id, respuesta=response.output_parsed)


# --- Guardado manual del platillo final (botón "Guardar" del front) -----------
class ConsumoIn(BaseModel):
    conversation_id: str
    comida_id: Optional[int] = None
    platillo: Optional[str] = None
    kilocalorias: Optional[float] = None
    proteinas: Optional[float] = None
    carbohidratos: Optional[float] = None
    grasas: Optional[float] = None


@router_protegido.post("/consumos")
def crear_consumo(consumo: ConsumoIn):
    """Upsert (por conversation_id) del platillo final que el usuario decidió guardar."""
    try:
        return guardar_consumo(consumo.conversation_id, consumo)
    except ValueError as exc:
        # comida_id que no existe, o que existe pero es de otro usuario.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — feedback de guardado al usuario
        raise HTTPException(status_code=503, detail=f"No se pudo guardar: {exc}") from exc


@router_protegido.delete("/consumos/{consumo_id}")
def eliminar_consumo_endpoint(consumo_id: int):
    """Borra un consumo (botón de eliminar del Listado)."""
    try:
        eliminar_consumo(consumo_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo eliminar: {exc}") from exc
    return {"ok": True}


# --- Comidas: agrupan varios consumos (botones Desayuno/Colación/Comida/Cena) --
def _validar_fecha_iso(v: str) -> str:
    """
    "YYYY-MM-DD" real (rechaza vacío, formato suelto tipo "2026-8-9" y fechas
    imposibles tipo "2026-99-99"). Sin esto, un `fecha` mal formado se guarda
    tal cual: nunca se parsea como fecha real en ningún otro lado del código
    (solo comparaciones/orden de string), así que no truena nada de inmediato,
    pero la fila queda "huérfana" — Calendario arma sus días desde un
    calendario real, no desde `comidas.fecha`, así que esa fila nunca tendría
    punto ni sería alcanzable dando clic en ningún día.
    """
    try:
        date.fromisoformat(v)
    except (TypeError, ValueError) as exc:
        raise ValueError('fecha debe tener formato "YYYY-MM-DD" y ser una fecha real') from exc
    return v


class ComidaIn(BaseModel):
    tipo: Literal["desayuno", "comida", "cena", "colacion"]
    # Posición en la secuencia del día (Desayuno=0, Colación 1=1, Comida=2,
    # Colación 2=3, Cena=4) — la manda el front según el botón que se picó.
    # Separado de `tipo` porque las dos colaciones comparten tipo.
    orden: int = 0
    # Fecha explícita "YYYY-MM-DD" (botones de /calendario, para crear en el
    # día elegido en vez de hoy). Si se omite (o se manda null), se crea con
    # fecha de hoy en CDMX (botones de /hoy).
    fecha: Optional[str] = None

    @field_validator("fecha")
    @classmethod
    def _fecha_valida(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _validar_fecha_iso(v)


class FechaIn(BaseModel):
    fecha: str  # "YYYY-MM-DD"

    @field_validator("fecha")
    @classmethod
    def _fecha_valida(cls, v: str) -> str:
        return _validar_fecha_iso(v)


@router_protegido.get("/comidas")
def listar_comidas_endpoint(desde: Optional[str] = None, hasta: Optional[str] = None):
    """
    Lista las comidas con al menos un consumo guardado, con sus consumos
    anidados. desde/hasta ("YYYY-MM-DD", opcionales): acotan por rango de
    fecha inclusivo — los usa /registro-diario para pedir un mes a la vez.
    """
    try:
        if desde is not None:
            desde = _validar_fecha_iso(desde)
        if hasta is not None:
            hasta = _validar_fecha_iso(hasta)
        return listar_comidas(desde, hasta)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo listar: {exc}") from exc


@router_protegido.post("/comidas")
def crear_comida_endpoint(comida: ComidaIn):
    """Crea una instancia de comida (fecha = hoy en CDMX por default, o la que se mande)."""
    try:
        return crear_comida(comida.tipo, comida.orden, comida.fecha)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo crear la comida: {exc}") from exc


@router_protegido.patch("/comidas/{comida_id}")
def actualizar_fecha_comida_endpoint(comida_id: int, body: FechaIn):
    """Cambia la fecha de una comida (botón de calendario del front)."""
    try:
        return actualizar_fecha_comida(comida_id, body.fecha)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo actualizar: {exc}") from exc


@router_protegido.delete("/comidas/{comida_id}")
def eliminar_comida_endpoint(comida_id: int):
    """Borra una comida completa y todos sus consumos (ícono de bote en la tarjeta)."""
    try:
        eliminar_comida(comida_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo eliminar: {exc}") from exc
    return {"ok": True}


# --- Métricas de iOS: un "cachador" genérico para lo que mande el Atajo -------
# (calorías quemadas, peso, lo que se agregue después). Un valor por
# (fecha, tipo); el tipo implica la unidad (kcal para calorias_quemadas, kg
# para peso) — no se guarda unidad aparte porque cada tipo tiene una sola.
# Tope superior generoso pero real por tipo: un Atajo mal armado (unidad
# equivocada, automatización duplicada) no debe poder guardar un número
# absurdo que luego se muestra tal cual en el front sin ningún otro filtro.
TOPES_METRICA_IOS = {"calorias_quemadas": 20_000.0, "peso": 300.0}


class MetricaIosIn(BaseModel):
    tipo: Literal["calorias_quemadas", "peso"]
    fecha: str  # "YYYY-MM-DD"
    valor: float
    fuente: str = "atajo_ios"
    # Descripción libre ("Correr 5km", "Pesas") — opcional porque el Atajo de
    # iOS nunca la manda, solo la captura manual de /ejercicio.
    concepto: Optional[str] = None

    @field_validator("fecha")
    @classmethod
    def _fecha_valida(cls, v: str) -> str:
        return _validar_fecha_iso(v)

    @field_validator("valor")
    @classmethod
    def _valor_valido(cls, v: float, info: ValidationInfo) -> float:
        if v < 0:
            raise ValueError("valor no puede ser negativo")
        tope = TOPES_METRICA_IOS.get(info.data.get("tipo"))
        if tope is not None and v > tope:
            raise ValueError(f"valor fuera de rango razonable (máximo {tope})")
        return v


@router_protegido.get("/metricas-ios")
def listar_metricas_ios_endpoint(desde: Optional[str] = None, hasta: Optional[str] = None):
    """
    Todas las filas guardadas; el front filtra por tipo y por día como con
    /comidas. desde/hasta ("YYYY-MM-DD", opcionales): mismo acotado por rango
    inclusivo que /comidas, para /registro-diario.
    """
    try:
        if desde is not None:
            desde = _validar_fecha_iso(desde)
        if hasta is not None:
            hasta = _validar_fecha_iso(hasta)
        return listar_metricas_ios(desde, hasta)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo listar: {exc}") from exc


@router_protegido.post("/metricas-ios")
def guardar_metrica_ios_endpoint(body: MetricaIosIn):
    """Upsert por (fecha, tipo) — lo que mande el Atajo de iOS (calorías quemadas, peso, ...)."""
    try:
        return guardar_metrica_ios(body.tipo, body.fecha, body.valor, body.fuente, body.concepto)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo guardar: {exc}") from exc


# --- Ejercicio manual: bitácora (varias entradas por día, no un solo valor) --
# A diferencia de metricas_ios (upsert, un valor por fecha+tipo, exclusivo del
# Atajo de iOS), cada "Guardar" de /ejercicio agrega una fila nueva — mismo
# espíritu que comidas/consumos.
TOPE_KCAL_EJERCICIO = 20_000.0


class EjercicioIn(BaseModel):
    fecha: str  # "YYYY-MM-DD"
    concepto: str
    kilocalorias: float

    @field_validator("fecha")
    @classmethod
    def _fecha_valida(cls, v: str) -> str:
        return _validar_fecha_iso(v)

    @field_validator("concepto")
    @classmethod
    def _concepto_valido(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("concepto no puede estar vacío")
        return v

    @field_validator("kilocalorias")
    @classmethod
    def _kilocalorias_validas(cls, v: float) -> float:
        if v < 0:
            raise ValueError("kilocalorias no puede ser negativo")
        if v > TOPE_KCAL_EJERCICIO:
            raise ValueError(f"kilocalorias fuera de rango razonable (máximo {TOPE_KCAL_EJERCICIO})")
        return v


@router_protegido.get("/ejercicios")
def listar_ejercicios_endpoint(desde: Optional[str] = None, hasta: Optional[str] = None):
    """
    Lista la bitácora de ejercicio manual. desde/hasta ("YYYY-MM-DD",
    opcionales): mismo acotado por rango inclusivo que /comidas, para
    /registro-diario.
    """
    try:
        if desde is not None:
            desde = _validar_fecha_iso(desde)
        if hasta is not None:
            hasta = _validar_fecha_iso(hasta)
        return listar_ejercicios(desde, hasta)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo listar: {exc}") from exc


@router_protegido.post("/ejercicios")
def crear_ejercicio_endpoint(ejercicio: EjercicioIn):
    """Agrega una entrada a la bitácora de ejercicio (botón Guardar de /ejercicio)."""
    try:
        return crear_ejercicio(ejercicio.fecha, ejercicio.concepto, ejercicio.kilocalorias)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo guardar: {exc}") from exc


@router_protegido.delete("/ejercicios/{ejercicio_id}")
def eliminar_ejercicio_endpoint(ejercicio_id: int):
    """Borra una entrada de la bitácora de ejercicio."""
    try:
        eliminar_ejercicio(ejercicio_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo eliminar: {exc}") from exc
    return {"ok": True}


# --- Favoritos: platillos ya calculados por la IA que el usuario guarda -------
# para reusar con un tap (POST directo a /consumos, sin pasar por /chat), sin
# volver a describirlos ni gastar otra llamada al modelo.
class FavoritoIn(BaseModel):
    nombre: str
    kilocalorias: Optional[float] = None
    proteinas: Optional[float] = None
    carbohidratos: Optional[float] = None
    grasas: Optional[float] = None

    @field_validator("nombre")
    @classmethod
    def _nombre_valido(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("nombre no puede estar vacío")
        return v


@router_protegido.get("/favoritos")
def listar_favoritos_endpoint():
    """Lista de platillos guardados para reuso rápido en el chat."""
    try:
        return listar_favoritos()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo listar: {exc}") from exc


@router_protegido.post("/favoritos")
def crear_favorito_endpoint(favorito: FavoritoIn):
    """Guarda un platillo (botón "Guardar como frecuente" del chat)."""
    try:
        return crear_favorito(
            favorito.nombre, favorito.kilocalorias, favorito.proteinas, favorito.carbohidratos, favorito.grasas
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo guardar: {exc}") from exc


@router_protegido.delete("/favoritos/{favorito_id}")
def eliminar_favorito_endpoint(favorito_id: int):
    """Borra un favorito de la lista rápida."""
    try:
        eliminar_favorito(favorito_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo eliminar: {exc}") from exc
    return {"ok": True}


# --- Perfil: fecha de nacimiento/estatura/sexo, para calcular metabolismo ----
# basal (Mifflin-St Jeor) en el front junto al peso del día. Una sola fila
# (app de un solo usuario) — fecha_nacimiento en vez de "edad" porque la edad
# cambia con el tiempo; se calcula al vuelo con la fecha de hoy.
class PerfilIn(BaseModel):
    fecha_nacimiento: str  # "YYYY-MM-DD"
    estatura_cm: float
    sexo: Literal["hombre", "mujer"]

    @field_validator("fecha_nacimiento")
    @classmethod
    def _fecha_nacimiento_valida(cls, v: str) -> str:
        v = _validar_fecha_iso(v)
        if v > hoy_cdmx():
            raise ValueError("fecha_nacimiento no puede ser en el futuro")
        return v

    @field_validator("estatura_cm")
    @classmethod
    def _estatura_valida(cls, v: float) -> float:
        if not (50 <= v <= 250):
            raise ValueError("estatura_cm fuera de rango razonable (50-250)")
        return v


@router_protegido.get("/perfil")
def obtener_perfil_endpoint():
    """None si todavía no se ha capturado (primera vez que se usa la app)."""
    try:
        return obtener_perfil()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo leer el perfil: {exc}") from exc


@router_protegido.post("/perfil")
def guardar_perfil_endpoint(body: PerfilIn):
    """Upsert del perfil (fecha_nacimiento/estatura/sexo)."""
    try:
        return guardar_perfil(body.fecha_nacimiento, body.estatura_cm, body.sexo)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo guardar: {exc}") from exc


# --- Auth ----------------------------------------------------------------------
class LoginIn(BaseModel):
    codigo_acceso: str


@app.post("/auth/login")
def login(body: LoginIn, x_internal_token: Optional[str] = Header(default=None)):
    """
    Canjea un código de acceso por el usuario correspondiente. No requiere
    sesión previa (es justo cómo se consigue una) pero SÍ requiere
    X-Internal-Token -- solo el proxy del front (Fase 4) puede llamarlo,
    nunca alguien pegándole directo a la API por internet a fuerza bruta
    de códigos.
    """
    if not INTERNAL_TOKEN or not hmac.compare_digest(x_internal_token or "", INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="No autorizado.")

    conn = get_connection()
    try:
        fila = conn.execute(
            "SELECT id, nombre, token_version FROM usuarios WHERE codigo_acceso = ? AND activo = 1",
            (body.codigo_acceso,),
        ).fetchone()
    finally:
        conn.close()

    if fila is None:
        raise HTTPException(status_code=401, detail="Código de acceso inválido.")

    return {"id": fila[0], "nombre": fila[1], "token_version": fila[2]}


@router_protegido.get("/auth/yo")
def yo():
    """Quién es el usuario de la sesión en curso (para pintar su nombre en el front)."""
    conn = get_connection()
    try:
        fila = conn.execute("SELECT id, nombre FROM usuarios WHERE id = ?", (uid(),)).fetchone()
    finally:
        conn.close()
    return {"id": fila[0], "nombre": fila[1]}


# --- Admin: gestión de usuarios (solo el dueño, ver auth.requerir_admin) -------
class UsuarioAdminIn(BaseModel):
    nombre: str

    @field_validator("nombre")
    @classmethod
    def _nombre_valido(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("nombre no puede estar vacío")
        return v


class ActivoIn(BaseModel):
    activo: bool


@router_protegido.get("/admin/usuarios", dependencies=[Depends(requerir_admin)])
def listar_usuarios_endpoint():
    return listar_usuarios()


@router_protegido.post("/admin/usuarios", dependencies=[Depends(requerir_admin)])
def crear_usuario_endpoint(body: UsuarioAdminIn):
    """Da de alta un usuario y regresa su código de acceso -- se muestra UNA
    vez en el front, el back nunca lo vuelve a exponer (listar_usuarios no lo trae)."""
    try:
        return crear_usuario_admin(body.nombre)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo crear: {exc}") from exc


@router_protegido.patch("/admin/usuarios/{usuario_id}/activo", dependencies=[Depends(requerir_admin)])
def actualizar_activo_endpoint(usuario_id: int, body: ActivoIn):
    try:
        actualizar_activo(usuario_id, body.activo)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router_protegido.post(
    "/admin/usuarios/{usuario_id}/regenerar-codigo", dependencies=[Depends(requerir_admin)]
)
def regenerar_codigo_endpoint(usuario_id: int):
    try:
        codigo = regenerar_codigo(usuario_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"codigo_acceso": codigo}


@router_protegido.post("/admin/usuarios/{usuario_id}/revocar", dependencies=[Depends(requerir_admin)])
def revocar_sesiones_endpoint(usuario_id: int):
    try:
        revocar_sesiones(usuario_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


# Se registra AL FINAL, ya con todas las rutas de arriba acumuladas en el router.
app.include_router(router_protegido)
