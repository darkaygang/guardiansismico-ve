"""
bot.py — Bot de Telegram: BuscaVenezuela + Guardian Sismico VE.

Modulos integrados:
  - BuscaVenezuela: busqueda de desaparecidos tras sismos (funcionalidad original)
  - Guardian Sismico VE: sistema de triaje de emergencia sismica con boton de panico

Optimizado para bajo consumo de datos y bateria.
Sin imagenes ni markdown pesado en flujos de emergencia.

Mejoras v2:
  - Flujo SOS sin GPS: captura referencia de texto si no hay ubicacion
  - Mensajes ultra-ligeros (_enviar_critico) para señal debil
  - Psicologia de crisis: mensajes empaticos
  - Retry automatico al canal (3 intentos, backoff exponencial)
  - Comando /sos_pendientes para admins y /resolver mejorado
"""

import asyncio
import logging
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from middleware import ThrottleDict, rate_limit_check, log_user_activity

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

import db
import scraper
import matcher

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
RESCUE_CHANNEL_ID = os.getenv("RESCUE_CHANNEL_ID")  # ID negativo del canal/grupo de rescatistas
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")  # 'development' o 'production'
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", 8443))

# ADMIN_IDS: lista separada por coma en .env; si esta vacio, cualquiera puede usar /sos_pendientes
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = set(int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Estados de conversacion — BuscaVenezuela (originales)
# ---------------------------------------------------------------------------
ESPERANDO_NOMBRE, ESPERANDO_ESTADO = range(2)

# ---------------------------------------------------------------------------
# Estados de conversacion — Guardian Sismico VE (nuevos, sin conflicto)
# ---------------------------------------------------------------------------
(
    MENU_PRINCIPAL,
    SOS_ESPERANDO_UBICACION,
    SOS_ESPERANDO_DESCRIPCION,
    SOS_ESPERANDO_TELEFONO,
    TEMBLANDO_ACTIVO,
    POST_TEMBLOR,
    AVISAR_FAMILIA_UBICACION,
    PREPARACION_SUBMENU,
) = range(10, 18)

# ---------------------------------------------------------------------------
# Queue global para operaciones SOS en background (no bloquea la respuesta)
# ---------------------------------------------------------------------------
sos_queue: asyncio.Queue = asyncio.Queue()

# ---------------------------------------------------------------------------
# Textos de botones — Menu original BuscaVenezuela
# ---------------------------------------------------------------------------
BTN_BUSCAR = "🔍 Buscar a un familiar"
BTN_MIS_BUSQUEDAS = "📋 Mis búsquedas activas"
BTN_ENCONTRADO = "✅ ¡Ya lo encontré!"
BTN_AYUDA = "🆘 Ayuda"

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_BUSCAR)],
        [KeyboardButton(BTN_MIS_BUSQUEDAS)],
        [KeyboardButton(BTN_ENCONTRADO), KeyboardButton(BTN_AYUDA)],
    ],
    resize_keyboard=True,
)

# ---------------------------------------------------------------------------
# Textos de botones — Menu Guardian Sismico VE (6 botones en 2 columnas)
# ---------------------------------------------------------------------------
BTN_ATRAPADO = "🚨 ESTOY ATRAPADO"
BTN_TEMBLANDO = "⚠️ ESTÁ TEMBLANDO"
BTN_CONTACTOS = "📞 Contactos"
BTN_SALVO = "✅ ESTOY A SALVO"
BTN_AVISAR = "📍 Avisar Familia"
BTN_PREPARACION = "ℹ️ Preparación"

GUARDIAN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_ATRAPADO), KeyboardButton(BTN_TEMBLANDO)],
        [KeyboardButton(BTN_CONTACTOS), KeyboardButton(BTN_SALVO)],
        [KeyboardButton(BTN_AVISAR), KeyboardButton(BTN_PREPARACION)],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

# Rango valido de coordenadas en Venezuela
VEN_LAT_MIN, VEN_LAT_MAX = 0.6, 12.2
VEN_LON_MIN, VEN_LON_MAX = -73.3, -59.8

# Estados venezolanos mas poblados para el menu inline
ESTADOS_INLINE = ["La Guaira", "Caracas", "Miranda", "Zulia", "Carabobo", "Aragua", "Bolívar"]


# ---------------------------------------------------------------------------
# Helpers basicos
# ---------------------------------------------------------------------------

def _coordenadas_validas(lat: float, lon: float) -> bool:
    """Verifica que las coordenadas esten dentro del rango geografico de Venezuela."""
    return VEN_LAT_MIN <= lat <= VEN_LAT_MAX and VEN_LON_MIN <= lon <= VEN_LON_MAX


def _maps_link(lat: float, lon: float) -> str:
    """Genera un link de Google Maps para las coordenadas dadas."""
    return f"https://maps.google.com/?q={lat},{lon}"


def _formato_hora() -> str:
    """Hora local formateada para mensajes de emergencia."""
    now = datetime.now()
    meses = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]
    return f"{now.strftime('%H:%M')} — {now.day} {meses[now.month-1]} {now.year}"


# ---------------------------------------------------------------------------
# MEJORA 2 — Mensajes ultra-ligeros para señal debil
# ---------------------------------------------------------------------------

