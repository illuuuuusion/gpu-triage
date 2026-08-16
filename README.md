# gpu-triage

## Schnellstart

1. Repository auf einem Windows-PC auschecken und einen Ventoy-Stick einstecken.
2. In PowerShell im Repository `.\tools\prepare-usb.ps1` ausführen.
3. Vom Stick booten und das bereitgestellte Arch-ISO im Ventoy-Menü wählen.
4. In der Root-Shell die eine Zeile aus `BOOT.txt` eingeben.

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

- [Schnellstart](#schnellstart)
- [Funktionsumfang](#funktionsumfang)
- [Voraussetzungen](#voraussetzungen)
- [Einrichtung](#einrichtung)
- [Verwendung](#verwendung)
- [Reports und Bewertung](#reports-und-bewertung)
- [Konfiguration](#konfiguration)
- [Projektstruktur](#projektstruktur)
- [Entwicklung](#entwicklung)
- [Designentscheidungen](#designentscheidungen)
- [Manueller Weg](#manueller-weg)
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
- USB-Stick mit [Ventoy](https://www.ventoy.net/) und ausreichend Platz für ISO, Repo und Pakete
- ein internetfähiges Arch-System (VM, WSL oder Container) — **nur** für den
  manuellen Weg, also wenn das Offline-Bundle selbst gebaut werden soll

## Einrichtung

Die Einrichtung erfolgt einmalig auf dem Vorbereitungs-PC. Es wird **kein eigenes
ArchISO gebaut**; das Repository bleibt vom Live-System getrennt.

### Der kurze Weg: ein Kommando

```powershell
.\tools\prepare-usb.ps1
```

Das Skript liest [offline/release.json](offline/release.json) — die eine Datei,
die ISO, Kernel und Offline-Bundle aneinander bindet — und erledigt den Rest:
Ventoy-Stick finden, Platz prüfen, ISO und Bundle hash-verifiziert laden,
Repository spiegeln, Ergebnis prüfen, `BOOT.txt` schreiben. Kein Linux nötig.

| Schalter | Wirkung |
| --- | --- |
| `-Check` | Doctor-Modus: prüft den Stick, schreibt nichts, Exit 0/1 |
| `-Drive E:` | Laufwerk vorgeben statt über das Label `Ventoy` zu suchen |
| `-Force` | alles erneut laden und prüfen, auch wenn es passend erscheint |
| `-InstallVentoy` | Ventoy vorher installieren — **löscht das Ziellaufwerk** |
| `-CacheDir` | Download-Cache (Standard: `%LOCALAPPDATA%\gpu-triage\cache`) |
| `-ReleasePath` | alternative `release.json`, vor allem für isolierte Tests |

Jeder Download wird gegen den Hash aus `release.json` geprüft und
zwischengespeichert; ein zweiter Lauf lädt nichts erneut und ist damit auch der
schnelle Weg, nur eine Code-Änderung auf den Stick zu bringen.

`-InstallVentoy` ist bewusst nicht Teil des Standardlaufs: Es akzeptiert nur
USB-Laufwerke unterhalb einer Größenschwelle (`-MaxDiskSizeGB`, Standard 256),
zeigt Modell, Seriennummer und Größe an und verlangt die getippte Bestätigung
`ERASE DISK <n>` — kein `[J/N]`. Die Ventoy-Version und ihr SHA256 sind in
[tools/ventoy-release.json](tools/ventoy-release.json) gepinnt.

> `release.json` entsteht im CI-Lauf von
> [.github/workflows/bundle.yml](.github/workflows/bundle.yml). Solange die
> Datei nicht im Repository liegt, bleibt nur der manuelle Weg unten —
> `prepare-usb.ps1` sagt das beim Start.

## Verwendung

Vom Ventoy-Stick booten, das offizielle Arch-ISO wählen und in der Root-Shell
diese **eine Zeile** tippen — sie steht wortgleich in `BOOT.txt` im Stick-Root:

```bash
m=/mnt/v; mkdir -p $m; mount /dev/disk/by-label/Ventoy $m; bash $m/gpu-triage/go.sh list
```

Danach genügt für jeden weiteren Aufruf:

```bash
bash $m/gpu-triage/go.sh quick --gpu 0000:03:00.0
```

`go.sh` ist ein Wrapper vor `start.sh`, kein zweiter Pfad: Er sucht die
Ventoy-Datenpartition selbst, mountet sie read-write nach, falls das
Live-System sie schreibgeschützt eingehängt hat, und übergibt dann an
`start.sh`. Das ist nötig, weil Reports auf den Stick zurückgeschrieben werden.

`start.sh` bleibt der einzige Einstiegspunkt und ruft bei Bedarf den Bootstrap
auf; direkt aufrufen lässt es sich weiterhin:

```bash
mkdir -p /mnt/ventoy
mount /dev/disk/by-label/Ventoy /mnt/ventoy
bash /mnt/ventoy/gpu-triage/start.sh list
```

> **Wenn das Mounten fehlschlägt.** Ventoy blendet die Partition aus, auf der
> das gebootete ISO liegt; sie ist dann nur über den Device-Mapper-Knoten
> (`/dev/mapper/sdX1` statt `/dev/sdX1`) erreichbar. `go.sh` probiert beide
> Varianten selbst durch — genau dafür gibt es ihn.

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
├── go.sh                       # Wrapper: Stick finden/mounten, dann start.sh
├── start.sh                    # Einstiegspunkt: Routing, Rechte, Bootstrap-Aufruf
├── app/
│   └── gpu_diag.py             # Diagnose-Orchestrator
├── scripts/
│   └── bootstrap.sh            # Offline-Runtime installieren, Treiber laden
├── offline/
│   ├── build_bundle.sh         # baut das Offline-Paketbundle (Internet-PC)
│   ├── bundle_helpers.sh       # testbare Subtraktions-/Dateinamenlogik
│   ├── release_meta.py         # löst ISO-Release auf, schreibt/prüft release.json
│   ├── release.json            # von CI erzeugt: pinnt ISO + Kernel + Bundle
│   ├── package-list.txt        # bewusst kleine Runtime-Paketliste
│   ├── packages/               # generiert, nicht in Git
│   ├── manifest.env            # generiert, bindet Bundle an ISO-Kernel
│   ├── SHA256SUMS              # generiert
│   ├── excluded.txt            # generiert, vom ISO bereitgestellte Pakete
│   ├── dist/                   # generiert, Bundle als ZIP-Artefakt
│   └── .dlcache/               # generiert, Download-Cache (nicht auf den Stick)
├── tools/
│   ├── prepare-usb.ps1         # Windows: der eine Vorbereitungsbefehl
│   ├── ventoy-release.json     # gepinnte Ventoy-Version samt SHA256
│   └── sync-to-usb.ps1         # Windows: Repo und ISO auf den Ventoy-Stick
├── .github/workflows/
│   └── bundle.yml              # baut das Bundle, veröffentlicht es, pinnt es
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
temporären Verzeichnissen. Die Shell-Suite prüft zusätzlich Bundle-Subtraktion,
`release.json`, Bootstrap-Stamp sowie Mount-/Remount-Routing von `go.sh`:

```bash
python3 -m unittest discover -s tests -v
for test in tests/test_*.sh; do bash "$test"; done
```

Der Windows-Ablauf wird ohne USB-Hardware mit einem temporären `subst`-Laufwerk
unter Windows PowerShell 5.1 geprüft:

```powershell
.\tests\test_prepare_usb.ps1
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

## Manueller Weg

Dieser Rückfallweg baut das Bundle selbst und kommt ohne GitHub-Releases aus.
Für den normalen Einstieg genügt der [Schnellstart](#schnellstart).

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

Auf einem Arch-System mit Internet — eine Arch-Installation, WSL2 oder schlicht
ein Container:

```bash
cd gpu-triage
sudo bash ./offline/build_bundle.sh /pfad/zu/archlinux-YYYY.MM.DD-x86_64.iso
```

```bash
# gleichwertig, ohne eigenes Arch-System:
docker run --rm -v "$PWD":/repo -v /pfad/zu/isos:/iso:ro -w /repo \
  -e GPU_TRIAGE_OWNER="$(id -u):$(id -g)" archlinux:latest \
  bash offline/build_bundle.sh /iso/archlinux-YYYY.MM.DD-x86_64.iso
```

Das Skript löst die Dependency-Closure gegen den Arch Linux Archive Snapshot zum
ISO-Datum auf, lädt sie mit pacman und erzeugt:

```text
offline/packages/*.pkg.tar.zst          # was auf den Stick gehört
offline/manifest.env                    # bindet das Bundle an die Kernelversion des ISOs
offline/SHA256SUMS
offline/excluded.txt                    # was das ISO schon mitbringt
offline/dist/gpu-triage-bundle-*.zip    # dasselbe als ein Artefakt (+ .sha256)
offline/.dlcache/                       # persistenter Download-Cache
```

Ausgeliefert wird nur, was das Live-System **nicht** schon hat: Das Skript liest
`arch/pkglist.x86_64.txt` aus dem ISO und zieht jedes Paket ab, das dort mit
exakt derselben Version steht. Für das ISO vom 2026.08.01 sind das 151 von 176
Paketen — 372 MB statt 659 MB. Weicht eine Version ab, bleibt das Paket im
Bundle; Sicherheit geht vor Größe.

| Option | Wirkung |
| --- | --- |
| `--no-iso-subtract` | volle Closure ausliefern, nichts abziehen |
| `--clean-cache` | Download-Cache vorher verwerfen |
| `--keep-cache-only` | kein `offline/dist/*.zip` schreiben |

Der Bau braucht weder `pacstrap` noch ein chroot, läuft also in einem gewöhnlichen
Container ohne `--privileged`. pacman selbst besteht bei `-Sy`/`-Sw` auf uid 0,
deshalb eskaliert das Skript per `sudo` — Datenbank, Cache, Log und Install-Root
zeigen aber alle ins Repo, der Paketzustand des Build-Systems bleibt unberührt.

> **Kernel-Kreuzprüfung.** Nennt das ISO eine andere `linux`-Version als der
> Archive-Snapshot, bricht der Bau ab, statt einen `nvidia-open`-Stack für den
> falschen Kernel zu bauen. Dieser Fehler fällt damit beim Bauen auf und nicht
> erst beim Booten.

### 5. Stick synchronisieren

In PowerShell aus dem Repo-Verzeichnis:

```powershell
.\tools\sync-to-usb.ps1 -Drive E: -IsoPath C:\Downloads\archlinux-YYYY.MM.DD-x86_64.iso
```

`-IsoPath` ist optional und nur beim ersten Mal bzw. nach einem ISO-Wechsel nötig.
Download-Cache und ZIP-Artefakt bleiben absichtlich zurück — auf den Stick gehört
nur `offline/packages/`. Ergebnis:

```text
VENTOY_USB/
├── archlinux-YYYY.MM.DD-x86_64.iso
├── BOOT.txt                     # nur von prepare-usb.ps1: das Boot-Kommando
└── gpu-triage/
    ├── go.sh
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

## Lizenz

Für dieses Repository ist derzeit keine Lizenz hinterlegt. Alle Rechte vorbehalten,
bis eine Lizenzdatei ergänzt wird.
