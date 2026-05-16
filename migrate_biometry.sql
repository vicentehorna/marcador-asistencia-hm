-- ═══════════════════════════════════════════════════════════════════════════
--  Migración biométrica — Ejecutar UNA VEZ en SQL Server
--  Base de datos: hm_atilio (o la que configure en .env → DB_NAME)
-- ═══════════════════════════════════════════════════════════════════════════

USE [hm_atilio];
GO

-- ── 1. Tabla de plantillas faciales ─────────────────────────────────────────
--  Si ya existe, este bloque no hace nada.
IF NOT EXISTS (
    SELECT 1 FROM sys.tables WHERE name = 'sy_person_biometry'
)
BEGIN
    CREATE TABLE [dbo].[sy_person_biometry] (
        [Person]        [varchar](20)   NOT NULL,
        [Face_Template] [varbinary](max) NOT NULL,
        [LastUpdate]    [datetime]       DEFAULT (GETDATE()),
        [Status]        [char](1)        DEFAULT ('A'),   -- 'A' = Activo
        PRIMARY KEY CLUSTERED ([Person] ASC)
    );
    PRINT 'Tabla sy_person_biometry creada.';
END
ELSE
    PRINT 'Tabla sy_person_biometry ya existe. Sin cambios.';
GO

-- ── 2. Columnas biométricas en RegistroAsistencia ───────────────────────────
--  Match_Score : similitud coseno 0.0–1.0 (NULL = biometría no procesada)
--  Es_Impostor : 1 si el score cayó bajo el umbral configurado en .env
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE  object_id = OBJECT_ID('RegistroAsistencia')
      AND  name = 'Match_Score'
)
BEGIN
    ALTER TABLE [dbo].[RegistroAsistencia]
        ADD [Match_Score] float NULL;
    PRINT 'Columna Match_Score agregada a RegistroAsistencia.';
END
ELSE
    PRINT 'Columna Match_Score ya existe. Sin cambios.';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE  object_id = OBJECT_ID('RegistroAsistencia')
      AND  name = 'Es_Impostor'
)
BEGIN
    ALTER TABLE [dbo].[RegistroAsistencia]
        ADD [Es_Impostor] bit NOT NULL DEFAULT (0);
    PRINT 'Columna Es_Impostor agregada a RegistroAsistencia.';
END
ELSE
    PRINT 'Columna Es_Impostor ya existe. Sin cambios.';
GO

PRINT '✓ Migración biométrica completada.';
GO
