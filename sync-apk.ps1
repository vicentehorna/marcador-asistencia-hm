# Sincroniza solo lo necesario para el APK en MARCADOR_APP (carpeta liviana de build).
$SRC = "C:\PROYECTOS\MARCADOR"
$DST = "C:\PROYECTOS\MARCADOR_APP"

if (-not (Test-Path $DST)) {
    New-Item -ItemType Directory -Path $DST -Force | Out-Null
}

$files = @(
    "main.py",
    "app.env",
    "pyproject.toml",
    "requirements.txt",
    "build-apk.ps1",
    "build_apk_launcher.py",
    "android-disable-lint.init.gradle",
    ".fletignore"
)

foreach ($f in $files) {
    Copy-Item -Force (Join-Path $SRC $f) (Join-Path $DST $f)
    Write-Host "Copiado: $f" -ForegroundColor Gray
}

if (Test-Path (Join-Path $SRC "assets")) {
    Copy-Item -Recurse -Force (Join-Path $SRC "assets") (Join-Path $DST "assets")
    Write-Host "Copiado: assets\" -ForegroundColor Gray
}

# No copiar .env ni api.py al folder de build (no van al APK; evita confusión).
Remove-Item -Force (Join-Path $DST "api.py") -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $DST ".env") -ErrorAction SilentlyContinue

Write-Host "Sincronización completada -> $DST" -ForegroundColor Green
