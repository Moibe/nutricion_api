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

El usuario a veces te manda una FOTO del platillo en vez de (o junto con) una \
descripción escrita. Analiza la imagen para identificar el platillo y estima su \
tamaño de porción usando referencias visuales del plato/mesa/mano si aparecen. Si \
el texto que acompaña la foto ya aclara cantidad o ingredientes, úsalo como fuente \
de verdad sobre lo que se ve. Solo pregunta si, después de ver la imagen, sigue \
faltando un dato clave que no puedes estimar razonablemente (p. ej. el tamaño de \
la porción no se alcanza a distinguir) — no preguntes por detalles que ya \
puedes inferir de la imagen.
"""

# Mismo espíritu que INSTRUCCIONES, pero para estimar kilocalorías QUEMADAS por
# una actividad física en vez de consumidas por un platillo.
INSTRUCCIONES_EJERCICIO = """\
Éste asistente ayuda a estimar cuántas kilocalorías se queman al hacer determinada \
actividad física. Para un cálculo más preciso, el asistente hará cuántas preguntas \
sean necesarias (duración, intensidad/ritmo, distancia, terreno, peso corporal si es \
relevante para el cálculo, etc.) antes de dar el total. El contexto es que estás en \
Ciudad de México.

Reglas de formato de respuesta:
- Si todavía necesitas más información para calcular con precisión, responde con \
requiere_mas_informacion=true y escribe tu duda en el campo "pregunta". Deja \
"concepto" y "kilocalorias" en null.
- Cuando ya tengas suficiente información, responde con requiere_mas_informacion=false, \
identifica el "concepto" (nombre/descripción breve, p. ej. "Correr 5km" o "45 min de \
pesas") y entrega el total estimado de kilocalorías quemadas en "kilocalorias".

El usuario a veces te manda una FOTO en vez de (o junto con) una descripción escrita \
— por ejemplo la pantalla de una caminadora/bici fija, un reloj deportivo, o el \
resumen de una app de ejercicio. Analiza la imagen para extraer los datos relevantes \
(duración, distancia, ritmo, o las kcal si la pantalla ya las muestra). Si el texto \
que acompaña la foto ya aclara datos, úsalo como fuente de verdad sobre lo que se ve. \
Solo pregunta si, después de ver la imagen, sigue faltando un dato clave que no \
puedes estimar razonablemente — no preguntes por detalles que ya puedes inferir de \
la imagen.
"""


def crear_cliente() -> OpenAI:
    """
    Crea el cliente de OpenAI. Hay dos keys posibles en .env -- la de CSI y la
    personal -- y OPENAI_API_KEY_ACTIVA ("csi" | "propia") decide cuál usar en
    este momento, para poder cambiar de cuál cuenta se factura sin borrar y
    volver a pegar keys.

    Retrocompatible con el .env viejo (una sola OPENAI_API_KEY_WORK): si
    OPENAI_API_KEY_ACTIVA no está configurada, cae a esa -- así este cambio no
    rompe un .env que todavía no se actualizó con las dos keys nuevas.
    """
    activa = os.getenv("OPENAI_API_KEY_ACTIVA", "").strip().lower()

    if not activa:
        api_key = os.getenv("OPENAI_API_KEY_WORK")
        if api_key:
            return OpenAI(api_key=api_key)
        raise RuntimeError(
            "Falta configurar la key de OpenAI. Define OPENAI_API_KEY_CSI y "
            "OPENAI_API_KEY_PROPIA en .env, y OPENAI_API_KEY_ACTIVA=csi|propia "
            "para elegir cuál se usa."
        )

    variable_por_activa = {"csi": "OPENAI_API_KEY_CSI", "propia": "OPENAI_API_KEY_PROPIA"}
    variable = variable_por_activa.get(activa)
    if variable is None:
        raise RuntimeError(
            f'OPENAI_API_KEY_ACTIVA="{activa}" no es válido -- usa "csi" o "propia".'
        )

    api_key = os.getenv(variable)
    if not api_key:
        raise RuntimeError(f'Falta {variable} en .env (OPENAI_API_KEY_ACTIVA="{activa}").')
    return OpenAI(api_key=api_key)
