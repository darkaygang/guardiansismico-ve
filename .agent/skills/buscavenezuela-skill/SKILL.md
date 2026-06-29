# SKILL: Guardian Sísmico VE — Bot de Pánico y Triaje de Emergencia

## Contexto del Proyecto

**Proyecto:** `buscavenezuela` (ya existente en el workspace)
**Propósito:** Transformar el bot de Telegram existente en un sistema de triaje de emergencia sísmica con botón de pánico, directorio de emergencias localizado y canal privado de rescatistas.
**Stack existente:** Python 3.14, python-telegram-bot v22, SQLite (db.py), fuzzy matching (matcher.py), alerter.py + GitHub Actions, middleware.py, scraper.py (no usar).

---

## Estructura del Proyecto (Ya Existente)

```
buscavenezuela/
├── __pycache__/
├── .agent/
├── .github/workflows/
│   └── alerter.yml
├── .venv/
├── venv/
├── .env
├── .env.example
├── alerter.py          ← MODIFICAR: añadir job de reenvío de SOS
├── bot.py              ← MODIFICAR PRINCIPAL: flujos de emergencia
├── buscavenezuela.db   ← MODIFICAR: nuevas tablas
├── db.py               ← MODIFICAR: nuevas tablas y WAL mode
├── matcher.py          ← REUTILIZAR: normalización de ISPs/bancos/zonas
├── middleware.py       ← MODIFICAR: añadir rate limiting
├── README.md
├── requirements.txt    ← ACTUALIZAR si se necesitan nuevas deps
└── scraper.py          ← NO TOCAR
```

---

## Nuevas Tablas SQLite (en db.py)

### Activar WAL Mode (CRÍTICO para concurrencia)
```python
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA synchronous=NORMAL;")
conn.execute("PRAGMA cache_size=10000;")
```

### Tabla: sos_reportes
```sql
CREATE TABLE IF NOT EXISTS sos_reportes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL,
    username TEXT,
    lat REAL,
    lon REAL,
    mensaje TEXT,
    estado TEXT DEFAULT 'activo',  -- 'activo', 'resuelto', 'falsa_alarma'
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    fuente TEXT  -- 'atrapado', 'temblando', 'ping_vida'
);
```

### Tabla: contactos_emergencia
```sql
CREATE TABLE IF NOT EXISTS contactos_emergencia (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    estado_ven TEXT NOT NULL,  -- 'Lara', 'Caracas', 'Miranda', etc.
    municipio TEXT,
    nombre_entidad TEXT NOT NULL,
    telefono TEXT NOT NULL,
    tipo TEXT,  -- 'Protección Civil', 'Bomberos', 'Cruz Roja', 'ONG'
    activo INTEGER DEFAULT 1
);
```

