"""
API RESTful de Control de Asistencia
=====================================
Puente entre la aplicación móvil (frontend Flet) y la base de datos SQL Server.

Dependencias principales:
  - FastAPI            : framework web
  - python-multipart   : requerido para Form y UploadFile
  - pyodbc             : driver de conexión a SQL Server
  - python-dotenv      : carga de variables de entorno desde .env
  - deepface + numpy   : biometría facial (opcional; ver requirements-server.txt)
  - tensorflow         : backend de DeepFace (CPU)

Arranque del servidor:
  uvicorn api:app --host 0.0.0.0 --port 8000 --reload

Migración requerida (ejecutar UNA VEZ en SQL Server antes de activar biometría):
  ALTER TABLE RegistroAsistencia ADD Match_Score float NULL;
  ALTER TABLE RegistroAsistencia ADD Es_Impostor bit NOT NULL DEFAULT 0;
"""

from __future__ import annotations

import logging
import os
import shutil
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

import pyodbc
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
# Carga de variables de entorno
# ─────────────────────────────────────────────────────────────
load_dotenv()

DB_SERVER   = os.getenv("DB_SERVER")
DB_PORT     = os.getenv("DB_PORT", "1433")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME     = os.getenv("DB_NAME")

_CONNECTION_STRING = (
    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
    f"SERVER={DB_SERVER},{DB_PORT};"
    f"DATABASE={DB_NAME};"
    f"UID={DB_USER};"
    f"PWD={DB_PASSWORD};"
    "TrustServerCertificate=yes;"
    "Connection Timeout=10;"
)

UPLOAD_DIR = os.getenv("UPLOAD_DIR", r"C:\ASISTENCIA\FOTOS")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Columnas de RegistroAsistencia (hm_quimica usa RutaFoto; hm_atilio puede usar rutafoto)
REGISTRO_COL_FOTO = os.getenv("REGISTRO_COL_FOTO", "RutaFoto")
REGISTRO_COL_PC   = os.getenv("REGISTRO_COL_PC", "PC")

# ─────────────────────────────────────────────────────────────
# Biometría facial — facenet-pytorch (100 % PyTorch, sin tensorflow)
# ─────────────────────────────────────────────────────────────
# Si facenet-pytorch no está instalado el sistema sigue funcionando:
# registra asistencia normalmente, sin analizar el rostro.
try:
    import numpy as np
    import torch as _torch
    from PIL import Image as _PilImage
    from facenet_pytorch import MTCNN as _MTCNN, InceptionResnetV1 as _InceptionResnetV1
    _BIOMETRY_AVAILABLE = True
    logger.info("facenet-pytorch disponible. Biometría facial habilitada.")
except ImportError:
    _BIOMETRY_AVAILABLE = False
    logger.warning(
        "facenet-pytorch no instalado. "
        "Instalar: pip install facenet-pytorch Pillow numpy. "
        "Biometría deshabilitada hasta entonces."
    )

# Rangos de similitud coseno (FaceNet VGGFace2). Ajustables en .env.
BIOMETRY_SCORE_OK = float(os.getenv("BIOMETRY_SCORE_OK", "0.75"))   # >= → verde
BIOMETRY_SCORE_MIN = float(os.getenv("BIOMETRY_SCORE_MIN", "0.55"))  # <  → rechazo

# Instancias globales — se inicializan una sola vez en el warmup
_mtcnn:  "_MTCNN | None"             = None
_resnet: "_InceptionResnetV1 | None" = None


def _warmup_models() -> None:
    """Carga MTCNN + InceptionResnetV1 (VGGFace2) en RAM al arrancar uvicorn."""
    global _mtcnn, _resnet
    if not _BIOMETRY_AVAILABLE:
        return
    try:
        _mtcnn  = _MTCNN(keep_all=False, device="cpu", post_process=True)
        _resnet = _InceptionResnetV1(pretrained="vggface2").eval()
        dummy   = _torch.zeros(1, 3, 160, 160)
        with _torch.no_grad():
            _resnet(dummy)
        logger.info("Modelos facenet-pytorch (VGGFace2, 512-dim) listos.")
    except Exception as exc:
        logger.warning("No se pudieron cargar modelos facenet-pytorch: %s", exc)


