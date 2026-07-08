-- ═══════════════════════════════════════════════════════════════════════════
--  RegistroAsistencia — columnas requeridas por api.py en hm_quimica
--  Ejecutar en SSMS si el marcado falla con HTTP 500 tras guardar la foto.
-- ═══════════════════════════════════════════════════════════════════════════

USE [hm_quimica];
GO

PRINT '--- Columnas actuales de RegistroAsistencia ---';
SELECT c.name AS columna, ty.name AS tipo, c.max_length, c.is_nullable
FROM sys.tables t
JOIN sys.columns c ON c.object_id = t.object_id
JOIN sys.types ty ON c.user_type_id = ty.user_type_id
WHERE t.name = 'RegistroAsistencia'
ORDER BY c.column_id;
GO

-- Columnas biométricas (evitan el fallback)
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('RegistroAsistencia') AND name = 'Match_Score'
)
BEGIN
    ALTER TABLE [dbo].[RegistroAsistencia] ADD [Match_Score] float NULL;
    PRINT 'Columna Match_Score agregada.';
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('RegistroAsistencia') AND name = 'Es_Impostor'
)
BEGIN
    ALTER TABLE [dbo].[RegistroAsistencia]
        ADD [Es_Impostor] bit NOT NULL
        CONSTRAINT [DF_RegistroAsistencia_Es_Impostor] DEFAULT (0);
    PRINT 'Columna Es_Impostor agregada.';
END
GO

-- Columna PC (nombre del equipo/tablet) — FALTA en hm_quimica
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('RegistroAsistencia') AND name = 'PC'
)
BEGIN
    ALTER TABLE [dbo].[RegistroAsistencia] ADD [PC] varchar(100) NULL;
    PRINT 'Columna PC agregada.';
END
ELSE
    PRINT 'Columna PC ya existe.';
GO

PRINT '--- NOTA: la API usa RutaFoto (no rutafoto). Verifica que exista: ---';
SELECT name FROM sys.columns
WHERE object_id = OBJECT_ID('RegistroAsistencia')
  AND name IN ('RutaFoto', 'rutafoto', 'PC', 'Match_Score', 'Es_Impostor');
GO

PRINT '--- Columnas que usa la API (deben existir) ---';
SELECT c.name AS columna, ty.name AS tipo
FROM sys.columns c
JOIN sys.types ty ON c.user_type_id = ty.user_type_id
WHERE c.object_id = OBJECT_ID('RegistroAsistencia')
  AND c.name IN (
      'IdTrabajador', 'Person', 'FechaHoraIngreso', 'rutafoto', 'PC',
      'xlastuser', 'xlastdate', 'Match_Score', 'Es_Impostor'
  )
ORDER BY c.name;
GO

-- Prueba manual del INSERT (ajusta Person si hace falta)
/*
INSERT INTO RegistroAsistencia
    (IdTrabajador, Person, FechaHoraIngreso, rutafoto, PC,
     xlastuser, xlastdate, Match_Score, Es_Impostor)
VALUES (1, '47635814', GETDATE(), 'test.jpg', 'TEST-PC', 'admin', GETDATE(), 1.0, 0);
*/
