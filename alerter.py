"""
alerter.py — Script standalone de monitoreo para BuscaVenezuela Bot.

Funciones originales:
  - run_alerter(): monitoreo de desaparecidos via GitHub Actions (cron cada 15 min)

Funciones Guardian Sísmico VE (nuevas):
  - forward_sos_to_channel(): reenvía un reporte SOS al canal de rescatistas
  - send_sos_summary(): resumen de reportes activos cada 10 minutos
"""

import asyncio
import hashlib
import logging
import os
import sys
from datetime import datetime

import requests
from dotenv import load_dotenv

import db
import scraper
import matcher

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
RESCUE_CHANNEL_ID = os.getenv("RESCUE_CHANNEL_ID", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ---------------------------------------------------------------------------
# Envío de mensajes via API REST de Telegram (síncrono, para el cron)
# ---------------------------------------------------------------------------

def enviar_mensaje(chat_id: int, texto: str) -> bool:
    """
    Envía un mensaje HTML a un chat usando la API REST de Telegram.
    Devuelve True si tuvo éxito.
    """
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN no configurado.")
        return False

    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("ok"):
            return True
        else:
            logger.warning(f"Telegram API error para chat {chat_id}: {data.get('description')}")
            return False
    except requests.RequestException as e:
        logger.error(f"Error de red al enviar mensaje a {chat_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Construcción de mensajes de alerta (BuscaVenezuela original)
# ---------------------------------------------------------------------------

def _construir_mensaje_alerta(watch: dict, resultado: dict, score: float) -> str:
    """Construye el texto HTML de la alerta para el familiar."""
    barra = int(score * 10)
    barra_str = "█" * barra + "░" * (10 - barra)
    return (
        f"🔔 <b>¡POSIBLE COINCIDENCIA ENCONTRADA!</b>\n\n"
        f"👤 Buscabas: <b>{watch['nombre']}</b>\n"
        f"📌 Encontrado: <b>{resultado['nombre']}</b>\n\n"
        f"📍 Estado/Ubicación: {resultado['estado']}\n"
        f"🌐 Fuente: {resultado['fuente']}\n"
        f"📊 Similitud: [{barra_str}] {score:.0%}\n"
        f"🔗 <a href='{resultado['url']}'>Ver registro completo</a>\n\n"
        f"<i>Texto encontrado:</i>\n"
        f"<code>{resultado['raw_text'][:200]}</code>\n\n"
        f"✅ Si lo encontraste, escríbeme /encontrado {watch['id']}\n"
        f"❌ Para detener esta alerta: /cancelar {watch['id']}"
    )


# ---------------------------------------------------------------------------
# Guardian Sísmico VE — Funciones asíncronas para el canal de rescatistas
# ---------------------------------------------------------------------------

def _formato_hora() -> str:
    """Hora local formateada para mensajes de emergencia."""
    now = datetime.now()
    meses = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]
    return f"{now.strftime('%H:%M')} — {now.day} {meses[now.month-1]} {now.year}"


def _maps_link(lat: float, lon: float) -> str:
    """Genera un link de Google Maps para las coordenadas dadas."""
    return f"https://maps.google.com/?q={lat},{lon}"


async def forward_sos_to_channel(report: dict) -> bool:
    """
    Formatea y envía un reporte SOS al canal de rescatistas de forma asíncrona.
    Incluye link a Google Maps y comando /resolver_<id>.

    Args:
        report: dict con keys: id, telegram_user_id, username, lat, lon,
                mensaje, timestamp, telefono (opcional)
    Returns:
        True si el mensaje fue enviado correctamente.
    """
    if not RESCUE_CHANNEL_ID:
        logger.warning("RESCUE_CHANNEL_ID no configurado. Reporte SOS no reenviado.")
        return False

    try:
        canal_id = int(RESCUE_CHANNEL_ID)
    except (ValueError, TypeError):
        logger.error(f"RESCUE_CHANNEL_ID inválido: {RESCUE_CHANNEL_ID}")
        return False

    lat = report.get("lat")
    lon = report.get("lon")
    referencia_texto = report.get("referencia_texto")

    # Construir seccion de ubicacion segun disponibilidad
    if lat is not None and lon is not None:
        ubicacion_str = f"GPS: {lat}, {lon}\n{_maps_link(lat, lon)}"
    elif referencia_texto:
        ubicacion_str = f"Referencia: {referencia_texto}"
    else:
        ubicacion_str = "Sin ubicacion disponible"

    telefono_str = report.get("telefono") or "No compartido"
    username = report.get("username") or "anonimo"
    hora = report.get("timestamp") or _formato_hora()
    reporte_id = report.get("id", "?")

    texto = (
        "🆘 NUEVO REPORTE SOS\n"
        "─────────────────────\n"
        f"👤 @{username} (ID: {report.get('telegram_user_id', '?')})\n"
        f"📍 {ubicacion_str}\n"
        f"📝 \"{report.get('mensaje') or 'Sin descripcion'}\"\n"
        f"📞 Telefono: {telefono_str}\n"
        f"🕐 {hora}\n"
        "─────────────────────\n"
        f"/resolver_{reporte_id} para marcar como atendido"
    )

    # Usar requests de forma síncrona dentro de asyncio (thread executor)
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": canal_id,
                    "text": texto,
                    "disable_web_page_preview": False,
                },
                timeout=15,
            )
        )
        data = result.json()
        if data.get("ok"):
            logger.info(f"[Canal] SOS #{reporte_id} enviado al canal {canal_id}")
            return True
        else:
            logger.warning(f"[Canal] Error al enviar SOS: {data.get('description')}")
            return False
    except Exception as e:
        logger.error(f"[Canal] Excepción al enviar SOS al canal: {e}")
        return False


