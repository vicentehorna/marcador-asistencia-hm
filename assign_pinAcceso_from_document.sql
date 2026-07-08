-- ═══════════════════════════════════════════════════════════════════════════
--  Asignar pinAcceso = últimos 4 dígitos de documentnumber
--  Compatible SQL Server 2012+ (sin STRING_AGG)
--  Base: hm_quimica
-- ═══════════════════════════════════════════════════════════════════════════

USE [hm_quimica];
GO

-- ── 0. Verificar columnas ───────────────────────────────────────────────────
IF COL_LENGTH('dbo.SY_Person', 'pinAcceso') IS NULL
BEGIN
    ALTER TABLE [dbo].[SY_Person] ADD [pinAcceso] [varchar](10) NULL;
    PRINT 'Columna pinAcceso creada en SY_Person.';
END
GO

IF COL_LENGTH('dbo.SY_Person', 'documentnumber') IS NULL
BEGIN
    RAISERROR('FALTA la columna documentnumber en SY_Person.', 16, 1);
    RETURN;
END
GO

-- ── Tabla temporal de trabajo (reutilizada en todo el script) ───────────────
IF OBJECT_ID('tempdb..#candidatos') IS NOT NULL
    DROP TABLE #candidatos;
GO

SELECT
    p.Person,
    p.Name,
    p.documentnumber,
    REPLACE(REPLACE(REPLACE(LTRIM(RTRIM(p.documentnumber)), '-', ''), '.', ''), ' ', '') AS doc_limpio,
    RIGHT(
        REPLACE(REPLACE(REPLACE(LTRIM(RTRIM(p.documentnumber)), '-', ''), '.', ''), ' ', ''),
        4
    ) AS pin_propuesto
INTO #candidatos
FROM dbo.SY_Person p
WHERE p.IsEmployee = 'Y'
  AND p.status = 'A';
GO

-- ── 1. Vista previa ─────────────────────────────────────────────────────────
PRINT '--- Vista previa (activos empleados) ---';
SELECT Person, Name, documentnumber, pin_propuesto
FROM #candidatos
ORDER BY pin_propuesto, Person;
GO

-- ── 2. Problemas detectados ─────────────────────────────────────────────────
PRINT '--- Problemas (sin doc, doc corto, no numérico, duplicado) ---';
SELECT 'SIN_DOCUMENTO' AS problema, Person, Name, documentnumber, NULL AS pin_propuesto
FROM #candidatos
WHERE documentnumber IS NULL OR LTRIM(RTRIM(documentnumber)) = ''

UNION ALL

SELECT 'DOCUMENTO_CORTO', Person, Name, documentnumber, pin_propuesto
FROM #candidatos
WHERE documentnumber IS NOT NULL
  AND LTRIM(RTRIM(documentnumber)) <> ''
  AND LEN(doc_limpio) < 4

UNION ALL

SELECT 'PIN_NO_NUMERICO', Person, Name, documentnumber, pin_propuesto
FROM #candidatos
WHERE documentnumber IS NOT NULL
  AND LTRIM(RTRIM(documentnumber)) <> ''
  AND LEN(doc_limpio) >= 4
  AND pin_propuesto NOT LIKE '[0-9][0-9][0-9][0-9]'

UNION ALL

SELECT 'PIN_DUPLICADO', c.Person, c.Name, c.documentnumber, c.pin_propuesto
FROM #candidatos c
INNER JOIN (
    SELECT pin_propuesto
    FROM #candidatos
    WHERE documentnumber IS NOT NULL
      AND LTRIM(RTRIM(documentnumber)) <> ''
      AND LEN(doc_limpio) >= 4
      AND pin_propuesto LIKE '[0-9][0-9][0-9][0-9]'
    GROUP BY pin_propuesto
    HAVING COUNT(*) > 1
) d ON d.pin_propuesto = c.pin_propuesto

