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

# Cargar configuración opcional desde .env (misma carpeta del proyecto).
load_dotenv()

# ── Constantes ────────────────────────────────────────────────────────
PIN_MAX_LENGTH    = 4
# Coincide con columna PC varchar(100) en SQL Server
_DEVICE_NAME_MAX = 100
API_BASE_URL      = os.getenv("API_BASE_URL", "http://179.61.14.224:8000").rstrip("/")
API_URL           = f"{API_BASE_URL}/api/marcar-asistencia"
API_VALIDATE_URL  = f"{API_BASE_URL}/api/validar-pin"
# Tiempos cortos para que, sin red o con servidor lento, el modo offline entre en ~1–2 s.
API_TIMEOUT_CHECK = 1.5
API_MARK_TIMEOUT = (2, 5)  # (conexión, lectura) para POST /marcar-asistencia
API_TIMEOUT_SYNC = 20  # cola offline: puede ir por red débil; reintentos posteriores
CAMERA_INDEX        = 1   # Predeterminado selfie (frontal); usar 0 si el dispositivo no expone índice 1
CAMERA_FPS          = 12  # 12 fps es más que suficiente para una foto de asistencia; reduce CPU ~60 %
UI_FPS              = 10  # Velocidad de refresco del ft.Image en pantalla
CAMERA_IDLE_TIMEOUT = 60  # Segundos sin interacción antes de apagar la cámara automáticamente
CAMERA_ROTATION     = os.getenv("CAMERA_ROTATION", "90ccw").strip().lower()
CAMERA_MIRROR       = os.getenv("CAMERA_MIRROR", "false").strip().lower() in {"1", "true", "yes", "si"}

# Directorio offline: en Android se intenta Documentos (visible en archivos) solo si hay
# escritura real; si no (scoped storage sin permiso), se usa carpeta interna de la app.
# Así el JSON y el contador funcionan igual en APK que en PC.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_EXTERNAL_OFFLINE = "/storage/emulated/0/Documents/AsistenciaOffline"
_INTERNAL_OFFLINE = os.path.join(_APP_DIR, "offline_storage")


def _pick_offline_dir() -> str:
    if os.path.isdir("/storage/emulated/0"):
        try:
            base = _EXTERNAL_OFFLINE
            os.makedirs(os.path.join(base, "photos"), exist_ok=True)
            probe = os.path.join(base, ".marcador_write_probe")
            with open(probe, "w", encoding="utf-8") as pf:
                pf.write("ok")
            os.remove(probe)
            return base
        except OSError:
            pass
    return _INTERNAL_OFFLINE


OFFLINE_DIR = _pick_offline_dir()
OFFLINE_PHOTOS = os.path.join(OFFLINE_DIR, "photos")
OFFLINE_DATA_FILE = os.path.join(OFFLINE_DIR, "pending_marks.json")
try:
    os.makedirs(OFFLINE_PHOTOS, exist_ok=True)
except OSError:
    OFFLINE_DIR = _INTERNAL_OFFLINE
    OFFLINE_PHOTOS = os.path.join(OFFLINE_DIR, "photos")
    OFFLINE_DATA_FILE = os.path.join(OFFLINE_DIR, "pending_marks.json")
    os.makedirs(OFFLINE_PHOTOS, exist_ok=True)

print(f"[OFFLINE] Carpeta de cola: {OFFLINE_DIR}")

_OFFLINE_FILE_LOCK = threading.RLock()

