# cca-promo-report

Genera cada semana el **Weekly Competitor's Promotions Report – MX Division**: un PDF en formato 16:9 con las promociones de smartphones Samsung, Apple, Honor y Motorola en retailers y operadores de Panamá, Costa Rica, Ecuador, Paraguay y Uruguay.

```
capture (red) ─► extract (offline) ─► review.xlsx (humano) ─► build ─► CCA_Consolidated_Competitors_Report-WeekWW.pdf
                                                                  └─► run_report.md
```

## 1. Instalación (una vez)

```powershell
python -m pip install --user uv
cd cca-promo-report
python -m uv sync                              # Python 3.12 + dependencias (uv.lock)
python -m uv run playwright install chromium   # navegador para capturar y generar el PDF
copy .env.example .env                         # opcional: proxies por país
```

Para los insights con Codex se necesita la CLI `codex` instalada y con sesión iniciada. Sin ella, la herramienta genera los insights con reglas.

## 2. Correr la semana

1. Actualiza `config/report.yaml`: `week: 2026-W46` y `cutoff_date: 2026-11-13`. Si hay un evento, ponlo en `event_caption` del país, por ejemplo `"Cyberday, November 2–4, 2026. Discounts are based on the previous cash price."`.
2. Ejecuta:

```powershell
python -m uv run cca-report run-all
```

El comando:
- **captura** todos los retailers, con unos 15–25 minutos de navegación cortés;
- **extrae** los productos;
- genera **`runs/<semana>/review.xlsx`**, con los insights redactados por Codex y verificados;
- **se detiene** para que revises el Excel;
- al confirmar, genera el **PDF** y el **`run_report.md`**.

Otras opciones:
- `--no-pause`: genera el PDF sin esperar la revisión.
- `--skip-capture`: reutiliza la captura ya hecha.
- `--force-review`: regenera un Excel ya editado.
- `-c PA` o `-r cr_monge`: limita el `run-all` a un país o a un retailer (`-c` y `-r` se suman).

Los pasos también existen por separado: `capture`, `extract`, `review` y `build`.

### Revisión en Excel (≈ 30 min)

`review.xlsx` es la **fuente de verdad** del PDF:

| Hoja | Qué revisar |
|---|---|
| País (PA, CR…) | Una fila por producto. Celdas amarillas = editables. `include`/`order` controlan qué sale y en qué orden. `flags` (naranja) = validaciones del §5 (cuotas que no cuadran con el precio, cuota = entrada, marca equivocada, columnas duplicadas). `row2_override`/`row3_override` reemplazan el texto calculado (líneas separadas con ` \| `). |
| Slides | Tipo de promoción y caption de cada slide. Las combinaciones **sin promoción** vienen con `include = FALSE`, porque según el §4 no generan slide. |
| Insights | Textos de la tabla Marca × Retailer y, en PY y UY, las viñetas (`(bullet N)`). |
| Status | Lo que no se pudo capturar y por qué. |

Si una celda numérica tiene un valor inválido, `build` falla e indica la hoja y la celda.

`review.xlsx` queda vinculado a la extracción que lo generó:
- Si vuelves a extraer, `build` se niega a mezclar datos viejos, salvo que uses `--allow-stale`.
- `run-all` guarda el Excel anterior como `review_<fecha>.xlsx` antes de generar uno nuevo, para que puedas volver a aplicar tus ediciones.

### Corrida automática y correo (viernes)

La tarea programada **"Cheil BI - CCA Promo Report"** corre los viernes a las 09:00, con el mismo esquema que "Cheil BI - Sprint Report":

- ejecuta `scripts\weekly.cmd`, que equivale a `cca-report weekly`;
- captura, extrae, genera los insights y el PDF, **espera hasta `mail.send_at` (10:00, hora de Panamá)** y envía el correo por Outlook;
- deja el registro en `logs\weekly_<fecha>.log`;
- si algo falla, envía una alerta a `mail.alert_to`, o a `mail.to` si `mail.alert_to` está vacío.

| Acción | Comando |
|---|---|
| Registrar o actualizar la tarea | `powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1` |
| Eliminar la tarea | `... -File scripts\register_task.ps1 -Unregister` |
| Probar todo sin red ni envío | `cca-report weekly --week 2026-W45 --skip-capture --dry-run`. Genera `email_preview.html`. |
| Enviar o reenviar el correo de una semana | `cca-report send --week 2026-W46 [--resend] [--to otra@cheil.com]` |