async def _enviar_critico(update: Update, texto: str, reply_markup=None) -> None:
    """
    Envia mensajes optimizados para conexiones lentas o señal debil.

    - Si el texto tiene <= 400 chars: envia en un solo mensaje
    - Si el texto tiene > 400 chars: divide en fragmentos de <= 300 chars
      sin cortar palabras; solo el ultimo fragmento lleva el reply_markup
    """
    if len(texto) <= 400:
        await update.message.reply_text(
            texto,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return

    # Dividir por lineas sin cortar palabras
    fragmentos = []
    fragmento_actual = []
    chars_actuales = 0

    for linea in texto.split("\n"):
        linea_con_salto = linea + "\n"
        if chars_actuales + len(linea_con_salto) > 300 and fragmento_actual:
            fragmentos.append("\n".join(fragmento_actual))
            fragmento_actual = [linea]
            chars_actuales = len(linea_con_salto)
        else:
            fragmento_actual.append(linea)
            chars_actuales += len(linea_con_salto)

    if fragmento_actual:
        fragmentos.append("\n".join(fragmento_actual))

    # Enviar cada fragmento; el markup solo va en el ultimo
    for i, frag in enumerate(fragmentos):
        es_ultimo = (i == len(fragmentos) - 1)
        await update.message.reply_text(
            frag,
            reply_markup=reply_markup if es_ultimo else None,
            disable_web_page_preview=True,
        )


# ---------------------------------------------------------------------------
# MEJORA 4 — Envio al canal con retry y backoff exponencial
# ---------------------------------------------------------------------------

async def _enviar_sos_al_canal(app, report: dict) -> None:
    """
    Formatea y envia el reporte SOS al canal de rescatistas.
    Intentos: 3 con backoff exponencial (0, 2, 4 segundos).
    Nunca lanza excepcion: loggea el error critico y sigue.
    """
    if not RESCUE_CHANNEL_ID:
        logger.warning("RESCUE_CHANNEL_ID no configurado. Reporte SOS no reenviado al canal.")
        return

    try:
        canal_id = int(RESCUE_CHANNEL_ID)
    except (ValueError, TypeError):
        logger.error(f"RESCUE_CHANNEL_ID invalido: {RESCUE_CHANNEL_ID}")
        return

    # Construir seccion de ubicacion segun lo disponible
    lat = report.get("lat")
    lon = report.get("lon")
    referencia_texto = report.get("referencia_texto")

    if lat is not None and lon is not None:
        ubicacion_str = f"GPS: {lat}, {lon}\n{_maps_link(lat, lon)}"
    elif referencia_texto:
        ubicacion_str = f"Referencia: {referencia_texto}"
    else:
        ubicacion_str = "Sin ubicacion disponible"

    telefono_str = report.get("telefono") or "No compartido"
    username = report.get("username") or "anonimo"
    hora = report.get("timestamp", _formato_hora())

    texto = (
        "🆘 NUEVO REPORTE SOS\n"
        "─────────────────────\n"
        f"👤 @{username} (ID: {report['telegram_user_id']})\n"
        f"📍 {ubicacion_str}\n"
        f"📝 \"{report.get('mensaje') or 'Sin descripcion'}\"\n"
        f"📞 Telefono: {telefono_str}\n"
        f"🕐 {hora}\n"
        "─────────────────────\n"
        f"/resolver_{report['id']} para marcar como atendido"
    )

    # Retry con backoff exponencial: 0s, 2s, 4s
    esperas = [0, 2, 4]
    for intento, espera in enumerate(esperas, 1):
        if espera > 0:
            await asyncio.sleep(espera)

        # Crear los botones interactivos para el canal de rescate
        teclado_rescate = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👀 Visto", callback_data=f"resc_visto_{report['id']}_{report['telegram_user_id']}"),
                InlineKeyboardButton("🚑 En camino", callback_data=f"resc_camino_{report['id']}_{report['telegram_user_id']}")
            ],
            [InlineKeyboardButton("✅ Cerrar Reporte", callback_data=f"resc_cerrar_{report['id']}_{report['telegram_user_id']}")]
        ])

        try:
            await app.bot.send_message(
                chat_id=canal_id,
                text=texto,
                disable_web_page_preview=(lat is None),
                reply_markup=teclado_rescate
            )
            logger.info(f"[Canal] SOS #{report['id']} reenviado (intento {intento}).")
            return
        except Exception as e:
            if intento < len(esperas):
                logger.warning(f"[Canal] Intento {intento} fallido: {e}. Reintentando en {esperas[intento]}s...")
            else:
                logger.error(f"[Canal] Error critico — no se pudo reenviar SOS #{report['id']}: {e}")


async def _sos_worker(app) -> None:
    """
    Worker que procesa la cola de tareas SOS en background.
    Guarda en DB y reenvía al canal sin bloquear las respuestas del bot.
    """
    while True:
        task = await sos_queue.get()
        try:
            action = task.get("action")

            if action == "forward_sos":
                await _enviar_sos_al_canal(app, task["report"])

            elif action == "update_phone":
                if RESCUE_CHANNEL_ID:
                    try:
                        await app.bot.send_message(
                            chat_id=int(RESCUE_CHANNEL_ID),
                            text=f"📞 Actualización SOS #{task['report_id']}: El teléfono de la víctima es {task['telefono']}"
                        )
                    except Exception as e:
                        logger.error(f"[Canal] Error actualizando teléfono: {e}")

            elif action == "notify_safe":
                # Notificar al canal que el usuario esta a salvo
                if RESCUE_CHANNEL_ID:
                    try:
                        canal_id = int(RESCUE_CHANNEL_ID)
                        username = task.get("username") or "anonimo"
                        hora = _formato_hora()
                        await app.bot.send_message(
                            chat_id=canal_id,
                            text=f"🟢 Resuelto: @{username} ya esta a salvo — {hora}",
                        )
                    except Exception as e:
                        logger.error(f"[Canal] Error al notificar 'a salvo': {e}")

        except Exception as e:
            logger.error(f"[SOSWorker] Error procesando tarea: {e}")
        finally:
            sos_queue.task_done()


# ---------------------------------------------------------------------------
# Helpers de busqueda (BuscaVenezuela original)
# ---------------------------------------------------------------------------

