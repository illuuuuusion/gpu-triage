# gpu-triage MVP

Offline-first MVP für die Diagnose von **AMD Radeon RX 5000–9000** und **NVIDIA GeForce RTX 3000–5000** auf einem separaten Diagnose-PC.

Der Diagnose-PC braucht **kein Internet**. Ein einziger USB-Stick enthält:

1. ein **offizielles Arch-Linux-ISO** zum Booten via Ventoy,
2. dieses Repository,
3. ein vorbereitetes Offline-Paketbundle,
4. die erzeugten Diagnose-Reports.

Es wird **kein eigenes ArchISO gebaut**. Das Repo und das Live-Betriebssystem bleiben während der MVP-Entwicklung getrennt.

## Repo-Struktur

```text
gpu-triage/
├── start.sh                    # einziger Einstiegspunkt auf dem Diagnose-PC
├── app/
│   └── gpu_diag.py             # Diagnose-Orchestrator
├── scripts/
│   ├── bootstrap.sh            # installiert Offline-Runtime + lädt Treiber
│   └── start.sh                # ein Einstiegspunkt für den Diagnose-PC
├── offline/
│   ├── build_bundle.sh         # Internet-PC: erstellt Offline-Paketbundle
│   ├── package-list.txt        # bewusst kleine Runtime-Paketliste
│   ├── packages/               # generiert, nicht in Git
│   ├── manifest.env            # generiert, bindet Bundle an Arch-ISO-Kernel
│   └── SHA256SUMS              # generiert
├── reports/                    # Reports auf demselben USB
├── tests/
│   └── test_gpu_diag.py        # Regressionstests, keine GPU erforderlich
├── tools/
│   └── sync-to-usb.ps1         # Windows: Repo + optional ISO auf Ventoy kopieren
├── README.md
└── ROADMAP.md
```

## Warum Ventoy statt Rufus/Balena für die Entwicklung?

Rufus/Balena sind ideal, wenn der Stick nur ein ISO abbilden soll. Für dieses Projekt soll **derselbe Stick** gleichzeitig booten, das häufig geänderte Git-Repo enthalten, Offline-Pakete bereitstellen und Reports zurücknehmen.

Mit Ventoy bleibt die große Datenpartition normal beschreibbar:

```text
VENTOY_USB/
├── archlinux-YYYY.MM.DD-x86_64.iso
└── gpu-triage/
    ├── start.sh
    ├── app/
    ├── scripts/
    ├── offline/
    └── reports/
```

Dadurch muss bei einer Änderung an `gpu_diag.py` nicht jedes Mal ein Live-ISO neu gebaut werden.

## Einmalige USB-Vorbereitung

### 1. Ventoy auf einen USB-Stick installieren

Das geschieht auf dem Windows-PC. Danach besitzt der Stick eine große Datenpartition mit dem Label `Ventoy`.

### 2. Offizielles Arch-ISO herunterladen

Beispielname:

```text
archlinux-2026.08.01-x86_64.iso
```

Den Dateinamen **nicht ändern**. Das Datum wird verwendet, um ein dazu passendes Offline-Paketbundle zu bauen.

### 3. Repository klonen

Auf dem Windows-PC normal über Git/GitHub.

### 4. Offline-Bundle auf einem Internet-fähigen Arch-System bauen

Das kann eine Arch-VM oder Arch unter WSL sein. Das Skript benötigt Internet nur auf diesem Entwicklungs-PC.

```bash
cd gpu-triage
sudo bash ./offline/build_bundle.sh /pfad/zu/archlinux-2026.08.01-x86_64.iso
```

Das Skript verwendet den **Arch Linux Archive Snapshot vom Datum des ISOs**, lädt eine vollständige Dependency-Closure und erzeugt:

```text
offline/packages/*.pkg.tar.zst
offline/manifest.env
offline/SHA256SUMS
```

Das Bundle enthält bewusst auch Kernel-/Basisabhängigkeiten. Speicherplatz ist im MVP weniger wichtig als ein reproduzierbarer Offline-Start.

### 5. Repo und ISO auf denselben Ventoy-Stick kopieren

In PowerShell aus dem Repo:

```powershell
.\tools\sync-to-usb.ps1 -Drive E: -IsoPath C:\Downloads\archlinux-2026.08.01-x86_64.iso
```

Danach liegt alles auf **einem** Stick.

## Diagnose-PC: komplett offline

Empfohlen:

- Monitor am Mainboard/iGPU,
- dGPU ist ausschließlich das Device Under Test,
- LAN/WLAN ist für die Diagnose nicht erforderlich.

### Boot

1. Vom Ventoy-Stick booten.
2. Offizielles Arch-Linux-ISO auswählen.
3. Im Arch-Root-Shell die Ventoy-Datenpartition mounten:

```bash
mkdir -p /mnt/ventoy
mount /dev/disk/by-label/Ventoy /mnt/ventoy
```

4. gpu-triage starten:

```bash
bash /mnt/ventoy/gpu-triage/start.sh
```

`start.sh` führt für Diagnoseläufe automatisch `scripts/bootstrap.sh` aus. Dieses installiert Pakete **ausschließlich von USB** und startet danach die Diagnose-App.

Direkte Befehle:

```bash
bash /mnt/ventoy/gpu-triage/start.sh list
bash /mnt/ventoy/gpu-triage/start.sh quick --gpu 0000:03:00.0 --vram-seconds 60
bash /mnt/ventoy/gpu-triage/start.sh quick --gpu 03:00.0 --no-vram
```