Detalles de funcionamiento:

- **Semana y fecha de corte:** `week: auto` y `cutoff_date: auto` calculan la semana ISO actual y su viernes, así que no hace falta editar `report.yaml` cada semana.
- **Correo:** incluye la tabla de insights Marca × Retailer de cada país, más las viñetas de Paraguay y Uruguay, y lleva el PDF adjunto. Las imágenes del PDF se comprimen en `render_cache/` y quedan en unos 7 MB; las capturas originales no se modifican.
- **Destinatarios:** se configuran en `mail:` dentro de `report.yaml`. Mientras sea una prueba, el destinatario es solo el responsable del reporte. Para enviarlo a los interesados, agrega las direcciones a `to`/`cc` y pon `include_run_status: false` para quitar la sección interna de cobertura.
- **Protecciones:** `weekly` no vuelve a capturar si ya capturó ese mismo día (si necesitas hacerlo, usa `--recapture`), y no reenvía una semana que ya tiene `sent.json`.

**Para que el reporte salga completo:** antes del viernes a las 09:00, ejecuta `cca-report manual-folders`. No usa la red: crea `runs/<semana>/manual/...` para los retailers manuales y para los que estuvieron bloqueados la semana anterior. En cada carpeta:

- deja una captura por producto;
- completa `precios.csv` con estas columnas, separadas por `;`: `archivo;precio_final;precio_antes;entrada;cuota_mensual;cuotas;regalo;etiqueta;promo`.

Así esos productos salen con precio en el PDF sin pasar por el Excel.

> ⚠️ **DRM corporativo:** Excel cifra (NASCA) los archivos que abre y guarda, y después la herramienta no puede leerlos. Edita `precios.csv` con el **Bloc de notas**. Si abres `review.xlsx` en Excel, `build` te avisará que está cifrado; en ese caso, regenera el archivo con `review --force` o pide a TI una copia sin protección. La corrida automática no se ve afectada, porque nadie abre el Excel en medio.

## 3. Sitios bloqueados → modo manual

Si un sitio bloquea o `robots.txt` no permite el acceso, la herramienta **no lo evade**: lo registra y crea `runs/<semana>/manual/<PAIS>_<retailer>_<Marca>/` con un `LEEME.txt`.

- Deja ahí **una captura por producto** (PNG o JPG). El nombre del archivo se usa como modelo, por ejemplo `Galaxy S26 Ultra.png`.
- Para `capture: listing`, nombra la captura del grid `listing.png`.
- Vuelve a correr `run-all --skip-capture --force-review` y completa los precios en el Excel.
- Mercado Libre Uruguay tiene además la carpeta `UY_mercadolibre_Campaign/`. Pon ahí una imagen del banner y un `caption.txt`: la línea 1 es el nombre de la campaña, la línea 2 el resumen y la línea 3 la URL de origen, para la trazabilidad. Con eso se genera la slide "Campaign overview".

Estado de la semana 45:

| Retailer | Modo | Motivo |
|---|---|---|
| MAC STORE (PA) | manual | robots.txt |
| Mercado Libre (UY) | manual | robots.txt y sin token de API |
| GOLLO (CR) | manual | robots.txt responde 403 |
| KOLBI (CR) | manual | el catálogo no carga en un navegador automatizado |
| CLARO EC (catálogo) | manual | robots.txt responde 403 |

## 4. Agregar o ajustar un retailer (solo YAML)

Crea `config/retailers/<pais>_<retailer>.yaml`. El esquema completo está en `RetailerConfig` (`src/cca_report/models.py`).

```yaml
country: CR
retailer_name: MONGE
type: retailer                 # operator | retailer | reseller | distributor | marketplace | brand
base_url: https://www.tiendamonge.com
currency: CRC
locale: es-CR
row_format: cr_default         # pa_operator | pa_retailer | cr_default | ec_claro | ec_tigo | py_default | uy_cover | uy_ml
preferred_installments: 15     # opcional: plan de cuotas a mostrar si la tarjeta lista varios
promo_types: [Special discount]  # opcional: fija el tipo de promoción de la slide
render_wait_seconds: 25        # opcional: espera máxima para catálogos que cargan lento
brands:
  Samsung:
    urls: ["https://www.tiendamonge.com/productos/celulares-y-tablets/celulares/celulares-samsung"]
    mode: dom                  # dom | shopify | manual
    capture: listing           # cards | listing | banner
    max_products: 5
    filter_by_name: false      # true si la página mezcla marcas
    selectors: {product_card: "", name: ""}   # opcional; vacío = detección automática
branding:                      # opcional: banners del home (slide Branding)
  - {brand: Samsung, urls: ["https://www.claro.cr/personas/"], capture: banner}
```

