"""
matcher.py — Comparación de nombres para BuscaVenezuela Bot.
Usa difflib + normalización Unicode + bonus por token compartido.
"""

import unicodedata
import difflib
import re

# Palabras vacías que se eliminan antes de comparar
_STOP_WORDS = {"de", "del", "la", "el", "y", "los", "las", "a", "en", "un", "una"}


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    """
    Convierte el texto a minúsculas, elimina tildes/diacríticos y
    quita las palabras de parada definidas en _STOP_WORDS.
    Devuelve un string limpio con tokens separados por espacio.
    """
    if not texto:
        return ""

    # Minúsculas
    texto = texto.lower().strip()

    # Eliminar tildes y caracteres diacríticos (NFD → solo ASCII base)
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")

    # Eliminar caracteres que no sean letras ni espacios
    texto = re.sub(r"[^a-z\s]", " ", texto)

    # Tokenizar y filtrar stop-words
    tokens = [t for t in texto.split() if t and t not in _STOP_WORDS]

    return " ".join(tokens)


# ---------------------------------------------------------------------------
# Cálculo de similitud
# ---------------------------------------------------------------------------

def calcular_similitud(a: str, b: str) -> float:
    """
    Calcula un score de similitud entre dos nombres.

    Pasos:
      1. Normaliza ambas cadenas.
      2. Aplica difflib.SequenceMatcher para obtener el ratio base.
      3. Bonus de +0.15 si los tokens comparten al menos una palabra de 4+ letras.
      4. Clampea el resultado a [0.0, 1.0].

    Devuelve float en [0.0, 1.0].
    """
    na = normalizar(a)
    nb = normalizar(b)

    if not na or not nb:
        return 0.0

    # Ratio base con SequenceMatcher
    base_ratio = difflib.SequenceMatcher(None, na, nb).ratio()

    # Bonus por token compartido de 4+ caracteres
    tokens_a = set(t for t in na.split() if len(t) >= 4)
    tokens_b = set(t for t in nb.split() if len(t) >= 4)
    bonus = 0.15 if tokens_a & tokens_b else 0.0

    score = min(1.0, base_ratio + bonus)
    return round(score, 4)


# ---------------------------------------------------------------------------
# Decisión de match
# ---------------------------------------------------------------------------

def es_match(a: str, b: str, umbral: float = 0.72) -> bool:
    """
    Devuelve True si la similitud entre a y b supera el umbral dado.
    Umbral por defecto: 0.72 (suficiente para tolerar errores tipográficos
    y variaciones de orden, sin generar demasiados falsos positivos).
    """
    return calcular_similitud(a, b) >= umbral


# ---------------------------------------------------------------------------
# Prueba rápida
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    casos = [
        # (nombre_buscado, nombre_encontrado, descripción)
        ("María de los Ángeles Pérez López", "Maria Perez Lopez", "Sin tildes y sin 'de los'"),
        ("Juan Carlos Rodríguez", "J. Carlos Rodriguez Mora", "Inicial + apellido extra"),
        ("Pedro Ramírez", "Pedro Ramirez Sanchez", "Apellido materno extra"),
        ("Ana Sofía Martínez", "Sofia Martines", "Apodo + error tipográfico"),
        ("Luis Gonzalez", "Roberto Fernandez", "Personas completamente distintas"),
        ("Carla Mendoza de Jiménez", "Carla Mendoza", "Con/sin apellido de casada"),
        ("José", "Jose Alberto Martinez", "Nombre parcial muy corto"),
    ]

    print(f"\n{'NOMBRE BUSCADO':<40} {'NOMBRE ENCONTRADO':<40} {'SCORE':>7}  {'¿MATCH?'}")
    print("─" * 105)
    for buscado, encontrado, desc in casos:
        score = calcular_similitud(buscado, encontrado)
        match = es_match(buscado, encontrado)
        icono = "✅" if match else "❌"
        print(f"{buscado:<40} {encontrado:<40} {score:>7.4f}  {icono}  ({desc})")

    print()
    print("📌 Umbral por defecto: 0.72")
    print(f"   normalizar('María de los Ángeles') → '{normalizar('María de los Ángeles')}'")
    print(f"   normalizar('Juan Carlos Rodríguez') → '{normalizar('Juan Carlos Rodríguez')}'")
