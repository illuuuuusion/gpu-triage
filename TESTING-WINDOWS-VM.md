# gpu-triage in einer VM unter Windows testen

Diese Anleitung beschreibt, wie sich gpu-triage von einem Windows-PC aus in einer
virtuellen Maschine testen lässt — ohne Diagnose-Hardware und ohne echte dGPU.

## Was eine VM testen kann — und was nicht

Zuerst die ehrliche Abgrenzung, sonst testet man das Falsche:

| Bereich | In der VM testbar? |
| --- | --- |
| Boot des Arch-ISOs, Mounten der Datenpartition | ✅ ja |
| `start.sh`-Routing, Rechte-Eskalation, `--help`/`list` | ✅ ja |
| Bundle-/Kernel-Abgleich (`manifest.env` vs. `uname -r`) | ✅ ja |
| SHA256-Verifikation des Offline-Bundles | ✅ ja |
| `pacman -U` ausschließlich vom Stick, ohne Netzwerk | ✅ ja |
| Stamp-Logik unter `/run/gpu-triage` | ✅ ja |
| Report-Erzeugung, Schreiben auf exFAT/NTFS | ✅ ja |
| Regressionstests (`tests/`) | ✅ ja |
| PCI-Enumeration einer echten AMD/NVIDIA-Karte | ❌ nein |
| PCIe-Link/BAR/AER-Auswertung | ❌ nein |
| hwmon-/`nvidia-smi`-Telemetrie | ❌ nein |
| echter VRAM-Test auf Hardware | ❌ nein |

Hyper-V liefert einen Synthetic-Video-Adapter über VMBus, VirtualBox einen
VMSVGA-Adapter mit Vendor `0x15ad`. Beides ist **keine** AMD/NVIDIA-Display-Klasse.
`start.sh list` meldet in der VM daher korrekt „No display-class PCI GPUs found"
und beendet sich mit Exit-Code 2 — **das ist das erwartete Ergebnis**, kein Fehler.

GPU-Passthrough (VT-d/AMD-Vi) ist bewusst nicht Teil dieser Anleitung: Hyper-V
(DDA) verlangt Windows Server, VirtualBox kann es nicht, und ein durchgereichter
PCIe-Link verfälscht genau die AER- und Link-Belege, um die es hier geht. Die
Hardware-Messpfade gehören auf echtes Blech.

## Zwei Wege durch dieses Dokument

| | Weg 1 — Internet-VM | Weg 2 — Offline-Test |
| --- | --- | --- |
| Setup-Aufwand | Minuten | WSL2 + Bundle-Bau (GB-Download) |
| Internet in der VM nötig | ja | nein — bewusst identisch zum echten Diagnose-PC |
| Testet App-Logik (`gpu_diag.py`, Regressionstests) | ✅ | ✅ |
| Testet Offline-Bootstrap (Prüfsummen, Kernel-Abgleich, USB-only-Install) | ❌ | ✅ |
| Testet Ventoy-/Stick-Layout | ❌ | nur Variante B |

Für schnelle Code-Iteration reicht Weg 1. Vor jeder Änderung an
`scripts/bootstrap.sh`, `offline/build_bundle.sh` oder `tools/sync-to-usb.ps1`
sollte zusätzlich Weg 2 laufen — das sind genau die Skripte, die Weg 1 gar nicht
anfasst.

---

## Weg 1 — GitHub-Repo direkt in einer Internet-VM

Ja, das geht, und es ist der schnellste Weg zu prüfen, ob eine Änderung
überhaupt läuft. Kein WSL2, kein Offline-Bundle, kein Ventoy-Stick nötig.

