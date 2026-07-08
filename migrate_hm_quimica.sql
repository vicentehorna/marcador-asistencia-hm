-- ═══════════════════════════════════════════════════════════════════════════
--  Migración Marcador de Asistencia — Base nueva: hm_quimica
--
--  Este proyecto NO modifica SY_Person ni sy_user.
--  Solo crea sy_person_biometry y (opcional) columnas en RegistroAsistencia.
--
--  Campos que la API LEE de SY_Person (deben existir en HM Planillas):
--    · Person      — ID del trabajador
--    · Name        — nombre mostrado en la app
--    · pinAcceso   — PIN de 4 dígitos para marcar
--
--  sy_user: no se usa en api.py ni main.py
-- ═══════════════════════════════════════════════════════════════════════════

USE [hm_quimica];
GO

-- ── 0. Verificación: columnas requeridas en SY_Person ───────────────────────
PRINT '--- Columnas usadas por la API en SY_Person ---';
SELECT c.name AS columna, ty.name AS tipo, c.max_length, c.is_nullable
FROM sys.tables t
JOIN sys.columns c ON c.object_id = t.object_id
JOIN sys.types ty ON c.user_type_id = ty.user_type_id
WHERE t.name IN ('SY_Person', 'sy_person')
  AND c.name IN ('Person', 'Name', 'pinAcceso')
ORDER BY c.name;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('SY_Person') AND name = 'pinAcceso'
)
   AND NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('sy_person') AND name = 'pinAcceso'
)
BEGIN
    RAISERROR(
        'FALTA pinAcceso en SY_Person/sy_person. Asigna PINs a los trabajadores antes de usar el marcador.',
        16, 1
    );
END
GO

-- ── 1. Tabla biométrica (nueva en este proyecto) ────────────────────────────
IF OBJECT_ID(N'[dbo].[sy_person_biometry]', N'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[sy_person_biometry] (
        [Person]        [varchar](20)      NOT NULL,
        [Face_Template] [varbinary](max)   NOT NULL,
        [LastUpdate]    [datetime]         NOT NULL CONSTRAINT [DF_sy_person_biometry_LastUpdate] DEFAULT (GETDATE()),
        [Status]        [char](1)          NOT NULL CONSTRAINT [DF_sy_person_biometry_Status] DEFAULT ('A'),
        CONSTRAINT [PK_sy_person_biometry] PRIMARY KEY CLUSTERED ([Person] ASC)
    ) ON [PRIMARY] TEXTIMAGE_ON [PRIMARY];
    PRINT 'Tabla sy_person_biometry creada.';
END
ELSE
    PRINT 'Tabla sy_person_biometry ya existe.';
GO

-- ── 2. Columnas biométricas en RegistroAsistencia ───────────────────────────
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('RegistroAsistencia') AND name = 'Match_Score'
)
BEGIN
    ALTER TABLE [dbo].[RegistroAsistencia] ADD [Match_Score] float NULL;
    PRINT 'Columna Match_Score agregada.';
END
ELSE
    PRINT 'Columna Match_Score ya existe.';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('RegistroAsistencia') AND name = 'Es_Impostor'
)
BEGIN
    ALTER TABLE [dbo].[RegistroAsistencia]
        ADD [Es_Impostor] bit NOT NULL CONSTRAINT [DF_RegistroAsistencia_Es_Impostor] DEFAULT (0);
    PRINT 'Columna Es_Impostor agregada.';
END
ELSE
    PRINT 'Columna Es_Impostor ya existe.';
GO

-- ── 3. Verificación final ───────────────────────────────────────────────────
PRINT '--- Objetos del marcador en hm_quimica ---';
SELECT name AS objeto, type_desc
FROM sys.objects
WHERE name IN ('sy_person_biometry', 'RegistroAsistencia', 'SY_Person', 'sy_person', 'sy_user')
ORDER BY name;
GO

PRINT 'Migración hm_quimica completada.';
GO