`--gpu` akzeptiert nur vollständige PCI-Adressen (`0000:03:00.0`) oder die Kurzform (`03:00.0`, wird auf Domain `0000` normalisiert). Teilangaben wie `1` werden abgelehnt, damit nie versehentlich die falsche Karte getestet wird.

### Bootstrap läuft nur, wenn er gebraucht wird

`list`, `selftest` und `--help` benötigen nur die Python-Standardbibliothek und starten direkt, ohne das Bundle zu prüfen oder Pakete zu installieren.

Für Diagnoseläufe läuft der Bootstrap **einmal pro Live-Boot**. Nach Erfolg wird eine Marke unter `/run/gpu-triage/bootstrap.ok` abgelegt, die an Kernel, Mountpfad, `manifest.env` und `SHA256SUMS` gebunden ist. Weitere Läufe im selben Boot überspringen die SHA256-Prüfung und die Paketinstallation. `/run` ist tmpfs — auf dem USB-Stick bleibt nichts zurück. Ein Wechsel des Sticks oder ein neu gebautes Bundle entwertet die Marke automatisch; erzwingen lässt sich der Bootstrap mit `GPU_TRIAGE_FORCE_BOOTSTRAP=1`.

## Wichtig: ISO und Offline-Bundle gehören zusammen

Arch ist Rolling Release. Besonders das Paket `nvidia-open` enthält Kernelmodule für eine konkrete Arch-Kernelversion.

Darum speichert `build_bundle.sh` die erwartete Kernelversion in `offline/manifest.env`. `bootstrap.sh` vergleicht sie vor jeder Installation mit:

```bash
uname -r
```

Bei einem Mismatch wird **abgebrochen**, statt einen potenziell falschen NVIDIA-Stack zu installieren.

Wenn das Arch-ISO aktualisiert wird, wird auch das Offline-Bundle neu gebaut.

## Was v0.1 diagnostiziert

- PCI-Präsenz und GPU-Identität
- AMD/NVIDIA-Erkennung
- PCI Device/Subsystem IDs und BDF
- Kernel-Treiberbindung
- DRM Nodes
- PCIe Link Speed / Width
- BAR-Ressourcen
- PCIe-AER vor/nach Last, inklusive Upstream-Ports
- AMD hwmon-Telemetrie
- NVIDIA `nvidia-smi`-Telemetrie
- Kernel-Fehlersignale wie NVIDIA Xid oder amdgpu reset/timeout/fault
- begrenzter Vulkan-VRAM-Test über `memtest_vulkan`
- Text- und JSON-Report

Reports landen standardmäßig unter:

```text
gpu-triage/reports/
```

also auf **demselben USB-Stick**.

`PASS` wird nur vergeben, wenn alle wesentlichen Tests tatsächlich gelaufen sind. Ein mit `--no-vram` übersprungener oder mangels `memtest_vulkan` nicht ausführbarer VRAM-Test ergibt `WARN`, nie `PASS`.

`memtest_vulkan` kommt aus dem Offline-Paketbundle und wird über `PATH` gefunden. Für Ad-hoc-Läufe lässt sich ein anderer Pfad über `GPU_TRIAGE_MEMTEST=/pfad/zu/memtest_vulkan` setzen.

## Sicherheitsgrenzen des MVP

Nicht enthalten:

- `/dev/mem`
- rohe MMIO/Register-Schreibzugriffe
- VBIOS-Flashing
- Overclocking
- Power-/Spannungsänderungen
- Fan-Control
- exakte physische VRAM-Chip-Zuordnung

Der NVIDIA-Bootstrap kann eine **nicht als Boot-GPU verwendete NVIDIA-dGPU** von `nouveau` lösen und anschließend den vorbereiteten `nvidia-open`-Treiber laden. Darum ist die zusätzliche iGPU Bestandteil des vorgesehenen Testaufbaus.

## Tests

Die Regressionstests brauchen keine echte GPU; sie bauen Fake-sysfs-Bäume in temporären Verzeichnissen und nutzen nur die Standardbibliothek:

```bash
python3 -m unittest discover -s tests -v
python3 app/gpu_diag.py selftest
```

## GitHub-Workflow

Das GitHub-Repo ist die Source of Truth:

```text
Windows-PC
  edit / test / commit / push
        ↓
offline bundle bei Bedarf neu bauen
        ↓
sync-to-usb.ps1
        ↓
Diagnose-PC offline
        ↓
reports/ zurück auf demselben Stick
```

Eine reine Python-/Dokumentationsänderung benötigt **kein neues Arch-ISO und normalerweise kein neues Offline-Bundle**. Nur wenn sich Runtime-Pakete oder das verwendete Arch-ISO ändern, muss `build_bundle.sh` erneut laufen.

## MVP-Philosophie

Ein defekter Zustand ist ein Ergebnis, kein Programmfehler:

```text
PCI sichtbar?
  ↓
Treiber gebunden?
  ↓
Vulkan verfügbar?
  ↓
VRAM-Test ausführbar?
  ↓
AER / Kernel / Datenfehler unter Last?
```

`PASS` bedeutet nur: Die aktuell implementierten Tests haben keinen Fehler gefunden. Es ist keine Garantie für eine vollständig gesunde GPU.
