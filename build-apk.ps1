# ============================================================
#  build-apk.ps1  —  VERSIÓN SIMPLIFICADA (SIN ERRORES DE SINTAXIS)
# ============================================================

$ErrorActionPreference = "Continue"
# $PROJECT_DIR = carpeta donde reside este script (NO hardcodear MARCADOR; permite
# ejecutar el build desde MARCADOR o desde MARCADOR_APP sin editar el .ps1).
$PROJECT_DIR = $PSScriptRoot
# APK publicado con nombre fijo dentro de build\ (como antes de Asistencia_Final en la raíz).
$APK_OUTPUT_NAME = "MARCADOR_APP.apk"
$OUTPUT_FILE     = Join-Path $PROJECT_DIR "build\$APK_OUTPUT_NAME"

Write-Host "--- INICIANDO PROCESO DE COMPILACION ---" -ForegroundColor Cyan

# 1. Configurar Unidad D:
Write-Host "[1/5] Configurando unidad virtual D: ..." -ForegroundColor Yellow
cmd /c subst D: /D >$null 2>&1
cmd /c subst D: "C:\Users\HP SUPPORT"
Write-Host "      Unidad D: vinculada a C:\Users\HP SUPPORT" -ForegroundColor Gray

# 2. Variables de Entorno
Write-Host "[2/5] Configurando variables de entorno ..." -ForegroundColor Yellow
$env:FLUTTER_ROOT     = "D:\flutter\3.41.4"
$env:ANDROID_HOME     = "D:\Android\sdk"
$env:ANDROID_SDK_ROOT = "D:\Android\sdk"
$env:JAVA_HOME        = "D:\java\17.0.13+11"
$env:PUB_CACHE        = "C:\PubCache"
$env:PYTHONUTF8       = "1"
$env:PYTHONIOENCODING = "utf-8"
try { chcp 65001 | Out-Null } catch { }
$env:Path = "D:\flutter\3.41.4\bin;D:\java\17.0.13+11\bin;" + (
    ($env:Path -split ';' | Where-Object { $_ -and $_ -notmatch '\\flutter\\' }) -join ';'
)

# Certificados SSL (GitHub / plantillas Flet). pip-system-certs usa el almacén de Windows.
$pyExe = "C:\PROYECTOS\MARCADOR\.venv\Scripts\python.exe"
if (Test-Path $pyExe) {
    try {
        & $pyExe -m pip install pip-system-certs -q 2>$null | Out-Null
        Write-Host "      pip-system-certs listo (SSL Windows)." -ForegroundColor Gray
    } catch { }
}

function Stop-GradleDaemons {
    Write-Host "      Deteniendo daemons de Gradle antes de limpiar..." -ForegroundColor Gray
    $gradlew = Join-Path $PROJECT_DIR "build\flutter\android\gradlew.bat"
    if (Test-Path -LiteralPath $gradlew) {
        try { & $gradlew --stop | Out-Null } catch { }
    }
    $gradleCmd = Get-Command gradle -ErrorAction SilentlyContinue
    if ($gradleCmd) {
        try { & $gradleCmd.Source --stop | Out-Null } catch { }
    }
    Start-Sleep -Seconds 2
}

Stop-GradleDaemons

# 3. Limpieza total antes de empaquetar (evita inflar el APK con basura previa)
Write-Host "[3/5] Limpieza total (build, dist, __pycache__) ..." -ForegroundColor Yellow
# Borrar APKs sueltos en la raíz del proyecto (restos de versiones anteriores del script).
Get-ChildItem -LiteralPath $PROJECT_DIR -Filter "*.apk" -File -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
Write-Host "      APKs en raíz del proyecto eliminados (si existían)." -ForegroundColor Gray

function Remove-FolderAggressive {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $true }
    for ($i = 0; $i -lt 8; $i++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return $true
        } catch { }
        Start-Sleep -Seconds 1
    }
    # Truco Windows: vaciar con robocopy /MIR y luego borrar
    $empty = Join-Path $env:TEMP ("flet_empty_" + [Guid]::NewGuid().ToString("N"))
    try {
        New-Item -ItemType Directory -Path $empty -Force | Out-Null
        & robocopy $empty $Path /MIR /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
        Remove-Item -LiteralPath $empty -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
        return -not (Test-Path $Path)
    } catch {
        return -not (Test-Path $Path)
    }
}

# __pycache__ en todo el proyecto (no .venv si existe fuera de fletignore del empaquetado)
Get-ChildItem -LiteralPath $PROJECT_DIR -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-FolderAggressive $_.FullName | Out-Null }

