"""
API RESTful de Control de Asistencia
=====================================
Puente entre la aplicación móvil (frontend Flet) y la base de datos SQL Server.

Dependencias principales:
  - FastAPI        : framework web
  - python-multipart: requerido para Form y UploadFile (instalar si falta)
  - pyodbc         : driver de conexión a SQL Server
  - python-dotenv  : carga de variables de entorno desde .env

Arranque del servidor:
  uvicorn api:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import logging
import os
import shutil
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

import pyodbc
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────
# Configuración de logging
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("asistencia-api")

# ─────────────────────────────────────────────────────────────
# Carga de variables de entorno desde el archivo .env
# ─────────────────────────────────────────────────────────────
load_dotenv()

DB_SERVER   = os.getenv("DB_SERVER")
DB_PORT     = os.getenv("DB_PORT", "1433")      # puerto por defecto de SQL Server
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME     = os.getenv("DB_NAME")

# Cadena de conexión ODBC para SQL Server 2016
# Se usa el driver 17, compatible con SQL Server 2008 R2 en adelante.
_CONNECTION_STRING = (
    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
    f"SERVER={DB_SERVER},{DB_PORT};"
    f"DATABASE={DB_NAME};"
    f"UID={DB_USER};"
    f"PWD={DB_PASSWORD};"
    "TrustServerCertificate=yes;"   # necesario en entornos sin certificado TLS válido
    "Connection Timeout=10;"
)

# ─────────────────────────────────────────────────────────────
# Directorio de subida de fotografías (Windows Server 2016)
# ─────────────────────────────────────────────────────────────
UPLOAD_DIR = os.getenv("UPLOAD_DIR", r"C:\ASISTENCIA\FOTOS")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# Instancia de la aplicación FastAPI
# ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="API Control de Asistencia",
    description="Registra ingresos de trabajadores mediante PIN y fotografía.",
    version="2.0.0",
)

# Permite peticiones desde cualquier origen (ajustar en producción)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────
# Schemas Pydantic
# ─────────────────────────────────────────────────────────────

class PinRequest(BaseModel):
    """Usado por el endpoint ligero /api/validar-pin (solo JSON, sin foto)."""
    pin: str = Field(..., min_length=1, max_length=10, description="PIN numérico.")


class ValidarPinResponse(BaseModel):
    """Respuesta de la pre-validación de PIN."""
    valido: bool
    nombre_trabajador: str


class AsistenciaResponse(BaseModel):
    """Respuesta exitosa del registro completo de asistencia."""
    status: str
    mensaje: str
    nombre_trabajador: str
    ruta_foto: str


# ─────────────────────────────────────────────────────────────
# Gestión de la conexión a la base de datos
# ─────────────────────────────────────────────────────────────

@contextmanager
def get_db_connection() -> Generator[pyodbc.Connection, None, None]:
    """
    Context manager que abre y cierra la conexión a SQL Server de forma segura.
    Hace rollback automático si ocurre cualquier excepción dentro del bloque.
    """
    conn: pyodbc.Connection | None = None
    try:
        conn = pyodbc.connect(_CONNECTION_STRING, autocommit=False)
        logger.info("Conexión a SQL Server establecida.")
        yield conn
    except pyodbc.Error as exc:
        logger.error("Error al conectar con SQL Server: %s", exc)
        # Convertimos el error de DB en un HTTP 503 para el cliente
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo conectar con la base de datos. Intente más tarde.",
        ) from exc
    finally:
        if conn:
            conn.close()
            logger.info("Conexión a SQL Server cerrada.")


# ─────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────

@app.get("/", tags=["Health"])
def health_check():
    """Verifica que la API esté en línea."""
    return {"status": "online", "servicio": "Control de Asistencia API"}


@app.post(
    "/api/validar-pin",
    response_model=ValidarPinResponse,
    status_code=status.HTTP_200_OK,
    tags=["Asistencia"],
    summary="Verifica si el PIN existe SIN registrar asistencia ni requerir foto.",
)
def validar_pin(payload: PinRequest):
    """
    Endpoint ligero para pre-validar el PIN antes de abrir la cámara.
    Solo consulta la tabla SY_Person; no inserta ningún registro.
    Responde rápido (sin overhead de archivos) para no demorar la UX.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT Name FROM SY_Person WHERE pinAcceso = ?",
                (payload.pin,),
            )
            persona = cursor.fetchone()
        except pyodbc.Error as exc:
            logger.error("Error al validar PIN: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error interno al verificar el PIN.",
            ) from exc

    if not persona:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Código inválido.",
        )

    return ValidarPinResponse(valido=True, nombre_trabajador=persona[0])