**Einschränkung vorweg:** Dieser Weg testet die **Diagnose-Logik**
(`app/gpu_diag.py`, `tests/`). Er testet **nicht** die Offline-Installations-
maschinerie (`scripts/bootstrap.sh`). Die verweigert absichtlich den Dienst,
wenn `offline/packages/` leer ist — der echte Diagnose-PC hat kein Internet und
darf nie heimlich von einem Mirror nachladen. Für diesen Teil führt kein Weg an
[Weg 2](#weg-2--vollständiger-offline-test-identisch-zum-echten-diagnose-pc)
vorbei.

### 1. VM anlegen

Beliebige VM-Software reicht — VirtualBox, VMware Workstation/Player oder
Hyper-V, je nachdem, was bereits installiert ist. Anders als bei Weg 2 unten
bleibt die VM-Konfiguration Standard:

- **Netzwerkadapter auf NAT** (Vorgabe der meisten VM-Programme) — diese VM
  braucht im Gegensatz zu Variante A/B echten Internetzugang.
- Offizielles Arch-ISO als Boot-Medium einhängen.
- Secure Boot aus (das ISO ist nicht mit dem Microsoft-Zertifikat signiert).
- 2–4 GB RAM reichen, da kein Offline-Bundle installiert wird.

### 2. Im Arch-Live-System: Netzwerk und Repo

```bash
ping -c1 archlinux.org || dhcpcd
```

Kabelgebundene NAT-Adapter bekommen normalerweise automatisch per `dhcpcd` eine
Adresse; schlägt der Ping fehl, startet `dhcpcd` ohne Argumente die
Konfiguration manuell nach.

Das offizielle Arch-ISO enthält **kein `git`** (geprüft gegen die aktuelle
`releng`-Paketliste des Projekts). `python3` ist dagegen bereits vorhanden — als
Abhängigkeit von `archinstall`. Git kommt reibungslos aus dem Netz nach:

```bash
pacman -Sy --needed git
git clone <repo-url> gpu-triage
cd gpu-triage
```

### 3. Was sofort läuft — ganz ohne weitere Pakete

```bash
bash start.sh list      # → "No display-class PCI GPUs found.", exit 2 — korrekt, keine echte GPU in der VM
bash start.sh --help
python3 -m unittest discover -s tests -v
bash tests/test_bootstrap_stamp.sh
```

Für einen **vollständigen** Report ganz ohne echte Hardware reicht die
Standardbibliothek — derselbe Fake-sysfs-Trick wie unten in
[Abschnitt 2.6](#26-report-erzeugung-gegen-ein-fake-sysfs), hier aber direkt
nach dem Klonen, ohne jeden Bootstrap:

```bash
python3 - <<'PY'
import sys, tempfile
from pathlib import Path
sys.path.insert(0, "app")
sys.path.insert(0, "tests")
import gpu_diag
from test_gpu_diag import make_pci_tree

gpu_diag.SYS_PCI = make_pci_tree(Path(tempfile.mkdtemp()))
args = gpu_diag.build_parser().parse_args(["quick", "--no-vram", "--report-dir", "reports"])
raise SystemExit(gpu_diag.run_quick(args))
PY
```

Das ist der praktisch vollständigste Test, den eine Internet-VM ohne
Offline-Bundle bieten kann.

### 4. Wenn mehr getestet werden soll

`scripts/bootstrap.sh` bricht hier erwartungsgemäß ab:

```bash
sudo bash scripts/bootstrap.sh
# [bootstrap] ERROR: Offline bundle is empty. Build it on the Internet-connected PC first.
```

Weil diese VM ohnehin Internet hat, ließe sich das umgehen, indem die
Laufzeitpakete direkt online installiert werden — das testet dann aber nur noch
`gpu_diag.py` selbst, nicht mehr die Offline-Bootstrap-Logik:

```bash
pacman -Sy --needed pciutils vulkan-tools
python3 app/gpu_diag.py quick --no-vram
```

Ohne echte GPU meldet auch das `ERROR: No AMD/NVIDIA display-class PCI GPU
found` — erwartet, kein Fehler im Tool.

---

## Produktiv-Setup: ein Stick, nicht zwei

Kurz beantwortet: **ein einziger** USB-Stick, kein Zwei-Stick-Aufbau. Das
funktioniert nur wegen Ventoy.

Mit Rufus oder Balena würde das ISO byteweise auf den Stick geschrieben — die
Datenpartition wäre danach weg, und ein zweiter Stick für Repo, Offline-Pakete
und Reports wäre tatsächlich nötig. Ventoy macht daraus einen gewöhnlichen,
normal beschreibbaren Datenträger:

1. Ventoy wird **einmalig** auf den Stick installiert und legt dabei eine
   große, normal beschreibbare Datenpartition an.
2. Das Arch-ISO liegt darauf als **normale Datei** — nicht gebrannt, sondern
   kopiert. Ventoys eigenes Bootmenü liest ISO-Dateien direkt von dieser
   Partition und bootet sie.
3. Auf derselben Partition liegt daneben der `gpu-triage`-Ordner mit Repo,
   gebautem Offline-Bundle und dem `reports/`-Verzeichnis.

```text
VENTOY_USB/                              (eine Partition, ein Stick)
├── archlinux-YYYY.MM.DD-x86_64.iso      # Bootmedium, als Datei
└── gpu-triage/
    ├── start.sh
    ├── app/
    ├── offline/packages/*.pkg.tar.zst   # das gebaute Offline-Bundle
    └── reports/                         # Reports landen hier zurück
```

`tools\sync-to-usb.ps1` erzeugt genau dieses Layout, siehe
[README, Abschnitt 5](README.md#5-stick-synchronisieren). Ein ISO-Wechsel
ersetzt nur die Datei; das Repo daneben bleibt unberührt und lässt sich per
`robocopy /MIR` aktualisieren, ohne den Stick neu zu bespielen.

**Variante B** unten (VirtualBox mit Raw-Disk-Zugriff auf den physischen Stick)
testet exakt dieses Layout — Ventoy-Bootmenü, ISO-Auswahl und Mount der
Datenpartition eingeschlossen. **Variante A** (Hyper-V/VHDX) simuliert nur die
Datenpartition und überspringt Ventoy selbst bewusst, um schneller zu sein.

Der Designhintergrund — warum Ventoy statt Rufus/Balena — steht bereits in der
[README](README.md#designentscheidungen).

---

## Weg 2 — Vollständiger Offline-Test (identisch zum echten Diagnose-PC)

Dieser Weg baut das reale Offline-Bundle und installiert es exakt so, wie es
später auf dem Diagnose-PC passiert: ohne Netzwerk, nur vom Stick, mit
Prüfsummen- und Kernel-Abgleich.

### Schritt 1 — Offline-Bundle bauen (WSL2)

Der Bundle-Bau braucht Internet, Arch und root. WSL2 reicht dafür aus.

```powershell
wsl --install archlinux
```

Danach in der Arch-WSL-Shell:

```bash
sudo pacman-key --init
sudo pacman-key --populate archlinux
sudo pacman -Syu --needed git arch-install-scripts
```

> **Wichtig:** Das Repo muss im **Linux-Dateisystem** liegen, nicht unter `/mnt/c`.
> `pacstrap` legt Symlinks, Hardlinks und Dateien mit Unix-Rechten an; auf dem
> Windows-Laufwerk (DrvFs) bricht der Bau ab oder erzeugt ein kaputtes Bundle.

```bash
cd ~
git clone <repo-url> gpu-triage
cd gpu-triage

# ISO ebenfalls ins Linux-Dateisystem kopieren
cp /mnt/c/Users/<DeinName>/Downloads/archlinux-2026.08.01-x86_64.iso ~/

sudo bash ./offline/build_bundle.sh ~/archlinux-2026.08.01-x86_64.iso
```

Der Lauf lädt mehrere GB. Am Ende meldet das Skript den erwarteten Kernel:

```text
[bundle] Bundle complete.
[bundle] Expected live kernel: 6.16.1-arch1-1
[bundle] Packages: 1xx
```

Anschließend den Repo-Stand nach Windows spiegeln — entweder per Kopie oder
direkt über den WSL-UNC-Pfad:

```powershell
robocopy \\wsl$\archlinux\home\<user>\gpu-triage C:\dev\gpu-triage /MIR /XD .git
```

### Variante A — Schneller Iterationstest mit Hyper-V und VHDX

Empfohlen für alles, was nicht Ventoy selbst betrifft. Windows kann VHDX nativ
erzeugen und formatieren, Hyper-V bindet sie nativ ein — kein USB-Stick nötig.
Voraussetzung: Windows Pro/Enterprise/Education mit aktiviertem Hyper-V.

#### A1. Datenplatte anlegen und befüllen

PowerShell **als Administrator**:

```powershell
$vhd = "C:\hyperv\gpu-triage-data.vhdx"
New-VHD -Path $vhd -SizeBytes 32GB -Dynamic
Mount-VHD -Path $vhd

# Datenträgernummer der frisch eingebundenen VHDX ermitteln
Get-Disk | Where-Object PartitionStyle -eq 'RAW'
```

Mit der ermittelten Nummer (hier `2`):

```powershell
Initialize-Disk -Number 2 -PartitionStyle GPT
New-Partition -DiskNumber 2 -UseMaximumSize -AssignDriveLetter |
    Format-Volume -FileSystem exFAT -NewFileSystemLabel Ventoy
```

Das Label `Ventoy` ist bewusst identisch zum echten Stick — so gilt der
dokumentierte Mount-Befehl unverändert. Jetzt das Repo synchronisieren
(Laufwerksbuchstabe ggf. anpassen):

```powershell
cd C:\dev\gpu-triage
.\tools\sync-to-usb.ps1 -Drive X:
Dismount-VHD -Path $vhd
```

`-IsoPath` wird hier **nicht** gebraucht: Das ISO hängt in Variante A direkt am
virtuellen DVD-Laufwerk, Ventoy ist nicht im Spiel.

#### A2. VM erzeugen

```powershell
$iso = "C:\dev\iso\archlinux-2026.08.01-x86_64.iso"

New-VM -Name gpu-triage-test -Generation 2 -MemoryStartupBytes 8GB -NoVHD
Set-VMProcessor -VMName gpu-triage-test -Count 4
Set-VMFirmware -VMName gpu-triage-test -EnableSecureBoot Off
Add-VMDvdDrive -VMName gpu-triage-test -Path $iso
Add-VMHardDiskDrive -VMName gpu-triage-test -Path $vhd
Set-VMFirmware -VMName gpu-triage-test `
    -FirstBootDevice (Get-VMDvdDrive -VMName gpu-triage-test)

Start-VM gpu-triage-test
vmconnect.exe localhost gpu-triage-test
```

**Secure Boot muss aus sein** — das Arch-ISO ist nicht mit dem Microsoft-Zertifikat
signiert.

**8 GB RAM sind kein Luxus.** Das Arch-Live-System hält sein Wurzeldateisystem in
einem RAM-Overlay; der Bootstrap installiert dort das komplette Bundle.

#### A3. Overlay vergrößern

Im Arch-Bootmenü `e` drücken und an die Kernel-Zeile anhängen:

```text
cow_spacesize=4G
```

Ohne das läuft der Overlay bei größeren Bundles voll und `pacman -U` bricht mit
„No space left on device" ab — ein VM-Artefakt, kein Bug in gpu-triage.

### Variante B — Vollständiger Test mit echtem Ventoy-Stick (VirtualBox)

Testet zusätzlich Ventoy, den Bootloader und das reale Artefakt (siehe
[Produktiv-Setup](#produktiv-setup-ein-stick-nicht-zwei) oben). VirtualBox kann
einen physischen Datenträger als Raw-Disk einbinden.

> Hyper-V und VirtualBox vertragen sich schlecht. Ist Hyper-V aktiv, läuft
> VirtualBox nur im langsamen Kompatibilitätsmodus. Für Variante B Hyper-V
> vorher deaktivieren (`bcdedit /set hypervisorlaunchtype off`, Neustart).

Stick wie in der [README](README.md#5-stick-synchronisieren) vorbereiten, dann
PowerShell **als Administrator**:

```powershell
# Nummer des USB-Datenträgers ermitteln
Get-Disk

cd "C:\Program Files\Oracle\VirtualBox"
.\VBoxManage.exe internalcommands createrawvmdk `
    -filename C:\dev\usb.vmdk -rawdisk \\.\PhysicalDrive2
```

Die `usb.vmdk` als Festplatte an eine neue VM hängen, in den VM-Einstellungen
unter *System* **EFI aktivieren**, 8 GB RAM vergeben und booten. VirtualBox muss
dauerhaft als Administrator laufen, sonst fehlt der Raw-Zugriff.

Im Ventoy-Menü das Arch-ISO wählen. Ab hier ist der Ablauf identisch mit dem
Diagnose-PC.

### Schritt 2 — Testablauf im Live-System

Alle Befehle in der Root-Shell des Arch-Live-Systems.

#### 2.1 Datenpartition mounten

```bash
mkdir -p /mnt/ventoy
mount /dev/disk/by-label/Ventoy /mnt/ventoy
ls /mnt/ventoy/gpu-triage
```

exFAT und NTFS werden vom Arch-Kernel direkt unterstützt. Findet der Mount das
Label nicht, hilft `lsblk -f` beim Auffinden der richtigen Partition.

#### 2.2 Pfade ohne Bootstrap

`list` und `--help` dürfen **nicht** installieren. Das Arch-ISO bringt Python
bereits mit (archinstall), also muss das ohne jede Vorbereitung laufen:

```bash
bash /mnt/ventoy/gpu-triage/start.sh list ; echo "exit=$?"
bash /mnt/ventoy/gpu-triage/start.sh --help
```

Erwartet:

```text
No display-class PCI GPUs found.
exit=2
```

Kein `[bootstrap]`-Ausgabeblock, keine sudo-Abfrage, keine Paketinstallation.

#### 2.3 Regressionstests

```bash
cd /mnt/ventoy/gpu-triage
python3 -m unittest discover -s tests -v
bash tests/test_bootstrap_stamp.sh
```

Erwartet: `Ran … tests … OK` und `bootstrap stamp tests: PASS`.

#### 2.4 Bootstrap — der eigentliche VM-Test

Das ist der Teil, den nur eine VM sinnvoll prüft: Kernelabgleich, Prüfsummen und
Installation ausschließlich vom Datenträger.

```bash
bash /mnt/ventoy/gpu-triage/scripts/bootstrap.sh
```

Erwartet:

```text
[bootstrap] Verifying offline bundle hashes...
[bootstrap] Installing required runtime packages from USB only...
[bootstrap] Refreshing module dependency metadata...
[bootstrap] Runtime ready.
```

Zweiter Lauf muss überspringen:

```bash
cat /run/gpu-triage/bootstrap.ok
bash /mnt/ventoy/gpu-triage/scripts/bootstrap.sh
# → "Runtime already prepared for this bundle in this boot"

GPU_TRIAGE_FORCE_BOOTSTRAP=1 bash /mnt/ventoy/gpu-triage/scripts/bootstrap.sh
# → läuft vollständig erneut durch
```

Danach prüfen, dass die Runtime wirklich installiert ist:

```bash
command -v memtest_vulkan lspci vulkaninfo
```

#### 2.5 Absichtliche Fehlerfälle

Die interessanteren Tests. Änderungen danach jeweils zurücknehmen.

**Kernel-/Bundle-Mismatch** — muss mit Exit 3 abbrechen:

```bash
cp /mnt/ventoy/gpu-triage/offline/manifest.env /tmp/manifest.bak
sed -i "s/^EXPECTED_KERNEL=.*/EXPECTED_KERNEL='9.9.9-arch1-1'/" \
    /mnt/ventoy/gpu-triage/offline/manifest.env
rm -f /run/gpu-triage/bootstrap.ok
bash /mnt/ventoy/gpu-triage/scripts/bootstrap.sh ; echo "exit=$?"
cp /tmp/manifest.bak /mnt/ventoy/gpu-triage/offline/manifest.env
```

**Beschädigtes Paket** — muss mit Exit 2 vor der Installation abbrechen:

```bash
PKG=$(ls /mnt/ventoy/gpu-triage/offline/packages/*.pkg.tar.zst | head -1)
cp "$PKG" /tmp/pkg.bak
printf 'corrupt' >> "$PKG"
rm -f /run/gpu-triage/bootstrap.ok
bash /mnt/ventoy/gpu-triage/scripts/bootstrap.sh ; echo "exit=$?"
cp /tmp/pkg.bak "$PKG"
```

**Ungültige PCI-Adresse** — darf nie eine falsche Karte treffen:

```bash
bash /mnt/ventoy/gpu-triage/start.sh quick --gpu 1
# → ERROR: Invalid PCI address '1'; expected 0000:03:00.0 or 03:00.0
```

#### 2.6 Report-Erzeugung gegen ein Fake-sysfs

Ohne echte GPU lässt sich der komplette Report-Pfad trotzdem durchlaufen, indem
`SYS_PCI` auf den Fake-Baum der Testsuite zeigt. Das prüft zugleich, ob sich auf
exFAT schreiben lässt und ob die Dateinamen dort gültig sind:

```bash
python3 - <<'PY'
import sys, tempfile
from pathlib import Path
REPO = Path("/mnt/ventoy/gpu-triage")
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "tests"))
import gpu_diag
from test_gpu_diag import make_pci_tree

gpu_diag.SYS_PCI = make_pci_tree(Path(tempfile.mkdtemp()))
args = gpu_diag.build_parser().parse_args(
    ["quick", "--no-vram", "--report-dir", str(REPO / "reports")]
)
raise SystemExit(gpu_diag.run_quick(args))
PY
```

Erwartet: ein vollständiger Textreport mit `Overall:   WARN`, Exit-Code 1 und
zwei neue Dateien in `reports/`:

```text
gpu-triage-<zeitstempel>-0000_03_00.0.json
gpu-triage-<zeitstempel>-0000_03_00.0.txt
```

`WARN` ist hier korrekt: `--no-vram` überspringt einen wesentlichen Test, und ein
übersprungener Test wird nie als `PASS` gewertet.

Die Dateien müssen anschließend unter Windows auf dem Stick bzw. in der VHDX
lesbar sein — damit ist der Rückweg der Reports verifiziert.

#### 2.7 Optional: Vulkan-Pfad in der VM erzwingen

Standardmäßig gibt es in der VM keinen Vulkan-Treiber, `memtest_vulkan` startet
also nicht. Wer den Testlauf-Codepfad trotzdem sehen will, ergänzt vor dem
Bundle-Bau in `offline/package-list.txt` einen Software-Renderer:

```text
vulkan-swrast
```

Danach liefert `vulkaninfo` ein lavapipe-Gerät und `memtest_vulkan` läuft. Das ist
ein reiner **Ablauf**-Test der Prozesssteuerung — Auswahl per stdin, Timeout,
SIGINT, Logdatei. Über echte VRAM-Gesundheit sagt lavapipe nichts aus, und
`vulkan-swrast` gehört anschließend wieder aus der Paketliste entfernt.

---

## Troubleshooting

| Symptom | Ursache / Lösung |
| --- | --- |
| Weg 1: `pacman -Sy` findet keinen Mirror / Timeout | VM-Netzwerkadapter steht auf „Internal"/„Host-only" statt NAT/Bridged → Adaptertyp in den VM-Einstellungen prüfen |
| Weg 1: `git clone` schlägt fehl, `pacman -Sy git` lief aber durch | Kein DNS in der VM → `cat /etc/resolv.conf`, ggf. `echo 'nameserver 1.1.1.1' > /etc/resolv.conf` |
| VM startet nicht vom ISO | Secure Boot aktiv → `Set-VMFirmware -EnableSecureBoot Off`; bei VirtualBox EFI einschalten |
| `pacman -U`: „No space left on device" | RAM-Overlay zu klein → `cow_spacesize=4G` an die Kernel-Zeile, VM-RAM erhöhen |
| `mount: unknown filesystem type 'exfat'` | Falsche Partition erwischt → `lsblk -f` prüfen |
| Bundle-Bau in WSL bricht ab | Repo liegt unter `/mnt/c` → ins Linux-Dateisystem (`~/`) klonen |
| `bootstrap` bricht mit Exit 3 ab | ISO-Datum und Bundle passen nicht zusammen → `build_bundle.sh` gegen genau dieses ISO neu laufen lassen |
| `Offline bundle is empty` | `offline/packages/` ist per `.gitignore` nicht in Git → Bundle bauen (Weg 2, Schritt 1) und per `sync-to-usb.ps1` übertragen |
| VirtualBox sieht die Raw-Disk nicht | VirtualBox nicht als Administrator gestartet |
| Skripte scheitern an `\r` | Datei über einen Windows-Editor umgebrochen → `.gitattributes` erzwingt LF für `*.sh`; robocopy kopiert byteweise, andere Wege ggf. nicht |

## Änderungen erneut testen

**Weg 1:** Im Live-System einfach `git pull` im geklonten Ordner — oder die VM
neu starten und frisch klonen. Kein Sync-Skript nötig, kein Stick im Spiel.

**Weg 2:** Reine Python- oder Dokumentationsänderungen brauchen **kein** neues
Bundle:

```powershell
# Variante A
Mount-VHD -Path C:\hyperv\gpu-triage-data.vhdx
.\tools\sync-to-usb.ps1 -Drive X:
Dismount-VHD -Path C:\hyperv\gpu-triage-data.vhdx

# Variante B
.\tools\sync-to-usb.ps1 -Drive E:
```

`build_bundle.sh` muss nur erneut laufen, wenn sich `offline/package-list.txt`
oder das verwendete Arch-ISO ändert.
