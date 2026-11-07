# cmdline y bootconfig de `vendor_boot`

El ABL del X910 toma la cmdline y el bootconfig de `vendor_boot`, no de
`boot.img`. Cambiar cualquiera de los dos exige regenerar y reflashear
`vendor_boot`.

## Por qué la cmdline no es la de postmarketOS

`cmdline.pmos.txt` es la copia de referencia del port postmarketOS, importada
para poder comparar. **No se usa.** La cmdline de Ubuntu difiere en tres cosas,
y una de ellas es imprescindible:

| Diferencia | Motivo |
|---|---|
| `root=LABEL=UBTS9U_UFS rootfstype=ext4` **añadido** | El initramfs de postmarketOS localiza su partición por sí mismo, así que su cmdline no lleva `root=`. `initramfs-tools` **no** hace eso: sin `root=` espera a un dispositivo que nunca llega y termina en la shell de emergencia. Se indica por etiqueta porque el orden de enumeración entre la microSD y la UFS no está garantizado |
| `ignore_console_null` **eliminado** | Es un parámetro del parche `ignore-console-null.patch`, que este port **no** aplica por defecto: el kernel validado de la build directa tampoco lo llevaba. Pasar un parámetro que ningún código lee solo genera ruido |
| `pmos.nosplash` **eliminado** | Específico de postmarketOS |

Se conservan `rootwait` —la UFS tampoco está lista cuando el kernel busca la
raíz—, `panic=10` y `msm.separate_gpu_kms=1`, sin el cual Adreno no crea su
render node.

## Por qué la etiqueta es `UBTS9U_UFS` y no `UBTS9U_ROOT`

La raíz vive ahora en la UFS interna, pero las microSD de las versiones
anteriores siguen existiendo y llevan una raíz Ubuntu arrancable etiquetada
`UBTS9U_ROOT`. Con la misma etiqueta en los dos sitios, `root=LABEL=` habría
resuelto a la que apareciese primero, y una tarjeta vieja olvidada en la ranura
habría arrancado en lugar de la instalación nueva: un fallo que se presenta
como «el flasheo no ha hecho nada». Con dos etiquetas distintas no hay
ambigüedad posible, y la tarjeta antigua sigue sirviendo de vuelta atrás si se
reflashea su ZIP.

Si se activa el parche de consola con `APPLY_IGNORE_CONSOLE_NULL=1`, hay que
añadir `ignore_console_null` aquí y regenerar `vendor_boot`.

## Experimentos sin tocar este fichero

Este `cmdline.txt` lo comparten el X910 y el X710, así que un experimento de
un solo tablet no se hace editándolo. `scripts/build-android-v4-bundle.sh` acepta
dos variables:

- `GTS9U_EXTRA_CMDLINE`: parámetros que se añaden al final.
- `GTS9U_DROP_CMDLINE`: parámetros de este fichero que se quitan de esa
  imagen, por ejemplo `GTS9U_DROP_CMDLINE=pd_ignore_unused`. Cada uno tiene que
  ser una palabra completa del fichero: una errata hace fallar la build en vez
  de empaquetar la cmdline sin cambios. `root=` no se puede quitar.

`clk_ignore_unused`, `pd_ignore_unused` y `regulator_ignore_unused` siguen
aquí desde la puesta en marcha. Qué apaga cada uno en el X710 y las imágenes
para probarlos de uno en uno: `docs/gts9wifi-bringup-flags.md` y
`tools/inplace/flags/`. No se quitan de este fichero hasta probarlos en los
dos tablets.
