# Guardian Sismico VE

Bot de Telegram para emergencias sismicas en Venezuela.
Funciona con señal debil, baja bateria y usuarios sin experiencia tecnica.

## Que hace

- Boton de panico: envia ubicacion GPS o referencia de texto a rescatistas
- Instrucciones durante el sismo: respuesta instantanea
- Directorio de emergencias por estado venezolano
- Boton a salvo: notifica que la emergencia paso
- Ping de vida: genera mensaje copiable para WhatsApp o SMS
- Guia de preparacion sismica
- Busqueda de familiares desaparecidos (funcionalidad BuscaVenezuela)

## Usar el bot

Busca tu bot en Telegram y toca /start

## Instalar y correr

Requisitos: Python 3.10+, bot de Telegram via @BotFather, grupo/canal de rescatistas

```bash
git clone https://github.com/TU_USUARIO/guardiansismico-ve
cd guardiansismico-ve
python -m venv .venv
source .venv/bin/activate      # Linux/Mac
# .venv\Scripts\activate       # Windows
pip install -r requirements.txt
cp .env.example .env
# Editar .env con tu BOT_TOKEN y RESCUE_CHANNEL_ID
python bot.py
```

## Variables de entorno

Ver `.env.example` para la lista completa.

| Variable | Descripcion | Requerida |
|---|---|---|
| BOT_TOKEN | Token de @BotFather | Si |
| RESCUE_CHANNEL_ID | ID negativo del canal de rescatistas | Recomendada |
| DB_PATH | Ruta a la base de datos SQLite | No (default: buscavenezuela.db) |
| ENVIRONMENT | development o production | No (default: development) |
| WEBHOOK_URL | URL publica HTTPS para produccion | Solo en produccion |
| WEBHOOK_PORT | Puerto del webhook | No (default: 8443) |
| ADMIN_IDS | IDs de Telegram con acceso a /sos_pendientes | No |

## Comandos del bot

| Comando | Quien lo usa | Descripcion |
|---|---|---|
| /start | Usuario | Menu principal |
| /sos_pendientes | Rescatistas/Admins | Lista de SOS activos |
| /resolver_ID | Rescatistas | Marca SOS como atendido |
| /resolver ID | Rescatistas | Alternativa con espacio |
| /vigilar | Usuario | Inicia busqueda de familiar |

## Contribuir

1. Haz fork del repositorio
2. Crea una rama: `git checkout -b mi-mejora`
3. Abre un Pull Request

Mejoras que necesitamos:
- Mas contactos de emergencia por municipio
- Reverse geocoding local sin API externa
- Integracion con RSS de FUNVISIS
- Traduccion al ingles de la documentacion
- Tests automatizados para flujos criticos

## Arquitectura

```
bot.py       — Conversaciones Telegram (python-telegram-bot v22)
db.py        — SQLite con WAL mode y migraciones idempotentes
middleware.py — Rate limiting y throttle
matcher.py   — Fuzzy matching para busqueda de nombres
alerter.py   — Jobs en segundo plano y canal de rescatistas
```

## Capacidad

Con WAL mode y asyncio queue, maneja 1000+ usuarios concurrentes
en un VPS de 1GB RAM. Probado con el stack de Guardian Sismico VE.

Caracteristicas de resiliencia:
- Retry automatico al canal de rescatistas (3 intentos, backoff exponencial)
- Flujo SOS sin GPS: acepta descripcion textual de ubicacion
- Rate limiting para prevenir spam en flujos de emergencia
- Mensajes ultra-ligeros para señal debil o datos limitados

## Licencia

MIT
