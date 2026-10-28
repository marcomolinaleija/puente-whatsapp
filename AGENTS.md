# Puente WhatsApp CAN

- `src/slidge_whatsapp/` y `src/slidge/` son la base recuperada de v35;
  conserva la autoría, licencias, identidad de cuentas y comportamiento existente.
- No incluyas conversaciones, bases de datos, credenciales, logs ni datos reales.
  Antes de commit o push, revisa los datos personales nuevos; nombres, teléfonos,
  JID, grupos y contenidos reales requieren consentimiento específico.
- Las pruebas usan fixtures ficticios. No uses cuentas reales para pruebas
  destructivas, envío de mensajes ni ingreso o salida de grupos.
- Build, commit, push, tag, publicación y despliegue requieren autorizaciones
  separadas. No reescribas historial publicado sin permiso.
- La imagen base v35 tiene entrada automática mediante enlaces de invitación.
  No declares corregido ese problema hasta validar el código nativo reconstruido.
- No cambies la imagen estable ni modifiques datos de un servicio en ejecución
  durante el desarrollo. Documenta las diferencias y ejecuta las pruebas Go,
  race y smokes relevantes cuando el entorno y la autorización lo permitan.
