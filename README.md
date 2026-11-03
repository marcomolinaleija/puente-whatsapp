# Puente WhatsApp CAN

Versión publicada: **v38**. Consulta `release-manifest.json` para la imagen y el
digest verificados, y [CHANGELOG.md](CHANGELOG.md) para los cambios y sus pruebas.

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

La fuente de desarrollo elimina la unión automática al procesar enlaces de
invitación y conserva el mensaje y su vista previa. El cambio requiere reconstruir
y validar el adaptador nativo antes de publicar o desplegar una imagen; no modifica
las imágenes existentes ni incorpora todavía una acción explícita para unirse.

La integración del VPS incorpora aislamiento de recepción, recuperación de
lecturas y vinculación, archivo asíncrono R7, salud funcional y voz. El watchdog
externo está en `deploy/`, separado de la imagen. Las fuentes preservan la
protección del repo contra la unión automática al recibir invitaciones.

## Voz, salud y versión Web

- `src/slidge_whatsapp/voicecore/` contiene el backend nativo de llamadas,
  transporte y códec MLow. `voice.py` integra el transporte con Slidge.
- `runtime/voice_bridge_entrypoint.py` prepara el socket privado **antes** de
  cargar Go; conserva los argumentos de `slidge-whatsapp` y el directorio de
  datos. Voz está desactivada por defecto (`CAN_VOICE_ENABLE=0`).
- Al habilitar voz, el administrador configura `CAN_VOICE_ENABLE=1`,
  `CAN_VOICE_URL`, `CAN_VOICE_CERT` y `CAN_VOICE_KEY`. Certificado y clave son
  montajes externos de sólo lectura, nunca contenido del repositorio o imagen.
  `CAN_VOICE_HOME` debe coincidir con `--home-dir`; no se debe fijar manualmente
  el socket generado por el entrypoint.
- La comprobación de salud verifica el bucle principal, archivo y una respuesta
  XMPP. No acredita por sí sola que una cuenta esté conectada a WhatsApp.
- La versión Web compilada se registra al iniciar. El baseline actual es
  `2.3000.1052556849`; reemplaza el número rechazado con `client_outdated (405)`.
  No se descarga una versión arbitraria durante el arranque ni se borra la sesión.

El target `release` depende de Go/race, regresiones de invitaciones y versión,
smokes del runtime y un probe nativo de recepción. BuildKit no puede omitirlos al
construir ese target. El probe inyectado sólo existe en una variante de prueba;
el binario distribuido no lo contiene. No se ejecutan tests del cliente CAN.

### Recuperación de la lista de grupos

Si WhatsApp rechaza `GetGroups` con `status 429: rate-overlimit`, la sesión
permanece vinculada y la recuperación espera 5, 10, 20 y luego 30 minutos entre
consultas. El intervalo queda limitado a 30 minutos mientras la cuenta siga
autenticada; los demás errores conservan el máximo de tres reintentos.

Las llamadas concurrentes por cuenta comparten una sola tarea nativa, incluso
si se cancela uno de sus consumidores. Un resultado completo se reutiliza cinco
minutos sólo dentro del mismo intento de sesión. Los fallos no se guardan como
éxito ni habilitan la barrera de grupos; cancelar o cambiar la sesión invalida
los resultados tardíos. Desconectar cancela la recuperación en segundo plano.

`sync=error` no implica desvinculación, pero tampoco garantiza recepción: los
callbacks de datos esperan a que terminen las barreras de contactos y grupos.
La salud del contenedor verifica XMPP/archivo, no la sincronización de WhatsApp.
La v38 integra estas fuentes en la imagen. La corrección se probó primero como
parche Python de sólo lectura sobre v37, sin cambiar el binario nativo. Esa
validación previa no sustituye las barreras del target `release` para publicar
la nueva imagen.

### Confirmación de estados de WhatsApp

El build incorpora el [parche upstream de nodos `status`](https://github.com/tulir/whatsmeow/commit/a2381054887e16d22428430fdc4d2b027a7f29a7)
sobre la dependencia fijada, sin actualizar el resto de Whatsmeow ni el backend
de voz. Los estados válidos reciben un ACK normal; los malformados conservan el
NACK de análisis y los canales conservan su procesamiento de mensajes.

`build/patch_whatsmeow_status.py` comprueba los hashes de los tres archivos
upstream antes de modificarlos y rechaza versiones desconocidas. La regresión
`TestCANStatusNodesAcknowledged` verifica el envío serializado de ACK para texto,
multimedia y estados pendientes, y el NACK del caso malformado, sin red ni cuentas
reales. El target `release` exige esa regresión tanto normal como con `-race`.
Este cambio de fuentes no modifica la imagen v36 ya publicada; la recepción real
y la estabilidad tras el despliegue requieren validación aparte.

La migración de voz conserva las licencias MIT de WaCalls y MLow, junto a la
licencia AGPL de Slidge. Se incluyen las tres tablas binarias del códec. Los 19
vectores públicos de prueba se descargan sólo en el stage aislado de tests:
están excluidos de Git y de la imagen distribuida porque pueden contener audio
de referencia. Su archivo público y todos los hashes están fijados en
`build/voice-upstream.json` y `build/voice-fixtures.sha256`. No se importaron
grabaciones privadas, cuentas, sesiones, credenciales ni logs del VPS.

La validación previa de la integración aprobó 216 casos Go y los mismos con
`-race`, 23 comprobaciones del runtime y el probe nativo de recepción. Conserva
los dos skips upstream del códec (`TestSmplTablesCDF` y `TestSynth`). La entrega
final vuelve a aplicar estas barreras sobre la fusión, sin ejecutar tests de CAN.

## Procedencia de esta integración

La referencia fue la imagen validada en el VPS el 2 de noviembre de 2026,
ImageID `sha256:a5c1e8f6a557dd04054f0653831868252655886e3edaa6736a00f3a35abed919`.
Se compararon fuentes de Slidge/WhatsApp, voz y los tres overrides de Whatsmeow;
se integraron los archivos, no un binario previamente compilado. `go.mod` y
`go.sum` contienen las dependencias efectivas de la reconstrucción, sin rutas de
reemplazo específicas del VPS. Las dependencias Python siguen siendo las de la
base pública fijada por digest.

El Dockerfile todavía usa esa base para Python y recursos; no es una
reconstrucción independiente del sistema operativo ni una promesa de igualdad
binaria con el VPS. La prueba humana previa verificó mensajes y llamadas en el
VPS con CAN de código y ejecutable; la nueva imagen debe superar sus propias
barreras antes de publicarse. Publicar no cambia automáticamente el contenedor
del VPS ni las instalaciones de otras personas.

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
