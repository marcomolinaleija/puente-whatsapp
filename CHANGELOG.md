# Publicaciones del puente

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