Pasos:
1. `python -m uv run cca-report validate-config`
2. `python -m uv run cca-report check-access -r <slug>`: verifica robots.txt, la petición HTTP y la carga en navegador.
3. `python -m uv run cca-report capture -r <slug>` y luego `extract`. Revisa la tabla de la consola. Si las tarjetas salen incompletas, define `selectors.product_card`; por ejemplo, `.deviceBox` en Tigo PA o `div.rounded-lg.shadow-md:has(h2)` en Tigo EC.

El orden de los retailers en el PDF se define en `config/report.yaml`, en el `retailer_order` de cada país.

## 5. Insights y costo

- `insights_provider: codex` en `report.yaml` (valor por defecto).
  - Por cada país se ejecuta `codex exec` en modo **solo lectura**, en una carpeta vacía y con un esquema JSON de salida.
  - Solo recibe los datos extraídos.
  - Cada celda se verifica: si menciona un retailer sin datos, queda "No information available."; si cita un número que no está en los datos, se usa el texto por reglas.
  - El prompt y la respuesta se guardan en `runs/<semana>/insights/<PAIS>/`.
- **Costo:** usa el plan de Codex de la cuenta con sesión iniciada; no requiere API de Anthropic ni de OpenAI. Son unas 5 llamadas por semana, una por país, de entre 1 y 7 minutos cada una, en paralelo.
- `insights_provider: rules`: genera los textos con reglas, sin costo.
- En ambos casos, los textos se editan en la hoja Insights.

## 6. Cortesía y cumplimiento

- Se respeta `robots.txt` según el RFC 9309 (Protego). Esto incluye redirecciones y navegaciones internas; si robots.txt devuelve 5xx, no se rastrea.
- Una sesión a la vez por dominio y al menos 3 s entre peticiones, o el `Crawl-delay` del sitio si es mayor. Los reintentos también respetan esa espera.
- Las páginas con CAPTCHA o anti-bot nunca se tocan: se detectan **antes** de cualquier clic, se capturan como evidencia y quedan como `blocked`.
- Solo para las capturas de pantalla se ocultan capas fijas como barras de cookies o chats, sin hacer clic ni cambiar datos.

## 7. Trazabilidad

`runs/<semana>/` contiene:

| Archivo | Contenido |
|---|---|
| `capture.json` | Lo visto en cada página: URL, hora, tarjetas, texto con estilos |
| `screenshots/…/card_XX.png` | Captura real de cada tarjeta como elemento, y `page.png` |
| `html/` | HTML guardado de cada página y `products.json` de Shopify |
| `data.json` | Productos extraídos y seleccionados |
| `review.xlsx` | Columnas `url`, `screenshot`, `html` y `captured_at` por producto |
| `report.html` | DOM final, ya ajustado, del PDF |
| `run_report.md` | Pasos, cobertura, pendientes manuales, validaciones y notas de insights |

## 8. Desarrollo

```powershell
python -m uv run pytest                  # incluye 3 tests "slow" que renderizan PDF con Chromium
python -m uv run pytest -m "not slow"
python -m uv run ruff check src tests
python -m uv run cca-report sample       # PDF de muestra con el fixture W44 (tests/fixtures/w44_sample.json)
python -m uv run cca-report discover -r <slug>   # propone URLs de categoría/marca navegando menús
```

Estructura:
- `src/cca_report/`:
  - `capture/`: Playwright (navegación, detección de tarjetas, capturas);
  - `extract/`: DOM, Shopify y manual;
  - `normalize.py`: formatos del §5 y validaciones;
  - `select.py`: elige entre 3 y 6 productos;
  - `insights.py`;
  - `review.py`;
  - `render/`: Jinja2, HTML/CSS y PDF;
  - `run_report.py`.
- Fuente: Inter (OFL) en `assets/fonts/`. Si colocas ahí `SamsungSharpSans-*` o `SamsungOne-*`, se usan automáticamente. Logo opcional: `assets/logo_cheil.png`.