def _extract_embedding(image_path: str) -> list:
    """
    Detecta el rostro con MTCNN y extrae su vector de 512 dims con FaceNet.
    Lanza ValueError si no se detecta ningún rostro en la imagen.
    """
    if _mtcnn is None or _resnet is None:
        raise RuntimeError("Modelos biométricos no inicializados.")
    img         = _PilImage.open(image_path).convert("RGB")
    face_tensor = _mtcnn(img)
    if face_tensor is None:
        raise ValueError("No se detectó ningún rostro en la imagen.")
    with _torch.no_grad():
        embedding = _resnet(face_tensor.unsqueeze(0))
    return embedding.squeeze().tolist()


def _embedding_to_bytes(embedding: list) -> bytes:
    """Serializa list[float] → bytes float32 (para VARBINARY(MAX) en SQL Server)."""
    return np.array(embedding, dtype=np.float32).tobytes()


def _bytes_to_embedding(data: bytes) -> "np.ndarray":
    """Deserializa bytes de VARBINARY(MAX) → numpy float32 array."""
    return np.frombuffer(data, dtype=np.float32)


def _cosine_similarity(a: "np.ndarray", b: "np.ndarray") -> float:
    """
    Similitud coseno entre dos vectores.
    Valor 1.0 = mismo rostro. Valor 0.0 = sin relación.
    """
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _clasificar_score(match_score: float) -> tuple[str, bool]:
    """
    Clasifica el resultado biométrico según reglas de negocio.
    Retorna (estado, es_impostor).
      aprobado    → score >= BIOMETRY_SCORE_OK
      observacion → BIOMETRY_SCORE_MIN <= score < BIOMETRY_SCORE_OK
      rechazado   → score < BIOMETRY_SCORE_MIN (no registrar asistencia)
    """
    if match_score >= BIOMETRY_SCORE_OK:
        return "aprobado", False
    if match_score >= BIOMETRY_SCORE_MIN:
        return "observacion", True
    return "rechazado", True


def _procesar_biometria(
    conn: pyodbc.Connection,
    cursor: pyodbc.Cursor,
    person_id,
    image_path: str,
) -> tuple[str, Optional[float], bool]:
    """
    Núcleo del sistema biométrico.  Retorna (estado, match_score, es_impostor).

    estados posibles:
      "enrolado"    → primera vez: embedding guardado en sy_person_biometry.
      "aprobado"    → score >= 0.75
      "observacion" → 0.55 <= score < 0.75
      "rechazado"   → score < 0.55 (el endpoint NO debe insertar asistencia)
      "sin_rostro"  → no se detectó cara en la foto.
      "error"       → fallo técnico (se registra asistencia igual, sin score).
    """
    # Paso 1: extraer embedding del frame actual
    try:
        embedding_nuevo = _extract_embedding(image_path)
    except ValueError:
        logger.warning("Biometría: rostro no detectado en %s", image_path)
        return "sin_rostro", None, False
    except Exception as exc:
        logger.warning("Biometría: error extrayendo embedding: %s", exc)
        return "error", None, False

    embedding_bytes = _embedding_to_bytes(embedding_nuevo)

    # Paso 2: buscar registro previo en sy_person_biometry
    try:
        cursor.execute(
            """
            SELECT Face_Template
            FROM   sy_person_biometry
            WHERE  Person = ? AND Status = 'A'
            """,
            (str(person_id),),
        )
        bio_row = cursor.fetchone()
    except pyodbc.Error as exc:
        logger.warning("Biometría: error consultando sy_person_biometry: %s", exc)
        return "error", None, False

    if bio_row is None:
        # ── Caso A: Enrolamiento ─────────────────────────────────────────
        try:
            cursor.execute(
                """
                INSERT INTO sy_person_biometry (Person, Face_Template, LastUpdate, Status)
                VALUES (?, ?, GETDATE(), 'A')
                """,
                (str(person_id), pyodbc.Binary(embedding_bytes)),
            )
            logger.info(
                "Biometría: enrolamiento exitoso para Person=%s modelo=facenet-vggface2",
                person_id,
            )
            return "enrolado", 1.0, False
        except pyodbc.Error as exc:
            logger.warning("Biometría: error al insertar en sy_person_biometry: %s", exc)
            return "error", None, False
    else:
        # ── Caso B: Validación ───────────────────────────────────────────
        try:
            stored_emb  = _bytes_to_embedding(bytes(bio_row[0]))
            nuevo_emb   = np.array(embedding_nuevo, dtype=np.float32)
            match_score = _cosine_similarity(stored_emb, nuevo_emb)
            estado, es_impostor = _clasificar_score(match_score)
            logger.info(
                "Biometría: Person=%s score=%.4f ok=%.2f min=%.2f estado=%s",
                person_id, match_score, BIOMETRY_SCORE_OK, BIOMETRY_SCORE_MIN, estado,
            )
            return estado, round(match_score, 4), es_impostor
        except Exception as exc:
            logger.warning("Biometría: error comparando embeddings: %s", exc)
            return "error", None, False


