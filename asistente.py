"""
Configuración del asistente Kilocalculator, compartida por la API (main.py) y
el script de demostración (demo.py).

Aquí vive lo que antes era el objeto "Assistant" en la Assistants API:
el modelo y las instrucciones de sistema.
"""

import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# Mismo modelo que tenías configurado en el dashboard.
MODELO = "gpt-4.1"

# Instrucciones tomadas tal cual de tu asistente (System instructions), más una
# nota sobre cómo usar el schema estructurado (cuándo preguntar vs. responder).
INSTRUCCIONES = """\
Éste asistente ayuda a definir cuántas kilocalorías y macronutrientes aportó \
determinado platillo que consumiste. Para tener un cálculo más preciso, el \
asistente hará cuántas preguntas sean necesarias para obtener la definición \
final. El contexto es que estás en Ciudad de México.

Reglas de formato de respuesta:
- Si todavía necesitas más información para calcular con precisión, responde con \
requiere_mas_informacion=true y escribe tu duda en el campo "pregunta". Deja \
"platillo" y los macros (kilocalorias, proteinas, carbohidratos, grasas) en null.
- Cuando ya tengas suficiente información, responde con requiere_mas_informacion=false, \
identifica el "platillo" (nombre/descripción breve) y entrega los totales del platillo \
completo en "kilocalorias" (kcal) y "proteinas", "carbohidratos", "grasas" (en gramos). \
No desgloses por ingrediente.
"""


def crear_cliente() -> OpenAI:
    """Crea el cliente de OpenAI usando la key OPENAI_API_KEY_WORK del .env."""
    api_key = os.getenv("OPENAI_API_KEY_WORK")
    if not api_key:
        raise RuntimeError(
            "Falta OPENAI_API_KEY_WORK. Copia .env.example a .env y coloca tu key."
        )
    return OpenAI(api_key=api_key)
