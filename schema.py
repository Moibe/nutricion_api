"""
Schema de Structured Output para el asistente Kilocalculator.

Diseño "híbrido mínimo": los nombres de los macros coinciden 1:1 con el schema
real del dashboard, `macronutrientes_y_kcal` (kilocalorias / proteinas /
carbohidratos / grasas). PERO no adoptamos ese schema literal: lo envolvemos en
un sobre de DOS MODOS (`requiere_mas_informacion` + `pregunta`) para conservar la
conducta de "hacer preguntas" del asistente, y agregamos `platillo` (texto) para
poder guardarlo. El SDK de OpenAI convierte este modelo Pydantic a un JSON Schema
estricto al usar `client.responses.parse(..., text_format=RespuestaKilocalculator)`.

Todos los campos vienen SIEMPRE presentes en el JSON; los que no aplican llegan
en `null`. El cliente ramifica por `requiere_mas_informacion`, no por presencia.
"""

from typing import Optional

from pydantic import BaseModel, Field


class RespuestaKilocalculator(BaseModel):
    """
    Respuesta del asistente en CADA turno, en uno de dos modos controlado por
    `requiere_mas_informacion`:

      * True  -> aún falta info. Llena `pregunta`; deja `platillo` y los macros en null.
      * False -> resultado final. Llena `platillo` y los 4 macros; deja `pregunta` en null.
    """

    requiere_mas_informacion: bool = Field(
        description="True si el asistente necesita preguntar antes de poder calcular."
    )
    pregunta: Optional[str] = Field(
        default=None,
        description="La pregunta de seguimiento al usuario (solo si requiere_mas_informacion=True).",
    )
    platillo: Optional[str] = Field(
        default=None,
        description="Nombre/descripción del platillo identificado (solo en el resultado final).",
    )
    kilocalorias: Optional[float] = Field(
        default=None, description="Valor energético total en kilocalorías (kcal). Solo en el final."
    )
    proteinas: Optional[float] = Field(
        default=None, description="Proteínas en gramos (g). Solo en el final."
    )
    carbohidratos: Optional[float] = Field(
        default=None, description="Carbohidratos en gramos (g). Solo en el final."
    )
    grasas: Optional[float] = Field(
        default=None, description="Grasas en gramos (g). Solo en el final."
    )