# ─────────────────────────────────────────────────────────────
# Lifespan: pre-carga del modelo al arrancar uvicorn
# ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app_: FastAPI):
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _warmup_models)
    yield


# ─────────────────────────────────────────────────────────────
# Instancia FastAPI
# ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="API Control de Asistencia",
    description="Registra ingresos de trabajadores mediante PIN y fotografía.",
    version="3.0.0",
    lifespan=lifespan,
)

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
    pin: str = Field(..., min_length=1, max_length=10)


class ValidarPinResponse(BaseModel):
    valido: bool
    nombre_trabajador: str


class AsistenciaResponse(BaseModel):
    status: str
    mensaje: str
    nombre_trabajador: str
    ruta_foto: str
    # ── Campos biométricos ──
    biometria: str = "omitida"          # enrolado | aprobado | observacion | rechazado | sin_rostro | error | omitida
    match_score: Optional[float] = None # None si no hay dato biométrico
    es_impostor: bool = False


# ─────────────────────────────────────────────────────────────
# Gestión de conexión a base de datos
# ─────────────────────────────────────────────────────────────

@contextmanager
def get_db_connection() -> Generator[pyodbc.Connection, None, None]:
    conn: pyodbc.Connection | None = None
    try:
        conn = pyodbc.connect(_CONNECTION_STRING, autocommit=False)
        logger.info("Conexión a SQL Server establecida.")
        yield conn
    except pyodbc.Error as exc:
        logger.error("Error al conectar con SQL Server: %s", exc)
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
    return {
        "status": "online",
        "servicio": "Control de Asistencia API",
        "biometria": _BIOMETRY_AVAILABLE,
        "modelo": "facenet-vggface2" if _BIOMETRY_AVAILABLE else None,
    }