### Tabla: rate_limit
```sql
CREATE TABLE IF NOT EXISTS rate_limit (
    user_id INTEGER PRIMARY KEY,
    count INTEGER DEFAULT 0,
    window_start DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### Tabla: usuarios
```sql
CREATE TABLE IF NOT EXISTS usuarios (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    nombre TEXT,
    telefono TEXT,  -- NULL hasta que lo comparta voluntariamente
    estado_ven TEXT,  -- Estado venezolano (Lara, Miranda, etc.)
    timestamp_registro DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## Datos Iniciales: Contactos de Emergencia

Insertar en la tabla `contactos_emergencia` al inicializar la DB:

```python
CONTACTOS_INICIALES = [
    # Lara / Barquisimeto
    ("Lara", "Barquisimeto", "Protección Civil Lara", "0251-231.0011", "Protección Civil"),
    ("Lara", "Barquisimeto", "Bomberos Barquisimeto", "0251-231.8028", "Bomberos"),
    ("Lara", "Barquisimeto", "Cruz Roja Lara", "0251-253.4028", "Cruz Roja"),
    # Caracas / Distrito Capital
    ("Caracas", "Libertador", "Protección Civil DC", "0212-483.7777", "Protección Civil"),
    ("Caracas", "Libertador", "Bomberos Caracas", "0212-484.6666", "Bomberos"),
    # Miranda
    ("Miranda", None, "Protección Civil Miranda", "0212-272.7000", "Protección Civil"),
    # Nacionales
    ("Nacional", None, "FUNVISIS (Fundación Venezolana de Investigaciones Sísmicas)", "0212-257.5897", "Sismología"),
    ("Nacional", None, "Emergencias Venezuela", "911", "Emergencias"),
]
```

---

## Flujos del Bot (bot.py)

### Menú Principal
```
🌋 Guardian Sísmico VE
────────────────────────
🚨 ESTOY ATRAPADO
⚠️ ESTÁ TEMBLANDO AHORA
📞 Contactos de Emergencia
✅ ESTOY A SALVO
📍 Avisar a mi Familia
ℹ️ Preparación Sísmica
```

### Flujo 1: "🚨 ESTOY ATRAPADO" (SOS)

**Pasos:**
1. Usuario presiona botón → Bot responde con teclado nativo `KeyboardButton(request_location=True)`
   - Mensaje: "📍 Envía tu ubicación para que los rescatistas sepan dónde estás.\n(Toca el botón de abajo ↓)"
2. Bot recibe `location` → Guarda `lat` y `lon` temporalmente en memoria de conversación
   - Respuesta: "📝 Recibido. Ahora, en UN SOLO MENSAJE, escribe qué está pasando.\nEjemplo: 'Piso 3 colapsado, somos 2 personas, 1 herida'"
3. Bot recibe texto → Guarda en `sos_reportes` con estado='activo'
   - Respuesta: "🆘 ALERTA REGISTRADA. Rescatistas han sido notificados.\n\n¿Puedes compartir tu teléfono para que puedan llamarte?"
   - Botón: `KeyboardButton(request_contact=True)` + botón "No, gracias"
4. Opcional: Usuario comparte contacto → Guarda teléfono en `usuarios`
5. Bot muestra directorio de emergencias según ubicación GPS (o pregunta estado)
6. `alerter.py` reenvía el reporte al canal de rescatistas

**Formato del reporte al canal de rescatistas:**
```
🆘 NUEVO REPORTE SOS
─────────────────────
👤 @username (ID: 12345678)
📍 Ubicación: 10.0678, -69.3467
🗺️ [Ver en mapa](https://maps.google.com/?q=10.0678,-69.3467)
📝 "Piso 3 colapsado, somos 2 personas, 1 herida"
📞 Teléfono: +58412XXXXXXX (si lo compartió)
🕐 16:42:03 — 28 Jun 2026
─────────────────────
/resolver_12345 para marcar como atendido
```

### Flujo 2: "⚠️ ESTÁ TEMBLANDO AHORA"

**Respuesta instantánea (un solo mensaje, sin esperas):**
```
🚨 ¡MANTÉN LA CALMA!

1️⃣ ALÉJATE de ventanas y estanterías
2️⃣ AGÁCHATE, cúbrete la cabeza, agárrate
3️⃣ Si estás afuera: aléjate de postes y edificios
4️⃣ NO uses ascensores

El bot está contigo. Cuando pare, toca aquí 👇
[✅ Ya paró el temblor]
```

Cuando presiona "Ya paró el temblor":
```
Bien. Ahora:
• Revisa si hay heridos
• Aléjate de edificios dañados
• No enciendas fuego (puede haber gas)
• Ten cuidado con réplicas

¿Necesitas ayuda? 👇
[🚨 Necesito rescate]  [📞 Contactos de emergencia]
```

### Flujo 3: "📞 Contactos de Emergencia"

1. Si ya tiene ubicación GPS en sesión → usa `lat/lon` para determinar estado
2. Si no → teclado inline con estados venezolanos más poblados
3. Devuelve lista de contactos de `contactos_emergencia` para ese estado
4. Formato: emoji + nombre + número (texto plano, no markdown pesado)

### Flujo 4: "✅ ESTOY A SALVO"

1. Si tiene reporte SOS activo → actualiza `estado` a 'resuelto' en `sos_reportes`
2. Reenvía al canal: "🟢 Resuelto: @username ya está a salvo — 16:55"
3. Si no tiene reporte activo → responde mensaje de alivio + checklist post-sismo

### Flujo 5: "📍 Avisar a mi Familia"

1. Solicita ubicación con `KeyboardButton(request_location=True)` (o usa la que ya tiene)
2. Genera texto pre-armado para copiar/pegar:
```
ESTOY BIEN ✅
Mi ubicación: https://maps.google.com/?q=10.0678,-69.3467
Hora: 16:40 — 28 Jun 2026
La red está inestable. No llamen. Solo lean esto.
— [Nombre o @username]
```
3. El usuario lo copia y pega en WhatsApp o SMS

### Flujo 6: "ℹ️ Preparación Sísmica"

Menú con 3 sub-opciones simples (texto plano, bajo consumo de datos):
- "Qué tener listo en casa" → checklist de 7 ítems
- "Kit de emergencia básico" → lista compacta
- "Después del sismo" → pasos post-evento

---

## Rate Limiting en middleware.py

Implementar en `middleware.py` como decorator o handler intermedio:

```python
RATE_LIMIT_WINDOW = 10  # segundos
RATE_LIMIT_MAX = 3       # máximo 3 alertas SOS en la ventana

async def check_rate_limit(user_id: int, db_conn) -> bool:
    """Retorna True si está dentro del límite, False si excede."""
    # Limpiar ventanas antiguas y contar en ventana actual
    # Si user_id envió > RATE_LIMIT_MAX en RATE_LIMIT_WINDOW segundos → return False
    pass
```

Aplicar solo a las acciones de SOS (no al menú general ni a las instrucciones).

---

## Webhook vs Polling

En el VPS (Ubuntu/Debian con Nginx):

### Configuración en bot.py
```python
# PRODUCCIÓN: Usar webhook
application.run_webhook(
    listen="0.0.0.0",
    port=8443,
    url_path=BOT_TOKEN,
    webhook_url=f"https://tu-dominio.com/{BOT_TOKEN}"
)

# DESARROLLO LOCAL: Usar polling
application.run_polling()
```

Detectar ambiente con `os.getenv("ENVIRONMENT", "development")`.

### Asyncio Queue para operaciones pesadas
```python
import asyncio

sos_queue = asyncio.Queue()

async def process_sos_worker(queue: asyncio.Queue):
    while True:
        task = await queue.get()
        await save_to_db(task)
        await forward_to_rescue_channel(task)
        queue.task_done()
```

El handler del bot pone el trabajo en la queue y responde "Recibido ✅" de inmediato al usuario.

---

## Variables de Entorno (.env)

Añadir a `.env.example`:
```
BOT_TOKEN=your_telegram_bot_token
RESCUE_CHANNEL_ID=-100XXXXXXXXXX  # ID del canal/grupo de rescatistas (negativo para grupos)
ENVIRONMENT=development  # 'development' (polling) o 'production' (webhook)
WEBHOOK_URL=https://tu-dominio.com  # Solo para producción
WEBHOOK_PORT=8443
```

---

## Orden de Implementación (MVP)

### Fase 1 — Base de datos y estructura
1. Modificar `db.py`:
   - Añadir WAL mode pragma
   - Crear tablas: `sos_reportes`, `contactos_emergencia`, `rate_limit`, `usuarios`
   - Función `seed_contactos()` con los datos iniciales
   - Función `get_contactos_by_estado(estado: str)`
   - Función `create_sos_report(user_id, username, lat, lon, mensaje, fuente)`
   - Función `resolve_sos_report(report_id)`

### Fase 2 — Middleware y seguridad
2. Modificar `middleware.py`:
   - Añadir `check_rate_limit(user_id)` usando la tabla `rate_limit`
   - Añadir `get_or_create_user(user_id, username)`

### Fase 3 — Lógica del bot
3. Modificar `bot.py`:
   - Implementar ConversationHandler para flujo SOS (3 estados: ESPERANDO_UBICACION, ESPERANDO_DESCRIPCION, ESPERANDO_TELEFONO)
   - Implementar handler para "ESTÁ TEMBLANDO"
   - Implementar handler para directorio de contactos
   - Implementar handler para "ESTOY A SALVO"
   - Implementar handler para "Avisar a mi familia" (ping de vida)
   - Implementar menú de preparación sísmica (texto plano, sin imágenes)
   - Añadir `asyncio.Queue` para procesamiento en background
   - Añadir detección de ambiente (polling vs webhook)

### Fase 4 — Alertas automáticas
4. Modificar `alerter.py`:
   - Añadir función `forward_sos_to_channel(report: dict)` que envía al `RESCUE_CHANNEL_ID`
   - Añadir comando `/resolver_{id}` que pueda recibir el canal de rescatistas para marcar como resuelto
   - Añadir job periódico (cada 5 min) para consolidar reportes activos y enviar resumen al canal

### Fase 5 — Ajustes finales
5. Actualizar `requirements.txt` con nuevas dependencias si las hay
6. Actualizar `README.md` con instrucciones de despliegue en VPS
7. Actualizar `.github/workflows/alerter.yml` si aplica

---

## Consideraciones de UX (Críticas)

- **Cero markdown pesado en emergencias**: No usar `**bold**`, `_italic_` ni tablas en los mensajes de crisis. Solo texto plano y emojis simples.
- **Máximo 3 botones visibles a la vez** en teclados inline durante crisis.
- **Respuesta "Recibido" siempre instantánea**: El usuario debe ver confirmación en menos de 1 segundo. El trabajo real va a la queue.
- **Mensajes cortos**: Máximo 4-5 líneas por mensaje en flujos de emergencia.
- **Sin imágenes ni stickers en flujos de crisis**: Consumen demasiados datos y batería.
- **Flujo de SOS debe funcionar con solo 3 interacciones** (ubicación, descripción, confirmación).
- **El bot siempre responde**, incluso si hay error: nunca dejar al usuario en silencio durante una emergencia.

---

## Archivos a NO Modificar

- `scraper.py` — frágil, no relevante para este proyecto
- `.venv/` — entorno virtual existente
- `.github/workflows/alerter.yml` — solo si Fase 4 lo requiere explícitamente

---

## Testing Manual Recomendado

Antes de desplegar en VPS:
1. Simular flujo SOS completo: ubicación falsa → descripción → teléfono → confirmar reporte en canal
2. Simular "ESTÁ TEMBLANDO" y verificar velocidad de respuesta (< 500ms)
3. Simular rate limiting: enviar 4 SOS en 10 segundos con el mismo user_id
4. Simular "ESTOY A SALVO" y verificar actualización del estado en SQLite
5. Verificar que el bot responde correctamente con datos móviles lentos (modo avión + WiFi débil)