@app.post(
    "/api/marcar-asistencia",
    response_model=AsistenciaResponse,
    status_code=status.HTTP_200_OK,
    tags=["Asistencia"],
    summary="Registra el ingreso de un trabajador mediante PIN y fotografía.",
)
@app.post(
    "/marcar",
    response_model=AsistenciaResponse,
    status_code=status.HTTP_200_OK,
    tags=["Asistencia"],
    summary="Alias de /api/marcar-asistencia (misma lógica y campos).",
)
def marcar_asistencia(
    pin:  str        = Form(..., min_length=1, max_length=10, description="PIN numérico del trabajador."),
    foto: UploadFile = File(..., description="Fotografía tomada en el momento del registro."),
    fecha_manual: str | None = Form(
        None,
        description="Opcional. Fecha/hora real del marcado (sync offline), formato YYYY-MM-DD HH:MM:SS.",
    ),
):
    """
    Recibe multipart/form-data con dos campos:
      · pin  (str)        – PIN numérico del trabajador
      · foto (UploadFile) – Fotografía del trabajador al momento de marcar

    Flujo:
      1. Valida el PIN contra la tabla `SY_Person`.
      2. Si el PIN no existe → 404.
      3. Guarda la fotografía en `fotos_asistencia/` con nombre único.
      4. Inserta en `RegistroAsistencia` con Person, FechaHoraIngreso (ahora del marcado o fecha_manual),
         rutafoto, xlastuser y xlastdate (GETDATE() al sincronizar).
      5. Retorna datos del trabajador y ruta del archivo guardado.

    La foto se guarda SOLO si el PIN es válido, evitando acumular
    archivos de intentos fallidos.
    """
    logger.info("Solicitud de marcado recibida para PIN: %s", pin)

    # Fecha/hora del evento: offline respeta fecha_manual; online usa hora actual del servidor.
    ahora = datetime.now()
    if fecha_manual and fecha_manual.strip():
        try:
            ahora = datetime.strptime(fecha_manual.strip(), "%Y-%m-%d %H:%M:%S")
            logger.info("Marca con fecha original (offline/sync): %s", fecha_manual.strip())
        except ValueError:
            logger.warning("fecha_manual inválida (%r); usando hora actual del servidor.", fecha_manual)
            ahora = datetime.now()

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # ── Paso 1: Verificar si el PIN existe ──────────────────────────
        # Consulta parametrizada para prevenir SQL Injection.
        SQL_VERIFICAR_PIN = """
            SELECT Person, Name
            FROM   SY_Person
            WHERE  pinAcceso = ?
        """
        try:
            cursor.execute(SQL_VERIFICAR_PIN, (pin,))
            persona = cursor.fetchone()
        except pyodbc.Error as exc:
            logger.error("Error al consultar el PIN: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error interno al verificar el PIN.",
            ) from exc

        # ── Paso 2: PIN no encontrado ────────────────────────────────────
        if not persona:
            logger.warning("Intento con PIN inválido.")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Código inválido.",
            )

        person_id: int = persona[0]
        nombre: str    = persona[1]
        logger.info("PIN válido para persona: %s (Person=%s)", nombre, person_id)

        # ── Paso 3: Guardar fotografía ───────────────────────────────────
        # El nombre incluye timestamp + pin para que sea único y trazable.
        # Se respeta la extensión original del archivo enviado (.jpg, .png…).
        ext_original = Path(foto.filename).suffix if foto.filename else ".jpg"
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre_archivo = f"{timestamp_str}_{pin}{ext_original}"

        ruta_completa = os.path.join(UPLOAD_DIR, nombre_archivo)
        try:
            # Guardar en streaming (mejor que foto.file.read() para memoria)
            with open(ruta_completa, "wb") as out_file:
                shutil.copyfileobj(foto.file, out_file)
            logger.info("Foto guardada: %s", ruta_completa)
        except OSError as exc:
            logger.error("Error al guardar la foto: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error interno al guardar la fotografía.",
            ) from exc

        # ── Paso 4: Registrar asistencia con ruta de foto ───────────────
        # FechaHoraIngreso siempre desde Python (`ahora`): marca online u hora original offline.
        # xlastdate sigue en GETDATE() para auditoría del momento de escritura en servidor.
        try:
            # Importante: en SQL guardamos SOLO el nombre del archivo.
            # Esto facilita que una Web lea la imagen luego sin depender de rutas absolutas.
            SQL_INSERTAR_ASISTENCIA = """
                INSERT INTO RegistroAsistencia (IdTrabajador, Person, FechaHoraIngreso, rutafoto, xlastuser, xlastdate)
                VALUES (?, ?, ?, ?, 'admin', GETDATE())
            """
            cursor.execute(
                SQL_INSERTAR_ASISTENCIA,
                (1, person_id, ahora, nombre_archivo),
            )
            conn.commit()
            logger.info(
                "Asistencia registrada para Person=%s. Foto: %s",
                person_id,
                nombre_archivo,
            )
        except pyodbc.Error as exc:
            conn.rollback()
            # Si el INSERT falla, eliminamos la foto ya guardada para mantener consistencia.
            try:
                if os.path.exists(ruta_completa):
                    os.remove(ruta_completa)
                    logger.warning(
                        "Foto eliminada por fallo en INSERT: %s",
                        nombre_archivo,
                    )
            except OSError:
                pass
            logger.error("Error al insertar asistencia: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error interno al registrar la asistencia.",
            ) from exc

    # ── Respuesta exitosa ─────────────────────────────────────────────────
    return AsistenciaResponse(
        status="success",
        mensaje="Ingreso registrado correctamente",
        nombre_trabajador=nombre,
        ruta_foto=nombre_archivo,
    )
