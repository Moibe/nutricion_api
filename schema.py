"""
Schema de Structured Output para el asistente Kilocalculator.

Reproduce el `response_format: json_schema` ("macronutrientes_y_kcal") que tenías
en la Assistants API. Aquí lo expresamos como modelos Pydantic; el SDK de OpenAI
los convierte automáticamente a un JSON Schema estricto cuando se usa
`client.responses.parse(..., text_format=RespuestaKilocalculator)`.

NOTA: este schema es una propuesta razonable. Cuando tengas a la mano tu schema
real (en el dashboard, ícono de editar junto a "Response format"), ajusta los
campos para que coincidan 1:1. La estructura general —poder PREGUNTAR o RESPONDER—
es lo importante para migrar el comportamiento del asistente.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class Ingrediente(BaseModel):
    """Desglose por ingrediente identificado en el platillo."""

    nombre: str = Field(description="Nombre del ingrediente, p. ej. 'tortilla de maíz'.")
    cantidad: str = Field(description="Cantidad estimada, p. ej. '2 piezas' o '150 g'.")
    kcal: float = Field(description="Kilocalorías aportadas por este ingrediente.")
    proteinas_g: float
    carbohidratos_g: float
    grasas_g: float


class Totales(BaseModel):
    """Suma de kcal y macronutrientes del platillo completo."""

    kcal: float
    proteinas_g: float
    carbohidratos_g: float
    grasas_g: float


class RespuestaKilocalculator(BaseModel):
    """
    Respuesta del asistente en CADA turno.

    El asistente "hace cuantas preguntas sean necesarias", así que cada respuesta
    es de uno de dos tipos, controlado por `requiere_mas_informacion`:

      * True  -> aún falta info. Llena `pregunta` y deja `totales`/`ingredientes` en null.
      * False -> ya hay cálculo final. Llena `platillo`, `ingredientes` y `totales`.
    """

    requiere_mas_informacion: bool = Field(
        description="True si el asistente necesita preguntar antes de poder calcular."
    )
    pregunta: Optional[str] = Field(
        default=None,
        description="La pregunta de seguimiento al usuario (solo si requiere_mas_informacion=True).",
    )
    platillo: Optional[str] = Field(
        default=None, description="Nombre del platillo una vez identificado."
    )
    ingredientes: Optional[List[Ingrediente]] = Field(
        default=None, description="Desglose por ingrediente (solo en la respuesta final)."
    )
    totales: Optional[Totales] = Field(
        default=None, description="Totales de kcal y macros (solo en la respuesta final)."
    )
    supuestos: Optional[str] = Field(
        default=None,
        description="Supuestos hechos para el cálculo (porciones, preparación, contexto CDMX).",
    )