# ── Paleta corporativa ────────────────────────────────────────────────
CLR_BG      = "#0D1B2A"
CLR_SURFACE = "#1B2D42"
CLR_ACCENT  = "#2563EB"
CLR_SUCCESS = "#16A34A"
CLR_ERROR   = "#DC2626"
CLR_TEXT    = "#F1F5F9"
CLR_SUBTEXT = "#94A3B8"
CLR_DEL     = "#374151"
CLR_ENTER   = "#16A34A"
CLR_WARNING = "#EA580C"


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

                    # 2. Rotación
                    if CAMERA_ROTATION == "90ccw":
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
                    print(f"[SYNC] PIN {pin} no existe en BD. Descartando marca offline.")
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
        p.title        = "Control de Asistencia"
        p.bgcolor      = CLR_BG
        p.padding      = 0
        p.spacing      = 0
        p.theme_mode   = ft.ThemeMode.DARK
        p.window.width     = 480
        p.window.height    = 900
        p.window.resizable = False
        p.fonts = {
            "RobotoMono": (
                "https://fonts.gstatic.com/s/robotomono/v23/"
                "L0xuDF4xlVMF-BfR8bXMIhJHg45mwgGEFl0_3vq_ROW4.woff2"
            )
        }

    # ──────────────────────────────────────────────────
    # Construcción de la UI
    # ──────────────────────────────────────────────────
    def _build_ui(self) -> None:
        # ── Reloj ──
        self.lbl_hora = ft.Text(
            value="00:00:00", size=38, weight=ft.FontWeight.BOLD,
            color=CLR_TEXT, font_family="RobotoMono",
            text_align=ft.TextAlign.CENTER,
        )
        self.lbl_fecha = ft.Text(
            value="", size=11, color=CLR_SUBTEXT,
            text_align=ft.TextAlign.CENTER,
            style=ft.TextStyle(letter_spacing=2),
        )

        header = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text(
                        "CONTROL DE ASISTENCIA", size=10, color=CLR_SUBTEXT,
                        weight=ft.FontWeight.W_500,
                        text_align=ft.TextAlign.CENTER,
                        style=ft.TextStyle(letter_spacing=3),
                    ),
                    ft.Container(height=2),
                    self.lbl_hora, self.lbl_fecha,
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=2,
            ),
            padding=ft.Padding.symmetric(vertical=10, horizontal=16),
            alignment=ft.Alignment.CENTER,
        )

        # ── Vista previa de cámara embebida ──
        # Se actualiza en tiempo real por _camera_update_task.
        # Si no hay cámara disponible muestra un placeholder.
        placeholder_src = "data:image/jpeg;base64," + self._placeholder_base64()
        self._camera_image = ft.Image(
            src=placeholder_src,
            width=260, height=160,
            fit=ft.BoxFit.COVER,
            border_radius=ft.BorderRadius(10, 10, 10, 10),
        )
        self._cam_status = ft.Text(
            "",
            size=12, color=CLR_ERROR,
            text_align=ft.TextAlign.CENTER,
            visible=False,   # se activa desde _camera_update_task si hay error
        )

        # Badge dinámico: dot + texto. Se actualiza desde _camera_update_task.
        # Estados: CONECTANDO (amarillo) → EN VIVO (verde) → SIN SEÑAL (rojo)
        self._badge_dot = ft.Container(
            width=7, height=7,
            bgcolor="#F59E0B",   # amarillo = conectando
            border_radius=4,
        )
        self._badge_text = ft.Text(
            "CONECTANDO", size=10, color=CLR_TEXT, weight=ft.FontWeight.BOLD,
        )
        self._cam_border = ft.Container(   # borde del contenedor, referencia para cambiar color
            width=260, height=160,
            border_radius=10,
            border=ft.Border.all(2, "#F59E0B"),   # amarillo inicial
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
                        top=0, left=0,
                    ),
                ],
            ),
        )
        camera_container = self._cam_border

        # ── Display PIN ──
        self.lbl_pin = ft.Text(
            value="", size=36, weight=ft.FontWeight.BOLD,
            color=CLR_TEXT, text_align=ft.TextAlign.CENTER,
            font_family="RobotoMono",
        )
        self.lbl_pin_hint = ft.Text(
            "Ingrese su PIN", size=12, color=CLR_SUBTEXT,
            text_align=ft.TextAlign.CENTER,
            style=ft.TextStyle(letter_spacing=1),
        )

        pin_display = ft.Container(
            content=ft.Column(
                controls=[self.lbl_pin_hint, self.lbl_pin],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=1,
            ),
            width=260, height=72,
            bgcolor=CLR_SURFACE, border_radius=14,
            border=ft.Border.all(1, "#2D4060"),
            alignment=ft.Alignment.CENTER,
            padding=ft.Padding.symmetric(horizontal=20, vertical=6),
        )

        # ── Feedback ──
        self._loading_ring = ft.ProgressRing(
            width=26, height=26, stroke_width=3,
            color=CLR_ACCENT, visible=False,
        )
        self.lbl_mensaje = ft.Text(
            value="", size=15, weight=ft.FontWeight.BOLD,
            text_align=ft.TextAlign.CENTER, visible=False,
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
                spacing=2,
            ),
            padding=ft.Padding.symmetric(vertical=4),
            alignment=ft.Alignment.CENTER,
        )

        # ── PIN Pad ──
        pad = self._build_pinpad()

        self.page.add(
            ft.Column(
                controls=[
                    header,
                    ft.Divider(height=1, color="#1E3A5F"),
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

    def _build_pinpad(self) -> ft.Container:
        SIZE    = 62
        SPACING = 8

        def num_btn(label: str) -> ft.Container:
            return ft.Container(
                content=ft.Text(
                    label, size=22, weight=ft.FontWeight.BOLD,
                    color=CLR_TEXT, text_align=ft.TextAlign.CENTER,
                ),
                width=SIZE, height=SIZE,
                bgcolor=CLR_ACCENT, border_radius=12,
                alignment=ft.Alignment.CENTER,
                on_click=lambda e, d=label: self._on_digit(d),
                ink=True,
                shadow=ft.BoxShadow(
                    spread_radius=0, blur_radius=6,
                    color="#1D4ED860", offset=ft.Offset(0, 3),
                ),
            )

        def action_btn(label: str, color: str, handler) -> ft.Container:
            return ft.Container(
                content=ft.Text(
                    label, size=12 if len(label) > 3 else 22,
                    weight=ft.FontWeight.BOLD,
                    color=CLR_TEXT, text_align=ft.TextAlign.CENTER,
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

        self._btn_icon    = ft.Icon(ft.Icons.CAMERA_ALT, color=CLR_TEXT, size=18)
        self._btn_label   = ft.Text(
            "  MARCAR ASISTENCIA", size=14,
            weight=ft.FontWeight.BOLD, color=CLR_TEXT,
            style=ft.TextStyle(letter_spacing=1.2),
        )
        self._btn_spinner = ft.ProgressRing(
            width=18, height=18, stroke_width=3, color=CLR_TEXT, visible=False,
        )
        self.btn_marcar = ft.Container(
            content=ft.Row(
                controls=[self._btn_icon, self._btn_spinner, self._btn_label],
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            width=280, height=48,
            bgcolor=CLR_ENTER, border_radius=14,
            alignment=ft.Alignment.CENTER,
            on_click=lambda e: asyncio.create_task(self._on_enter()),
            ink=True,
            shadow=ft.BoxShadow(
                spread_radius=0, blur_radius=10,
                color="#16A34A50", offset=ft.Offset(0, 3),
            ),
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
            border=ft.Border.all(1, "#2D4060"), width=300,
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
        self._badge_dot.bgcolor  = "#64748B"
        self._badge_text.value   = "INACTIVA"
        self._cam_border.border  = ft.Border.all(2, "#64748B")
        self.page.update()

    async def _camera_update_task(self) -> None:
        """
        Refresca ft.Image con el último frame y gestiona el badge de estado.

        Estados del badge
        -----------------
        INACTIVA   (gris)    → cámara apagada por inactividad o aún no despertada
        CONECTANDO (amarillo)→ hilo arrancado, esperando primeros frames
        EN VIVO    (verde)   → frames llegando correctamente
        SIN SEÑAL  (rojo)    → error de hardware / desconexión
        """
        interval    = 1.0 / UI_FPS
        _last_state = "idle"   # estado inicial: cámara no arrancada

        while True:
            # Determinar estado actual
            if not self._camera.streaming:
                state = "idle"
            elif self._camera.active:
                state = "live"
            elif self._camera.error:
                state = "error"
            else:
                state = "connecting"

            # Actualizar imagen sólo cuando la cámara está viva
            if state in ("live", "connecting"):
                b64 = self._camera.get_frame_base64()
                if b64:
                    self._camera_image.src = "data:image/jpeg;base64," + b64

            # Actualizar badge sólo cuando cambia el estado (evita renders innecesarios)
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
                    self._badge_dot.bgcolor  = "#F59E0B"
                    self._badge_text.value   = "CONECTANDO"
                    self._cam_border.border  = ft.Border.all(2, "#F59E0B")
                else:  # idle
                    self._badge_dot.bgcolor  = "#64748B"   # gris
                    self._badge_text.value   = "INACTIVA"
                    self._cam_border.border  = ft.Border.all(2, "#64748B")

            self.page.update()
            await asyncio.sleep(interval)

    # ──────────────────────────────────────────────────
    # Handlers del PIN Pad
    # ──────────────────────────────────────────────────
    def _on_digit(self, digit: str) -> None:
        self._wake_camera()   # activa la cámara al primer toque y reinicia el timer
        if len(self.pin) < PIN_MAX_LENGTH:
            self.pin += digit
            self._refresh_pin_display()

    def _on_backspace(self) -> None:
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
    async def _call_api_async(
        self, pin: str, foto_bytes: bytes, foto_nombre: str, nombre_trabajador: str
    ) -> None:
        """Multipart: foto + pin + pc (nombre del equipo). Sin lectura de disco."""
        def _do_post() -> requests.Response:
            return requests.post(
                API_URL,
                files={"foto": (foto_nombre, foto_bytes, "image/jpeg")},
                data={"pin": pin, "pc": self.device_name},
                timeout=API_MARK_TIMEOUT,
            )
        try:
            response = await asyncio.to_thread(_do_post)
            if response.status_code == 200:
                if (nombre_trabajador or "").strip():
                    self._show_message(
                        f"¡Hola {nombre_trabajador}!\nIngreso registrado.", CLR_SUCCESS
                    )
                else:
                    self._show_message("Ingreso registrado correctamente.", CLR_SUCCESS)
                asyncio.create_task(self._sync_offline_then_refresh())
            else:
                try:
                    detalle = response.json().get("detail", "Error en el servidor.")
                except Exception:
                    detalle = "Error en el servidor."
                print(f"[ERROR] API {response.status_code}: {detalle}")
                self._show_message(detalle, CLR_ERROR)
        except requests.exceptions.ConnectionError as exc:
            print(f"[ERROR] ConnectionError: {exc}")
            self._save_offline(pin, foto_bytes)
            self._show_message(
                "Sin conexión.\nMarca guardada localmente.",
                CLR_WARNING,
            )
            self._update_offline_counter()
            asyncio.create_task(self._offline_counter_refresh_delayed())
        except requests.exceptions.Timeout as exc:
            print(f"[ERROR] Timeout: {exc}")
            self._save_offline(pin, foto_bytes)
            self._show_message(
                "Sin conexión.\nMarca guardada localmente.",
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
            self._reset_task = asyncio.create_task(self._auto_reset())

    async def _auto_reset(self) -> None:
        await asyncio.sleep(3)
        self._reset_state()

    # ──────────────────────────────────────────────────
    # Helpers de UI
    # ──────────────────────────────────────────────────
    def _set_btn_loading(self, loading: bool, label: str = "  MARCAR ASISTENCIA") -> None:
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
        """Frame negro 260×160 como placeholder mientras carga la cámara."""
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
