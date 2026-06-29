"""
db.py — Capa de base de datos SQLite para BuscaVenezuela Bot.
Tablas originales:
  - watches         : búsquedas activas por familia
  - alertas_enviadas: historial de alertas ya notificadas (deduplicación por hash)

Tablas Guardian Sísmico VE (nuevas):
  - sos_reportes        : reportes de emergencia en tiempo real
  - contactos_emergencia: directorio de entidades de emergencia por estado
  - rate_limit          : control de flood en acciones SOS
  - usuarios            : perfil básico de usuarios del bot
"""

import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "buscavenezuela.db")

# Contactos de emergencia iniciales (estado, municipio, nombre, telefono, tipo)
CONTACTOS_INICIALES = [
    # La Guaira (Zona Cero)
    ("La Guaira", "Vargas", "0800-RESCATE", "0800-7372283", "Emergencias"),
    ("La Guaira", "Vargas", "Protección Civil La Guaira", "0212-332.2464", "Protección Civil"),
    # Lara / Barquisimeto
    ("Lara", "Barquisimeto", "Protección Civil Lara", "0251-231.0011", "Protección Civil"),
    ("Lara", "Barquisimeto", "Bomberos Barquisimeto", "0251-231.8028", "Bomberos"),
    # Caracas / Distrito Capital
    ("Caracas", "Libertador", "0800-RESCATE", "0800-7372283", "Emergencias"),
    ("Caracas", "Libertador", "Protección Civil DC", "0212-483.7777", "Protección Civil"),
    # Nacionales
    ("Nacional", None, "0800-RESCATE", "0800-7372283", "Línea Directa Emergencias"),
    ("Nacional", None, "Emergencias Venezuela", "911", "Emergencias"),
    ("Nacional", None, "FUNVISIS", "0212-257.5897", "Sismología"),
]