async def _buscar_y_responder(update: Update, nombre_query: str) -> None:
    """Ejecuta la busqueda y responde de forma empatica."""
    msg = await update.effective_message.reply_text(
        f"🔍 Estoy revisando las listas oficiales de desaparecidos buscando a <b>{nombre_query}</b>...\n"
        "Esto toma unos segunditos.",
        parse_mode=ParseMode.HTML,
    )

    try:
        resultados = scraper.buscar_en_todas_las_fuentes(nombre_query)
    except Exception as e:
        logger.error(f"Error en scraper: {e}")
        await msg.edit_text(
            "😔 En este momento las paginas de rescate estan recibiendo muchisimas visitas y estan lentas.\n\n"
            "<b>No te preocupes:</b> tu alerta ya esta guardada y yo seguire intentando automaticamente en segundo plano.\n"
            "Apenas pueda conectar y lo vea, te mandare un mensaje. Ten fe. 🙏",
            parse_mode=ParseMode.HTML,
        )
        return

    if not resultados:
        await msg.edit_text(
            f"😔 Acabo de revisar y por ahora no veo a <b>{nombre_query}</b> en las listas.\n\n"
            "Esto es muy normal porque tardan un poco en actualizar los nombres.\n"
            "<b>Tu alerta ya esta activa</b>, asi que yo seguire revisando dia y noche por ti. Te enviare un mensaje aqui mismo apenas aparezca. 🙏",
            parse_mode=ParseMode.HTML,
        )
        return

    # Si hay resultados, ordenar por similitud
    con_score = [
        (r, matcher.calcular_similitud(nombre_query, r["nombre"]))
        for r in resultados
    ]
    con_score.sort(key=lambda x: x[1], reverse=True)
    top = con_score[:3]

    texto = f"🔔 Encontre posibles coincidencias para <b>{nombre_query}</b>:\n\n"
    for r, score in top:
        texto += f"👤 <b>{r['nombre']}</b>\n"
        texto += f"  📍 Ubicacion: {r['estado']}\n"
        texto += f"  🌐 Origen: {r['fuente']}\n"
        texto += f"  🔗 <a href='{r['url']}'>Toca aqui para ver detalles</a>\n\n"

    texto += "<i>Si alguna de estas personas es tu familiar, por favor toca el boton de '✅ ¡Ya lo encontre!' abajo.</i>"

    await msg.edit_text(texto, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# Flujo Principal — Comandos (BuscaVenezuela original)
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Comando /start — muestra el menu de Guardian Sismico VE como menu principal.
    La funcionalidad de BuscaVenezuela sigue accesible via botones o comandos.
    """
    texto = (
        "🌋 Guardian Sismico VE\n"
        "────────────────────────\n\n"
        "Soy tu asistente de emergencia sismica para Venezuela.\n"
        "Presiona un boton para comenzar:\n\n"
        "🚨 ATRAPADO — Envia tu ubicacion a rescatistas\n"
        "⚠️ TEMBLANDO — Instrucciones inmediatas de seguridad\n"
        "📞 Contactos — Directorio de emergencias local\n"
        "✅ A SALVO — Notifica que estas bien\n"
        "📍 Avisar Familia — Genera mensaje para WhatsApp\n"
        "ℹ️ Preparacion — Guia de preparacion sismica\n\n"
        "Tambien puedo buscar familiares desaparecidos. Toca 🔍 Buscar."
    )
    await update.message.reply_text(texto, reply_markup=GUARDIAN_MENU)


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🆘 <b>Como te ayudo?</b>\n\n"
        "1. Toca <b>🔍 Buscar a un familiar</b> abajo.\n"
        "2. Dime su nombre y en que zona estaba.\n"
        "3. Listo! Yo vigilare las listas oficiales por ti y te mandare un mensaje si aparece.\n\n"
        "No necesitas hacer nada complicado. Yo me encargo de lo tecnico.",
        parse_mode=ParseMode.HTML,
        reply_markup=GUARDIAN_MENU,
    )


# ---------------------------------------------------------------------------
# Flujo de Conversacion — BuscaVenezuela (original, sin modificar)
# ---------------------------------------------------------------------------

async def iniciar_busqueda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👤 <b>Vamos a buscar a tu familiar</b>\n\n"
        "Por favor, escribe su <b>nombre completo</b> (con apellidos si los sabes).\n"
        "<i>Ejemplo: Juan Perez Gomez</i>\n\n"
        "Si te equivocas o cambias de opinion, toca '❌ Cancelar'.",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup([["❌ Cancelar"]], resize_keyboard=True),
    )
    return ESPERANDO_NOMBRE


async def cancelar_conversacion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "❌ Busqueda cancelada. No hay problema, puedes volver a intentar cuando te sientas listo.",
        reply_markup=GUARDIAN_MENU,
    )
    return ConversationHandler.END


async def recibir_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nombre = update.message.text.strip()

    if nombre == "❌ Cancelar":
        return await cancelar_conversacion(update, context)

    if len(nombre) < 3:
        await update.message.reply_text("❌ Ese nombre es muy corto. Escribelo completo, por favor:")
        return ESPERANDO_NOMBRE

    context.user_data["nombre"] = nombre

    estados = [["La Guaira", "Caracas"], ["Miranda", "No lo se"], ["❌ Cancelar"]]

    await update.message.reply_text(
        f"👍 Muy bien, buscare a: <b>{nombre}</b>\n\n"
        "Ahora dime: ¿En que zona estaba cuando ocurrio el sismo?\n"
        "<i>Toca uno de los botones abajo o escribelo.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(estados, resize_keyboard=True),
    )
    return ESPERANDO_ESTADO


async def recibir_estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    estado = update.message.text.strip()

    if estado == "❌ Cancelar":
        return await cancelar_conversacion(update, context)

    nombre = context.user_data["nombre"]
    chat_id = update.effective_chat.id

    watch_id = db.add_watch(chat_id=chat_id, nombre=nombre)

    await update.message.reply_text(
        f"✅ <b>Todo listo!</b>\n"
        f"He anotado a <b>{nombre}</b>. A partir de ahora vigilare las paginas oficiales automaticamente.\n",
        parse_mode=ParseMode.HTML,
        reply_markup=GUARDIAN_MENU,
    )

    await _buscar_y_responder(update, nombre)

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Ver Mis Busquedas / Encontrado (BuscaVenezuela original)
# ---------------------------------------------------------------------------

async def mis_busquedas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    watches = db.get_watches_by_chat(chat_id)

    if not watches:
        await update.message.reply_text(
            "📭 No estas buscando a nadie en este momento.\n"
            "Toca '🔍 Buscar a un familiar' para empezar.",
            reply_markup=GUARDIAN_MENU,
        )
        return

    await update.message.reply_text(
        "📋 <b>Estas son las personas que estoy vigilando por ti:</b>\n"
        "<i>(Si quieres eliminar a alguien de la lista, toca '❌ Dejar de buscar' debajo de su nombre)</i>",
        parse_mode=ParseMode.HTML,
    )

    for w in watches:
        keyboard = [[InlineKeyboardButton("❌ Dejar de buscar", callback_data=f"del_{w['id']}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"👤 <b>{w['nombre']}</b>\n📅 Empiece a buscar el: {w['created_at'][:10]}",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )


async def ya_lo_encontre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    watches = db.get_watches_by_chat(chat_id)

    if not watches:
        await update.message.reply_text(
            "📭 No tienes busquedas activas.",
            reply_markup=GUARDIAN_MENU,
        )
        return

    await update.message.reply_text(
        "🎉 Que alegria enorme! 🙏\n"
        "A quien lograste encontrar? Toca el boton debajo de su nombre:",
        parse_mode=ParseMode.HTML,
    )

    for w in watches:
        keyboard = [[InlineKeyboardButton("🎉 Si, ya lo encontre!", callback_data=f"found_{w['id']}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"👤 <b>{w['nombre']}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )


# ---------------------------------------------------------------------------
# Manejador de Botones Inline (BuscaVenezuela + Guardian Sismico VE)
# ---------------------------------------------------------------------------

async def boton_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = query.message.chat_id

    # --- BuscaVenezuela: eliminar busqueda ---
    if data.startswith("del_"):
        watch_id = int(data.split("_")[1])
        db.delete_watch(watch_id, chat_id)
        await query.edit_message_text("Listo. Ya no buscare a esa persona.")

    # --- BuscaVenezuela: marcar como encontrado ---
    elif data.startswith("found_"):
        watch_id = int(data.split("_")[1])
        db.mark_watch_encontrado(watch_id, chat_id)
        await query.edit_message_text(
            "🎉 <b>Que gran noticia!</b>\n\n"
            "Me alegra muchisimo saber que aparecio. He retirado su nombre de mis alertas.\n"
            "Un abrazo fuerte. 💛❤️💙",
            parse_mode=ParseMode.HTML,
        )

    # --- Guardian Sismico: seleccion de estado para directorio ---
    elif data.startswith("estado_"):
        estado = data.split("_", 1)[1]
        await _mostrar_contactos_estado(query, estado)

    # --- Guardian Sismico: resolver SOS desde el canal ---
    elif data.startswith("resolver_inline_"):
        report_id = int(data.split("_")[2])
        resuelto = db.resolve_sos_report(report_id)
        if resuelto:
            await query.edit_message_text(f"🟢 Reporte #{report_id} marcado como resuelto.")
        else:
            await query.answer("Este reporte ya fue resuelto o no existe.", show_alert=True)


async def _mostrar_contactos_estado(query, estado: str) -> None:
    """Muestra los contactos de emergencia para un estado venezolano."""
    try:
        contactos = db.get_contactos_by_estado(estado)

        if not contactos:
            await query.edit_message_text(
                f"No encontre contactos para {estado} en este momento.\n\n"
                "Llama al 911 (Emergencias Venezuela)."
            )
            return

        lineas = [f"📞 Contactos de emergencia — {estado}\n"]
        for c in contactos:
            emoji = {"Bomberos": "🚒", "Proteccion Civil": "🛡️", "Protección Civil": "🛡️",
                     "Cruz Roja": "🏥", "Sismologia": "📡", "Sismología": "📡",
                     "Emergencias": "🆘", "Linea Directa Emergencias": "🆘",
                     "Línea Directa Emergencias": "🆘"}.get(c["tipo"], "📞")
            lineas.append(f"{emoji} {c['nombre_entidad']}")
            lineas.append(f"   {c['telefono']}")

        await query.edit_message_text("\n".join(lineas))
    except Exception as e:
        logger.error(f"Error mostrando contactos para {estado}: {e}")
        await query.edit_message_text("Error al cargar contactos. Llama al 911.")


# ---------------------------------------------------------------------------
# Guardian Sismico VE — Flujo 1: ESTOY ATRAPADO (SOS)
# ---------------------------------------------------------------------------

async def sos_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entrada al flujo SOS. Responde INMEDIATAMENTE con boton de ubicacion.
    El tiempo de respuesta es critico: < 500ms.
    """
    user = update.effective_user
    log_user_activity(user.id, "SOS_ATRAPADO_inicio")

    # Verificar rate limit ANTES de hacer nada
    permitido = await rate_limit_check(update, context)
    if not permitido:
        # Bloqueado silenciosamente — no le decimos nada al spammer
        await _enviar_critico(
            update,
            "Sistema ocupado. Intenta en unos segundos.",
            reply_markup=GUARDIAN_MENU,
        )
        return ConversationHandler.END

    # Registrar usuario si es la primera vez
    db.get_or_create_user(user.id, user.username)

    # Limpiar datos de sesion de flujo SOS anterior
    context.user_data.pop("sos_lat", None)
    context.user_data.pop("sos_lon", None)
    context.user_data.pop("sos_referencia_texto", None)
    context.user_data.pop("sos_ubicacion_texto_enviado", None)

    # Teclado con boton de ubicacion nativa + cancelar
    teclado = ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Enviar mi ubicacion ahora", request_location=True)],
            [KeyboardButton("❌ Cancelar")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await _enviar_critico(
        update,
        "📍 Envia tu ubicacion para que los rescatistas sepan donde estas.\n"
        "(Toca el boton de abajo ↓)",
        reply_markup=teclado,
    )
    return SOS_ESPERANDO_UBICACION


async def _sos_pedir_referencia_texto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    MEJORA 1: El usuario toco 'Describir ubicacion en texto'.
    Le pedimos descripcion detallada de su punto de referencia.
    """
    await update.message.reply_text(
        "Escribe en un solo mensaje:\n"
        "- Nombre de la calle o avenida\n"
        "- Numero de casa o edificio\n"
        "- Barrio o sector\n"
        "- Ciudad\n"
        "- Cerca de que lugar conocido (iglesia, farmacia, escuela)\n\n"
        "Ejemplo: Av. Lara, Edif. Sol, Piso 3, frente a iglesia Santa Rosa, Barquisimeto",
        reply_markup=ReplyKeyboardMarkup([["❌ Cancelar"]], resize_keyboard=True),
    )
    return SOS_ESPERANDO_UBICACION


async def sos_recibir_ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Recibe la ubicacion GPS del usuario y pide descripcion.
    MEJORA 1: Si no hay GPS, ofrece flujo alternativo de texto.
    """
    texto_msg = update.message.text.strip() if update.message.text else ""

    # --- Cancelar ---
    if texto_msg.lower() in ("cancelar", "❌ cancelar"):
        return await _sos_cancelar(update, context)

    # --- Llego ubicacion GPS ---
    if update.message.location:
        lat = update.message.location.latitude
        lon = update.message.location.longitude

        # Validar que las coordenadas esten en Venezuela
        if not _coordenadas_validas(lat, lon):
            await update.message.reply_text(
                "Las coordenadas recibidas no corresponden a Venezuela.\n"
                "Por favor, envia tu ubicacion real."
            )
            return SOS_ESPERANDO_UBICACION

        # Guardar en datos de sesion del usuario
        context.user_data["sos_lat"] = lat
        context.user_data["sos_lon"] = lon

        await update.message.reply_text(
            "Recibido. Ahora, en UN SOLO MENSAJE, escribe que esta pasando.\n"
            "Ejemplo: Piso 3 colapsado, somos 2 personas, 1 herida",
            reply_markup=ReplyKeyboardMarkup([["❌ Cancelar"]], resize_keyboard=True),
        )
        return SOS_ESPERANDO_DESCRIPCION

    # --- Llego texto (sin GPS) ---
    # Si ya paso por el aviso inicial y eligio describir en texto:
    if context.user_data.get("sos_ubicacion_texto_enviado"):
        # El texto que mando es la referencia de ubicacion
        if len(texto_msg) >= 5:
            context.user_data["sos_referencia_texto"] = texto_msg
            await update.message.reply_text(
                "Recibido. Ahora, en UN SOLO MENSAJE, escribe que esta pasando.\n"
                "Ejemplo: Piso 3 colapsado, somos 2 personas, 1 herida",
                reply_markup=ReplyKeyboardMarkup([["❌ Cancelar"]], resize_keyboard=True),
            )
            return SOS_ESPERANDO_DESCRIPCION
        else:
            await update.message.reply_text(
                "Por favor escribe una descripcion mas detallada de tu ubicacion."
            )
            return SOS_ESPERANDO_UBICACION

    # Primera vez sin GPS: mostrar opciones
    context.user_data["sos_ubicacion_texto_enviado"] = True

    teclado_fallback = ReplyKeyboardMarkup(
        [
            [KeyboardButton("Enviar GPS ahora", request_location=True)],
            [KeyboardButton("Describir ubicacion en texto")],
            [KeyboardButton("❌ Cancelar")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.message.reply_text(
        "IMPORTANTE: Activa el GPS / Ubicacion de tu telefono ahora mismo.\n\n"
        "Una vez activado, toca 'Enviar GPS ahora'.\n\n"
        "Si el GPS no funciona, toca 'Describir ubicacion en texto' y escribe:\n"
        "- Nombre de la calle o avenida\n"
        "- Numero de casa o edificio\n"
        "- Barrio o sector\n"
        "- Cerca de que lugar conocido (iglesia, farmacia, escuela)\n"
        "Ejemplo: Av. Lara, Edif. Sol, Piso 3, frente Iglesia Santa Rosa, Barquisimeto",
        reply_markup=teclado_fallback,
    )
    return SOS_ESPERANDO_UBICACION


async def sos_recibir_descripcion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recibe la descripcion textual y guarda el reporte SOS en la DB."""
    texto = update.message.text.strip() if update.message.text else ""

    if texto == "❌ Cancelar":
        return await _sos_cancelar(update, context)

    if len(texto) < 5:
        await update.message.reply_text(
            "Por favor escribe una descripcion mas detallada de la situacion:"
        )
        return SOS_ESPERANDO_DESCRIPCION

    user = update.effective_user
    lat = context.user_data.get("sos_lat")
    lon = context.user_data.get("sos_lon")
    referencia_texto = context.user_data.get("sos_referencia_texto")

    # Guardar en DB y obtener ID del reporte
    try:
        report_id = db.create_sos_report(
            user_id=user.id,
            username=user.username,
            lat=lat,
            lon=lon,
            mensaje=texto,
            fuente="atrapado",
            referencia_texto=referencia_texto,
        )
        context.user_data["sos_report_id"] = report_id
        log_user_activity(user.id, f"SOS_REPORTE_CREADO id={report_id}")
    except Exception as e:
        logger.error(f"Error guardando SOS en DB: {e}")
        await _enviar_critico(
            update,
            "Hubo un error tecnico. Llama al 911 de inmediato.\n"
            "Tu seguridad es lo primero.",
            reply_markup=GUARDIAN_MENU,
        )
        return ConversationHandler.END

    # Poner en queue para reenviar al canal (no bloquea la respuesta)
    report_data = {
        "id": report_id,
        "telegram_user_id": user.id,
        "username": user.username or "anonimo",
        "lat": lat,
        "lon": lon,
        "referencia_texto": referencia_texto,
        "mensaje": texto,
        "timestamp": _formato_hora(),
        "telefono": None,
    }
    await sos_queue.put({"action": "forward_sos", "report": report_data})
    context.user_data["pending_report"] = report_data

    # MEJORA 3 — Mensaje empatico post-reporte
    teclado_telefono = ReplyKeyboardMarkup(
        [
            [KeyboardButton("📞 Compartir mi telefono", request_contact=True)],
            [KeyboardButton("No, gracias")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await _enviar_critico(
        update,
        "Tu alerta fue registrada. Ya viene ayuda.\n\n"
        "Respira. Estas haciendo lo correcto.\n\n"
        "Puedes compartir tu numero para que te llamen?",
        reply_markup=teclado_telefono,
    )
    return SOS_ESPERANDO_TELEFONO


async def sos_recibir_telefono(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recibe opcionalmente el telefono del usuario y cierra el flujo SOS."""
    user = update.effective_user
    report_id = context.user_data.get("sos_report_id")

    # Verificar si compartio contacto o rechazo
    if update.message.contact:
        telefono = update.message.contact.phone_number
        db.update_user_phone(user.id, telefono)

        # Enviar solo una actualizacion rapida al canal, no todo el SOS repetido
        await sos_queue.put({
            "action": "update_phone",
            "report_id": report_id,
            "telefono": telefono
        })

        log_user_activity(user.id, "SOS_TELEFONO_COMPARTIDO")

    # Obtener estado del usuario para mostrar contactos locales
    estado_usuario = _inferir_estado_desde_sesion(context)

    # MEJORA 3 — Confirmacion empatica final
    await _enviar_critico(
        update,
        "Perfecto. Rescatistas notificados.\n\n"
        "Mientras llega la ayuda:\n"
        "- Haz ruido: golpea paredes, grita\n"
        "- Guarda bateria del telefono\n"
        "- No muevas escombros grandes\n"
        "- Si hay polvo, cubrete nariz con tela\n\n"
        "No estas solo. Aguanta.",
        reply_markup=GUARDIAN_MENU,
    )

    # Mostrar directorio de contactos locales
    if estado_usuario:
        await _mostrar_contactos_texto(update, estado_usuario)
    else:
        await _mostrar_menu_estados(update)

    return ConversationHandler.END


async def _sos_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela el flujo SOS y vuelve al menu principal."""
    await update.message.reply_text(
        "Flujo cancelado. Estas en el menu principal.",
        reply_markup=GUARDIAN_MENU,
    )
    return ConversationHandler.END


def _inferir_estado_desde_sesion(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Intenta determinar el estado venezolano desde los datos de sesion."""
    lat = context.user_data.get("sos_lat")
    lon = context.user_data.get("sos_lon")
    # Logica simplificada de geolocalizacion aproximada
    # (para implementacion completa se usaria reverse geocoding)
    if lat and lon:
        if 10.3 <= lat <= 10.6 and -67.1 <= lon <= -66.7:
            return "La Guaira"
        elif 10.4 <= lat <= 10.5 and -67.0 <= lon <= -66.8:
            return "Caracas"
        elif 9.8 <= lat <= 10.5 and -70.5 <= lon <= -68.9:
            return "Miranda"
        elif 9.7 <= lat <= 10.3 and -71.0 <= lon <= -69.5:
            return "Aragua"
        elif 9.5 <= lat <= 10.7 and -70.5 <= lon <= -68.8:
            return "Lara"
    return None


# ---------------------------------------------------------------------------
# Guardian Sismico VE — Flujo 2: ESTA TEMBLANDO AHORA
# ---------------------------------------------------------------------------

async def temblando_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Flujo mas critico en velocidad: responde INMEDIATAMENTE con instrucciones.
    Sin esperar nada del usuario. Respuesta en < 500ms.
    MEJORA 3 — Texto empatico y directo. < 300 chars.
    """
    log_user_activity(update.effective_user.id, "TEMBLANDO_ACTIVO")

    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("Ya paro el temblor", callback_data="ya_paro")]
    ])

    # MEJORA 3 — Instrucciones durante el sismo (< 300 chars, texto plano)
    await _enviar_critico(
        update,
        "PARA. RESPIRA. ESCUCHA.\n\n"
        "1. ALEJATE de ventanas ya\n"
        "2. AGACHATE, cabeza cubierta, agarrate\n"
        "3. En la calle: lejos de postes y paredes\n"
        "4. NO uses ascensores\n\n"
        "Cuando pare, toca aqui",
        reply_markup=teclado,
    )
    return TEMBLANDO_ACTIVO


async def temblando_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Maneja el boton 'Ya paro el temblor' del flujo TEMBLANDO."""
    query = update.callback_query
    await query.answer()

    if query.data != "ya_paro":
        return TEMBLANDO_ACTIVO

    teclado_post = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚨 Necesito rescate", callback_data="sos_post_temblor"),
            InlineKeyboardButton("📞 Contactos", callback_data="contactos_post"),
        ]
    ])

    await query.edit_message_text(
        "Bien. Ahora:\n"
        "• Revisa si hay heridos\n"
        "• Alejate de edificios danados\n"
        "• No enciendas fuego (puede haber gas)\n"
        "• Ten cuidado con replicas\n\n"
        "Necesitas ayuda? 👇",
        reply_markup=teclado_post,
    )
    return POST_TEMBLOR


async def post_temblor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Maneja los botones del menu post-temblor."""
    query = update.callback_query
    await query.answer()

    if query.data == "sos_post_temblor":
        await query.edit_message_text(
            "Voy a ayudarte. Usa el boton 🚨 ESTOY ATRAPADO del menu principal."
        )
    elif query.data == "contactos_post":
        await _mostrar_menu_estados_inline(query)

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Guardian Sismico VE — Flujo 3: Directorio de Contactos
# ---------------------------------------------------------------------------

async def mostrar_contactos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Muestra directorio de contactos. Usa ubicacion GPS si esta disponible."""
    log_user_activity(update.effective_user.id, "DIRECTORIO_CONTACTOS")

    # Intentar usar la ubicacion de sesion
    estado = _inferir_estado_desde_sesion(context)

    if estado:
        await _mostrar_contactos_texto(update, estado)
    else:
        # Sin ubicacion: mostrar menu inline con estados
        await _mostrar_menu_estados(update)

    return ConversationHandler.END


async def _mostrar_contactos_texto(update: Update, estado: str) -> None:
    """Muestra los contactos de un estado en texto plano."""
    try:
        contactos = db.get_contactos_by_estado(estado)

        if not contactos:
            await update.message.reply_text(
                f"No hay contactos registrados para {estado}.\n\n"
                "Contacto nacional: 911"
            )
            return

        lineas = [f"📞 Emergencias — {estado}\n"]
        for c in contactos:
            emoji = {"Bomberos": "🚒", "Proteccion Civil": "🛡️", "Protección Civil": "🛡️",
                     "Cruz Roja": "🏥", "Sismologia": "📡", "Sismología": "📡",
                     "Emergencias": "🆘", "Linea Directa Emergencias": "🆘",
                     "Línea Directa Emergencias": "🆘"}.get(c["tipo"], "📞")
            lineas.append(f"{emoji} {c['nombre_entidad']}")
            lineas.append(f"   Tel: {c['telefono']}\n")

        await update.message.reply_text(
            "\n".join(lineas),
            reply_markup=GUARDIAN_MENU,
        )
    except Exception as e:
        logger.error(f"Error mostrando contactos: {e}")
        await update.message.reply_text("Error al cargar contactos. Llama al 911.")


async def _mostrar_menu_estados(update: Update) -> None:
    """Muestra un menu inline para seleccionar el estado venezolano."""
    botones = []
    fila = []
    for estado in ESTADOS_INLINE:
        fila.append(InlineKeyboardButton(estado, callback_data=f"estado_{estado}"))
        if len(fila) == 2:
            botones.append(fila)
            fila = []
    if fila:
        botones.append(fila)

    teclado = InlineKeyboardMarkup(botones)
    await update.message.reply_text(
        "En cual estado estas? Toca para ver los contactos de emergencia:",
        reply_markup=teclado,
    )


async def _mostrar_menu_estados_inline(query) -> None:
    """Version de _mostrar_menu_estados para usar con callback_query."""
    botones = []
    fila = []
    for estado in ESTADOS_INLINE:
        fila.append(InlineKeyboardButton(estado, callback_data=f"estado_{estado}"))
        if len(fila) == 2:
            botones.append(fila)
            fila = []
    if fila:
        botones.append(fila)

    teclado = InlineKeyboardMarkup(botones)
    await query.edit_message_text(
        "En cual estado estas? Toca para ver los contactos de emergencia:",
        reply_markup=teclado,
    )


# ---------------------------------------------------------------------------
# Guardian Sismico VE — Flujo 4: ESTOY A SALVO
# ---------------------------------------------------------------------------

async def estoy_a_salvo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Marca al usuario como a salvo y notifica al canal de rescatistas."""
    user = update.effective_user
    log_user_activity(user.id, "ESTOY_A_SALVO")

    # Verificar si tiene un reporte SOS activo
    reporte_activo = db.get_user_active_sos(user.id)

    if reporte_activo:
        # Actualizar estado en la DB
        db.resolve_sos_report(reporte_activo["id"])

        # Notificar al canal de rescatistas (en background)
        await sos_queue.put({
            "action": "notify_safe",
            "username": user.username or str(user.id),
        })

        await _enviar_critico(
            update,
            "🟢 Tu reporte ha sido marcado como resuelto.\n"
            "Los rescatistas han sido notificados de que estas a salvo.\n\n"
            "Checklist post-sismo:\n"
            "- Revisa que no haya heridos\n"
            "- Alejate de estructuras danadas\n"
            "- No uses ascensores\n"
            "- Preparate para replicas\n"
            "- Carga tu telefono si puedes",
            reply_markup=GUARDIAN_MENU,
        )
    else:
        # MEJORA 3 — Sin reporte activo: mensaje de alivio empatico
        await _enviar_critico(
            update,
            "Que alivio saber que estas bien.\n\n"
            "Cosas importantes ahora:\n"
            "- Revisa si hay heridos cerca\n"
            "- Alejate de edificios con grietas\n"
            "- No enciendas fuego (puede haber gas)\n"
            "- Carga el telefono cuando puedas\n"
            "- Las replicas pueden durar horas",
            reply_markup=GUARDIAN_MENU,
        )


# ---------------------------------------------------------------------------
# Guardian Sismico VE — Flujo 5: Avisar a mi Familia (Ping de Vida)
# ---------------------------------------------------------------------------

async def avisar_familia_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia el flujo de ping de vida. Solicita ubicacion si no hay una en sesion."""
    log_user_activity(update.effective_user.id, "AVISAR_FAMILIA_inicio")

    lat = context.user_data.get("sos_lat")
    lon = context.user_data.get("sos_lon")

    # Si ya tiene ubicacion en sesion, generar mensaje directamente
    if lat and lon:
        await _generar_ping_vida(update, context, lat, lon)
        return ConversationHandler.END

    # Solicitar ubicacion
    teclado = ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Enviar mi ubicacion", request_location=True)],
            [KeyboardButton("❌ Cancelar")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await _enviar_critico(
        update,
        "Voy a generar un mensaje para que tu familia sepa que estas bien.\n"
        "Primero, envia tu ubicacion:\n"
        "(Toca el boton de abajo ↓)",
        reply_markup=teclado,
    )
    return AVISAR_FAMILIA_UBICACION


async def avisar_familia_ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recibe la ubicacion para el ping de vida."""
    if update.message.text and update.message.text.strip() == "❌ Cancelar":
        await update.message.reply_text(
            "Cancelado. Puedes volver al menu principal.",
            reply_markup=GUARDIAN_MENU,
        )
        return ConversationHandler.END

    if not update.message.location:
        await update.message.reply_text(
            "Por favor usa el boton para enviar tu ubicacion GPS."
        )
        return AVISAR_FAMILIA_UBICACION

    lat = update.message.location.latitude
    lon = update.message.location.longitude

    if not _coordenadas_validas(lat, lon):
        await update.message.reply_text(
            "Las coordenadas no parecen ser de Venezuela. Intenta de nuevo."
        )
        return AVISAR_FAMILIA_UBICACION

    context.user_data["sos_lat"] = lat
    context.user_data["sos_lon"] = lon

    await _generar_ping_vida(update, context, lat, lon)
    return ConversationHandler.END


async def _generar_ping_vida(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lat: float,
    lon: float,
) -> None:
    """
    Genera el mensaje copiable de 'Estoy Bien' para WhatsApp/SMS.
    El usuario debe COPIARLO Y PEGARLO — el bot no lo envia por el.
    """
    user = update.effective_user
    nombre = user.first_name or user.username or "Yo"
    mapa = _maps_link(lat, lon)
    hora = _formato_hora()

    mensaje_copiable = (
        f"ESTOY BIEN\n"
        f"Mi ubicacion: {mapa}\n"
        f"Hora: {hora}\n"
        f"La red esta inestable. No llamen. Solo lean esto.\n"
        f"— {nombre}"
    )

    await _enviar_critico(
        update,
        "COPIA y pega este mensaje en WhatsApp o SMS:\n\n"
        "─────────────────────\n"
        f"{mensaje_copiable}\n"
        "─────────────────────\n\n"
        "(Selecciona el texto de arriba y copialo)",
        reply_markup=GUARDIAN_MENU,
    )


# ---------------------------------------------------------------------------
# Guardian Sismico VE — Flujo 6: Preparacion Sismica
# ---------------------------------------------------------------------------

async def preparacion_sismica(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra menu de preparacion sismica con 3 sub-opciones."""
    log_user_activity(update.effective_user.id, "PREPARACION")

    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Que tener en casa", callback_data="prep_casa")],
        [InlineKeyboardButton("🎒 Kit de emergencia", callback_data="prep_kit")],
        [InlineKeyboardButton("🌀 Despues del sismo", callback_data="prep_post")],
    ])

    await update.message.reply_text(
        "Preparacion Sismica\n\n"
        "Selecciona un tema:",
        reply_markup=teclado,
    )


async def preparacion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Maneja las sub-opciones del menu de preparacion."""
    query = update.callback_query
    await query.answer()

    if query.data == "prep_casa":
        texto = (
            "Que tener listo en casa\n\n"
            "1. Agua: 4 litros por persona por dia (minimo 3 dias)\n"
            "2. Comida no perecedera para 3 dias\n"
            "3. Linterna y pilas de repuesto\n"
            "4. Botiquin basico de primeros auxilios\n"
            "5. Documentos en bolsa impermeable\n"
            "6. Copia de numeros de emergencia en papel\n"
            "7. Punto de encuentro familiar acordado"
        )
    elif query.data == "prep_kit":
        texto = (
            "Kit de emergencia basico\n\n"
            "- Mochila resistente\n"
            "- Agua (1.5L por persona)\n"
            "- Barras energeticas o galletas\n"
            "- Silbato (para pedir ayuda si quedas atrapado)\n"
            "- Guantes de trabajo\n"
            "- Mascarilla N95\n"
            "- Copia de cedula/pasaporte\n"
            "- Dinero en efectivo (bolivares y dolares)"
        )
    elif query.data == "prep_post":
        texto = (
            "Despues del sismo\n\n"
            "1. Revisa si hay heridos — no muevas a alguien con dolor en cuello\n"
            "2. Detecta fugas de gas (olor) — abre ventanas, no enciendas nada\n"
            "3. Corta la electricidad si hay cables caidos\n"
            "4. Sal del edificio si hay danos estructurales visibles\n"
            "5. Mantente lejos de la costa (riesgo de tsunami)\n"
            "6. Escucha radio o senal de emergencia\n"
            "7. Espera replicas — pueden durar dias"
        )
    else:
        return

    await query.edit_message_text(texto)


# ---------------------------------------------------------------------------
# MEJORA 5 — Comando /sos_pendientes para admins
# ---------------------------------------------------------------------------

async def cmd_sos_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Lista los reportes SOS activos. Acceso restringido a ADMIN_IDS si esta configurado.
    Si ADMIN_IDS esta vacio, cualquier usuario puede consultarlo (modo sin restriccion).
    """
    user_id = update.effective_user.id

    # Verificar permisos
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("Sin permisos.")
        return

    reportes = db.get_active_sos_reports()

    if not reportes:
        await update.message.reply_text("No hay SOS activos.")
        return

    # Maximo 10 reportes en el mensaje
    total = len(reportes)
    mostrar = reportes[:10]
    hora = _formato_hora()

    lineas = [f"🆘 SOS activos ({total}) — {hora}\n"]
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
        lineas.append(f"\n... y {total - 10} mas.")

    await update.message.reply_text("\n".join(lineas))


# ---------------------------------------------------------------------------
# MEJORA 5 — Comando /resolver mejorado (acepta /resolver_id y /resolver id)
# ---------------------------------------------------------------------------

async def cmd_resolver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Marca un reporte SOS como atendido.
    Acepta:
      - /resolver_12345  (formato original con guion bajo)
      - /resolver 12345  (formato con espacio, usando context.args)
    """
    report_id = None

    # Intentar extraer el ID de context.args (formato /resolver 12345)
    if context.args:
        try:
            report_id = int(context.args[0])
        except (ValueError, IndexError):
            pass

    # Si no funciono, intentar del texto crudo (formato /resolver_12345)
    if report_id is None:
        try:
            texto = update.message.text or ""
            # Buscar numero despues de /resolver_ o /resolver
            partes = texto.strip().split("_")
            if len(partes) >= 2:
                report_id = int(partes[-1].split()[0])
        except (ValueError, IndexError):
            pass

    if report_id is None:
        await update.message.reply_text(
            "Formato invalido.\n"
            "Usa: /resolver_12345\n"
            "  o: /resolver 12345"
        )
        return

    resuelto = db.resolve_sos_report(report_id)

    if resuelto:
        hora = _formato_hora()
        texto_resp = f"🟢 Reporte #{report_id} marcado como RESUELTO — {hora}"
        await update.message.reply_text(texto_resp)

        # Notificar tambien al canal si hay configurado
        if RESCUE_CHANNEL_ID:
            try:
                await context.bot.send_message(
                    chat_id=int(RESCUE_CHANNEL_ID),
                    text=texto_resp,
                )
            except Exception as e:
                logger.error(f"Error notificando resolucion al canal: {e}")
    else:
        await update.message.reply_text(
            f"El reporte #{report_id} ya fue resuelto o no existe."
        )


# ---------------------------------------------------------------------------
# Health Check HTTP (para Railway/Render)
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    """Responde 200 OK en GET / para que Railway sepa que el proceso esta vivo."""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK - Guardian Sismico VE")

    def log_message(self, format, *args):  # silenciar logs HTTP
        pass


def _start_health_server(port: int = 8080) -> None:
    """Arranca el servidor de health check en un hilo daemon (no bloquea el bot)."""
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health check escuchando en :{port}")


# ---------------------------------------------------------------------------
# Manejo de botones de rescate en el canal (Modificacion 4)
# ---------------------------------------------------------------------------

async def rescate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Maneja los botones que presionan los rescatistas en el canal privado."""
    query = update.callback_query
    await query.answer()

    # Extraer datos del callback
    partes = query.data.split("_")
    if len(partes) != 4:
        return

    _, accion, report_id, victima_id = partes
    rescater = update.effective_user.username or update.effective_user.first_name

    msg_victima = ""
    msg_canal = ""

    if accion == "visto":
        msg_victima = f"👀 Rescatistas han VISTO tu reporte #{report_id}. Mantén la calma."
        msg_canal = f"👀 @{rescater} está revisando el SOS #{report_id}."
    elif accion == "camino":
        msg_victima = f"🚑 ¡Mantente a salvo! Ayuda EN CAMINO para tu emergencia #{report_id}."
        msg_canal = f"🚑 @{rescater} va EN CAMINO al SOS #{report_id}."
    elif accion == "cerrar":
        db.resolve_sos_report(int(report_id))
        msg_victima = f"✅ Tu emergencia #{report_id} ha sido marcada como resuelta por los rescatistas."
        msg_canal = f"✅ @{rescater} CERRÓ el SOS #{report_id}."
        await query.edit_message_reply_markup(reply_markup=None)

    # 1. Enviar alerta directa a la victima
    try:
        await context.bot.send_message(chat_id=int(victima_id), text=msg_victima)
    except Exception as e:
        logger.error(f"No se pudo avisar a la víctima {victima_id}: {e}")

    # 2. Avisar en el grupo de rescate que está pasando
    try:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=msg_canal,
            reply_to_message_id=query.message.message_id
        )
    except Exception as e:
        logger.error(f"No se pudo actualizar el canal: {e}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN no esta configurado. Crea un archivo .env con tu token.")

    db.init_db()
    logger.info("Base de datos inicializada.")

    # Health check en puerto 8080
    _start_health_server(port=int(os.getenv("PORT", 8080)))

    # Construir la aplicacion con timeouts explicitos
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(10)
        .build()
    )

    # Arrancar el worker de la queue SOS como tarea de fondo
    async def on_startup(application: Application) -> None:
        asyncio.create_task(_sos_worker(application))
        logger.info("Worker de queue SOS iniciado.")

    app.post_init = on_startup

    # Throttle general (group=-1, corre antes que todos los handlers)
    throttle = ThrottleDict(rate_limit=1.5)

    async def _throttle_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and throttle.is_limited(update.effective_user.id):
            return

    app.add_handler(MessageHandler(filters.ALL, _throttle_check), group=-1)

    # -----------------------------------------------------------------------
    # ConversationHandler — BuscaVenezuela (busqueda de desaparecidos)
    # -----------------------------------------------------------------------
    conv_busqueda = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_BUSCAR}$"), iniciar_busqueda),
            CommandHandler("vigilar", iniciar_busqueda),
        ],
        states={
            ESPERANDO_NOMBRE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nombre)
            ],
            ESPERANDO_ESTADO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_estado)
            ],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Cancelar$"), cancelar_conversacion),
            CommandHandler("salir", cancelar_conversacion),
        ],
    )

    # -----------------------------------------------------------------------
    # ConversationHandler — Guardian Sismico VE: flujo SOS (ATRAPADO)
    # MEJORA 1: añade handler para "Describir ubicacion en texto"
    # -----------------------------------------------------------------------
    conv_sos = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_ATRAPADO}$"), sos_inicio),
        ],
        states={
            SOS_ESPERANDO_UBICACION: [
                MessageHandler(filters.LOCATION, sos_recibir_ubicacion),
                MessageHandler(
                    filters.Regex("^Describir ubicacion en texto$"),
                    _sos_pedir_referencia_texto
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sos_recibir_ubicacion),
            ],
            SOS_ESPERANDO_DESCRIPCION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sos_recibir_descripcion),
            ],
            SOS_ESPERANDO_TELEFONO: [
                MessageHandler(filters.CONTACT, sos_recibir_telefono),
                MessageHandler(filters.Regex("^No, gracias$"), sos_recibir_telefono),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sos_recibir_telefono),
            ],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Cancelar$"), _sos_cancelar),
            CommandHandler("cancelar", _sos_cancelar),
        ],
        per_message=False,
    )

    # -----------------------------------------------------------------------
    # ConversationHandler — Guardian Sismico VE: ESTA TEMBLANDO
    # -----------------------------------------------------------------------
    conv_temblando = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_TEMBLANDO}$"), temblando_inicio),
        ],
        states={
            TEMBLANDO_ACTIVO: [
                CallbackQueryHandler(temblando_callback, pattern="^ya_paro$"),
            ],
            POST_TEMBLOR: [
                CallbackQueryHandler(post_temblor_callback, pattern="^(sos_post_temblor|contactos_post)$"),
            ],
        },
        fallbacks=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, temblando_inicio),
        ],
        per_message=False,
    )

    # -----------------------------------------------------------------------
    # ConversationHandler — Guardian Sismico VE: Avisar a mi Familia
    # -----------------------------------------------------------------------
    conv_avisar = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_AVISAR}$"), avisar_familia_inicio),
        ],
        states={
            AVISAR_FAMILIA_UBICACION: [
                MessageHandler(filters.LOCATION, avisar_familia_ubicacion),
                MessageHandler(filters.TEXT & ~filters.COMMAND, avisar_familia_ubicacion),
            ],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Cancelar$"), _sos_cancelar),
        ],
        per_message=False,
    )

    # -----------------------------------------------------------------------
    # Registrar todos los handlers
    # -----------------------------------------------------------------------

    # Comandos globales
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("mis_busquedas", mis_busquedas))
    app.add_handler(CommandHandler("encontrado", ya_lo_encontre))

    # MEJORA 5 — Comandos de gestion SOS
    app.add_handler(CommandHandler("sos_pendientes", cmd_sos_pendientes))
    app.add_handler(CommandHandler("resolver", cmd_resolver))  # /resolver 12345

    # Comando /resolver_<id> para rescatistas (formato con guion bajo)
    app.add_handler(MessageHandler(
        filters.Regex(r"^/resolver_\d+"),
        cmd_resolver,
    ))

    # ConversationHandlers (orden importa: mas especificos primero)
    app.add_handler(conv_sos)
    app.add_handler(conv_temblando)
    app.add_handler(conv_avisar)
    app.add_handler(conv_busqueda)

    # Botones del menu Guardian Sismico (no son conversaciones largas)
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_CONTACTOS}$"), mostrar_contactos))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SALVO}$"), estoy_a_salvo))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_PREPARACION}$"), preparacion_sismica))

    # Botones del menu original BuscaVenezuela
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_MIS_BUSQUEDAS}$"), mis_busquedas))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_ENCONTRADO}$"), ya_lo_encontre))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_AYUDA}$"), ayuda))

    # Callback unificado para botones inline
    app.add_handler(CallbackQueryHandler(preparacion_callback, pattern="^prep_"))
    app.add_handler(CallbackQueryHandler(rescate_callback, pattern="^resc_"))
    app.add_handler(CallbackQueryHandler(boton_callback))

    # -----------------------------------------------------------------------
    # Modo de ejecucion: polling (dev) o webhook (produccion)
    # -----------------------------------------------------------------------
    logger.info(f"Iniciando Guardian Sismico VE en modo '{ENVIRONMENT}'...")

    if ENVIRONMENT == "production" and WEBHOOK_URL:
        logger.info(f"Modo webhook: {WEBHOOK_URL}/{BOT_TOKEN} en puerto {WEBHOOK_PORT}")
        app.run_webhook(
            listen="0.0.0.0",
            port=WEBHOOK_PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        )
    else:
        logger.info("Modo polling (desarrollo local).")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )


if __name__ == "__main__":
    main()