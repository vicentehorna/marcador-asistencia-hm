-- ═══════════════════════════════════════════════════════════════════════════
--  Recrear tabla sy_person_biometry (plantillas faciales FaceNet)
--  Base de datos: hm_atilio (o la de DB_NAME en .env)
--
--  Usada por api.py en enrolamiento y validación biométrica.
--  Si la tabla se perdió, los trabajadores se volverán a enrolar en el
--  primer marcado exitoso después de ejecutar este script.
-- ═══════════════════════════════════════════════════════════════════════════

USE [hm_atilio];
GO

IF OBJECT_ID(N'[dbo].[sy_person_biometry]', N'U') IS NOT NULL
BEGIN
    PRINT 'sy_person_biometry ya existe. No se hizo nada.';
    PRINT 'Si necesitas recrearla desde cero, descomenta el DROP TABLE al final de este archivo.';
END
ELSE
BEGIN
    CREATE TABLE [dbo].[sy_person_biometry] (
        [Person]        [varchar](20)      NOT NULL,
        [Face_Template] [varbinary](max)   NOT NULL,
        [LastUpdate]    [datetime]         NOT NULL CONSTRAINT [DF_sy_person_biometry_LastUpdate] DEFAULT (GETDATE()),
        [Status]        [char](1)          NOT NULL CONSTRAINT [DF_sy_person_biometry_Status] DEFAULT ('A'),
        CONSTRAINT [PK_sy_person_biometry] PRIMARY KEY CLUSTERED ([Person] ASC)
    ) ON [PRIMARY] TEXTIMAGE_ON [PRIMARY];

    PRINT 'Tabla sy_person_biometry creada correctamente.';
END
GO

-- Verificación
SELECT
    t.name AS tabla,
    c.name AS columna,
    ty.name AS tipo,
    c.max_length,
    c.is_nullable
FROM sys.tables t
JOIN sys.columns c ON c.object_id = t.object_id
JOIN sys.types ty ON c.user_type_id = ty.user_type_id
WHERE t.name = 'sy_person_biometry'
ORDER BY c.column_id;
GO

-- ── Solo si quieres BORRAR y recrear (pierdes todos los enrolamientos) ───────
-- DROP TABLE IF EXISTS [dbo].[sy_person_biometry];
-- GO
-- (luego vuelve a ejecutar el bloque CREATE de arriba)