def _get_conn() -> sqlite3.Connection:
    """
    Abre una conexión SQLite optimizada para carga concurrente.

    Pragmas críticos para producción con múltiples usuarios:
    - WAL:           lecturas y escrituras no se bloquean entre sí
    - synchronous=NORMAL: 3x más rápido que FULL, suficientemente seguro
    - cache_size:    64 MB de cache en RAM (evita I/O innecesario)
    - temp_store:    tablas temporales en RAM (más velocidad)
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size=-64000;")   # 64 MB de cache en RAM
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Inicialización
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Crea todas las tablas si no existen. Seguro para llamar múltiples veces."""
    with _get_conn() as conn:
        # Tablas originales de BuscaVenezuela
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS watches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                nombre      TEXT    NOT NULL,
                estado      TEXT    NOT NULL DEFAULT 'activo',
                created_at  TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_watches_chat_id
                ON watches (chat_id);

            CREATE INDEX IF NOT EXISTS idx_watches_estado
                ON watches (estado);

            CREATE TABLE IF NOT EXISTS alertas_enviadas (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id        INTEGER NOT NULL
                                    REFERENCES watches(id) ON DELETE CASCADE,
                fuente          TEXT    NOT NULL,
                registro_hash   TEXT    NOT NULL UNIQUE,
                sent_at         TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_alertas_watch_id
                ON alertas_enviadas (watch_id);

            -- ---------------------------------------------------------------
            -- Tablas Guardian Sísmico VE
            -- ---------------------------------------------------------------

            CREATE TABLE IF NOT EXISTS sos_reportes (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                username         TEXT,
                lat              REAL,
                lon              REAL,
                mensaje          TEXT,
                estado           TEXT DEFAULT 'activo',
                timestamp        DATETIME DEFAULT CURRENT_TIMESTAMP,
                fuente           TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_sos_user
                ON sos_reportes (telegram_user_id);

            CREATE INDEX IF NOT EXISTS idx_sos_estado
                ON sos_reportes (estado);

            CREATE TABLE IF NOT EXISTS contactos_emergencia (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                estado_ven     TEXT NOT NULL,
                municipio      TEXT,
                nombre_entidad TEXT NOT NULL,
                telefono       TEXT NOT NULL,
                tipo           TEXT,
                activo         INTEGER DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_contactos_estado
                ON contactos_emergencia (estado_ven);

            CREATE TABLE IF NOT EXISTS rate_limit (
                user_id      INTEGER PRIMARY KEY,
                count        INTEGER DEFAULT 0,
                window_start DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS usuarios (
                telegram_id        INTEGER PRIMARY KEY,
                username           TEXT,
                nombre             TEXT,
                telefono           TEXT,
                estado_ven         TEXT,
                timestamp_registro DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

    # Añadir columna referencia_texto si no existe (migración idempotente)
    # SQLite no soporta IF NOT EXISTS en ALTER TABLE, usamos try/except
    try:
        with _get_conn() as conn:
            conn.execute(
                "ALTER TABLE sos_reportes ADD COLUMN referencia_texto TEXT"
            )
    except Exception:
        pass  # La columna ya existe — esto es normal en reinicios

    # Inserta contactos de emergencia si la tabla está vacía
    seed_contactos()


def seed_contactos() -> None:
    """Inserta los contactos de emergencia iniciales si no existen."""
    with _get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM contactos_emergencia").fetchone()[0]
        if count > 0:
            return  # Ya poblado, no duplicar

        conn.executemany(
            """
            INSERT INTO contactos_emergencia
                (estado_ven, municipio, nombre_entidad, telefono, tipo)
            VALUES (?, ?, ?, ?, ?)
            """,
            CONTACTOS_INICIALES,
        )


# ---------------------------------------------------------------------------
# CRUD — watches (originales)
# ---------------------------------------------------------------------------

def add_watch(chat_id: int, nombre: str) -> int:
    """
    Registra una nueva búsqueda para un chat_id.
    Devuelve el id del registro creado.
    """
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO watches (chat_id, nombre, estado, created_at) VALUES (?, ?, 'activo', ?)",
            (chat_id, nombre.strip(), now),
        )
        return cur.lastrowid


def get_all_watches() -> list[dict]:
    """
    Retorna todas las búsquedas activas (todos los chats).
    Usado por el alerter para el ciclo de monitoreo.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM watches WHERE estado = 'activo' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def get_watches_by_chat(chat_id: int) -> list[dict]:
    """
    Retorna las búsquedas activas de un chat_id específico.
    Usado por /mis_busquedas.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM watches WHERE chat_id = ? AND estado = 'activo' ORDER BY created_at",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_watch(watch_id: int, chat_id: int) -> bool:
    """
    Marca una búsqueda como 'cancelado' (soft-delete) validando que
    pertenezca al chat_id que solicita la cancelación.
    Devuelve True si se modificó alguna fila.
    """
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE watches SET estado = 'cancelado' WHERE id = ? AND chat_id = ? AND estado = 'activo'",
            (watch_id, chat_id),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# CRUD — alertas_enviadas (originales)
# ---------------------------------------------------------------------------

def mark_alerta_sent(watch_id: int, fuente: str, registro_hash: str) -> bool:
    """
    Registra que una alerta ya fue enviada para evitar duplicados.
    Devuelve True si se insertó (alerta nueva), False si ya existía (UNIQUE falla).
    """
    now = datetime.utcnow().isoformat()
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO alertas_enviadas (watch_id, fuente, registro_hash, sent_at) VALUES (?, ?, ?, ?)",
                (watch_id, fuente, registro_hash, now),
            )
        return True
    except sqlite3.IntegrityError:
        # registro_hash ya existe → alerta duplicada, no se vuelve a enviar
        return False


def get_alertas_by_watch(watch_id: int) -> list[dict]:
    """
    Retorna todas las alertas enviadas para un watch_id.
    Útil para auditoría y el comando /encontrado.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alertas_enviadas WHERE watch_id = ? ORDER BY sent_at DESC",
            (watch_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_watch_encontrado(watch_id: int, chat_id: int) -> bool:
    """
    Marca una búsqueda como 'encontrado' (fin feliz).
    Devuelve True si se modificó alguna fila.
    """
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE watches SET estado = 'encontrado' WHERE id = ? AND chat_id = ? AND estado = 'activo'",
            (watch_id, chat_id),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# CRUD — sos_reportes (Guardian Sísmico VE)
# ---------------------------------------------------------------------------

def create_sos_report(
    user_id: int, username: str | None, lat: float | None,
    lon: float | None, mensaje: str | None, fuente: str,
    referencia_texto: str | None = None
) -> int:
    """
    Registra un nuevo reporte SOS en la base de datos.
    Retorna el ID del reporte creado.

    Si no hay GPS (lat/lon), se usa referencia_texto como punto de referencia.
    """
    with _get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO sos_reportes
                (telegram_user_id, username, lat, lon, mensaje, estado, fuente, referencia_texto)
            VALUES (?, ?, ?, ?, ?, 'activo', ?, ?)
            """,
            (user_id, username, lat, lon, mensaje, fuente, referencia_texto),
        )
        return cur.lastrowid


def resolve_sos_report(report_id: int) -> bool:
    """
    Marca un reporte SOS como resuelto.
    Devuelve True si se modificó alguna fila.
    """
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE sos_reportes SET estado = 'resuelto' WHERE id = ? AND estado = 'activo'",
            (report_id,),
        )
        return cur.rowcount > 0


def get_active_sos_reports() -> list[dict]:
    """
    Retorna todos los reportes SOS con estado 'activo', ordenados por timestamp.
    Usado por el resumen periódico del canal de rescatistas.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sos_reportes WHERE estado = 'activo' ORDER BY timestamp ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_active_sos(telegram_user_id: int) -> dict | None:
    """
    Retorna el reporte SOS activo más reciente de un usuario, o None si no tiene.
    """
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM sos_reportes
            WHERE telegram_user_id = ? AND estado = 'activo'
            ORDER BY timestamp DESC LIMIT 1
            """,
            (telegram_user_id,),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# CRUD — contactos_emergencia (Guardian Sísmico VE)
# ---------------------------------------------------------------------------

def get_contactos_by_estado(estado: str) -> list[dict]:
    """
    Retorna los contactos de emergencia activos para un estado venezolano.
    Siempre incluye también los contactos nacionales.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM contactos_emergencia
            WHERE (estado_ven = ? OR estado_ven = 'Nacional') AND activo = 1
            ORDER BY tipo
            """,
            (estado,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# CRUD — rate_limit (Guardian Sísmico VE)
# ---------------------------------------------------------------------------

def check_rate_limit(user_id: int, window_sec: int = 10, max_count: int = 3) -> bool:
    """
    Verifica si el usuario está dentro del límite de acciones SOS permitidas.
    Retorna True si puede continuar, False si excede el límite.

    Lógica:
    - Si no hay registro → crea uno con count=1 → permite
    - Si la ventana de tiempo expiró → reinicia → permite
    - Si está en la ventana y count < max_count → incrementa → permite
    - Si está en la ventana y count >= max_count → bloquea silenciosamente
    """
    now = datetime.utcnow()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, count, window_start FROM rate_limit WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            # Usuario nuevo: crear registro con count=1
            conn.execute(
                "INSERT INTO rate_limit (user_id, count, window_start) VALUES (?, 1, ?)",
                (user_id, now.isoformat()),
            )
            return True

        count = row["count"]
        window_start = datetime.fromisoformat(row["window_start"])

        # ¿Expiró la ventana de tiempo?
        if now - window_start > timedelta(seconds=window_sec):
            conn.execute(
                "UPDATE rate_limit SET count = 1, window_start = ? WHERE user_id = ?",
                (now.isoformat(), user_id),
            )
            return True

        # Dentro de la ventana: verificar conteo
        if count >= max_count:
            return False  # Bloqueado silenciosamente

        conn.execute(
            "UPDATE rate_limit SET count = count + 1 WHERE user_id = ?",
            (user_id,),
        )
        return True


# ---------------------------------------------------------------------------
# CRUD — usuarios (Guardian Sísmico VE)
# ---------------------------------------------------------------------------

def get_or_create_user(telegram_id: int, username: str | None) -> dict:
    """
    Retorna el perfil del usuario, creándolo si no existe.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM usuarios WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()

        if row:
            return dict(row)

        # Crear nuevo usuario con datos mínimos
        conn.execute(
            "INSERT INTO usuarios (telegram_id, username) VALUES (?, ?)",
            (telegram_id, username),
        )
        return {"telegram_id": telegram_id, "username": username, "nombre": None,
                "telefono": None, "estado_ven": None}


def update_user_phone(telegram_id: int, phone: str) -> None:
    """Actualiza el teléfono de un usuario en la tabla usuarios."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE usuarios SET telefono = ? WHERE telegram_id = ?",
            (phone, telegram_id),
        )


# ---------------------------------------------------------------------------
# Punto de entrada para prueba rápida
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    print(f"✅ Base de datos inicializada en: {os.path.abspath(DB_PATH)}")

    # Demo rápido — tablas originales
    wid = add_watch(chat_id=123456789, nombre="Juan Carlos Pérez López")
    print(f"   Watch creado con id={wid}")

    watches = get_all_watches()
    print(f"   Watches activos: {len(watches)}")

    sent = mark_alerta_sent(wid, fuente="sosvzla.lat", registro_hash="abc123hash")
    print(f"   Alerta marcada (nueva={sent})")

    dup = mark_alerta_sent(wid, fuente="sosvzla.lat", registro_hash="abc123hash")
    print(f"   Alerta duplicada rechazada (nueva={dup})")

    deleted = delete_watch(wid, chat_id=123456789)
    print(f"   Watch cancelado: {deleted}")

    # Demo — Guardian Sísmico VE
    user = get_or_create_user(telegram_id=999, username="test_user")
    print(f"   Usuario creado: {user}")

    rl = check_rate_limit(999)
    print(f"   Rate limit OK: {rl}")

    report_id = create_sos_report(999, "test_user", 10.06, -69.34, "Prueba SOS", "atrapado")
    print(f"   Reporte SOS creado: id={report_id}")

    contactos = get_contactos_by_estado("Lara")
    print(f"   Contactos en Lara: {len(contactos)}")
