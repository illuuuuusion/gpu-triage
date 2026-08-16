# gpu-triage

Offline-Diagnosewerkzeug für dedizierte Grafikkarten. gpu-triage sammelt auf einem
separaten Diagnose-PC belastbare Belege zum Zustand einer GPU — PCI-Sichtbarkeit,
Treiberbindung, PCIe-Link, Fehlerzähler, Telemetrie und ein begrenzter VRAM-Test —
und schreibt daraus einen Text- und JSON-Report.

Der Diagnose-PC benötigt dabei **kein Internet**: Live-System, Anwendung, Pakete
und Reports liegen gemeinsam auf einem einzigen USB-Stick.

- **Status:** v0.1 (MVP), siehe [ROADMAP.md](ROADMAP.md)
- **Unterstützte Hardware:** AMD Radeon RX 5000–9000, NVIDIA GeForce RTX 3000–5000
- **Plattform:** Arch Linux Live-System (offizielles ISO), Python 3 aus der Standardbibliothek

## Inhalt

- [Funktionsumfang](#funktionsumfang)
- [Voraussetzungen](#voraussetzungen)
- [Einrichtung](#einrichtung)
- [Verwendung](#verwendung)
- [Reports und Bewertung](#reports-und-bewertung)
- [Konfiguration](#konfiguration)
- [Projektstruktur](#projektstruktur)
- [Entwicklung](#entwicklung)
- [Designentscheidungen](#designentscheidungen)
- [Lizenz](#lizenz)

## Funktionsumfang

v0.1 erhebt pro Zielkarte:

| Bereich | Belege |
| --- | --- |
| Identität | PCI-Präsenz, Vendor/Device- und Subsystem-IDs, BDF, AMD/NVIDIA-Erkennung |
| Treiber | gebundener Kernel-Treiber, DRM-Nodes |
| Anbindung | PCIe Link Speed / Width, BAR-Ressourcen |
| Fehler | PCIe-AER vor und nach Last, inklusive Upstream-Ports |
| Telemetrie | AMD hwmon, NVIDIA `nvidia-smi` |
| Kernel | Fehlersignale wie NVIDIA Xid oder amdgpu reset/timeout/fault |
| VRAM | begrenzter Vulkan-Test über `memtest_vulkan` |
| Ausgabe | Text- und JSON-Report auf dem USB-Stick |


## Voraussetzungen

**Diagnose-PC (offline)**

- USB-Boot möglich, Secure Boot deaktiviert
- Anzeige über Mainboard/iGPU; die zu prüfende dGPU ist ausschließlich Device Under Test
- kein Netzwerk erforderlich

**Vorbereitungs-PC**

- Windows mit Git und PowerShell zum Synchronisieren des Sticks
- ein internetfähiges Arch-System (VM oder WSL) zum Bauen des Offline-Bundles
- USB-Stick mit [Ventoy](https://www.ventoy.net/) und ausreichend Platz für ISO, Repo und Pakete

## Einrichtung

Die Einrichtung erfolgt einmalig auf dem Vorbereitungs-PC. Es wird **kein eigenes
ArchISO gebaut**; das Repository bleibt vom Live-System getrennt.

### 1. Ventoy installieren

Ventoy auf den USB-Stick installieren. Der Stick erhält dadurch eine große,
normal beschreibbare Datenpartition mit dem Label `Ventoy`.

### 2. Offizielles Arch-ISO herunterladen

```text
archlinux-YYYY.MM.DD-x86_64.iso
```

Den Dateinamen **nicht ändern** — das enthaltene Datum bestimmt, gegen welchen
Snapshot das Offline-Bundle gebaut wird.

### 3. Repository klonen

```bash
git clone <repo-url> gpu-triage
```

### 4. Offline-Bundle bauen

Auf dem internetfähigen Arch-System:

```bash
cd gpu-triage
sudo bash ./offline/build_bundle.sh /pfad/zu/archlinux-YYYY.MM.DD-x86_64.iso
```

Das Skript nutzt den Arch Linux Archive Snapshot zum ISO-Datum, löst eine
vollständige Dependency-Closure auf und erzeugt:

```text
offline/packages/*.pkg.tar.zst
offline/manifest.env     # bindet das Bundle an die Kernelversion des ISOs
offline/SHA256SUMS
```

Kernel- und Basisabhängigkeiten sind absichtlich enthalten. Ein reproduzierbarer
Offline-Start wiegt hier schwerer als Speicherplatz.

### 5. Stick synchronisieren

In PowerShell aus dem Repo-Verzeichnis:

```powershell
.\tools\sync-to-usb.ps1 -Drive E: -IsoPath C:\Downloads\archlinux-YYYY.MM.DD-x86_64.iso
```

`-IsoPath` ist optional und nur beim ersten Mal bzw. nach einem ISO-Wechsel nötig.
Ergebnis auf dem Stick:

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

> **ISO und Bundle gehören zusammen.** Arch ist Rolling Release, und `nvidia-open`
> enthält Kernelmodule für eine konkrete Kernelversion. `bootstrap.sh` vergleicht
> `manifest.env` vor jeder Installation mit `uname -r` und bricht bei Abweichung ab,
> statt einen unpassenden NVIDIA-Stack zu installieren. Nach einem ISO-Update muss
> `build_bundle.sh` erneut laufen.

## Verwendung

Vom Ventoy-Stick booten, das offizielle Arch-ISO wählen und in der Root-Shell die
Datenpartition mounten:

```bash
mkdir -p /mnt/ventoy
mount /dev/disk/by-label/Ventoy /mnt/ventoy
bash /mnt/ventoy/gpu-triage/start.sh
```

`start.sh` ist der einzige Einstiegspunkt und ruft bei Bedarf den Bootstrap auf.

### Befehle

```bash
start.sh list                                        # verfügbare GPUs auflisten
start.sh quick --gpu 0000:03:00.0                    # Diagnose mit VRAM-Test (60 s)
start.sh quick --gpu 03:00.0 --vram-seconds 120      # längerer VRAM-Test
start.sh quick --gpu 03:00.0 --no-vram               # nur Probe, keine VRAM-Last
start.sh quick --gpu 03:00.0 --report-dir /mnt/...   # abweichendes Reportverzeichnis
start.sh --help
```

`--gpu` akzeptiert ausschließlich vollständige PCI-Adressen (`0000:03:00.0`) oder
die Kurzform (`03:00.0`, wird auf Domain `0000` normalisiert). Teilangaben wie `1`
werden abgelehnt, damit nie versehentlich die falsche Karte getestet wird.

### Bootstrap

Der Bootstrap installiert Pakete **ausschließlich vom USB-Stick** und läuft nur,
wenn er gebraucht wird:

- `list` und `--help` benötigen nur die Python-Standardbibliothek und starten
  sofort, ohne Bundle-Prüfung oder Paketinstallation.
- Für Diagnoseläufe läuft der Bootstrap **einmal pro Live-Boot**. Danach liegt unter
  `/run/gpu-triage/bootstrap.ok` eine Marke, die an Kernel, Mountpfad, `manifest.env`
  und `SHA256SUMS` gebunden ist; weitere Läufe überspringen Prüfsummen und
  Installation. `/run` ist tmpfs — auf dem Stick bleibt nichts zurück.
- Ein anderer Stick oder ein neu gebautes Bundle entwertet die Marke automatisch.
  Erzwingen lässt sie sich mit `GPU_TRIAGE_FORCE_BOOTSTRAP=1`.

## Reports und Bewertung

Reports landen standardmäßig in `reports/` — also auf demselben USB-Stick — als
Text- und JSON-Datei.

`PASS` wird nur vergeben, wenn alle wesentlichen Tests tatsächlich gelaufen sind.
Ein mit `--no-vram` übersprungener oder mangels `memtest_vulkan` nicht ausführbarer
VRAM-Test ergibt `WARN`, nie `PASS`. Auch ein `PASS` bedeutet lediglich: Die aktuell
implementierten Tests haben keinen Fehler gefunden — es ist keine Garantie für eine
vollständig gesunde GPU.

Die Diagnose folgt einer festen Kette, in der ein defekter Zustand ein Ergebnis ist
und kein Programmfehler:

```text
PCI sichtbar? → Treiber gebunden? → Vulkan verfügbar? → VRAM-Test ausführbar?
             → AER / Kernel / Datenfehler unter Last?
```

Ein Ergebnis wird immer nur der tatsächlich getesteten Karte zugeschrieben. Listet
`memtest_vulkan` die Zieladresse nicht auf, bricht der VRAM-Test ab, statt die
automatisch vorausgewählte Nachbarkarte zu belasten und deren Ergebnis zu melden.

### Exit-Codes

| Code | Bedeutung |
| --- | --- |
| `0` | Diagnose gelaufen, Gesamtergebnis `PASS` |
| `1` | Diagnose gelaufen, Gesamtergebnis `WARN` oder `FAIL` — der Report zählt die Befunde auf |
| `2` | Lauf nicht möglich: keine GPU gefunden, ungültige Adresse, Reportverzeichnis nicht beschreibbar |
| `3` | Bootstrap abgebrochen: Offline-Bundle passt nicht zum laufenden Kernel |
| `130` | Abbruch durch Strg+C |

`1` heißt „gemessen und etwas gefunden", `2` heißt „gar nicht erst gemessen" — für
Skripte ist das der wichtige Unterschied.

## Konfiguration

| Variable | Wirkung |
| --- | --- |
| `GPU_TRIAGE_FORCE_BOOTSTRAP` | `1` erzwingt einen vollständigen Bootstrap trotz gültiger Marke |
| `GPU_TRIAGE_MEMTEST` | Pfad zu einer abweichenden `memtest_vulkan`-Binary (sonst über `PATH`) |
| `GPU_TRIAGE_REPORT_DIR` | Zielverzeichnis für Reports (entspricht `--report-dir`) |

## Projektstruktur

```text
gpu-triage/
├── start.sh                    # Einstiegspunkt: Routing, Rechte, Bootstrap-Aufruf
├── app/
│   └── gpu_diag.py             # Diagnose-Orchestrator
├── scripts/
│   └── bootstrap.sh            # Offline-Runtime installieren, Treiber laden
├── offline/
│   ├── build_bundle.sh         # baut das Offline-Paketbundle (Internet-PC)
│   ├── package-list.txt        # bewusst kleine Runtime-Paketliste
│   ├── packages/               # generiert, nicht in Git
│   ├── manifest.env            # generiert, bindet Bundle an ISO-Kernel
│   └── SHA256SUMS              # generiert
├── tools/
│   └── sync-to-usb.ps1         # Windows: Repo und ISO auf den Ventoy-Stick
├── tests/                      # Regressionstests, keine GPU erforderlich
├── reports/                    # Reportausgabe auf demselben USB-Stick
├── README.md
├── TESTING-WINDOWS-VM.md       # Testablauf in einer VM unter Windows
└── ROADMAP.md
```

## Entwicklung

Das GitHub-Repo ist die Source of Truth; der USB-Stick ist eine Kopie davon:

```text
Entwicklungs-PC (edit / test / commit / push)
        ↓  Offline-Bundle nur bei Bedarf neu bauen
sync-to-usb.ps1
        ↓
Diagnose-PC (offline)
        ↓
reports/ zurück auf demselben Stick
```

Reine Python- oder Dokumentationsänderungen brauchen **kein neues Arch-ISO und
kein neues Offline-Bundle**. `build_bundle.sh` muss nur erneut laufen, wenn sich
Runtime-Pakete oder das verwendete Arch-ISO ändern.

### Tests

Die Regressionstests benötigen keine echte GPU. Sie bauen Fake-sysfs-Bäume in
temporären Verzeichnissen und nutzen ausschließlich die Standardbibliothek:

```bash
python3 -m unittest discover -s tests -v
bash tests/test_bootstrap_stamp.sh
```

Boot, Bootstrap, Paketverifikation und Reportausgabe lassen sich ohne Diagnose-PC
in einer VM prüfen — siehe [TESTING-WINDOWS-VM.md](TESTING-WINDOWS-VM.md). Die
Hardware-Messpfade bleiben echtem Blech vorbehalten.

## Designentscheidungen

**Ventoy statt Rufus/Balena.** Rufus und Balena eignen sich, wenn ein Stick genau
ein ISO abbilden soll. Hier muss derselbe Stick gleichzeitig booten, ein häufig
geändertes Git-Repo tragen, Offline-Pakete bereitstellen und Reports zurückführen.
Ventoy lässt die Datenpartition normal beschreibbar — eine Änderung an
`gpu_diag.py` erfordert damit kein neu gebautes Live-ISO.

**Repo getrennt vom Live-System.** Solange das Repo nicht in ein eigenes ISO
eingebacken wird, bleiben Entwicklungs- und Boot-Zyklus unabhängig voneinander.
Ein eigenes gpu-triage-Live-ISO ist als späteres Release-Artefakt vorgesehen.

**Abbrechen statt raten.** Passt das Bundle nicht zum laufenden Kernel, bricht der
Bootstrap ab. Ein nicht durchgeführter Test wird als `WARN` ausgewiesen und nie
als Erfolg gewertet.

## Lizenz

Für dieses Repository ist derzeit keine Lizenz hinterlegt. Alle Rechte vorbehalten,
bis eine Lizenzdatei ergänzt wird.
