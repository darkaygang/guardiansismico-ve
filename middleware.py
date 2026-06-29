"""
middleware.py — Capa intermedia del bot BuscaVenezuela / Guardian Sísmico VE.

Contiene:
  - ThrottleDict: Limita flood general (mensajes repetidos en <1.5 s)
  - rate_limit_check: Verifica el límite de acciones SOS en la DB
  - log_user_activity: Registro básico de acciones de usuario
"""

import logging
from cachetools import TTLCache

import db

logger = logging.getLogger(__name__)


class ThrottleDict:
    """
    Rate limiter simple: ignora mensajes duplicados del mismo usuario
    si llegan antes de `rate_limit` segundos del anterior.

    Usa TTLCache de cachetools para que las entradas expiren automáticamente.
    maxsize=10_000 → soporta 10 mil usuarios distintos en RAM sin problema.
    """

    def __init__(self, rate_limit: float = 1.5):
        self.cache = TTLCache(maxsize=10_000, ttl=rate_limit)

    def is_limited(self, user_id: int) -> bool:
        """
        Devuelve True si el usuario mandó otro mensaje hace menos de rate_limit segundos.
        Si no estaba en cache, lo registra y devuelve False (mensaje permitido).
        """
        if user_id in self.cache:
            logger.warning(f"[Throttle] Mensaje bloqueado — user_id={user_id}")
            return True
        self.cache[user_id] = 1
        return False


# ---------------------------------------------------------------------------
# Rate Limiting para acciones SOS (usa la tabla rate_limit en SQLite)
# ---------------------------------------------------------------------------

async def rate_limit_check(update, context) -> bool:
    """
    Verifica si el usuario puede enviar una acción SOS.
    Usa la tabla rate_limit en SQLite para persistencia entre reinicios.

    Retorna True si está dentro del límite, False si debe ser bloqueado.
    Solo se aplica a acciones de emergencia SOS, nunca al menú general.
    """
    if update.effective_user is None:
        return True

    user_id = update.effective_user.id
    permitido = db.check_rate_limit(user_id, window_sec=10, max_count=3)

    if not permitido:
        logger.warning(
            f"[RateLimit-SOS] Bloqueado silenciosamente — user_id={user_id}"
        )

    return permitido


# ---------------------------------------------------------------------------
# Logging básico de actividad
# ---------------------------------------------------------------------------

def log_user_activity(user_id: int, action: str) -> None:
    """
    Registra una acción del usuario en el log del servidor.
    Útil para auditoría y métricas de uso en emergencias.
    """
    logger.info(f"[Actividad] user_id={user_id} accion='{action}'")
