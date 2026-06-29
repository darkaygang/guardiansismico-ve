# 🌋 Guardian Sísmico VE

Bot de Telegram diseñado para emergencias sísmicas y de infraestructura en Venezuela.
Optimizado para funcionar con **señal móvil débil, baja batería y usuarios sin experiencia técnica**.

## 🛟 ¿Qué hace?

- 🚨 **Botón de pánico:** Envía ubicación GPS exacta o referencia de texto a grupos de rescatistas.
- ⚠️ **Instrucciones durante el sismo:** Respuesta instantánea, directa y al grano.
- 📞 **Directorio de emergencias:** Números locales filtrados por estado venezolano.
- ✅ **Botón de "A salvo":** Notifica que la emergencia pasó para liberar recursos de rescate.
- 📍 **Ping de vida:** Genera un mensaje pre-armado con coordenadas y hora para copiar y pegar en WhatsApp o SMS.
- ℹ️ **Guía de preparación:** Tips rápidos de prevención y supervivencia.
- 🔍 **Búsqueda (BuscaVenezuela):** Funcionalidad integrada para rastreo de familiares desaparecidos.

## 📲 Usar el bot

Busca tu bot en Telegram y simplemente toca `/start` para desplegar el menú principal.

## 🛠️ Instalar y correr

**Requisitos:** Python 3.10+, un bot de Telegram (vía `@BotFather`), y un grupo/canal privado de rescatistas.

```bash
git clone [https://github.com/TU_USUARIO/guardiansismico-ve](https://github.com/TU_USUARIO/guardiansismico-ve)
cd guardiansismico-ve
python -m venv .venv
source .venv/bin/activate      # Linux/Mac
# .venv\Scripts\activate       # Windows
pip install -r requirements.txt
cp .env.example .env
# Editar .env con tu BOT_TOKEN y RESCUE_CHANNEL_ID
python bot.py
⚙️ Variables de entornoRevisa el archivo .env.example para la lista completa.VariableDescripciónRequeridaBOT_TOKENToken otorgado por @BotFatherSíRESCUE_CHANNEL_IDID (negativo) del canal/grupo de rescatistasRecomendadaDB_PATHRuta a la base de datos SQLiteNo (default: buscavenezuela.db)ENVIRONMENTdevelopment o productionNo (default: development)WEBHOOK_URLURL pública HTTPS para producciónSolo en producciónWEBHOOK_PORTPuerto del webhookNo (default: 8443)ADMIN_IDSIDs de Telegram con acceso a /sos_pendientesNo⌨️ Comandos del botComandoQuién lo usaDescripción/startUsuarioDespliega el menú principal/sos_pendientesRescatistas/AdminsLista de reportes SOS activos en tiempo real/resolver_IDRescatistasMarca un SOS como atendido directamente desde el grupo/resolver IDRescatistasAlternativa con espacio/vigilarUsuarioInicia búsqueda de un familiar desaparecido🤝 Código Abierto y Cómo Contribuir¿Es Open Source? ¡Totalmente! 🔓Este código es 100% libre. Puedes clonarlo, usarlo, modificarlo y adaptarlo para tu comunidad. Si tienes conocimientos de programación y quieres añadirle nuevas funciones, ¡todas las ideas y mejoras son más que bienvenidas!Haz fork del repositorio.Crea una rama nueva: git checkout -b mi-mejoraTrabaja tu magia y abre un Pull Request.Ideas en las que nos puedes ayudar:📍 Más contactos de emergencia detallados por municipio.🗺️ Reverse geocoding local sin depender de APIs externas.📡 Integración con alertas RSS oficiales de FUNVISIS.🇺🇸 Traducción al inglés de la documentación.🧪 Tests automatizados para los flujos críticos.🏗️ ArquitecturaPlaintextbot.py         — Conversaciones y flujos Telegram (python-telegram-bot v22)
db.py          — SQLite optimizado con WAL mode y migraciones idempotentes
middleware.py  — Rate limiting y anti-spam (throttle)
matcher.py     — Fuzzy matching para búsqueda difusa de nombres
alerter.py     — Jobs en segundo plano y conexión con el canal de rescatistas
⚡ Capacidad y ResilienciaCon WAL mode activado en SQLite y el uso de asyncio queues, el bot está diseñado para manejar 1000+ usuarios concurrentes corriendo en un VPS modesto de tan solo 1GB de RAM.Características de resiliencia:Retry automático: 3 intentos con backoff exponencial si la conexión con los servidores falla.Flujo SOS sin GPS: Si la señal colapsa, acepta descripciones textuales de ubicación.Rate limiting: Previene spam o ataques DDoS en el botón de pánico.Ultra-ligero: Los mensajes se fragmentan inteligentemente para fluir a través de redes inestables o conexiones 2G.👥 CréditosDesarrollado y estructurado inicialmente por Darkay.El objetivo de este repositorio no es la gloria personal, sino aportar una herramienta real y funcional a la comunidad. El verdadero crédito es para todos los voluntarios, rescatistas y desarrolladores que trabajan en el terreno para proteger a otros.📄 LicenciaMITAunque el sol se oculte, la esperanza es la luz que nos guía a un nuevo amanecer.