Get-ChildItem -LiteralPath $PROJECT_DIR -Directory -Filter "build_old_*" -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-FolderAggressive $_.FullName | Out-Null }

Remove-FolderAggressive (Join-Path $PROJECT_DIR "dist") | Out-Null
if (-not (Remove-FolderAggressive (Join-Path $PROJECT_DIR "build"))) {
    Write-Host "      ERROR: No se pudo eliminar build\. Cierra IDE/terminals que la usen y reintenta." -ForegroundColor Red
    exit 1
}
Write-Host "      Carpeta build (y cachés) eliminadas antes de flet build." -ForegroundColor Gray


# 4. Ejecutar Compilacion
Write-Host "[4/5] Ejecutando Flet Build (Esto tomara tiempo)..." -ForegroundColor Cyan
Set-Location $PROJECT_DIR
# --arch arm64-v8a: un solo ABI (APK mucho más liviano; sin esto suele ser "fat" ~40–50 % más pesado).
# Tablets/teléfonos Android actuales = ARM64. Muy antiguos solo armeabi-v7a: usar --arch armeabi-v7a.

# Instalar (idempotente) el init script de Gradle en ~/.gradle/init.d/.
# Gradle aplica automáticamente todo .gradle dentro de init.d a cualquier build,
# desactivando lintVital* y evitando el clásico bloqueo de .jar en Windows en
# :file_picker:lintVitalAnalyzeRelease.
# Se evita pasar --flutter-build-args -I porque el argparse de Flet (>=0.85)
# rechaza valores que empiezan con guion en ese parámetro.
$InitGradleSrc = Join-Path $PROJECT_DIR "android-disable-lint.init.gradle"
if (-not (Test-Path -LiteralPath $InitGradleSrc)) {
    Write-Host "ERROR: No existe $InitGradleSrc (copialo junto al proyecto)." -ForegroundColor Red
    exit 1
}
$GradleInitDir = Join-Path $env:USERPROFILE ".gradle\init.d"
if (-not (Test-Path -LiteralPath $GradleInitDir)) {
    New-Item -ItemType Directory -Path $GradleInitDir -Force | Out-Null
}
Copy-Item -Force $InitGradleSrc (Join-Path $GradleInitDir "android-disable-lint.init.gradle")
Write-Host "      Init script de Gradle instalado en $GradleInitDir" -ForegroundColor Gray

# Tamaño del APK: solo compilamos/limpiamos el código de la app (--compile-app / --cleanup-app).
# NO usar --compile-packages ni --cleanup-packages: OpenCV (cv2) carga archivos .py del paquete
# (p. ej. config.py) que desaparecen con esa opción → ImportError en el móvil.
# Ver: https://github.com/flet-dev/flet/issues/4850
$fletPy = "C:\PROYECTOS\MARCADOR\.venv\Scripts\python.exe"
$launcher = Join-Path $PROJECT_DIR "build_apk_launcher.py"
if (-not (Test-Path $fletPy)) { $fletPy = "python" }
if (-not (Test-Path $launcher)) {
    $launcher = Join-Path "C:\PROYECTOS\MARCADOR" "build_apk_launcher.py"
}
& $fletPy $launcher

# 5. Localizar y Copiar el APK (Búsqueda Agresiva)
Write-Host "[5/5] Buscando el archivo APK generado..." -ForegroundColor Yellow
Start-Sleep -Seconds 5

$apk = Get-ChildItem -Path "$PROJECT_DIR\build" -Filter "*.apk" -Recurse -ErrorAction SilentlyContinue | 
       Where-Object { $_.Length -gt 1MB } | 
       Sort-Object LastWriteTime -Descending | 
       Select-Object -First 1

if ($apk) {
    $buildDir = Split-Path -Parent $OUTPUT_FILE
    if (-not (Test-Path -LiteralPath $buildDir)) {
        New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
    }
    Copy-Item -Path $apk.FullName -Destination $OUTPUT_FILE -Force
    Write-Host "**********************************************" -ForegroundColor Green
    Write-Host " EXITO: APK publicado en:" -ForegroundColor Green
    Write-Host "        $OUTPUT_FILE" -ForegroundColor Green
    Write-Host "   (origen Flet: $($apk.FullName))" -ForegroundColor Gray
    Write-Host "**********************************************" -ForegroundColor Green
    Start-Process explorer.exe $buildDir
} else {
    Write-Host "ERROR: No se encontro el archivo APK en la carpeta build." -ForegroundColor Red
    Write-Host "Revisa si hubo errores en la compilacion arriba." -ForegroundColor White
}