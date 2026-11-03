# Publicaciones del puente

## v37 — 3 de noviembre de 2026

Imagen `ghcr.io/marcomolinaleija/cliente-xmpp-bridge:v37`, plataforma `linux/amd64`.
Digest: `sha256:b2cb1b5418238e8b9090fc45dcda0460f2a8b7e47d33ff7100f9e968f7add825`.
Código construido: [8f7c873](https://github.com/marcomolinaleija/puente-whatsapp/commit/8f7c8730682477fd841c8fb0015ce1fdc304fe45).

- Corrige la recepción detenida o retrasada por estados de WhatsApp pendientes de
  confirmación: incorpora el [parche upstream `a238105488`](https://github.com/tulir/whatsmeow/commit/a2381054887e16d22428430fdc4d2b027a7f29a7).
- Conserva la dependencia fijada, el backend de llamadas, los overrides de CAN y
  la vinculación. El build comprueba los hashes antes de aplicar el backport.
- Los estados válidos reciben ACK y los malformados conservan su NACK de análisis.

La validación aprobó 216 casos Go y los mismos con `race`, la nueva regresión de
ACK (texto, multimedia, pendientes y caso malformado) también con `race`, cifrado
de stickers, 23 comprobaciones del runtime y el probe de recepción de 200 mensajes
entre dos cuentas ficticias. Se conservan los dos skips upstream del códec.
La recepción real se confirmó con la candidata; la imagen publicada conserva
exactamente su binario nativo. El pull anónimo por digest y las 85 capas en GHCR
se verificaron antes de actualizar el manifiesto estable.

Esta versión sólo publica el puente; no construye ni modifica la aplicación CAN.

## v36 — 2 de noviembre de 2026

Imagen `ghcr.io/marcomolinaleija/cliente-xmpp-bridge:v36`, plataforma `linux/amd64`.
Digest: `sha256:5a8ed6cb7a531878e6b29afc58047b08beab096a42dd896651a19139247d295e`.
Código construido: [4f57b58](https://github.com/marcomolinaleija/puente-whatsapp/commit/4f57b58fece4754ae013aeb1f97a3d344c616077).

- Integra las llamadas de voz y la supervisión del VPS desde fuentes versionadas.
- Conserva el archivo asíncrono R7 y la protección contra uniones automáticas.
- Actualiza el baseline WhatsApp Web a `2.3000.1052556849` para corregir el rechazo 405.
- Los vectores de audio del códec sólo se descargan en los tests aislados; no se
  incluyen en Git ni en la imagen final. Se conservan las tablas y las licencias.

La reconstrucción final aprobó 216 casos Go y los mismos con `race`, el cifrado
de stickers, 23 comprobaciones de runtime y el probe nativo de recepción.
Se mantienen los dos skips upstream del códec. La imagen y sus 85 capas se
verificaron en GHCR, incluido un pull anónimo por digest.

El manifiesto estable está en `release-manifest.json`; el cliente conserva su
espejo compatible en `tools/wsl-appliance/bridge-update-manifest.json` de su repo.
Esta publicación no construye Windows ni sustituye automáticamente el puente
activo del VPS o de otras instalaciones.
