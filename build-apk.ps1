# ============================================================
#  build-apk.ps1  —  VERSIÓN SIMPLIFICADA (SIN ERRORES DE SINTAXIS)
# ============================================================

$ErrorActionPreference = "Continue"
$PROJECT_DIR  = "C:\PROYECTOS\MARCADOR"
$OUTPUT_FILE  = "$PROJECT_DIR\Asistencia_Final.apk"

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
$env:Path = "D:\flutter\3.41.4\bin;" + $env:Path

# 3. Limpieza total antes de empaquetar (evita inflar el APK con basura previa)
Write-Host "[3/5] Limpieza total (build, dist, __pycache__) ..." -ForegroundColor Yellow
# Borrar APKs anteriores en la raíz para evitar el "Efecto Matrioshka"
Remove-Item -LiteralPath $PROJECT_DIR -Filter "*.apk" -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "      APKs anteriores eliminados." -ForegroundColor Gray

function Remove-FolderAggressive {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $true }
    for ($i = 0; $i -lt 3; $i++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return $true
        } catch { }
        Start-Sleep -Milliseconds 500
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
& flet build apk --project MarcadorAsistencia --org com.tuempresa --arch arm64-v8a --yes

# 5. Localizar y Copiar el APK (Búsqueda Agresiva)
Write-Host "[5/5] Buscando el archivo APK generado..." -ForegroundColor Yellow
Start-Sleep -Seconds 5

$apk = Get-ChildItem -Path "$PROJECT_DIR\build" -Filter "*.apk" -Recurse -ErrorAction SilentlyContinue | 
       Where-Object { $_.Length -gt 1MB } | 
       Sort-Object LastWriteTime -Descending | 
       Select-Object -First 1

if ($apk) {
    Copy-Item -Path $apk.FullName -Destination $OUTPUT_FILE -Force
    Write-Host "**********************************************" -ForegroundColor Green
    Write-Host " EXITO: APK COPIADO A $OUTPUT_FILE" -ForegroundColor Green
    Write-Host "**********************************************" -ForegroundColor Green
    Start-Process explorer.exe $PROJECT_DIR
} else {
    Write-Host "ERROR: No se encontro el archivo APK en la carpeta build." -ForegroundColor Red
    Write-Host "Revisa si hubo errores en la compilacion arriba." -ForegroundColor White
}