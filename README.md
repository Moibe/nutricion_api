# Kilocalculator — migración a Responses API

Prueba de concepto que migra el asistente **Kilocalculator** desde la
**Assistants API** de OpenAI (en beta, deprecada — *sunset* el **26 de agosto de 2026**)
hacia el reemplazo oficial: **Responses API** + **Conversations API**.

El asistente calcula kilocalorías y macronutrientes de un platillo, haciendo
preguntas de seguimiento hasta tener un cálculo preciso (contexto: Ciudad de México).
Devuelve **Structured Output** (JSON), igual que el `response_format: json_schema`
original.

## Mapeo de la migración

| Assistants API (antes) | Este proyecto (Responses + Conversations) |
| --- | --- |
| Objeto `Assistant` | `MODELO` + `INSTRUCCIONES` en [asistente.py](asistente.py) |
| `Thread` | **Conversation** (`client.conversations.create()`) |
| `Run` + polling | `client.responses.parse(...)` (síncrono) |
| `response_format: json_schema` | `text_format=RespuestaKilocalculator` ([schema.py](schema.py)) |

## Requisitos

- Python 3.11+
- Una API key de OpenAI

## Instalación

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows (PowerShell/cmd)
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt
```

## Configuración

```bash
copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux
```

Edita `.env` y coloca tu key en `OPENAI_API_KEY_WORK`.

## Probar

### Opción A — demo standalone (lo más rápido)

```bash
python demo.py
```

Corre una conversación de 3 turnos y muestra cómo el asistente primero pregunta
y luego entrega el cálculo estructurado.

### Opción B — la API FastAPI

```bash
uvicorn main:app --reload
```

Abre http://127.0.0.1:8000/docs y prueba `POST /chat`.

- **Primer turno** (sin `conversation_id`):

  ```json
  { "mensaje": "Me comí unos tacos al pastor." }
  ```

  La respuesta trae un `conversation_id`.

- **Turnos siguientes**: reenvía ese `conversation_id` para mantener el hilo:

  ```json
  { "mensaje": "Fueron 3 tacos con tortilla de maíz.", "conversation_id": "conv_..." }
  ```

## Archivos

| Archivo | Qué hace |
| --- | --- |
| [asistente.py](asistente.py) | Modelo, instrucciones y cliente de OpenAI (la "config" del asistente). |
| [schema.py](schema.py) | Schema Pydantic del Structured Output (reemplaza tu `macronutrientes_y_kcal`). |
| [main.py](main.py) | API FastAPI con `POST /chat` y `GET /health`. |
| [demo.py](demo.py) | Demo de conversación multi-turno sin servidor. |

## Pendiente / siguiente paso

El schema en [schema.py](schema.py) es una propuesta. Sustitúyelo por tu schema
real `macronutrientes_y_kcal` (lo puedes copiar desde el dashboard, ícono de
editar junto a *Response format*) para que el resultado coincida 1:1 con tu
asistente original.
