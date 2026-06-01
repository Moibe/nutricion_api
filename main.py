"""
Prueba de concepto: migración del asistente "Kilocalculator" desde la
Assistants API (deprecada, sunset 26-ago-2026) hacia Responses API + Conversations API.

Arranca con:        uvicorn main:app --reload
Docs interactivas:  http://127.0.0.1:8000/docs
"""

from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from asistente import INSTRUCCIONES, MODELO, crear_cliente
from schema import RespuestaKilocalculator

client = crear_cliente()

app = FastAPI(title="Kilocalculator — Responses API PoC", version="0.0.1")


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