ORDER BY problema, pin_propuesto, Person;
GO

-- ── 3. Resumen duplicados (SQL 2012: FOR XML PATH en lugar de STRING_AGG) ─────
PRINT '--- PIN duplicados (mismo último 4 dígitos) ---';
SELECT
    d.pin_propuesto,
    d.empleados_con_mismo_pin,
    STUFF((
        SELECT ' | ' + c2.Person + ' - ' + c2.Name
        FROM #candidatos c2
        WHERE c2.pin_propuesto = d.pin_propuesto
        ORDER BY c2.Person
        FOR XML PATH(''), TYPE
    ).value('.', 'nvarchar(max)'), 1, 3, '') AS detalle
FROM (
    SELECT pin_propuesto, COUNT(*) AS empleados_con_mismo_pin
    FROM #candidatos
    WHERE documentnumber IS NOT NULL
      AND LTRIM(RTRIM(documentnumber)) <> ''
      AND LEN(doc_limpio) >= 4
      AND pin_propuesto LIKE '[0-9][0-9][0-9][0-9]'
    GROUP BY pin_propuesto
    HAVING COUNT(*) > 1
) d
ORDER BY d.pin_propuesto;
GO

-- ── 4. UPDATE (solo PIN únicos y válidos) ───────────────────────────────────
BEGIN TRANSACTION;

DECLARE @dup_count int;
DECLARE @updated   int;

SELECT @dup_count = COUNT(*)
FROM (
    SELECT pin_propuesto
    FROM #candidatos
    WHERE documentnumber IS NOT NULL
      AND LTRIM(RTRIM(documentnumber)) <> ''
      AND LEN(doc_limpio) >= 4
      AND pin_propuesto LIKE '[0-9][0-9][0-9][0-9]'
    GROUP BY pin_propuesto
    HAVING COUNT(*) > 1
) dup;

UPDATE p
SET p.pinAcceso = c.pin_propuesto
FROM dbo.SY_Person p
INNER JOIN #candidatos c ON c.Person = p.Person
INNER JOIN (
    SELECT pin_propuesto
    FROM #candidatos
    WHERE documentnumber IS NOT NULL
      AND LTRIM(RTRIM(documentnumber)) <> ''
      AND LEN(doc_limpio) >= 4
      AND pin_propuesto LIKE '[0-9][0-9][0-9][0-9]'
    GROUP BY pin_propuesto
    HAVING COUNT(*) = 1
) u ON u.pin_propuesto = c.pin_propuesto;

SET @updated = @@ROWCOUNT;

IF @dup_count > 0
BEGIN
    PRINT 'AVISO: ' + CAST(@dup_count AS varchar(10))
        + ' PIN(s) duplicado(s). Esos empleados NO fueron actualizados.';
    PRINT 'Revise el listado PIN_DUPLICADO arriba y asigne PIN manual.';
END
ELSE
    PRINT 'Todos los PIN propuestos son únicos.';

PRINT 'Filas actualizadas: ' + CAST(@updated AS varchar(10));

COMMIT TRANSACTION;
GO

-- ── 5. Verificación final ─────────────────────────────────────────────────────
PRINT '--- Resultado pinAcceso asignado ---';
SELECT Person, Name, documentnumber, pinAcceso
FROM dbo.SY_Person
WHERE IsEmployee = 'Y'
  AND status = 'A'
ORDER BY pinAcceso, Person;
GO

PRINT '--- Empleados activos SIN pinAcceso (revisar) ---';
SELECT Person, Name, documentnumber, pinAcceso
FROM dbo.SY_Person
WHERE IsEmployee = 'Y'
  AND status = 'A'
  AND (pinAcceso IS NULL OR LTRIM(RTRIM(pinAcceso)) = '')
ORDER BY Person;
GO

IF OBJECT_ID('tempdb..#candidatos') IS NOT NULL
    DROP TABLE #candidatos;
GO