async def send_sos_summary() -> bool:
    """
    Envía al canal de rescatistas un resumen de los reportes SOS activos.
    Diseñado para ejecutarse cada 10 minutos.
    Retorna True si se envió algún mensaje.
    """
    if not RESCUE_CHANNEL_ID:
        logger.warning("RESCUE_CHANNEL_ID no configurado. Resumen SOS omitido.")
        return False

    try:
        canal_id = int(RESCUE_CHANNEL_ID)
    except (ValueError, TypeError):
        logger.error(f"RESCUE_CHANNEL_ID inválido: {RESCUE_CHANNEL_ID}")
        return False

    # Obtener reportes activos
    reportes = db.get_active_sos_reports()

    if not reportes:
        logger.info("[Resumen SOS] No hay reportes activos en este momento.")
        return False

    hora = _formato_hora()
    lineas = [
        f"📊 RESUMEN SOS — {hora}",
        f"Reportes activos: {len(reportes)}\n",
    ]

    for r in reportes:
        lat = r.get("lat")
        lon = r.get("lon")
        coords = f"{lat}, {lon}" if lat and lon else "Sin GPS"
        username = r.get("username") or "anónimo"
        ts = r.get("timestamp", "?")[:16] if r.get("timestamp") else "?"
        lineas.append(
            f"• #{r['id']} @{username} — {coords} — {ts}"
        )

    lineas.append(f"\nUsa /resolver_<id> para marcar como atendido.")

    texto = "\n".join(lineas)

    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": canal_id,
                "text": texto,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("ok"):
            logger.info(f"[Resumen SOS] Enviado al canal. {len(reportes)} reportes activos.")
            return True
        else:
            logger.warning(f"[Resumen SOS] Error Telegram: {data.get('description')}")
            return False
    except Exception as e:
        logger.error(f"[Resumen SOS] Excepción al enviar resumen: {e}")
        return False


# ---------------------------------------------------------------------------
# Proceso principal — BuscaVenezuela (búsqueda de desaparecidos)
# ---------------------------------------------------------------------------

