"""
Prueba de concepto: migración del asistente "Kilocalculator" desde la
Assistants API (deprecada, sunset 26-ago-2026) hacia Responses API + Conversations API.

Arranca con:        uvicorn main:app --reload
Docs interactivas:  http://127.0.0.1:8000/docs
"""

import os
from datetime import date
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ValidationInfo, field_validator

from asistente import INSTRUCCIONES, MODELO, crear_cliente
from connection import (
    actualizar_fecha_comida,
    crear_comida,
    eliminar_comida,
    eliminar_consumo,
    guardar_consumo,
    guardar_metrica_ios,
    listar_comidas,
    listar_metricas_ios,
    registrar_uso,
    resumen_uso,
)
from schema import RespuestaKilocalculator

client = crear_cliente()

app = FastAPI(title="Kilocalculator — Responses API PoC", version="0.0.1")


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
class ChatRequest(BaseModel):
    mensaje: str
    # En el primer turno se omite; luego se reenvía el de la respuesta anterior
    # para mantener el hilo (equivale al thread_id de la Assistants API).
    conversation_id: Optional[str] = None
    # Solo al EDITAR un consumo ya guardado: descripción del consumo actual
    # (platillo + macros). Se inyecta en el primer turno para que el asistente
    # sepa qué está editando aunque el hilo de OpenAI ya no tenga ese contexto.
    contexto: Optional[str] = None


class ChatResponse(BaseModel):
    conversation_id: str
    respuesta: RespuestaKilocalculator


# Precios de gpt-4.1 por 1M de tokens (USD). Configurables por env por si
# cambian o se usa otro modelo. El costo del monitor es una ESTIMACIÓN con estos.
PRECIO_INPUT_USD_POR_1M = float(os.getenv("PRECIO_INPUT_USD_POR_1M", "2.0"))
PRECIO_OUTPUT_USD_POR_1M = float(os.getenv("PRECIO_OUTPUT_USD_POR_1M", "8.0"))


# --- Endpoints ----------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/uso")
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


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """
    Un turno de conversación.

    1) Si no hay conversation_id, crea una Conversation (reemplazo del Thread).
    2) Llama a responses.parse con el modelo, las instrucciones, el mensaje del
       usuario y el schema estructurado. Al pasar `conversation`, OpenAI guarda
       e incluye automáticamente el historial — no hay que reenviar mensajes.
    """
    conversation_id = req.conversation_id
    if conversation_id is None:
        conversation = client.conversations.create()
        conversation_id = conversation.id

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

    try:
        response = client.responses.parse(
            model=MODELO,
            conversation=conversation_id,
            instructions=INSTRUCCIONES,
            input=entrada,
            text_format=RespuestaKilocalculator,
            temperature=1.0,
        )
    except Exception as exc:  # noqa: BLE001 — en PoC propagamos el detalle
        raise HTTPException(status_code=502, detail=f"Error de OpenAI: {exc}") from exc

    # Registrar uso de tokens (monitor de gasto). Best-effort: si falla, no
    # rompemos la respuesta del chat.
    try:
        usage = getattr(response, "usage", None)
        if usage is not None:
            registrar_uso(
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


@app.post("/consumos")
def crear_consumo(consumo: ConsumoIn):
    """Upsert (por conversation_id) del platillo final que el usuario decidió guardar."""
    try:
        return guardar_consumo(consumo.conversation_id, consumo)
    except Exception as exc:  # noqa: BLE001 — feedback de guardado al usuario
        raise HTTPException(status_code=503, detail=f"No se pudo guardar: {exc}") from exc


@app.delete("/consumos/{consumo_id}")
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


@app.get("/comidas")
def listar_comidas_endpoint():
    """Lista las comidas con al menos un consumo guardado, con sus consumos anidados."""
    try:
        return listar_comidas()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo listar: {exc}") from exc


@app.post("/comidas")
def crear_comida_endpoint(comida: ComidaIn):
    """Crea una instancia de comida (fecha = hoy en CDMX por default, o la que se mande)."""
    try:
        return crear_comida(comida.tipo, comida.orden, comida.fecha)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo crear la comida: {exc}") from exc


@app.patch("/comidas/{comida_id}")
def actualizar_fecha_comida_endpoint(comida_id: int, body: FechaIn):
    """Cambia la fecha de una comida (botón de calendario del front)."""
    try:
        return actualizar_fecha_comida(comida_id, body.fecha)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo actualizar: {exc}") from exc


@app.delete("/comidas/{comida_id}")
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


@app.get("/metricas-ios")
def listar_metricas_ios_endpoint():
    """Todas las filas guardadas; el front filtra por tipo y por día como con /comidas."""
    try:
        return listar_metricas_ios()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo listar: {exc}") from exc


@app.post("/metricas-ios")
def guardar_metrica_ios_endpoint(body: MetricaIosIn):
    """Upsert por (fecha, tipo) — lo que mande el Atajo de iOS (calorías quemadas, peso, ...)."""
    try:
        return guardar_metrica_ios(body.tipo, body.fecha, body.valor, body.fuente)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo guardar: {exc}") from exc
