# Safe Boot für die Pre-Driver-Triage

Ein Befehl nach dem Boot kann keinen Hard-Lock verhindern, der bereits beim
udev-Coldplug oder bei `Triggering uevents...` entsteht. Deshalb muss der DUT-
Treiber schon in der Ventoy-/Arch-Bootzeile gesperrt werden. `gpu-triage`
lädt, entfernt, bindet oder entbindet selbst keinen GPU-Treiber.

## Freigegebene Profile mit dem offiziellen Arch-ISO

Die Profile gelten nur, wenn die Anzeige von einer GPU eines **anderen
Herstellers** übernommen wird und die DUT-BDF anschließend explizit an
`triage --gpu` übergeben wird.

AMD-DUT (Anzeige beispielsweise über Intel oder NVIDIA):

```text
module_blacklist=amdgpu,radeon
```

NVIDIA-DUT (Anzeige beispielsweise über Intel oder AMD):

```text
module_blacklist=nouveau,nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm
```

Im Arch-ISO-Bootmenü den Kernel-Eintrag bearbeiten, den passenden Parameter an
die vorhandene Kernelzeile anhängen und booten. Danach zuerst `go.sh list` und
dann beispielsweise ausführen:

```bash
bash /mnt/v/gpu-triage/go.sh triage --gpu 0000:03:00.0 --preflight-only
```

Die CLI prüft sowohl die Bootzeile als auch den tatsächlich beobachteten
Treiber-Symlink. Ein Blacklist-Text allein gilt nicht als Beweis. Ist trotz
behauptetem Safe Boot ein Treiber gebunden, wird mit Exit 2 abgebrochen.

## Same-Vendor-Grenze

Bei AMD-iGPU + AMD-DUT oder NVIDIA-Anzeige + NVIDIA-DUT würde die globale
Blacklist auch die Anzeige betreffen. Dieser Aufbau bleibt daher
`BLOCKED: SAFE_BOOT_NOT_PROVEN`. Unter `liveiso/` liegt ein BDF-spezifischer
`driver_override`-Initramfs-Prototyp. Er ist absichtlich nicht in den normalen
Startpfad integriert und nicht als sicher freigegeben.

Vor einer Freigabe sind mindestens nachzuweisen:

- Reihenfolge vor udev-Coldplug in einer VM mit protokolliertem Boot;
- Known-good AMD-DUT plus fremde Anzeige-GPU;
- Known-good NVIDIA-DUT plus fremde Anzeige-GPU;
- Same-Vendor-Aufbau mit weiterhin funktionsfähiger Anzeige;
- Fehlerfälle für fehlende, ungültige und bereits gebundene Ziel-BDF;
- Marker und `driver_override` nach jedem Boot sowie vollständige cmdline.

Der Stand dieses Repositories ist: **Prototyp vorhanden, praktische
Validierung ausstehend, Same-Vendor nicht freigegeben.**

## Hard-Lock-Laborverfahren

Ein absichtlicher Treiber-Probeversuch gehört nicht zur CLI. Falls er separat
genehmigt wird, braucht er serielle Konsole oder netconsole, aktiviertes
pstore, dokumentierte BDF/cmdline/Kernelversion, einen festgelegten harten
Power-Cycle und einen anschließenden Safe-Boot zum Kopieren der Evidenz.
gpu-triage liest bzw. kopiert pstore nur und löscht es nie.
