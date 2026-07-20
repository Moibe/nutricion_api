"""
Prueba de concepto: migración del asistente "Kilocalculator" desde la
Assistants API (deprecada, sunset 26-ago-2026) hacia Responses API + Conversations API.

Arranca con:        uvicorn main:app --reload
Docs interactivas:  http://127.0.0.1:8000/docs
"""

import os
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from asistente import INSTRUCCIONES, MODELO, crear_cliente
from connection import actualizar_fecha_comida, crear_comida, guardar_consumo
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
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# --- Modelos de la API HTTP ---------------------------------------------------
class ChatRequest(BaseModel):
    mensaje: str
    # En el primer turno se omite; luego se reenvía el de la respuesta anterior
    # para mantener el hilo (equivale al thread_id de la Assistants API).
    conversation_id: Optional[str] = None


class ChatResponse(BaseModel):
    conversation_id: str
    respuesta: RespuestaKilocalculator


# --- Endpoints ----------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True}


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

    try:
        response = client.responses.parse(
            model=MODELO,
            conversation=conversation_id,
            instructions=INSTRUCCIONES,
            input=req.mensaje,
            text_format=RespuestaKilocalculator,
            temperature=1.0,
        )
    except Exception as exc:  # noqa: BLE001 — en PoC propagamos el detalle
        raise HTTPException(status_code=502, detail=f"Error de OpenAI: {exc}") from exc

    return ChatResponse(conversation_id=conversation_id, respuesta=response.output_parsed)


# --- Guardado manual del platillo final (botón "Guardar" del front) -----------
class ConsumoIn(BaseModel):
    conversation_id: str
    platillo: Optional[str] = None
    kilocalorias: Optional[float] = None
    proteinas: Optional[float] = None
    carbohidratos: Optional[float] = None
    grasas: Optional[float] = None


@app.post("/consumos")
def crear_consumo(consumo: ConsumoIn):
    """Upsert (por conversation_id) del platillo final que el usuario decidió guardar."""
    try:
        guardar_consumo(consumo.conversation_id, consumo)
    except Exception as exc:  # noqa: BLE001 — feedback de guardado al usuario
        raise HTTPException(status_code=503, detail=f"No se pudo guardar: {exc}") from exc
    return {"ok": True}


# --- Comidas: agrupan varios consumos (botones Desayuno/Colación/Comida/Cena) --
class ComidaIn(BaseModel):
    tipo: Literal["desayuno", "comida", "cena", "colacion"]


class FechaIn(BaseModel):
    fecha: str  # "YYYY-MM-DD"


@app.post("/comidas")
def crear_comida_endpoint(comida: ComidaIn):
    """Crea una instancia de comida (fecha = hoy en CDMX por default)."""
    try:
        return crear_comida(comida.tipo)
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
