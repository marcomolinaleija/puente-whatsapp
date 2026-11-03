# Puente WhatsApp CAN

- `src/slidge_whatsapp/` y `src/slidge/` contienen la base v35 y la integración
  del código activo del VPS (voz, archivo asíncrono y salud funcional). Conserva
  autoría, licencias, identidad de cuentas y comportamiento existente.
- `src/slidge_whatsapp/voicecore/` incluye WaCalls/MLow y sus licencias; los
  vectores públicos tienen procedencia y hashes en `build/voice-upstream.json`
  y `build/voice-fixtures.sha256`. No los sustituyas con capturas privadas.
- `runtime/` arranca voz opcional y comprueba salud; `deploy/` conserva el
  watchdog externo, cuya instalación nunca es automática por construir la imagen.
- No incluyas conversaciones, bases de datos, credenciales, logs ni datos reales.
  Antes de commit o push, revisa los datos personales nuevos; nombres, teléfonos,
  JID, grupos y contenidos reales requieren consentimiento específico.
- Las pruebas usan fixtures ficticios. No uses cuentas reales para pruebas
  destructivas, envío de mensajes ni ingreso o salida de grupos.
- Build, commit, push, tag, publicación y despliegue requieren autorizaciones
  separadas. No reescribas historial publicado sin permiso.
- La base v35 tiene entrada automática por enlaces de invitación; las fuentes
  del repo la eliminan. Conserva su regresión nativa y no declares validada una
  nueva imagen hasta reconstruir y ejecutar las barreras del target `release`.
- No cambies la imagen estable ni modifiques datos de un servicio en ejecución
  durante el desarrollo. Documenta las diferencias y ejecuta las pruebas Go,
  race y smokes relevantes cuando el entorno y la autorización lo permitan.