def run_alerter() -> tuple[int, int]:
    """
    Ciclo completo:
      1. Lee todos los watches activos.
      2. Por cada watch, busca en todas las fuentes.
      3. Si hay match y es nuevo (hash único), envía alerta.

    Devuelve (watches_chequeados, alertas_enviadas).
    """
    db.init_db()

    watches = db.get_all_watches()
    total_watches = len(watches)
    total_alertas = 0

    if not watches:
        logger.info("No hay watches activos. Nada que hacer.")
        return 0, 0

    logger.info(f"Chequeando {total_watches} watch(es) activo(s)...")

    for watch in watches:
        watch_id = watch["id"]
        chat_id = watch["chat_id"]
        nombre_buscado = watch["nombre"]

        logger.info(f"  [Watch #{watch_id}] Buscando: {nombre_buscado}")

        try:
            resultados = scraper.buscar_en_todas_las_fuentes(nombre_buscado)
        except Exception as e:
            logger.error(f"  [Watch #{watch_id}] Error en scraper: {e}")
            continue

        if not resultados:
            logger.info(f"  [Watch #{watch_id}] Sin resultados.")
            continue

        for resultado in resultados:
            score = matcher.calcular_similitud(nombre_buscado, resultado["nombre"])

            if not matcher.es_match(nombre_buscado, resultado["nombre"]):
                continue

            # Deduplicación por hash del contenido raw
            raw = resultado.get("raw_text", "")
            registro_hash = hashlib.md5(
                f"{resultado['fuente']}::{raw}".encode("utf-8")
            ).hexdigest()

            es_nuevo = db.mark_alerta_sent(
                watch_id=watch_id,
                fuente=resultado["fuente"],
                registro_hash=registro_hash,
            )

            if not es_nuevo:
                logger.debug(f"  [Watch #{watch_id}] Alerta duplicada omitida ({registro_hash[:8]}…)")
                continue

            # Enviar alerta
            texto = _construir_mensaje_alerta(watch, resultado, score)
            ok = enviar_mensaje(chat_id=chat_id, texto=texto)

            if ok:
                total_alertas += 1
                logger.info(
                    f"  [Watch #{watch_id}] ✅ Alerta enviada a chat {chat_id} "
                    f"({resultado['fuente']}, score={score:.2f})"
                )
            else:
                logger.warning(
                    f"  [Watch #{watch_id}] ❌ Fallo al enviar alerta a chat {chat_id}"
                )

    return total_watches, total_alertas


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN no configurado. Crea un archivo .env o setea la variable de entorno.")
        sys.exit(1)

    # MEJORA 7 — Soporte para --sos-summary
    # Uso: python alerter.py --sos-summary
    # Envia un resumen de reportes SOS activos al canal de rescatistas.
    # Si no hay reportes activos, sale silenciosamente sin enviar nada.
    if "--sos-summary" in sys.argv:
        db.init_db()
        reportes = db.get_active_sos_reports()

        if not reportes:
            logger.info("[SOS-Summary] No hay reportes activos. Sin mensaje al canal.")
            sys.exit(0)

        if not RESCUE_CHANNEL_ID:
            logger.error("[SOS-Summary] RESCUE_CHANNEL_ID no configurado.")
            sys.exit(1)

        try:
            canal_id = int(RESCUE_CHANNEL_ID)
        except (ValueError, TypeError):
            logger.error(f"[SOS-Summary] RESCUE_CHANNEL_ID invalido: {RESCUE_CHANNEL_ID}")
            sys.exit(1)

        hora = _formato_hora()
        total = len(reportes)
        mostrar = reportes[:10]

        lineas = [
            f"RESUMEN SOS — {hora}",
            f"Reportes activos: {total}\n",
        ]
        for r in mostrar:
            lat = r.get("lat")
            lon = r.get("lon")
            ref = r.get("referencia_texto", "")
            if lat and lon:
                ubic = f"GPS {lat:.4f},{lon:.4f}"
            elif ref:
                ubic = ref[:40]
            else:
                ubic = "Sin ubicacion"

            username = r.get("username") or "anonimo"
            ts = str(r.get("timestamp", ""))[:16]
            lineas.append(f"#{r['id']} @{username} — {ubic} — {ts}")
            lineas.append(f"  /resolver_{r['id']}")

        if total > 10:
            lineas.append(f"\n... y {total - 10} reportes mas.")

        lineas.append("\nUsa /resolver_<id> para marcar como atendido.")
        texto_resumen = "\n".join(lineas)

        try:
            resp = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": canal_id,
                    "text": texto_resumen,
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("ok"):
                logger.info(f"[SOS-Summary] Resumen enviado. {total} reportes activos.")
                sys.exit(0)
            else:
                logger.error(f"[SOS-Summary] Error Telegram: {data.get('description')}")
                sys.exit(1)
        except Exception as e:
            logger.error(f"[SOS-Summary] Excepcion al enviar: {e}")
            sys.exit(1)

    # Flujo normal del alerter (busqueda de desaparecidos)
    watches_chequeados, alertas_enviadas = run_alerter()

    # Enviar resumen SOS si hay reportes activos (modo normal)
    if RESCUE_CHANNEL_ID:
        asyncio.run(send_sos_summary())

    resumen = (
        f"\n{'='*50}\n"
        f"Alerter finalizado\n"
        f"   Watches chequeados : {watches_chequeados}\n"
        f"   Alertas enviadas   : {alertas_enviadas}\n"
        f"{'='*50}"
    )
    print(resumen)
    logger.info(resumen)
