# Blueprint Laravel

Estos archivos son la base para integrar el motor Python en Laravel cuando PHP
este instalado en PATH.

## Flujo

1. `PlanController@store` recibe el PDF y metadatos.
2. Laravel guarda el PDF original.
3. Laravel ejecuta `python tools/cotas_engine.py analyze ...`.
4. Laravel registra el resultado en base de datos.
5. La pantalla de revision muestra `candidates.json` sobre el PDF.
6. El usuario aprueba o corrige.

## Tablas principales

- `customers`
- `parts`
- `drawings`
- `drawing_revisions`
- `dimension_marks`

## Comando Python esperado

```php
$process = new Symfony\Component\Process\Process([
    config('services.cotas.python'),
    base_path('tools/cotas_engine.py'),
    'analyze',
    $pathToPdf,
    '--client', $customerName,
    '--part-number', $partNumber,
    '--drawing-number', $drawingNumber,
    '--revision', $revision,
]);
```
