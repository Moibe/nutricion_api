"""
Demo standalone (sin servidor) del flujo Responses API + Conversations API +
Structured Output, simulando una conversación con el asistente Kilocalculator.

Uso:   python demo.py

Hace 3 turnos automáticos para que veas cómo:
  - se crea UNA conversación y se reutiliza su id (reemplazo del Thread),
  - el asistente PREGUNTA mientras le falta info (requiere_mas_informacion=True),
  - y RESPONDE con kcal + macros estructurados cuando ya tiene suficiente.

OJO: este script SÍ hace llamadas reales a la API de OpenAI (tiene costo).
"""

import json

from asistente import INSTRUCCIONES, MODELO, crear_cliente
from schema import RespuestaKilocalculator

# Turnos simulados del usuario. El primero es vago a propósito para que el
# asistente tenga que preguntar; los siguientes aportan detalles.
TURNOS_USUARIO = [
    "Me comí unos tacos al pastor.",
    "Fueron 3 tacos, con tortilla de maíz, piña, cebolla y cilantro.",
    "Sí, con todo y una cucharada de salsa roja. Cada taco de tamaño normal.",
]


def imprimir_respuesta(turno: int, mensaje_usuario: str, r: RespuestaKilocalculator) -> None:
    print(f"\n{'='*70}\nTURNO {turno}")
    print(f"👤 Usuario: {mensaje_usuario}")
    if r.requiere_mas_informacion:
        print(f"🤖 Asistente PREGUNTA: {r.pregunta}")
    else:
        print(f"🤖 Asistente RESPONDE (platillo: {r.platillo}):")
        print(json.dumps(r.model_dump(), indent=2, ensure_ascii=False))


def main() -> None:
    client = crear_cliente()

    # 1) Crear la conversación una sola vez (reemplaza al Thread).
    conversation = client.conversations.create()
    print(f"Conversation creada: {conversation.id}")

    # 2) Recorrer los turnos reutilizando el conversation id.
    for i, mensaje in enumerate(TURNOS_USUARIO, start=1):
        response = client.responses.parse(
            model=MODELO,
            conversation=conversation.id,
            instructions=INSTRUCCIONES,
            input=mensaje,
            text_format=RespuestaKilocalculator,
            temperature=1.0,
        )
        imprimir_respuesta(i, mensaje, response.output_parsed)


if __name__ == "__main__":
    main()
