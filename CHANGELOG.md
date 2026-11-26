# Publicaciones del puente

## v40 — 25 de noviembre de 2026

Imagen `ghcr.io/marcomolinaleija/cliente-xmpp-bridge:v40`, plataforma `linux/amd64`.
Digest: `sha256:baaec66aeecadea05fc732a915d6039d1df54febdb37440c7f7e7334c31176c9`.
Código construido: [142fe3b](https://github.com/marcomolinaleija/puente-whatsapp/commit/142fe3bd0c962bba3ca7de98c535aad5be8a8173).

- Un `start` con un token cuyo intento terminó responde `stale_request`; uno nuevo retira
  el intento anterior (sólo `Disconnect`, nunca `Logout`) sin exigir `cancel`.
- `phone` valida el número (E.164), responde de inmediato `already_paired` o
  `attempt_ended` y, si WhatsApp rechaza el número, conserva el intento con su QR.
- Avisos de estado sin cuerpo (`type="normal"`, XEP-0334 `no-store`) despiertan al cliente;
  no llevan QR, código ni credenciales y no entran en MAM.
- El vencimiento lo dispara un temporizador; `snapshot()` ya no muta el estado.
- Un escaneo que gana a la cancelación se informa como `paired_after_cancel`; la
  expiración nativa se reporta como `expired`; `op="sync"` reintenta la sincronización.
- Conserva la recuperación de v38 ante `429: rate-overlimit` y todo lo de v39.

El target `release` aprobó Go, Go/race, las 23 comprobaciones del runtime (con las
nuevas pruebas de vinculación) y el probe de entrada. Un e2e de 27 pasos con Prosody
real y WhatsMeow simulado pasó. La publicación comprobó que no existía `v40` y que la
última etiqueta era `v39`, y verificó el manifiesto anónimo por digest. Desplegada por
digest: `healthy`, cuentas existentes autenticadas y una sonda de 14 pasos con cuenta
ficticia (QR real de WhatsApp, cancelado sin escanear) aprobada. La vinculación real
con un teléfono y NVDA quedan para prueba humana.

## v39 — 16 de noviembre de 2026

Imagen `ghcr.io/marcomolinaleija/cliente-xmpp-bridge:v39`, plataforma `linux/amd64`.
Digest: `sha256:016af7dc331cb2bd173449485e49638123814acbb497dda3e742f8f09a3fb5b6`.
Código construido: [b4002ba](https://github.com/marcomolinaleija/puente-whatsapp/commit/b4002bac218f94a595cf46896b72071e0b8d0758).

- Admite los asistentes de WhatsApp: Meta AI y sus personas usan su alias
  telefónico documentado; los bots sin alias, como Muse, su identidad `@bot`
  (`+bot.<id>` en XMPP). Muse se añade al roster cuando la cuenta lo usa.
- Porta de whatsmeow `ecc39170bc14` el cifrado WASA de Muse. El secreto raíz se
  lee de la app state sin escribir versiones ni repetir eventos, y también se
  guarda al recibir su distribución en vivo.
- Las respuestas en streaming llegan como un mensaje y una corrección final, sin
  correcciones vacías. Las respuestas enriquecidas incluyen su texto, enlaces y
  sugerencias como listas legibles.
- Las solicitudes de aprobación de Muse se muestran con su pregunta, detalles,
  opciones y estado, y pueden responderse desde el cliente (`hitl.approval.decide`):
  sólo pendientes, sólo opciones ofrecidas y una única respuesta por solicitud.
- Backport de whatsmeow: etapa `full` de los bots y reintento de descifrado con
  el ID propio del mensaje.

El target `release` aprobó las pruebas Go y Go/race (incluidas las nuevas de Meta
AI, WASA y aprobaciones), cifrado de stickers, ACK de estados, las comprobaciones
del runtime y el probe de entrada. La publicación comprobó que no existía `v39`
ni etiquetas posteriores a `v38`, y verificó el manifiesto anónimo por digest.
Las respuestas, sugerencias y aprobaciones de Muse (permitir y denegar) se
validaron con una cuenta real antes de publicar.

## v38 — 3 de noviembre de 2026

Imagen `ghcr.io/marcomolinaleija/cliente-xmpp-bridge:v38`, plataforma `linux/amd64`.
Digest: `sha256:f0642ecaf96c13b9126d479ea64d9b0036484beba67fc232aa3d24a541c36a94`.
Código construido: [3ab5eaa](https://github.com/marcomolinaleija/puente-whatsapp/commit/3ab5eaa7ac3827f2407a78311cddbfec6738d655).

- Recupera la carga inicial de grupos rechazada con `429: rate-overlimit` sin
  desvincular WhatsApp. Espera 5, 10, 20 y luego 30 minutos entre consultas mientras
  la cuenta siga autenticada; otros errores conservan el máximo de tres reintentos.
- Comparte una sola consulta nativa por cuenta y conserva el cooldown aunque se
  cancele un consumidor. Reutiliza resultados completos cinco minutos por intento.
- No marca la sincronización como lista ante errores, cancelaciones ni resultados
  de una sesión obsoleta. Conserva el binario nativo, el ACK de estados y la voz.

El target `release` aprobó 216 casos Go y los mismos con `race`, cifrado de stickers
y ACK normal/race, las 23 comprobaciones del runtime (incluidas 27 pruebas de
vinculación y recuperación) y el probe de 200 mensajes entre dos cuentas ficticias
con dos historiales bloqueados. Se mantienen los dos skips upstream del códec.
El binario nativo conserva SHA-256
`7402034c5de5766c9f8f5ef0b3464f4a9ea064fa8e390fa5e1ec46208c0cdfa4`.

La corrección recibió confirmación humana como parche sobre v37. Esto no sustituye
la prueba humana específica posterior a instalar la imagen v38. La publicación
verificó pull anónimo por digest, configuración y disponibilidad de las 85 capas.
No se construyó ni modificó la aplicación CAN.

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
