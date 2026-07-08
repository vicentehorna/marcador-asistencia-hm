"""
Aplicación Kiosco — Control de Asistencia
==========================================
Ejecutar: python main.py

Arquitectura de cámara
-----------------------
ft.Camera NO existe en Flet 0.82.  Solución: OpenCV + ft.Image(src_base64).

  CameraStream  → hilo daemon que lee frames de cv2.VideoCapture
                  y los comprime a JPEG en memoria (sin disco)
  _camera_update_task → corrutina asyncio que copia el base64 del frame
                        al ft.Image cada 100 ms (10 fps en pantalla)

Flujo de marcado (cero selección de archivos)
----------------------------------------------
  1. Usuario ingresa PIN de 4 dígitos
  2. _on_enter()          → valida largo localmente
  3. _validar_pin_async() → verifica PIN contra la BD (ligero, sin foto)
  4. CameraStream.get_frame_bytes() → captura el frame ACTUAL al instante
  5. _call_api_async()    → envía PIN + bytes JPEG como multipart/form-data

Nota sobre deployment
---------------------
  · flet run --android : Python corre en el PC → cámara = webcam del PC
  · flet build apk     : Python corre en el tablet → cámara = cámara del dispositivo
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import socket
import threading
import time
from datetime import datetime

import cv2
import requests
import flet as ft
from dotenv import load_dotenv

# Imports de módulo: stdlib + flet + requests + cv2 (app kiosco).
# Algunos clientes Flet (p. ej. visor mínimo / APK sin extensiones) no registran
# controles como Audio ni HapticFeedback → provocan "Unknown control".
# La confirmación de éxito es solo visual (mensaje verde en lbl_mensaje).
# numpy aparece solo dentro de _placeholder_base64 (lazy).

# Config: app.env se empaqueta en el APK; .env es solo PC/servidor (.fletignore).
def _load_env_files() -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(root, "app.env"))
    load_dotenv(os.path.join(root, ".env"), override=True)


_load_env_files()

# ── Constantes ────────────────────────────────────────────────────────
PIN_MAX_LENGTH    = 4
# Coincide con columna PC varchar(100) en SQL Server
_DEVICE_NAME_MAX = 100
API_BASE_URL      = os.getenv("API_BASE_URL", "http://179.61.14.224:8000").rstrip("/")
API_URL           = f"{API_BASE_URL}/api/marcar-asistencia"
API_VALIDATE_URL  = f"{API_BASE_URL}/api/validar-pin"
# La API puede tardar al primer enrolamiento biométrico (FaceNet/modelos en CPU).
API_TIMEOUT_CHECK = 8
API_TIMEOUT_MARK = (5, 60)  # (conexión, lectura/procesamiento de foto + biometría)
API_TIMEOUT_SYNC = 60  # cola offline: puede ir por red débil; reintentos posteriores
# Windows: casi siempre la webcam es índice 0. Android/tablet frontal suele ser 1.
# Sobrescribe en .env: CAMERA_INDEX=0 o CAMERA_INDEX=1
def _camera_index_default() -> int:
    raw = os.getenv("CAMERA_INDEX")
    if raw is None or str(raw).strip() == "":
        return 0 if os.name == "nt" else 1
    try:
        return int(str(raw).strip())
    except ValueError:
        return 0 if os.name == "nt" else 1


CAMERA_INDEX = _camera_index_default()
CAMERA_FPS          = 12  # 12 fps es más que suficiente para una foto de asistencia; reduce CPU ~60 %
UI_FPS              = 10  # Velocidad de refresco del ft.Image en pantalla
CAMERA_IDLE_TIMEOUT = 60  # Segundos sin interacción antes de apagar la cámara automáticamente


def _is_android_runtime() -> bool:
    """True cuando Python corre dentro de un APK / Android (no Windows desktop)."""
    if os.environ.get("ANDROID_ROOT") or os.environ.get("ANDROID_DATA"):
        return True
    if os.environ.get("PYTHON_ANDROID"):
        return True
    try:
        rel = (platform.release() or "").lower()
        if "android" in rel:
            return True
    except Exception:
        pass
    try:
        uname = os.uname()
        rel = str(getattr(uname, "release", "") or "").lower()
        if "android" in rel:
            return True
    except (AttributeError, OSError):
        pass
    return False


def _default_camera_rotation() -> str:
    # En Android muchos teléfonos entregan buffer landscape (w>h) en app retrato → 'auto' corrige solo ese caso.
    # Tablet que ya entrega retrato (h>=w) no se rota. PC/tablet Windows: sin rotación por defecto.
    return "auto" if _is_android_runtime() else "0"


_raw_rot = os.getenv("CAMERA_ROTATION")
if _raw_rot is None or str(_raw_rot).strip() == "":
    CAMERA_ROTATION = _default_camera_rotation().strip().lower()
else:
    CAMERA_ROTATION = str(_raw_rot).strip().lower()
CAMERA_MIRROR       = os.getenv("CAMERA_MIRROR", "false").strip().lower() in {"1", "true", "yes", "si"}

# Rotación "auto" en Android: se deriva del tamaño lógico de ventana (Flet).
_ANDROID_AUTO_ROT_VALUE = "90ccw"
_CAMERA_ANDROID_AUTO_LOCK = threading.Lock()


def _android_tablet_min_short_side() -> int:
    raw = os.getenv("CAMERA_ANDROID_TABLET_MIN_SHORT")
    if raw is None or str(raw).strip() == "":
        return 580
    try:
        return max(400, int(str(raw).strip()))
    except ValueError:
        return 580


def update_android_camera_auto_form_factor(window_w: int, window_h: int) -> None:
    global _ANDROID_AUTO_ROT_VALUE
    if not _is_android_runtime():
        return
    try:
        w, h = int(window_w), int(window_h)
    except (TypeError, ValueError):
        return

    # Detectamos si es Tablet o Móvil
    short_side = min(w, h)
    is_tablet = short_side >= _android_tablet_min_short_side()

    with _CAMERA_ANDROID_AUTO_LOCK:
        if is_tablet:
            # En Tablets horizontales como la Redmi, el sensor ya está derecho.
            _ANDROID_AUTO_ROT_VALUE = "none"
        else:
            # En móviles, el sensor está de lado y necesita giro.
            _ANDROID_AUTO_ROT_VALUE = "90ccw"

# Directorio offline: en POSIX (p. ej. Android/APK) se usa cwd/offline_storage con
# respaldo a ruta bajo Android/data; en Windows, carpeta relativa offline_storage.
def get_safe_path() -> str:
    if os.name != "nt":
        base = os.path.join(os.getcwd(), "offline_storage")
        try:
            if not os.path.exists(base):
                os.makedirs(base, exist_ok=True)
            return base
        except OSError:
            return "/sdcard/Android/data/com.tuempresa.asistencia/files/offline"
    return "offline_storage"


OFFLINE_DIR = get_safe_path()
OFFLINE_PHOTOS = os.path.join(OFFLINE_DIR, "photos")
OFFLINE_DATA_FILE = os.path.join(OFFLINE_DIR, "pending_marks.json")
os.makedirs(OFFLINE_PHOTOS, exist_ok=True)

print(f"[OFFLINE] Carpeta de cola: {OFFLINE_DIR}")

_OFFLINE_FILE_LOCK = threading.RLock()

# ── Temas UI (selector en .env → UI_THEME) ───────────────────────────
_UI_THEME_PRESETS: dict[str, dict[str, str]] = {
  # Alternativa 1: Premium Tech Slate (oscuro moderno)
    "premium_slate": {
        "bg": "#0F172A",
        "surface": "#1E293B",
        "accent": "#06B6D4",
        "success": "#10B981",
        "error": "#F43F5E",
        "text": "#F8FAFC",
        "subtext": "#94A3B8",
        "del": "#334155",
        "enter": "#10B981",
        "warning": "#F59E0B",
        "reject": "#F43F5E",
        "on_accent": "#F8FAFC",
        "border": "#334155",
        "badge": "#F8FAFC",
        "cam_idle": "#475569",
        "shadow_num": "#06B6D440",
        "shadow_enter": "#10B98150",
        "shadow_panel": "#00000000",
        "flet_theme": "dark",
    },
    # Alternativa 2: Enterprise Clean (claro corporativo)
    "enterprise_clean": {
        "bg": "#F8F9FA",
        "surface": "#FFFFFF",
        "accent": "#2563EB",
        "success": "#059669",
        "error": "#DC2626",
        "text": "#1E3A5F",
        "subtext": "#64748B",
        "del": "#64748B",
        "enter": "#059669",
        "warning": "#D97706",
        "reject": "#DC2626",
        "on_accent": "#FFFFFF",
        "border": "#E2E8F0",
        "badge": "#FFFFFF",
        "cam_idle": "#94A3B8",
        "shadow_num": "#2563EB30",
        "shadow_enter": "#05966940",
        "shadow_panel": "#00000012",
        "flet_theme": "light",
    },
}

_UI_THEME_ALIASES = {
    "1": "premium_slate",
    "slate": "premium_slate",
    "premium": "premium_slate",
    "dark": "premium_slate",
    "oscuro": "premium_slate",
    "2": "enterprise_clean",
    "enterprise": "enterprise_clean",
    "clean": "enterprise_clean",
    "light": "enterprise_clean",
    "claro": "enterprise_clean",
}


def _resolve_ui_theme_name(raw: str | None) -> str:
    key = (raw or "premium_slate").strip().lower()
    return _UI_THEME_ALIASES.get(key, key if key in _UI_THEME_PRESETS else "premium_slate")


def _apply_ui_theme(theme_name: str) -> str:
    """Carga la paleta en variables globales CLR_* usadas por toda la UI."""
    preset = _UI_THEME_PRESETS[theme_name]
    g = globals()
    g["CLR_BG"] = preset["bg"]
    g["CLR_SURFACE"] = preset["surface"]
    g["CLR_ACCENT"] = preset["accent"]
    g["CLR_SUCCESS"] = preset["success"]
    g["CLR_ERROR"] = preset["error"]
    g["CLR_TEXT"] = preset["text"]
    g["CLR_SUBTEXT"] = preset["subtext"]
    g["CLR_DEL"] = preset["del"]
    g["CLR_ENTER"] = preset["enter"]
    g["CLR_WARNING"] = preset["warning"]
    g["CLR_REJECT"] = preset["reject"]
    g["CLR_ON_ACCENT"] = preset["on_accent"]
    g["CLR_BORDER"] = preset["border"]
    g["CLR_BADGE"] = preset["badge"]
    g["CLR_CAM_IDLE"] = preset["cam_idle"]
    g["CLR_SHADOW_NUM"] = preset["shadow_num"]
    g["CLR_SHADOW_ENTER"] = preset["shadow_enter"]
    g["CLR_SHADOW_PANEL"] = preset["shadow_panel"]
    g["UI_THEME_ACTIVE"] = theme_name
    return preset["flet_theme"]


UI_THEME_ACTIVE = "premium_slate"
# Valores por defecto (se sobrescriben al cargar .env)
CLR_BG = CLR_SURFACE = CLR_ACCENT = CLR_SUCCESS = CLR_ERROR = ""
CLR_TEXT = CLR_SUBTEXT = CLR_DEL = CLR_ENTER = CLR_WARNING = CLR_REJECT = ""
CLR_ON_ACCENT = CLR_BORDER = CLR_BADGE = CLR_CAM_IDLE = ""
CLR_SHADOW_NUM = CLR_SHADOW_ENTER = CLR_SHADOW_PANEL = ""

_flet_theme = _apply_ui_theme(_resolve_ui_theme_name(os.getenv("UI_THEME")))
_PAGE_THEME_MODE = (
    ft.ThemeMode.LIGHT if _flet_theme == "light" else ft.ThemeMode.DARK
)
print(f"[UI] Tema activo: {UI_THEME_ACTIVE} ({_flet_theme})")


# ─────────────────────────────────────────────────────────────────────
# Módulo de cámara — hilo de captura independiente de la UI
# ─────────────────────────────────────────────────────────────────────

class CameraStream:
    """
    Hilo daemon ON-DEMAND que captura frames de OpenCV.

    La cámara NO se abre al crear el objeto.  Se activa sólo cuando el
    usuario toca el teclado numérico y se apaga automáticamente tras
    CAMERA_IDLE_TIMEOUT segundos sin interacción (ahorra batería y CPU).

    API pública
    -----------
    start_stream() → bool      Abre cámara y lanza hilo (idempotente si ya corre)
    stop_stream()  → None      Detiene hilo y libera hardware
    streaming      → bool      True mientras el hilo está corriendo
    active         → bool      True cuando llegan frames reales
    error          → str|None  Mensaje del último fallo
    get_frame_bytes()  → bytes | None
    get_frame_base64() → str   | None
    """

    _FALLBACK_INDICES = (1, 0, 2)
    _MAX_FAILURES     = 10

    def __init__(self, camera_index: int | None = None, fps: int = 12):
        self._preferred   = 1 if camera_index is None else camera_index
        self._interval    = 1.0 / fps
        self._cap: cv2.VideoCapture | None = None
        self._frame_bytes: bytes | None = None
        self._lock        = threading.Lock()
        self._running     = False
        self._active      = False
        self._error: str | None = None

    # ── Propiedades ────────────────────────────────────────────────────
    @property
    def streaming(self) -> bool:
        """True si el hilo de captura está en marcha (aunque aún no haya frames)."""
        return self._running

    @property
    def active(self) -> bool:
        """True cuando llegan frames reales del hardware."""
        return self._active

    @property
    def error(self) -> str | None:
        return self._error

    # ── Ciclo de vida ON-DEMAND ────────────────────────────────────────
    def start_stream(self) -> bool:
        """Abre la cámara dejando que Android decida la resolución nativa para evitar pérdida de color."""
        if self._running:
            return True
        with self._lock:
            self._frame_bytes = None
        self._error  = None
        self._active = False

        indices = [self._preferred] + [
            i for i in self._FALLBACK_INDICES if i != self._preferred
        ]
        for idx in indices:
            try:
                cap = cv2.VideoCapture(idx)
                if not cap.isOpened():
                    cap.release()
                    continue

                ret, _ = cap.read()
                if not ret:
                    print(f"[CAMERA] Índice {idx}: sin frames (ocupada?).")
                    cap.release()
                    continue
                print(f"[CAMERA] Activa en índice {idx}.")
                self._cap     = cap
                self._running = True
                threading.Thread(target=self._loop, daemon=True).start()
                return True
            except Exception as exc:
                print(f"[CAMERA] Error al abrir índice {idx}: {exc}")

        self._error = "No se encontró ninguna cámara disponible."
        print(f"[CAMERA] {self._error}")
        return False

    def stop_stream(self) -> None:
        """Detiene el hilo de captura y libera el hardware de cámara."""
        self._running = False
        self._active  = False
        cap, self._cap = self._cap, None
        if cap:
            try:
                cap.release()
            except Exception:
                pass
        with self._lock:
            self._frame_bytes = None

    # Compatibilidad con código anterior
    def start(self) -> bool:
        return self.start_stream()

    def stop(self) -> None:
        self.stop_stream()

    # ── Lectura de frames ──────────────────────────────────────────────
    def get_frame_bytes(self) -> bytes | None:
        with self._lock:
            return self._frame_bytes

    def get_frame_base64(self) -> str | None:
        with self._lock:
            if self._frame_bytes is None:
                return None
            return base64.b64encode(self._frame_bytes).decode("ascii")

    @staticmethod
    def _ensure_bgr(frame):
        """Asegura color BGR, incluyendo formatos Android como NV21/NV12."""
        if frame is None:
            return frame

        try:
            # Algunos dispositivos Android pueden entregar YUV apilado (h*3/2, w).
            if len(frame.shape) == 2:
                h, w = frame.shape[:2]
                if h % 3 == 0:
                    nv_h = (h * 2) // 3
                    yuv = frame.reshape((nv_h * 3 // 2, w))
                    try:
                        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV21)
                    except Exception:
                        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
                return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            if len(frame.shape) == 3 and frame.shape[2] == 4:
                return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        except Exception:
            pass
        return frame

    @staticmethod
    def _crop_smart(frame, target_width=260, target_height=160):
        """Recorta al ratio del visor sin reducir resolución original."""
        if frame is None:
            return frame

        h, w = frame.shape[:2]
        target_ratio = target_width / target_height
        current_ratio = w / h

        if current_ratio > target_ratio:
            new_w = int(h * target_ratio)
            start_x = (w - new_w) // 2
            frame = frame[:, start_x:start_x + new_w]
        elif current_ratio < target_ratio:
            new_h = int(w / target_ratio)
            start_y = (h - new_h) // 2
            frame = frame[start_y:start_y + new_h, :]

        return frame

    # ── Hilo de captura ────────────────────────────────────────────────
    def _loop(self) -> None:
        consecutive_failures = 0
        while self._running:
            try:
                cap = self._cap
                if cap is None:
                    break
                ret, frame = cap.read()
                if ret:
                    # 1. Recuperar Color
                    frame = self._ensure_bgr(frame)

                    # 2. Rotación según plataforma
                    if _is_android_runtime():
                        rot_mode = CAMERA_ROTATION
                        # El mismo .env suele tener CAMERA_ROTATION=none para Windows (sin girar).
                        # En Android OpenCV suele entregar buffer landscape en pantalla retrato → imagen torcida.
                        # Forzar "auto" aquí mantiene tablet sin giro y smartphone con 90ccw vía update_android_*.
                        if rot_mode in ("none", "0"):
                            rot_mode = "auto"
                        if rot_mode == "auto":
                            with _CAMERA_ANDROID_AUTO_LOCK:
                                actual_rot = _ANDROID_AUTO_ROT_VALUE

                            if actual_rot == "90cw":
                                frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
                            elif actual_rot == "90ccw":
                                frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
                            elif actual_rot == "180":
                                frame = cv2.rotate(frame, cv2.ROTATE_180)
                            # Si es "none", se deja el frame original.
                        elif rot_mode == "90ccw":
                            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
                        elif rot_mode == "90cw":
                            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
                        elif rot_mode == "180":
                            frame = cv2.rotate(frame, cv2.ROTATE_180)
                    elif CAMERA_ROTATION == "90ccw":
                        frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
                    elif CAMERA_ROTATION == "90cw":
                        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
                    elif CAMERA_ROTATION == "180":
                        frame = cv2.rotate(frame, cv2.ROTATE_180)

                    # 3. Eliminar franja lateral recortando al ratio del visor
                    frame = self._crop_smart(frame, target_width=260, target_height=160)

                    # 4. Efecto Espejo
                    if CAMERA_MIRROR:
                        frame = cv2.flip(frame, 1)

                    # 5. Compresión de ALTA CALIDAD (90) y TAMAÑO ORIGINAL
                    ok, buf = cv2.imencode(
                        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90]
                    )
                    if ok:
                        with self._lock:
                            self._frame_bytes = buf.tobytes()
                        self._active = True
                        consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= self._MAX_FAILURES:
                        self._active = False
                        self._error = "Cámara desconectada (sin señal)."
                        print(f"[CAMERA] {self._error}")
                        break
                    time.sleep(0.1)
            except Exception as exc:
                consecutive_failures += 1
                self._active = False
                self._error = str(exc)
                print(f"[CAMERA] Error: {exc}")
                if consecutive_failures >= self._MAX_FAILURES:
                    break
                time.sleep(0.5)
            time.sleep(self._interval)

        self._active = False
        self._running = False
        print("[CAMERA] Hilo finalizado.")


# ─────────────────────────────────────────────────────────────────────
# Aplicación Kiosco
# ─────────────────────────────────────────────────────────────────────

class KioskApp:
    """Controlador principal de la aplicación de control de asistencia."""

    def __init__(self, page: ft.Page):
        self.page = page
        self.device_name = socket.gethostname()[:_DEVICE_NAME_MAX]
        self.pin: str = ""
        self._reset_task: asyncio.Task | None = None
        self._idle_task:  asyncio.Task | None = None   # temporizador de auto-apagado de cámara
        self._input_locked = False  # bloqueo temporal del teclado (rechazo biométrico)

        # La cámara se crea pero NO se inicia hasta el primer toque del teclado
        self._camera = CameraStream(camera_index=CAMERA_INDEX, fps=CAMERA_FPS)

        self._setup_page()
        self._build_ui()
        asyncio.create_task(self._clock_task())
        asyncio.create_task(self._camera_update_task())
        asyncio.create_task(self._sync_offline_then_refresh())

    # ──────────────────────────────────────────────────
    # Modo offline — cola local y sincronización
    # ──────────────────────────────────────────────────

    def _load_pending_marks(self) -> list:
        with _OFFLINE_FILE_LOCK:
            if not os.path.exists(OFFLINE_DATA_FILE):
                return []
            try:
                with open(OFFLINE_DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                return []

    def _save_pending_marks(self, marcas: list) -> None:
        os.makedirs(OFFLINE_DIR, exist_ok=True)
        with _OFFLINE_FILE_LOCK:
            with open(OFFLINE_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(marcas, f, indent=4, ensure_ascii=False)

    def _merge_pending_after_sync(self, restantes: list, snapshot_fotos: frozenset[str]) -> None:
        """
        Evita pisar marcas guardadas mientras corría la sync: conserva entradas nuevas
        cuyo foto_local no estaba en el snapshot inicial.
        """
        os.makedirs(OFFLINE_DIR, exist_ok=True)
        with _OFFLINE_FILE_LOCK:
            current: list = []
            if os.path.exists(OFFLINE_DATA_FILE):
                try:
                    with open(OFFLINE_DATA_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    current = data if isinstance(data, list) else []
                except (json.JSONDecodeError, OSError):
                    current = []
            nuevos = [
                m
                for m in current
                if isinstance(m, dict)
                and m.get("foto_local")
                and m["foto_local"] not in snapshot_fotos
            ]
            merged = restantes + nuevos
            with open(OFFLINE_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=4, ensure_ascii=False)

    def _save_offline(self, pin: str, photo_bytes: bytes) -> None:
        """Guarda la marca y la foto en el almacenamiento local."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        filename = f"off_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{pin}.jpg"
        photo_path = os.path.join(OFFLINE_PHOTOS, filename)

        os.makedirs(OFFLINE_PHOTOS, exist_ok=True)
        with _OFFLINE_FILE_LOCK:
            with open(photo_path, "wb") as f:
                f.write(photo_bytes)

            nueva_marca = {
                "pin": pin,
                "fecha_hora": timestamp,
                "foto_local": filename,
                "pc": self.device_name,
            }
            marcas_pendientes = self._load_pending_marks()
            marcas_pendientes.append(nueva_marca)
            self._save_pending_marks(marcas_pendientes)

        print(f"[OFFLINE] Marca guardada localmente para PIN {pin}")

    def _sync_offline_marks_worker(self) -> None:
        """Intenta subir las marcas pendientes al servidor (ejecutar en hilo)."""
        if not os.path.exists(OFFLINE_DATA_FILE):
            return

        with _OFFLINE_FILE_LOCK:
            try:
                with open(OFFLINE_DATA_FILE, "r", encoding="utf-8") as f:
                    pendientes = json.load(f)
            except (json.JSONDecodeError, OSError):
                return

        if not isinstance(pendientes, list) or not pendientes:
            return

        snapshot_fotos = frozenset(
            m["foto_local"]
            for m in pendientes
            if isinstance(m, dict) and m.get("foto_local")
        )

        print(f"[SYNC] Intentando subir {len(pendientes)} marcas pendientes...")
        restantes: list = []

        for marca in pendientes:
            if not isinstance(marca, dict):
                continue
            pin = marca.get("pin")
            foto_name = marca.get("foto_local")
            fecha_hora = marca.get("fecha_hora")
            if not pin or not foto_name or not fecha_hora:
                continue

            foto_path = os.path.join(OFFLINE_PHOTOS, foto_name)
            if not os.path.exists(foto_path):
                continue

            try:
                with open(foto_path, "rb") as f:
                    foto_bytes = f.read()
                pc_envio = str(
                    marca.get("pc") or self.device_name or "Tablet-Android"
                )[:_DEVICE_NAME_MAX]
                resp = requests.post(
                    API_URL,
                    files={"foto": (foto_name, foto_bytes, "image/jpeg")},
                    data={
                        "pin": str(pin),
                        "fecha_manual": str(fecha_hora),
                        "pc": pc_envio,
                    },
                    timeout=API_TIMEOUT_SYNC,
                )
                if resp.status_code == 200:
                    try:
                        os.remove(foto_path)
                    except OSError:
                        pass
                    print(f"[SYNC] Sincronizado PIN {pin}")
                elif resp.status_code == 404:
                    # PIN inválido: no reintentar; libera la cola y el disco.
                    try:
                        if os.path.exists(foto_path):
                            os.remove(foto_path)
                    except OSError:
                        pass
                    print(f"[SYNC] PIN {pin} inválido. Descartado.")
                else:
                    restantes.append(marca)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                restantes.append(marca)
            except OSError:
                restantes.append(marca)

        self._merge_pending_after_sync(restantes, snapshot_fotos)

    async def _sync_offline_marks(self) -> None:
        await asyncio.to_thread(self._sync_offline_marks_worker)

    async def _sync_offline_then_refresh(self) -> None:
        await self._sync_offline_marks()
        self._update_offline_counter()

    async def _offline_counter_refresh_delayed(self) -> None:
        """En Android el FS a veces retrasa la lectura del JSON tras guardar; un segundo paint ayuda."""
        await asyncio.sleep(0.25)
        self._update_offline_counter()

    def _update_offline_counter(self) -> None:
        """Revisa el JSON y actualiza el contador en pantalla."""
        if os.path.exists(OFFLINE_DATA_FILE):
            try:
                with open(OFFLINE_DATA_FILE, "r", encoding="utf-8") as f:
                    pendientes = json.load(f)
                if isinstance(pendientes, list):
                    count = len(pendientes)
                    if count > 0:
                        self.lbl_offline_count.value = (
                            f"⚠️ {count} marcas pendientes de sincronizar"
                        )
                        self.lbl_offline_count.visible = True
                    else:
                        self.lbl_offline_count.visible = False
                else:
                    self.lbl_offline_count.visible = False
            except Exception:
                self.lbl_offline_count.visible = False
        else:
            self.lbl_offline_count.visible = False
        self.page.update()

    # ──────────────────────────────────────────────────
    # Configuración de la página
    # ──────────────────────────────────────────────────
    def _setup_page(self) -> None:
        p = self.page
        p.title = "Control de Asistencia"
        p.bgcolor = CLR_BG
        p.padding = 0
        p.spacing = 0
        p.theme_mode = _PAGE_THEME_MODE

        # Sin medidas fijas: la ventana puede redimensionarse (PC) y la tablet rotar.
        # p.window.width     = 480
        # p.window.height    = 900
        # p.window.resizable = False

        p.fonts = {
            "RobotoMono": (
                "https://fonts.gstatic.com/s/robotomono/v23/"
                "L0xuDF4xlVMF-BfR8bXMIhJHg45mwgGEFl0_3vq_ROW4.woff2"
            )
        }
        p.on_resize = self.on_page_resize

    def on_page_resize(self, e) -> None:
        """Redibuja la UI al cambiar tamaño u orientación (evita repaints si no cambia)."""
        if getattr(self, "_ui_building", False):
            return
        key = (int(self.page.width or 0), int(self.page.height or 0))
        if key == getattr(self, "_last_resize_layout_key", None):
            return
        self._build_ui()

    # ──────────────────────────────────────────────────
    # Construcción de la UI
    # ──────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self._ui_building = True
        try:
            self.page.controls.clear()

            pw = self.page.width or 0
            ph = self.page.height or 0
            if pw <= 0 or ph <= 0:
                try:
                    if pw <= 0:
                        pw = int(self.page.window.width or 0)
                    if ph <= 0:
                        ph = int(self.page.window.height or 0)
                except (TypeError, ValueError, AttributeError):
                    pw, ph = pw or 0, ph or 0

            is_tablet = pw > 600
            is_landscape = pw > ph

            if is_tablet:
                ui_width = 450
                cam_h = int(ui_width * 0.6)
                pin_h, btn_h = 80, 70
            else:
                ui_width = 300
                cam_h = int(ui_width * 0.6)
                pin_h, btn_h = 72, 48

            now = datetime.now()
            # ── Reloj ──
            self.lbl_hora = ft.Text(
                value=now.strftime("%H:%M:%S"),
                size=38,
                weight=ft.FontWeight.BOLD,
                color=CLR_TEXT,
                font_family="RobotoMono",
                text_align=ft.TextAlign.CENTER,
            )
            self.lbl_fecha = ft.Text(
                value=now.strftime("%A, %d de %B de %Y").upper(),
                size=11,
                color=CLR_SUBTEXT,
                text_align=ft.TextAlign.CENTER,
                style=ft.TextStyle(letter_spacing=2),
            )

            header = ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(
                            "CONTROL DE ASISTENCIA",
                            size=10,
                            color=CLR_SUBTEXT,
                            weight=ft.FontWeight.W_500,
                            text_align=ft.TextAlign.CENTER,
                            style=ft.TextStyle(letter_spacing=3),
                        ),
                        ft.Container(height=2),
                        self.lbl_hora,
                        self.lbl_fecha,
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=2,
                ),
                padding=ft.Padding.symmetric(vertical=10, horizontal=16),
                alignment=ft.Alignment.CENTER,
            )

            # ── Vista previa de cámara embebida ──
            self._camera_image = ft.Image(
                src="data:image/jpeg;base64," + self._placeholder_base64(),
                width=ui_width,
                height=cam_h,
                fit=ft.BoxFit.COVER,
                border_radius=ft.BorderRadius(10, 10, 10, 10),
                gapless_playback=True,
            )
            self._cam_status = ft.Text(
                "",
                size=12,
                color=CLR_ERROR,
                text_align=ft.TextAlign.CENTER,
                visible=False,
            )

            self._badge_dot = ft.Container(
                width=7,
                height=7,
                bgcolor=CLR_CAM_IDLE,
                border_radius=4,
            )
            self._badge_text = ft.Text(
                "INACTIVA",
                size=10,
                color=CLR_BADGE,
                weight=ft.FontWeight.BOLD,
            )
            self._cam_border = ft.Container(
                width=ui_width,
                height=cam_h,
                border_radius=10,
                border=ft.Border.all(2, CLR_BORDER),
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                content=ft.Stack(
                    controls=[
                        self._camera_image,
                        ft.Container(
                            content=ft.Row(
                                controls=[self._badge_dot, self._badge_text],
                                spacing=4,
                            ),
                            padding=ft.Padding.symmetric(horizontal=6, vertical=3),
                            bgcolor="#A0000000",
                            border_radius=ft.BorderRadius(0, 0, 0, 10),
                            top=0,
                            left=0,
                        ),
                    ],
                ),
            )
            camera_container = self._cam_border

            # ── Display PIN ──
            self.lbl_pin = ft.Text(
                value="●" * len(self.pin),
                size=36,
                weight=ft.FontWeight.BOLD,
                color=CLR_TEXT,
                text_align=ft.TextAlign.CENTER,
                font_family="RobotoMono",
            )
            self.lbl_pin_hint = ft.Text(
                "Ingrese su PIN" if not self.pin else f"{len(self.pin)} / {PIN_MAX_LENGTH}",
                size=12,
                color=CLR_SUBTEXT,
                text_align=ft.TextAlign.CENTER,
                style=ft.TextStyle(letter_spacing=1),
            )

            pin_display = ft.Container(
                content=ft.Column(
                    controls=[self.lbl_pin_hint, self.lbl_pin],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=1,
                ),
                width=ui_width,
                height=pin_h,
                bgcolor=CLR_SURFACE,
                border_radius=14,
                border=ft.Border.all(1, CLR_BORDER),
                alignment=ft.Alignment.CENTER,
                padding=ft.Padding.symmetric(horizontal=20, vertical=6),
                shadow=ft.BoxShadow(
                    spread_radius=0, blur_radius=8,
                    color=CLR_SHADOW_PANEL, offset=ft.Offset(0, 2),
                ) if CLR_SHADOW_PANEL != "#00000000" else None,
            )

            # ── Feedback (mensajes de confirmación) ──
            fuente_mensaje = 26 if is_tablet else 16
            self._loading_ring = ft.ProgressRing(
                width=30,
                height=30,
                stroke_width=3,
                color=CLR_ACCENT,
                visible=False,
            )
            self.lbl_mensaje = ft.Text(
                value="",
                size=fuente_mensaje,
                weight=ft.FontWeight.BOLD,
                text_align=ft.TextAlign.CENTER,
                visible=False,
            )
            self.lbl_offline_count = ft.Text(
                value="",
                color=ft.Colors.ORANGE_700,
                weight=ft.FontWeight.BOLD,
                size=12,
                text_align=ft.TextAlign.CENTER,
                visible=False,
            )
            feedback_area = ft.Container(
                content=ft.Column(
                    controls=[
                        self._loading_ring,
                        self.lbl_mensaje,
                        self.lbl_offline_count,
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=10,
                ),
                padding=ft.Padding.symmetric(vertical=15),
                alignment=ft.Alignment.CENTER,
                width=ui_width,
            )

            pad = self._build_pinpad(ui_width, is_tablet, btn_h)

            if is_landscape and is_tablet:
                col_izq = ft.Column(
                    controls=[
                        header,
                        ft.Divider(height=1, color=CLR_BORDER),
                        ft.Container(height=10),
                        camera_container,
                        self._cam_status,
                        ft.Container(height=10),
                        feedback_area,
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                )
                col_der = ft.Column(
                    controls=[
                        pin_display,
                        ft.Container(height=15),
                        pad,
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                )
                layout = ft.Row(
                    controls=[col_izq, col_der],
                    alignment=ft.MainAxisAlignment.CENTER,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=50,
                )
                self.page.add(
                    ft.Container(
                        content=layout,
                        alignment=ft.Alignment.CENTER,
                        expand=True,
                        padding=ft.Padding.symmetric(vertical=20),
                    )
                )
            else:
                self.page.add(
                    ft.Column(
                        controls=[
                            header,
                            ft.Divider(height=1, color=CLR_BORDER),
                            ft.Container(height=4),
                            camera_container,
                            self._cam_status,
                            ft.Container(height=4),
                            pin_display,
                            ft.Container(height=2),
                            feedback_area,
                            ft.Container(height=2),
                            pad,
                            ft.Container(height=4),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=0,
                        scroll=ft.ScrollMode.AUTO,
                    )
                )

            self._last_resize_layout_key = (
                int(self.page.width or 0),
                int(self.page.height or 0),
            )
            update_android_camera_auto_form_factor(pw, ph)
            self._update_offline_counter()
        finally:
            self._ui_building = False

    def _build_pinpad(
        self, ui_width: int, is_tablet: bool, btn_h: int
    ) -> ft.Container:
        SIZE    = 90 if is_tablet else 62
        SPACING = 12 if is_tablet else 8

        def num_btn(label: str) -> ft.Container:
            return ft.Container(
                content=ft.Text(
                    label, size=22, weight=ft.FontWeight.BOLD,
                    color=CLR_ON_ACCENT, text_align=ft.TextAlign.CENTER,
                ),
                width=SIZE, height=SIZE,
                bgcolor=CLR_ACCENT, border_radius=12,
                alignment=ft.Alignment.CENTER,
                on_click=lambda e, d=label: self._on_digit(d),
                ink=True,
                shadow=ft.BoxShadow(
                    spread_radius=0, blur_radius=6,
                    color=CLR_SHADOW_NUM, offset=ft.Offset(0, 3),
                ),
            )

        def action_btn(label: str, color: str, handler) -> ft.Container:
            return ft.Container(
                content=ft.Text(
                    label, size=12 if len(label) > 3 else 22,
                    weight=ft.FontWeight.BOLD,
                    color=CLR_ON_ACCENT, text_align=ft.TextAlign.CENTER,
                    max_lines=2, overflow=ft.TextOverflow.VISIBLE,
                ),
                width=SIZE, height=SIZE, bgcolor=color,
                border_radius=12, alignment=ft.Alignment.CENTER,
                on_click=handler, ink=True,
            )

        rows_digits = [["1","2","3"], ["4","5","6"], ["7","8","9"]]
        digit_rows = [
            ft.Row(
                controls=[num_btn(d) for d in row],
                alignment=ft.MainAxisAlignment.CENTER, spacing=SPACING,
            )
            for row in rows_digits
        ]

        last_row = ft.Row(
            controls=[
                action_btn("⌫", CLR_DEL,   lambda e: self._on_backspace()),
                num_btn("0"),
                action_btn("✓", CLR_ENTER, lambda e: asyncio.create_task(self._on_enter())),
            ],
            alignment=ft.MainAxisAlignment.CENTER, spacing=SPACING,
        )

        self._btn_icon    = ft.Icon(ft.Icons.CAMERA_ALT, color=CLR_ON_ACCENT, size=18)
        self._btn_label   = ft.Text(
            "  MARCAR ASISTENCIA", size=14,
            weight=ft.FontWeight.BOLD, color=CLR_ON_ACCENT,
            style=ft.TextStyle(letter_spacing=1.2),
        )
        self._btn_spinner = ft.ProgressRing(
            width=18, height=18, stroke_width=3, color=CLR_ON_ACCENT, visible=False,
        )
        self.btn_marcar = ft.Container(
            content=ft.Row(
                controls=[self._btn_icon, self._btn_spinner, self._btn_label],
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            width=ui_width + 20,
            height=btn_h,
            bgcolor=CLR_ENTER, border_radius=14,
            alignment=ft.Alignment.CENTER,
            on_click=lambda e: asyncio.create_task(self._on_enter()),
            ink=True,
            shadow=ft.BoxShadow(
                spread_radius=0, blur_radius=10,
                color=CLR_SHADOW_ENTER, offset=ft.Offset(0, 3),
            ),
        )

        pinpad_shadow = None
        if CLR_SHADOW_PANEL != "#00000000":
            pinpad_shadow = ft.BoxShadow(
                spread_radius=0, blur_radius=12,
                color=CLR_SHADOW_PANEL, offset=ft.Offset(0, 2),
            )

        return ft.Container(
            content=ft.Column(
                controls=[*digit_rows, last_row,
                           ft.Container(height=4), self.btn_marcar],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=SPACING,
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            bgcolor=CLR_SURFACE, border_radius=18,
            border=ft.Border.all(1, CLR_BORDER),
            shadow=pinpad_shadow,
            width=ui_width + 40,
        )

    # ──────────────────────────────────────────────────
    # Corrutinas de fondo
    # ──────────────────────────────────────────────────
    async def _clock_task(self) -> None:
        while True:
            now = datetime.now()
            self.lbl_hora.value  = now.strftime("%H:%M:%S")
            self.lbl_fecha.value = now.strftime("%A, %d de %B de %Y").upper()
            self.page.update()
            await asyncio.sleep(1)

    # ── Cámara on-demand ───────────────────────────────────────────────
    def _wake_camera(self) -> None:
        """
        Activa la cámara si estaba inactiva y reinicia el temporizador de
        auto-apagado.  Se llama en cada toque del teclado numérico.
        """
        if not self._camera.streaming:
            ok = self._camera.start_stream()
            print(f"[CAM] wake → start_stream {'OK' if ok else 'FALLÓ'}")
        # Cancelar timer anterior y arrancar uno nuevo
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._idle_timer_task())

    async def _idle_timer_task(self) -> None:
        """
        Espera CAMERA_IDLE_TIMEOUT segundos sin interacción y apaga la cámara.
        Se cancela y reinicia cada vez que _wake_camera() es invocado.
        """
        await asyncio.sleep(CAMERA_IDLE_TIMEOUT)
        self._camera.stop_stream()
        print(f"[CAM] Auto-apagado tras {CAMERA_IDLE_TIMEOUT}s sin interacción.")
        # Actualizar badge sin esperar al próximo tick de _camera_update_task
        self._badge_dot.bgcolor  = CLR_CAM_IDLE
        self._badge_text.value   = "INACTIVA"
        self._cam_border.border  = ft.Border.all(2, CLR_BORDER)
        self.page.update()

    async def _camera_update_task(self) -> None:
        """
        Refresca ft.Image con el último frame y gestiona el badge de estado.

        Optimización anti-parpadeo:
        - Los frames actualizan SOLO self._camera_image.update() (sin re-layout).
        - El badge se actualiza por separado SOLO cuando cambia el estado.
        - Nunca se llama self.page.update() desde este loop.

        Estados del badge
        -----------------
        INACTIVA   (gris)    → cámara apagada por inactividad o aún no despertada
        CONECTANDO (amarillo)→ hilo arrancado, esperando primeros frames
        EN VIVO    (verde)   → frames llegando correctamente
        SIN SEÑAL  (rojo)    → error de hardware / desconexión
        """
        interval    = 1.0 / UI_FPS
        _last_state = "idle"

        while True:
            await asyncio.sleep(interval)

            # ── Estado actual ──────────────────────────────────────────────
            if not self._camera.streaming:
                state = "idle"
            elif self._camera.active:
                state = "live"
            elif self._camera.error:
                state = "error"
            else:
                state = "connecting"

            # ── Frame: actualiza SOLO el control ft.Image (sin tocar el layout) ──
            if state in ("live", "connecting"):
                b64 = self._camera.get_frame_base64()
                if b64:
                    self._camera_image.src = "data:image/jpeg;base64," + b64
                    self._camera_image.update()

            # ── Badge: solo cuando cambia el estado ────────────────────────
            if state != _last_state:
                _last_state = state
                if state == "live":
                    self._badge_dot.bgcolor  = CLR_SUCCESS
                    self._badge_text.value   = "EN VIVO"
                    self._cam_border.border  = ft.Border.all(2, CLR_SUCCESS)
                elif state == "error":
                    self._badge_dot.bgcolor  = CLR_ERROR
                    self._badge_text.value   = "SIN SEÑAL"
                    self._cam_border.border  = ft.Border.all(2, CLR_ERROR)
                    self._cam_status.value   = f"⚠ {self._camera.error}"
                    self._cam_status.visible = True
                elif state == "connecting":
                    self._badge_dot.bgcolor  = CLR_WARNING
                    self._badge_text.value   = "CONECTANDO"
                    self._cam_border.border  = ft.Border.all(2, CLR_WARNING)
                else:  # idle
                    self._badge_dot.bgcolor  = CLR_CAM_IDLE
                    self._badge_text.value   = "INACTIVA"
                    self._cam_border.border  = ft.Border.all(2, CLR_BORDER)
                # Solo en cambio de estado actualizamos el contenedor (badge + borde)
                self._cam_border.update()

    # ──────────────────────────────────────────────────
    # Handlers del PIN Pad
    # ──────────────────────────────────────────────────
    def _on_digit(self, digit: str) -> None:
        if self._input_locked:
            return
        self._wake_camera()   # activa la cámara al primer toque y reinicia el timer
        if len(self.pin) < PIN_MAX_LENGTH:
            self.pin += digit
            self._refresh_pin_display()

    def _on_backspace(self) -> None:
        if self._input_locked:
            return
        self._wake_camera()   # también cuenta como interacción
        if self.pin:
            self.pin = self.pin[:-1]
            self._refresh_pin_display()

    # ──────────────────────────────────────────────────
    # Flujo principal: Marcar Asistencia
    # ──────────────────────────────────────────────────
    async def _on_enter(self) -> None:
        """
        Fase A: validación local de longitud de PIN.
        Fase B: pre-validación del PIN contra la BD.
        Fase C: captura instantánea del frame actual de la cámara y envío.
        """
        if self._input_locked:
            return
        self._wake_camera()   # garantiza que la cámara esté activa al marcar

        # ── A) Validación local ──────────────────────────────────────────
        if len(self.pin) != PIN_MAX_LENGTH:
            self._show_message(f"Ingrese {PIN_MAX_LENGTH} dígitos.", CLR_ERROR)
            self._reset_task = asyncio.create_task(self._auto_reset())
            return

        if self._reset_task and not self._reset_task.done():
            self._reset_task.cancel()

        pin_actual = self.pin
        self._set_btn_loading(True, "  Validando PIN...")
        self._set_loading_ring(True)

        # ── B) Pre-validación contra la BD ──────────────────────────────
        valido, nombre_o_error = await self._validar_pin_async(pin_actual)
        if not valido:
            self._show_message(nombre_o_error, CLR_ERROR)
            self._set_btn_loading(False)
            self._set_loading_ring(False)
            self._reset_task = asyncio.create_task(self._auto_reset())
            return

        # ── C) Captura instantánea del frame actual ──────────────────────
        self._set_btn_loading(True, "  Capturando foto...")
        foto_bytes = self._camera.get_frame_bytes()

        if not foto_bytes:
            print("[ERROR] No hay frame disponible de la cámara.")
            self._show_message("Cámara no disponible.\nContacte al administrador.", CLR_ERROR)
            self._set_btn_loading(False)
            self._set_loading_ring(False)
            self._reset_task = asyncio.create_task(self._auto_reset())
            return

        print(f"[OK] Frame capturado: {len(foto_bytes)} bytes → enviando a API...")
        self._set_btn_loading(True, "  Subiendo foto...")
        asyncio.create_task(
            self._call_api_async(pin_actual, foto_bytes, "captura.jpg", nombre_o_error)
        )

    # ──────────────────────────────────────────────────
    # Pre-validación ligera del PIN
    # ──────────────────────────────────────────────────
    async def _validar_pin_async(self, pin: str) -> tuple[bool, str]:
        def _do_check() -> requests.Response:
            return requests.post(
                API_VALIDATE_URL,
                json={"pin": pin},
                timeout=API_TIMEOUT_CHECK,
            )
        try:
            resp = await asyncio.to_thread(_do_check)
            if resp.status_code == 200:
                return True, resp.json().get("nombre_trabajador", "")
            try:
                detalle = resp.json().get("detail", "Código inválido.")
            except Exception:
                detalle = "Código inválido."
            return False, detalle
        except requests.exceptions.ConnectionError:
            # Sin red: permitir marcar y guardar offline (nombre vacío).
            return True, ""
        except requests.exceptions.Timeout:
            return True, ""
        except Exception as exc:
            print(f"[ERROR] validar_pin: {exc}")
            return False, "Error inesperado.\nContacte al administrador."

    # ──────────────────────────────────────────────────
    # Envío a la API con PIN + foto
    # ──────────────────────────────────────────────────
    @staticmethod
    def _score_pct(match_score: float | None) -> str:
        if match_score is None:
            return ""
        return f"{int(round(float(match_score) * 100))}%"

    async def _handle_biometric_reject(self) -> None:
        """Rechazo biométrico: bloquea teclado 3 s, mensaje rojo, reset sin tocar cola offline."""
        self._set_input_locked(True)
        self._show_message("ERROR: Acérquese a Recursos Humanos", CLR_REJECT)
        await asyncio.sleep(3)
        self._set_input_locked(False)
        self._reset_state()

    async def _call_api_async(
        self, pin: str, foto_bytes: bytes, foto_nombre: str, nombre_trabajador: str
    ) -> None:
        """Multipart: foto + pin + pc (nombre del equipo). Sin lectura de disco."""
        def _do_post() -> requests.Response:
            return requests.post(
                API_URL,
                files={"foto": (foto_nombre, foto_bytes, "image/jpeg")},
                data={"pin": pin, "pc": self.device_name},
                timeout=API_TIMEOUT_MARK,
            )

        biometric_rejected = False
        try:
            response = await asyncio.to_thread(_do_post)
            if response.status_code == 200:
                data        = response.json()
                nombre_t    = data.get("nombre_trabajador", nombre_trabajador or "").strip()
                biometria   = data.get("biometria", "omitida")
                match_score = data.get("match_score")
                pct         = self._score_pct(match_score)
                saludo      = f"¡Hola {nombre_t}!\n" if nombre_t else ""

                if biometria == "enrolado":
                    cuerpo = f"Ingreso registrado ✓ {pct}" if pct else "Ingreso registrado ✓"
                    msg    = f"{saludo}{cuerpo}\nRostro registrado ✓"
                    color  = CLR_SUCCESS
                elif biometria in ("aprobado", "verificado"):
                    cuerpo = f"Ingreso registrado ✓ {pct}" if pct else "Ingreso registrado ✓"
                    msg    = f"{saludo}{cuerpo}"
                    color  = CLR_SUCCESS
                elif biometria == "observacion":
                    cuerpo = f"Verificando identidad... {pct}" if pct else "Verificando identidad..."
                    msg    = f"{saludo}{cuerpo}"
                    color  = CLR_WARNING
                elif biometria == "sin_rostro":
                    msg   = f"{saludo}Ingreso registrado.\n(Foto sin rostro detectado)"
                    color = CLR_SUCCESS
                elif nombre_t:
                    msg   = f"{saludo}Ingreso registrado."
                    color = CLR_SUCCESS
                else:
                    msg   = "Ingreso registrado correctamente."
                    color = CLR_SUCCESS

                self._show_message(msg, color)
                asyncio.create_task(self._sync_offline_then_refresh())
            elif response.status_code == 403:
                try:
                    data = response.json()
                except Exception:
                    data = {}
                if data.get("biometria") == "rechazado" or data.get("status") == "rejected":
                    biometric_rejected = True
                    print(
                        f"[RECHAZO] Biometría score={data.get('match_score')} "
                        f"Person={data.get('nombre_trabajador', '')}"
                    )
                    await self._handle_biometric_reject()
                else:
                    try:
                        detalle = response.json().get("detail", "Acceso denegado.")
                    except Exception:
                        detalle = "Acceso denegado."
                    self._show_message(str(detalle), CLR_ERROR)
            else:
                try:
                    detalle = response.json().get("detail", "Error en el servidor.")
                except Exception:
                    detalle = "Error en el servidor."
                print(f"[ERROR] API {response.status_code}: {detalle}")
                self._show_message(detalle, CLR_ERROR)
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            print(f"[ERROR] Red no disponible (_call_api_async): {exc}")
            self._save_offline(pin, foto_bytes)
            self._show_message(
                "Sin conexión. Guardado localmente.",
                CLR_WARNING,
            )
            self._update_offline_counter()
            asyncio.create_task(self._offline_counter_refresh_delayed())
        except Exception as exc:
            print(f"[ERROR] _call_api_async inesperado: {exc}")
            self._show_message("Error inesperado.\nContacte al administrador.", CLR_ERROR)
        finally:
            self._set_btn_loading(False)
            self._set_loading_ring(False)
            if not biometric_rejected:
                self._reset_task = asyncio.create_task(self._auto_reset())

    async def _auto_reset(self) -> None:
        await asyncio.sleep(3)
        self._reset_state()

    # ──────────────────────────────────────────────────
    # Helpers de UI
    # ──────────────────────────────────────────────────
    def _set_input_locked(self, locked: bool) -> None:
        """Bloquea teclado numérico y botón marcar (p. ej. tras rechazo biométrico)."""
        self._input_locked = locked
        if hasattr(self, "btn_marcar"):
            self.btn_marcar.disabled = locked
        self.page.update()

    def _set_btn_loading(self, loading: bool, label: str = "  MARCAR ASISTENCIA") -> None:
        if self._input_locked:
            return
        self.btn_marcar.disabled  = loading
        self.btn_marcar.opacity   = 0.6 if loading else 1.0
        self._btn_icon.visible    = not loading
        self._btn_spinner.visible = loading
        self._btn_label.value     = label if loading else "  MARCAR ASISTENCIA"
        self.page.update()

    def _set_loading_ring(self, visible: bool) -> None:
        self._loading_ring.visible = visible
        self.lbl_mensaje.visible   = False if visible else self.lbl_mensaje.visible
        self.page.update()

    def _refresh_pin_display(self) -> None:
        self.lbl_pin.value         = "●" * len(self.pin)
        self.lbl_pin_hint.value    = (
            "Ingrese su PIN" if not self.pin else f"{len(self.pin)} / {PIN_MAX_LENGTH}"
        )
        self.lbl_mensaje.visible   = False
        self._loading_ring.visible = False
        self.page.update()

    def _show_message(self, text: str, color: str) -> None:
        self._loading_ring.visible = False
        self.lbl_mensaje.value     = text
        self.lbl_mensaje.color     = color
        self.lbl_mensaje.visible   = True
        self.page.update()

    def _reset_state(self) -> None:
        self.pin                   = ""
        self.lbl_pin.value         = ""
        self.lbl_pin_hint.value    = "Ingrese su PIN"
        self.lbl_mensaje.visible   = False
        self._loading_ring.visible = False
        self._update_offline_counter()

    @staticmethod
    def _placeholder_base64() -> str:
        """Frame negro JPEG como placeholder mientras carga la cámara (tamaño interno fijo)."""
        import numpy as np
        img = np.zeros((160, 260, 3), dtype="uint8")
        _, buf = cv2.imencode(".jpg", img)
        return base64.b64encode(buf.tobytes()).decode("ascii")


# ─────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────
async def main(page: ft.Page) -> None:
    KioskApp(page)


if __name__ == "__main__":
    ft.run(main)
