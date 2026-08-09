# COTAS-INTELIGENTES

Sistema de numeracion automatica de cotas

Primera version del proyecto para resolver el flujo:

1. Registrar cliente, numero de parte, numero de plano y revision.
2. Analizar el PDF del plano.
3. Detectar posibles cotas automaticamente.
4. Generar un PDF numerado.
5. Guardar historial para reutilizar el plano si vuelve el mismo trabajo.

## Estado actual

Este repositorio arranca con el motor Python funcional. En esta maquina no hay
`php` disponible en PATH, por eso la parte Laravel queda preparada como contrato
de integracion y estructura sugerida.

## Prueba rapida del motor Python

Usar el Python empaquetado de Codex, que ya trae `pdfplumber`, `pypdf` y
`reportlab`:

```powershell
& 'C:\Users\rfall\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' tools\cotas_engine.py analyze .\plano.pdf --client "Cliente" --part-number "P-001" --drawing-number "D-001" --revision "A" --strategy auto
```

El comando genera una carpeta en `storage/jobs/` con:

- `original.pdf`
- `candidates.json`
- `numbered.pdf`
- `job.json`

La opcion `--strategy auto` prueba perfiles distintos porque no todos los planos
vienen con la misma estructura. Tambien se puede forzar:

- `standard`: PDF vectorial normal.
- `conservative`: mas estricto para evitar notas, cajetines o falsos positivos.
- `permissive`: mas amplio para formatos no estandar.
- `ocr`: fuerza OCR para planos escaneados como imagen.

## Interfaz web provisional

Mientras se instala PHP para Laravel, se puede usar una pantalla web en Python:

```powershell
& 'C:\Users\rfall\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' tools\web_app.py 8088
```

Abrir:

```text
http://127.0.0.1:8088
```

Desde ahi se puede:

- subir un PDF;
- capturar cliente y numero de plano como campos obligatorios;
- capturar numero de parte y revision como campos opcionales;
- buscar clientes precargados o escribir un cliente manualmente;
- generar el PDF numerado;
- escoger estrategia automatica o manual segun el tipo de plano;
- ignorar automaticamente numeros dentro de tablas o cajetines;
- usar OCR automatico cuando el PDF viene como imagen escaneada;
- revisar cotas propuestas antes de imprimir;
- eliminar cotas detectadas por error;
- cambiar el orden o coordenadas de los globos;
- consultar historial;
- descargar el PDF numerado anterior cuando el trabajo ya exista.
- entrar a `/admin` para ver metricas, grafico por cliente, historial administrativo paginado, eliminar planos y cargar uno nuevo.

La lista inicial de clientes esta en `data/clients.json`. `SAMTEC` aparece
primero por defecto. La lista puede editarse sin cambiar el codigo.

## Idea de integracion con Laravel

Laravel debe encargarse de:

- usuarios y permisos;
- clientes;
- partes;
- planos;
- revisiones;
- historial;
- subida y descarga de PDFs;
- pantalla para revisar, mover, borrar o aprobar numeros.

Python debe encargarse de:

- leer el PDF;
- detectar textos que parecen cotas;
- asignar numeros;
- crear el PDF numerado;
- devolver JSON para que Laravel lo muestre y permita editar.

## Siguiente paso recomendado

Instalar PHP 8.3+ y Composer en PATH. Luego se puede crear el proyecto Laravel y
copiar los archivos de `laravel-blueprint/` como base de modelos, migraciones y
controladores.
