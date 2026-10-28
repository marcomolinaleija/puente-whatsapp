# Puente WhatsApp CAN

Fuente de trabajo para el puente entre XMPP y WhatsApp. El código inicial se recuperó
de la imagen pública `ghcr.io/marcomolinaleija/cliente-xmpp-bridge:v35`, digest
`sha256:5cc36c7f2d1cfc08632f6d6e88cd38c009138915069fc0de282c3a43c60de86e`.
Las capas OCI se descargaron de forma anónima y se verificaron por SHA-256 antes
de recuperar las fuentes. No se importaron datos de instalaciones, sesiones,
registros, credenciales ni bases de datos.

El punto de partida incorpora Slidge `0.4.1.dev35+g0f0f993f0`, Slidge WhatsApp
`0.4.0b1.post7.dev0+25431c4` y las modificaciones de CAN ya presentes en v35.
Whatsmeow y las demás dependencias Go están fijadas por `go.mod` y `go.sum`.
Los metadatos conservan la autoría y las licencias originales. Slidge y Slidge
WhatsApp se distribuyen bajo AGPL-3.0-or-later; consulta `LICENSE` y las licencias
incluidas en los metadatos. Los paquetes Go descargados conservan sus licencias.

## Construcción y validación

El Dockerfile parte de la imagen
v35 fijada por digest, incorpora las fuentes de este repositorio y recompila el
adaptador nativo. No afirma reproducir byte a byte la imagen original. El compilador
está fijado por digest; los paquetes del sistema y la disponibilidad de módulos Go
requieren fijación adicional para una construcción reproducible independiente.
La imagen base conserva recursos y dependencias que aún no se reconstruyen aquí.

La fuente Go recuperada venía acompañada de un directorio `vendor`, pero no se
incluye porque la recuperación inicial no conservó todos sus archivos. El build
reconstruye `vendor` a partir de `go.mod` y `go.sum` y aplica los tres archivos
personalizados de Whatsmeow de `src-overrides/whatsmeow/`, recuperados de v35.
Requiere acceso a las dependencias Go.

Los tests de `tests/go`, `tests/whatsmeow` y `tests/runtime` provienen de la
candidata v35 del repositorio cliente y usan datos ficticios. La primera validación
local reconstruyó el adaptador, pasó los tests Go y Go/race, la prueba de cifrado
de stickers y los once smokes de runtime. No se conectó una cuenta real.

```sh
docker build --target go-tests --output type=cacheonly .
docker build --target runtime-tests --output type=cacheonly .
docker build --target inbound-probe-tests --output type=cacheonly .
docker build --target release -t puente-can-migration:baseline-v35 .
```

Estas comprobaciones no acreditan el funcionamiento real con una cuenta de
WhatsApp, envío o recepción, NVDA, ni equivalencia completa con la imagen original.
Las pruebas no publican ni despliegan una imagen.

La v35 conserva una llamada que acepta ciertos enlaces de invitación al procesar
un mensaje. **Esta base no corrige ese comportamiento**; debe tratarse en una rama
y validarse antes de publicar una nueva imagen.

La rama de desarrollo incorpora mejoras del puente desplegado para aislar la
recepción, conservar el mensaje cuando falla un adjunto, evitar bloqueos en la
carga de contactos y grupos, ordenar los ecos, acotar el historial al entrar en
un grupo, recuperar lecturas y tolerar caracteres XML inválidos en el archivo.
Incluye también la vinculación por QR o código telefónico y el reintento de
sincronización posterior a la autenticación. Aún no se migró el watchdog externo
ni la integración de voz. El resultado requiere pruebas reales antes de
considerarse equivalente a la instalación del servidor.

## Flujo propuesto

- `main`: versión estable revisada del puente.
- `desarrollo`: integración de cambios y pruebas.
- Una rama por tarea, por ejemplo `fix/invitaciones-grupos`.
- La etiqueta Git identifica el código de una versión; el digest identifica la
  imagen construida. Publicar y desplegar son pasos independientes.

El puente usa Slidge y Slidge WhatsApp de <https://codeberg.org/slidge/>. Esta
migración no altera el repositorio del cliente ni sus ramas. Antes de integrar en
Git o publicar, revisar cambios y datos personales conforme a las instrucciones
del proyecto. No incluir bases de datos, credenciales, sesiones ni registros reales.