@app.post(
    "/api/validar-pin",
    response_model=ValidarPinResponse,
    status_code=status.HTTP_200_OK,
    tags=["Asistencia"],
    summary="Verifica si el PIN existe SIN registrar asistencia ni requerir foto.",
)
def validar_pin(payload: PinRequest):
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Código inválido.")

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
    summary="Alias de /api/marcar-asistencia.",
)
def marcar_asistencia(
    pin:  str        = Form(..., min_length=1, max_length=10),
    foto: UploadFile = File(...),
    fecha_manual: str | None = Form(None),
    pc: str = Form("PC-Desconocida", max_length=100),
):
    """
    Flujo completo:
      1. Valida PIN → obtiene person_id y nombre.
      2. Guarda fotografía en UPLOAD_DIR.
      3. Análisis biométrico (si DeepFace está disponible):
           · Primer registro  → extrae embedding y enrola en sy_person_biometry.
           · Registro previo  → compara embeddings (similitud coseno) y evalúa impostores.
      4. Inserta en RegistroAsistencia con Match_Score y Es_Impostor.
      5. Devuelve respuesta con resultado biométrico.
    """
    pc_norm = (pc or "").strip()[:100] or "PC-Desconocida"
    logger.info("Marcado recibido: PIN=%s PC=%s", pin, pc_norm)

    # Fecha/hora del evento (offline respeta fecha_manual)
    ahora = datetime.now()
    if fecha_manual and fecha_manual.strip():
        try:
            ahora = datetime.strptime(fecha_manual.strip(), "%Y-%m-%d %H:%M:%S")
            logger.info("Marca con fecha original (offline): %s", fecha_manual.strip())
        except ValueError:
            logger.warning("fecha_manual inválida (%r); usando hora del servidor.", fecha_manual)
            ahora = datetime.now()

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # ── 1. Validar PIN ─────────────────────────────────────────────
        try:
            cursor.execute(
                "SELECT Person, Name FROM SY_Person WHERE pinAcceso = ?",
                (pin,),
            )
            persona = cursor.fetchone()
        except pyodbc.Error as exc:
            logger.error("Error al consultar PIN: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error interno al verificar el PIN.",
            ) from exc

        if not persona:
            logger.warning("Intento con PIN inválido.")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Código inválido.")

        person_id: str = str(persona[0])
        nombre: str    = persona[1]
        logger.info("PIN válido: %s (Person=%s)", nombre, person_id)

        # ── 2. Guardar fotografía ──────────────────────────────────────
        ext_original  = Path(foto.filename).suffix if foto.filename else ".jpg"
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre_archivo = f"{timestamp_str}_{pin}{ext_original}"
        ruta_completa  = os.path.join(UPLOAD_DIR, nombre_archivo)

        try:
            with open(ruta_completa, "wb") as out_file:
                shutil.copyfileobj(foto.file, out_file)
            logger.info("Foto guardada: %s", ruta_completa)
        except OSError as exc:
            logger.error("Error al guardar foto: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error interno al guardar la fotografía.",
            ) from exc

        # ── 3. Biometría facial (opcional) ─────────────────────────────
        bio_estado: str          = "omitida"
        match_score: float | None = None
        es_impostor: bool         = False

        if _BIOMETRY_AVAILABLE:
            bio_estado, match_score, es_impostor = _procesar_biometria(
                conn, cursor, person_id, ruta_completa
            )

        # ── Rechazo biométrico: no registrar asistencia ni conservar la foto ──
        if bio_estado == "rechazado":
            conn.rollback()
            _cleanup_foto(ruta_completa, nombre_archivo)
            logger.warning(
                "Marca rechazada por biometría: Person=%s score=%s",
                person_id, match_score,
            )
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "status": "rejected",
                    "mensaje": "Verificación biométrica fallida",
                    "nombre_trabajador": nombre,
                    "ruta_foto": "",
                    "biometria": "rechazado",
                    "match_score": match_score,
                    "es_impostor": True,
                },
            )

        # ── 4. Registrar asistencia ────────────────────────────────────
        col_foto = REGISTRO_COL_FOTO
        col_pc   = REGISTRO_COL_PC
        sql_completo = f"""
                INSERT INTO RegistroAsistencia
                    (IdTrabajador, Person, FechaHoraIngreso, {col_foto}, {col_pc},
                     xlastuser, xlastdate, Match_Score, Es_Impostor)
                VALUES (?, ?, ?, ?, ?, 'admin', GETDATE(), ?, ?)
            """
        sql_legacy = f"""
                        INSERT INTO RegistroAsistencia
                            (IdTrabajador, Person, FechaHoraIngreso, {col_foto}, {col_pc},
                             xlastuser, xlastdate)
                        VALUES (?, ?, ?, ?, ?, 'admin', GETDATE())
                    """
        try:
            cursor.execute(
                sql_completo,
                (1, person_id, ahora, nombre_archivo, pc_norm,
                 match_score, 1 if es_impostor else 0),
            )
        except pyodbc.Error as exc_full:
            err_str = str(exc_full).lower()
            bio_cols_missing = (
                "match_score" in err_str
                or "es_impostor" in err_str
            )
            if bio_cols_missing:
                logger.warning(
                    "Columnas biométricas no existen. Ejecutar ALTER TABLE. "
                    "Fallback a INSERT sin biometría."
                )
                try:
                    cursor.execute(
                        sql_legacy,
                        (1, person_id, ahora, nombre_archivo, pc_norm),
                    )
                except pyodbc.Error as exc_legacy:
                    conn.rollback()
                    _cleanup_foto(ruta_completa, nombre_archivo)
                    logger.error("Error INSERT legacy RegistroAsistencia: %s", exc_legacy)
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Error interno al registrar la asistencia.",
                    ) from exc_legacy
            else:
                conn.rollback()
                _cleanup_foto(ruta_completa, nombre_archivo)
                logger.error("Error al insertar asistencia: %s", exc_full)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Error interno al registrar la asistencia.",
                ) from exc_full

        conn.commit()
        logger.info(
            "Asistencia registrada: Person=%s foto=%s bio=%s score=%s impostor=%s",
            person_id, nombre_archivo, bio_estado, match_score, es_impostor,
        )

    return AsistenciaResponse(
        status="success",
        mensaje="Ingreso registrado correctamente",
        nombre_trabajador=nombre,
        ruta_foto=nombre_archivo,
        biometria=bio_estado,
        match_score=match_score,
        es_impostor=es_impostor,
    )


def _cleanup_foto(ruta: str, nombre: str) -> None:
    """Elimina la foto guardada cuando el INSERT falla (consistencia)."""
    try:
        if os.path.exists(ruta):
            os.remove(ruta)
            logger.warning("Foto eliminada por fallo en INSERT: %s", nombre)
    except OSError:
        pass
