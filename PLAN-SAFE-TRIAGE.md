# gpu-triage: Safe Triage + Memory Diagnostics

Stand des Audits: 2026-08-18
Audit-Basis: Branch `main`, Commit `8928298`

Priorität dieses Plans:

> Sicherheit > korrekte Attribution > reproduzierbare Evidenz > Automatisierung > Komfort

Dieses Dokument ist ein Umsetzungsplan. Es enthält absichtlich noch keinen
Produktivcode. Insbesondere wurde beim Audit keine GPU gebunden, entfernt,
zurückgesetzt, initialisiert oder anderweitig verändert.

## 1. Current-State Audit

### 1.1 Geprüfter Repository-Stand

- Branch: `main`
- HEAD: `8928298 Phase 5 mit Dokumentation und Regressionstests abschliessen`
- Arbeitsbaum vor diesem Plan: sauber, abgesehen vom unversionierten
  Aufgaben-Prompt `Prompt für Coding-Agent_ gpu-triage Safe Triage + Memory Diagnostics.md`
- Letzte relevante Entwicklung:
  - `1c7b378`: USB-Vorbereitung und `go.sh`
  - `5289fab`: strengere Zuordnung von `memtest_vulkan`
  - `0e865a4`/`b446463`: reproduzierbares Offline-Bundle
- Geprüft wurden `README.md`, `ROADMAP.md`, `go.sh`, `start.sh`,
  `scripts/bootstrap.sh`, `app/gpu_diag.py`, beide PowerShell-Werkzeuge,
  `offline/package-list.txt`, `offline/release.json` und sämtliche Tests.
- Aktueller Offline-Release: Arch ISO `2026.08.01`, erwarteter Kernel
  `7.1.5-arch1-2`, 25 Bundle-Pakete.

### 1.2 Was bereits gut funktioniert

- Vollständige und kurze BDFs werden strikt normalisiert; Teiltreffer wie `1`
  werden nicht mehr akzeptiert.
- Die GPU-Liste enthält BDF, Vendor, Device, Treiber und `boot_vga`.
- PCIe-Linkfelder und BAR-Größen werden bereits erhoben.
- AER wird am Endpoint und an per sysfs erkannten PCI-Ancestors vor und nach
  dem Test erfasst; neu auftauchende Zähler gehen nicht verloren.
- `memtest_vulkan` wird nicht einfach auf seinem automatisch gewählten ersten
  Gerät laufen gelassen. Fehlt das ausgegebene Bus:Device-Paar oder ist es
  mehrfach vorhanden, wird abgebrochen.
- Übersprungener oder nicht verfügbarer VRAM-Test kann bereits kein `PASS`
  erzeugen.
- Es gibt Text-, JSON- und memtest-Sidecar-Ausgabe.
- `go.sh` kann bei einem bereits erreichbaren Repository read-only Mounts
  remounten und kennt für die spätere Gerätesuche `/dev/mapper/<Partition>`.
- Offline-ISO und Paketbundle sind über `release.json`, Kernelversion und
  SHA256-Metadaten gekoppelt. Der Bootstrap-Stamp ist bootlokal unter `/run`.
- Die vorhandenen 39 Python-Tests und alle Shell-Tests liefen im Audit
  erfolgreich. Der PowerShell-Test konnte in dieser WSL-Sitzung wegen eines
  Host-Integrationsfehlers (`UtilBindVsockAnyPort`) nicht gestartet werden;
  dies ist kein festgestellter Testfehler im Skript.

### 1.3 Status der bekannten Probleme

| Problem | Status in HEAD | Befund |
| --- | --- | --- |
| Ventoy-Erstmount | teilweise gelöst | `go.sh` bevorzugt Mapper-Knoten, aber sowohl `BOOT.txt` als auch README und `sync-to-usb.ps1` mounten zuerst und ausschließlich `/dev/disk/by-label/Ventoy`. Genau dort liegt der Bootstrap-Zirkelschluss. Der Test deckt nur den Raw-Pfad ab. |
| Runtime vs. Treiber | offen, sicherheitskritisch | Jeder `quick`-Lauf ruft `bootstrap.sh` auf. Dieser installiert nicht nur Pakete, sondern führt `modprobe amdgpu`, Nouveau-Unbind sowie `modprobe nvidia*` aus. |
| Hard-Lock vor Userspace | offen | Es gibt keinen Safe-Bootmodus und keine Prüfung, ob der Zieltreiber schon vor `go.sh` gefährlich initialisiert wurde. |
| Pre-Driver-Triage | teilweise vorhanden | `list` und Teile von `basic_probe()` funktionieren ohne Treiber. Einen eigenen Modus, Stages, Treiber-Intent, pstore, DMI, vollständige Raw-Sidecars und mode-aware Ergebnisse gibt es nicht. |
| Ziel-GPU-Sicherheit | teilweise gelöst | Exakte CLI-BDFs sind möglich. Ohne `--gpu` wird bei genau einem Kandidaten weiterhin automatisch gewählt; für einen belastenden Lauf ist das zu großzügig. |
| Vulkan-BDF-Zuordnung | unzureichend | `memtest_vulkan` gibt nur Bus:Device aus. Domain und Function werden verworfen. Der Test `test_non_zero_domain_target_still_matches` schreibt dieses unsichere Verhalten sogar fest. |
| Treiberstatus/Klassifikation | falsch für Safe Mode | Jeder ungebundene Treiber ist `FAIL`, auch wenn die Blacklist beabsichtigt war. `SKIPPED`, `UNAVAILABLE` und `INCONCLUSIVE` werden überwiegend zu einem generischen `WARN`. |
| PCIe/AER-Semantik | teilweise gelöst | Zähler-Deltas sind vorhanden. Gelatchte `DevSta`-Bits werden nicht eigens modelliert; jede positive AER-Änderung, einschließlich korrigierbarer Ereignisse, wird pauschal `FAIL`. Root-Port-Aggregate werden nicht sauber von Endpoint-Attribution getrennt. |
| Kernel-Evidenz | teilweise gelöst | BDF-, AER-, Xid- und einige amdgpu-Zeilen werden gefiltert. Hard/soft lockup, watchdog, pstore, Vorher/Nachher-Deltas und ein vollständiges Kernel-Sidecar fehlen. |
| VBIOS | offen | Es wird nur die Existenz von `rom` gemeldet. Es gibt weder kontrolliertes Lesen noch Parsing, Hash oder Cleanup. |
| Report | teilweise gelöst | Der Text ist kurz, aber keine Ergebnis-Matrix. Messung, Beobachtung, Interpretation und Hypothese sind nicht getrennt. Writes erfolgen nicht atomar und erst am Ende. |
| Crash-Festigkeit | offen | Das memtest-Log wird laufend geflusht, Hauptreport und JSON entstehen aber erst nach allen Tests. Kein `fsync`, kein Stage-Checkpoint, kein pstore-Import. |
| VRAM-Reparaturevidenz | offen | Nur grobe Textzusammenfassungen aus `memtest_vulkan`; keine strukturierten Offsets/Werte/XORs, Reproduzierbarkeit oder unabhängige PCIe/VRAM/Compute-Matrix. |
| Physischer Speicherchip | korrekt nicht implementiert | Das MVP behauptet keinen Chip, hat aber auch noch kein formales UNKNOWN-/Confidence-Modell. |
| Testabdeckung | deutlich unvollständig | Gute Regressionen für frühere Fehler, aber keine vollständigen Fixtures für die 22 geforderten Zustände und keine State-Machine-Tests. |

### 1.4 Inzwischen veraltete Annahmen aus dem Aufgaben-Prompt

- Ein Mapper-Fallback fehlt nicht vollständig: Er existiert in `go.sh`, ist
  nur beim allerersten Mount noch nicht erreichbar.
- Unscharfe BDF-Eingaben und die unkontrollierte Autoselektion des ersten
  `memtest_vulkan`-Geräts wurden bereits deutlich verbessert.
- Endpoint- und Upstream-AER-Snapshots, Root-Port-Aggregatzähler sowie
  Before/After-Deltas existieren bereits.
- Ein übersprungener oder nicht verfügbarer VRAM-Test wird bereits nicht mehr
  als `PASS` klassifiziert.
- USB-Vorbereitung, Release-Pinning, Bundle-Subtraktion und Bootstrap-Stamp
  sind weiter entwickelt, als die Problembeschreibung allein vermuten lässt.

Diese Verbesserungen lösen jedoch weder die Boot-/Treibergefahr noch die
fehlende vollständige Vulkan-BDF-Zuordnung.

## 2. Safety Model

### 2.1 Safe by default

`gpu-triage triage --gpu <BDF>` wird adaptiv, aber nicht mutierend:

1. Stage 0 und 1 laufen immer zuerst.
2. Ein fehlender Treiber wird niemals automatisch geladen.
3. Ein gebundener Treiber wird niemals automatisch entfernt, gewechselt oder
   neu gebunden.
4. Driver-bound- und Lasttests laufen nur, wenn der erwartete Treiber bereits
   gebunden ist, die BDF exakt validiert wurde und das Target nicht als
   Boot-/Display-GPU benutzt wird.
5. `PASS` ist nur im vollständigen Modus möglich, wenn alle dafür erforderlichen
   Tests tatsächlich `PASS` sind.
6. Der Safe-Preflight der RX 6900 XT endet absichtlich `INCOMPLETE`, nicht
   `FAIL` oder `PASS`.

### 2.2 Operationsklassen

| Klasse | Beispiele | Default |
| --- | --- | --- |
| Read-only, pre-driver | sysfs-Textattribute, PCI-Topologie, AER-Zähler, `/proc/cmdline`, DMI, geladene Module, Kernel- und pstore-Logs, normales `lspci` ohne `-xxx/-xxxx` | erlaubt |
| Hostzustand, kein Device-State | Offline-Hashprüfung, Installation eines minimalen Safe-Runtime-Profils, Schreiben von Reports | erlaubt nach Stage-0-Prüfung; niemals Netzwerk |
| Driver-interaktiv | hwmon, `nvidia-smi`, Vulkan-Enumeration, Speicherallokation, Compute/Copy | nur wenn der korrekte Treiber bereits sicher gebunden ist |
| Device-State ändernd | PCI `enable`, ROM-Decoder `rom=1`, bind/unbind, Modul-Laden, Reset, remove/rescan | Default verboten; ROM nur explizit und mit Cleanup |
| Grundsätzlich ausgeschlossen | `/dev/mem`, BAR/MMIO-Schreiben, Register-Pokes, Clocks, Voltage, Powerlimit, Fan-Steuerung, Fehler-Injection, Flashen, Reflow | nicht implementieren |

`lspci -vv` bleibt als angeforderte Evidenz erhalten, aber sysfs ist für
maschinenlesbare Felder kanonisch. Extrem breite Config-Space-Dumps (`-xxx`,
`-xxxx`) und Bus-Mapping (`-M`) gehören nicht in den Standardlauf.

### 2.3 ROM ist ein eigener Opt-in

Linux dokumentiert `enable` als Referenzzähler; ein Zurückzählen auf null macht
nicht zwingend jede Initialisierung rückgängig. Außerdem muss der ROM-Decoder
durch Schreiben von `1` aktiviert werden. Deshalb ist ROM-Lesen nicht Teil des
read-only Defaults.

Für `--rom` gilt:

- ursprünglichen `enable`-Zähler lesen;
- nur bei `0` genau einmal `1` auf `enable` schreiben und merken, dass dieser
  Lauf inkrementiert hat;
- `rom=1`, binär lesen, in einem inneren `finally` immer `rom=0`;
- nur wenn selbst inkrementiert wurde, danach genau einmal `enable=0`;
- Cleanup-Fehler als eigenen Befund melden;
- niemals auf einen vermeintlichen Zielwert „hoch-/runterzählen“;
- niemals schreiben, flashen oder den ROM-Inhalt in den Hauptreport einbetten.

### 2.4 Safe Boot ist ein separater Systemzustand

Ein nach dem Boot eingegebener Befehl kann einen Hard-Lock während
`Triggering uevents...` nicht verhindern. Daher gibt es zwei Ausbaustufen:

1. **Sofort nutzbarer Safe-Boot mit offiziellem Arch-ISO:** dokumentierte,
   releasegetestete Bootparameter mit `module_blacklist=` für den DUT-Vendor.
   AMD-DUT: mindestens `amdgpu,radeon`; NVIDIA-DUT: mindestens `nouveau` und
   alle NVIDIA-Module. Keine globalen Parameter wie `pci=nomsi`.
2. **Robuster eigener Live-ISO-Pfad:** ein sehr früher Initramfs-Hook übernimmt
   eine explizite `gpu_triage.target=<BDF>`-Adresse und setzt vor dem udev-
   Coldplug einen nicht existierenden Sentinel in `driver_override`. Aus der
   Kernel-Dokumentation folgt, dass danach nur ein Treiber mit genau diesem
   Namen matchen dürfte; der Ansatz muss jedoch in VM und auf echter Hardware
   bewiesen werden, bevor er als sicher freigegeben wird.

Die globale Vendor-Blacklist kann eine gleichartige iGPU mit deaktivieren. Sie
ist für „AMD-iGPU + AMD-dGPU“ bzw. „NVIDIA-Anzeige + NVIDIA-DUT“ daher keine
vollständige Lösung. Bis der BDF-spezifische Initramfs-Guard validiert ist,
meldet die CLI diesen Aufbau als `BLOCKED: SAFE_BOOT_NOT_PROVEN`.

## 3. CLI / State Machine

### 3.1 Vorgeschlagene CLI

```text
go.sh list
go.sh doctor [--report-dir PATH]
go.sh triage --gpu 0000:03:00.0 [--preflight-only]
             [--rom] [--vram-seconds 60] [--no-vram]
```

- `list`: nur PCI/sysfs, keine Installation und keine Treiberaktion.
- `doctor`: ISO/Kernel/Bundle/Runtime/Reportziel prüfen, keine GPU-Aktion.
- `triage`: ein adaptiver Lauf; `--gpu` ist verpflichtend.
- `--preflight-only`: erzwingt Ende nach Stage 1, auch bei gebundenem Treiber.
- `--rom`: bewusstes Opt-in für Stage 2.
- `--no-vram`: Driver-bound Evidenz ohne Speicherlast; Overall bleibt
  `INCOMPLETE`.
- `quick` bleibt zunächst als deprecated Alias für `triage`, darf aber nicht
  das heutige unsichere Bootstrap-Verhalten behalten.

Es wird vorerst **kein** `probe-driver`-Kommando implementiert. Ein Userspace-
Timeout schützt nicht gegen einen Kernel-Hard-Lock. Ein absichtlicher
Treiber-Probeversuch gehört zunächst in eine manuelle Laboranweisung mit
serieller Konsole/pstore und hartem Power-Cycle-Protokoll, nicht in die normale
CLI.

### 3.2 Zustände und Übergänge

```text
START
  -> S0_ENVIRONMENT
       -> ABORTED                 (Target/Report/Bundle/Safety ungültig)
       -> S1_PRE_DRIVER
            -> [S2_ROM]           (nur --rom; Cleanup zwingend)
            -> COMPLETE_INCOMPLETE (ungebunden oder --preflight-only)
            -> S3_DRIVER_BOUND    (nur bereits korrekt gebunden)
                 -> COMPLETE_INCOMPLETE (Vulkan nicht exakt zuordenbar)
                 -> S4_VRAM_COMPUTE
                      -> COMPLETE_PASS | COMPLETE_FAIL | COMPLETE_INCOMPLETE
```

Jeder Zustandswechsel schreibt einen atomaren Checkpoint.

### 3.3 Stage-0-Gates

Abbruch mit Exit 2 vor jeglicher Device-Interaktion, wenn:

- BDF syntaktisch ungültig oder nicht exakt vorhanden;
- mehrere Kandidaten existieren und keine BDF angegeben wurde;
- Target kein unterstütztes Display-/3D-PCI-Gerät ist;
- Reportziel initial nicht atomar beschreibbar ist;
- Target `boot_vga=1` ist oder ein aktives Framebuffer-/DRM-Gerät nicht sicher
  ausgeschlossen werden kann;
- Safe-Boot behauptet wird, Zieltreiber aber bereits gebunden ist;
- Bundle/Kernel nicht zusammenpassen, sobald ein Runtime-Profil benötigt wird.

### 3.4 Treiberzustandsmodell

- `QUARANTINED_BDF`: Initramfs-Sentinel in `driver_override`, kein Treiber.
- `INTENTIONAL_GLOBAL_BLACKLIST`: passender Kernelparameter vorhanden, Ziel
  ungebunden; die Wirkung wird beobachtet, nicht allein aus cmdline behauptet.
- `BOUND_EXPECTED`: für Vendor/Generation erwarteter Treiber gebunden.
- `BOUND_OTHER`: z. B. `vfio-pci` oder `nouveau`; nur passende read-only
  Backend-Funktionen, keine automatische Migration.
- `UNBOUND_UNEXPLAINED`: keine belegte Quarantäne/Blacklist; Driver Init
  `FAIL`, Driver-bound Tests `BLOCKED`.
- `DISPLAY_RISK`: Target scheint Anzeige/Console zu tragen; Last und ROM
  `UNSAFE_SKIPPED`.

Die reine Anwesenheit eines Blacklist-Strings beweist keine wirksame
Quarantäne. Entscheidend sind cmdline **und** der beobachtete Driver-Symlink
bzw. der BDF-spezifische `driver_override`.

## 4. Data Collection

Raw-Ausgaben werden begrenzt in Sidecars gespeichert; der Hauptreport enthält
nur normalisierte Werte und Verweise.

| Messung | Quelle | Frage/Zweck | Risiko | Reportfeld |
| --- | --- | --- | --- | --- |
| Kernel/ISO | `uname`, `/etc/os-release`, ArchISO-Metadaten | Welches Laufzeitsystem? | read-only | Environment |
| Kernel-cmdline | `/proc/cmdline` | Safe-Boot-/Blacklist-Intent? | read-only | Safety Mode |
| Tool-Version | `git rev-parse` falls `.git` vorhanden, sonst eingebettete Release-ID | Welcher Code erzeugte den Report? | read-only | Environment |
| Bundle | `release.json`, `manifest.env`, `SHA256SUMS`, Bootstrap-Stamp | ISO/Kernel/Runtime konsistent? | Hash-I/O | Environment/Safety |
| Reportmedium | Tempdatei + Flush + Rename-Probe | Bleibt Evidenz speicherbar? | Host-Dateisystem-Write | Safety |
| Mainboard/BIOS | allowlist aus `/sys/class/dmi/id/{board_vendor,board_name,board_version,bios_version}` | Hostgegenprobe und Reproduzierbarkeit | read-only; keine Seriennummern per Default | Environment |
| PCI-Gesamtübersicht | `lspci -Dnnk`, Sidecar | Kandidaten und gebundene Treiber | Config-read | PCI sidecar |
| Target-Kurzinfo | `lspci -D -s BDF -nnk` | menschenlesbare Identität | Config-read | Target |
| Target-Details | `lspci -D -s BDF -vv` | Capabilities/DevSta als Beobachtung | breiterer Config-read, aber kein Dump | PCI sidecar/Observations |
| Topologie | `lspci -PP -s BDF`, `lspci -t`, sysfs-Symlinkpfad | Endpoint und Upstream-Kette | read-only | PCI topology |
| PCI-Identität | sysfs `vendor`, `device`, `revision`, `class`, Subsystem-IDs, `modalias` | exakte Target-/Board-Identität | read-only | Target |
| Anzeige-Rolle | `boot_vga`, `/sys/class/graphics/fb*`, DRM-Device-Symlinks | Ist Last/State-Änderung vertretbar? | read-only | Safety |
| Device-State | `enable`, `power_state`, Driver-Symlink, `driver_override`, `reset_method` nur lesen | Bindung/Quarantäne/Power | read-only | Safety/Driver |
| BARs | sysfs `resource` | BAR vorhanden/plausibel? | read-only | PCI |
| Link | sysfs current/max speed/width | Link aktiv/degradiert? | read-only | PCIe Link |
| DRM | `/sys/class/drm/*/device` und `dev` | BDF↔DRM-Major/Minor | read-only | Driver/Vulkan identity |
| AER Endpoint | drei `aer_dev_*`-Dateien | gezählte PCIe-Fehler am DUT | read-only | AER |
| AER Upstream | dieselben Dateien und Root-Port-Aggregate an allen Ancestors | Fehler oberhalb DUT? | read-only; Attribution kann geteilt sein | AER |
| Kernel vorher/nachher | `journalctl -k -b` oder `dmesg` | neue BDF/vendor/lockup/AER/reset/fault/Xid-Signale | read-only | Observations + kernel sidecar |
| Persistente Logs | `/sys/fs/pstore`, nur kopieren | Evidenz vom vorherigen Crash | read-only; nie löschen | Observations + pstore sidecar |
| Module | `/proc/modules`, `/sys/module/<driver>/{version,parameters}` mit Allowlist | geladener Stack und relevante Parameter | read-only | Driver |
| AMD-Telemetrie | BDF-zugeordnetes hwmon | Temperatur/Power/Fan als Messung | treiberinteraktiv | Telemetry |
| AMD RAS | nur vorhandene sysfs-`ras/*_err_count`, nie debugfs-control/inject | angebotene CE/UE-Evidenz | read-only, driver-bound | Driver evidence |
| NVIDIA | `nvidia-smi -i <BDF>` und BDF-bezogene Xids | Telemetrie/Fehler | treiberinteraktiv | Telemetry/Observations |
| Vulkan | eigener Mapper über PCI- und DRM-Properties | ist exakt dieses PhysicalDevice testbar? | driver-interaktiv | Vulkan identity |
| ROM | kontrollierter sysfs-ROM-Pfad | lesbar, Struktur und Identität plausibel? | Device-State ändernd | VBIOS + Sidecar |

Nicht in den knappen Standardreport gehören vollständige XML-, `lspci -vv`-
oder Kernel-Dumps. IOMMU-Gruppe wird nur aufgenommen, wenn `BOUND_OTHER`/
`vfio-pci` oder der Safe-Boot-Guard sie diagnostisch relevant macht.

## 5. Compact Report Design

### 5.1 Statusmodell

Test-/Matrixstatus:

- `PASS`: der für diesen Modus definierte Test lief vollständig und erfüllte
  seine Kriterien.
- `FAIL`: der Test lief oder ein erforderlicher Zustand wurde geprüft und
  lieferte klare negative Evidenz.
- `WARN`: Messung lief, aber es gibt eine nicht beweisende Auffälligkeit.
- `NOT_RUN`: bewusst nicht Teil dieses Laufs.
- `UNSAFE_SKIPPED`: Safety-Gate verbietet die Ausführung.
- `UNAVAILABLE`: benötigte Schnittstelle/Backend fehlt.
- `BLOCKED`: eine nicht erfüllte Voraussetzung verhindert den Test.
- `INCONCLUSIVE`: Test begann, erlaubt aber keine belastbare Attribution.
- `UNKNOWN`: ausschließlich für nicht bestimmbares Wissen/Hypothesen, etwa
  den physischen Speicherchip; kein Ersatz für einen Ausführungsstatus.

Overall hat nur drei Werte:

- `PASS`: ausschließlich Full-Mode, alle erforderlichen Matrixzeilen `PASS`.
- `FAIL`: mindestens ein klarer Fehler; andere Zeilen dürfen trotzdem
  `NOT_RUN` sein und bleiben sichtbar.
- `INCOMPLETE`: kein klarer Fehler, aber mindestens ein erforderlicher Test
  nicht erfolgreich abgeschlossen.

AER wird differenziert:

- unveränderte Zähler: `PASS (no active counted errors observed)`;
- neue correctable Events: `WARN`, sofern kein Schwellenwert/Linkabbruch;
- neue nonfatal/fatal Events oder Linkverlust: `FAIL`;
- alte `DevSta+`-Bits ohne Zählerdelta: Observation/WARN, nie allein Beweis.

### 5.2 Dateiformat

```text
<stem>.md                  kurzer Primärreport, Ziel 50–120 Zeilen
<stem>.json                strukturiertes vollständiges Ergebnis
<stem>-kernel.log          relevante Raw-Kernelzeilen + Zeitfenster
<stem>-lspci.txt           angeforderte PCI-Ausgaben
<stem>-pstore/             kopierte persistente Logs, falls vorhanden
<stem>-vram.jsonl          begrenzte Fehlerrecords und Aggregationen
<stem>-vbios.rom           nur bei --rom und erfolgreichem Lesen
```

Der Report trennt strikt:

- `KEY MEASUREMENTS`: direkt gemessene Werte;
- `OBSERVATIONS`: Ereignisse/Logs, einschließlich vom Nutzer importierter
  Vorgeschichte;
- `INTERPRETATION`: eng aus Messungen abgeleitete Aussage;
- `HYPOTHESES`: rangierte, widerlegbare Möglichkeiten mit Evidenz dafür/dagegen.

### 5.3 Beispiel: RX 6900 XT im Safe-Boot

```text
GPU-TRIAGE REPORT
Run: 2026-08-18T14:20:00+02:00  Tool: <commit/release>

TARGET
BDF: 0000:03:00.0
PCI: 1002:73af rev c0  subsystem 1462:3955
Role: non-boot display device (boot_vga=0; no framebuffer owner found)

ENVIRONMENT / SAFETY
Mode: SAFE PREFLIGHT + EXPLICIT ROM
Boot evidence: amdgpu intentionally blacklisted; target unbound
Automatic driver load/bind/reset: NOT PERFORMED

RESULT MATRIX
PCI enumeration       PASS
Target identity       PASS
PCIe link             PASS       16 GT/s x16 (max 16 GT/s x16)
AER                    PASS       no active counted errors observed
Driver init            NOT_RUN    intentional safe mode
Telemetry              UNSAFE_SKIPPED
Vulkan                 UNSAFE_SKIPPED
VBIOS ROM              PASS       readable and structurally plausible
VRAM correctness       NOT_RUN    no safely initialized Vulkan device
Compute                NOT_RUN
Physical VRAM package  UNKNOWN
Overall                INCOMPLETE

KEY MEASUREMENTS
Driver symlink: none
PCI ancestors: 0000:03:00.0 -> <upstream BDFs>
AER endpoint/upstream counters: all observed values 0
ROM size: 121856 bytes
ROM SHA256: <sha256>
ROM header/PCIR: valid chain; PCI identity consistent where encoded
ROM strings observed: 113-V395TRIO-3OC; 113-MSITV395MH.301; NAVI21EXT;
                      K4ZAF325BM

OBSERVATIONS
- Prior normal boots reportedly hard-lock during amdgpu initialization near
  "Triggering uevents"; the reported CPU number varied between boots.
- Prior manual amdgpu load reportedly stopped after early MMIO/init messages
  and was followed by hard-lock/power-off.
- Prior Windows result: Code 43.
- lspci DevSta contains latched CorrErr+/UnsupReq+ indications, while no AER
  counter increase was observed in this run.

INTERPRETATION
- PCI enumeration, identity, BAR presence and negotiated link are available.
- This run found no active counted AER errors; it does not prove the link is
  fault-free under load.
- Driver initialization was deliberately not attempted.
- VRAM and compute correctness remain untested.
- The ROM is readable and plausible, not cryptographically authenticated.

HYPOTHESES
1. Failure during GPU driver/device initialization — confidence MEDIUM
   For: repeated prior Linux hard-lock at amdgpu init; Windows Code 43.
   Against: exact failing block is not observed in this safe run.
2. VRAM or memory-path fault — confidence LOW / UNPROVEN
   For: compatible with some initialization failures in principle.
   Against: no VRAM correctness test ran; PCI/AER/ROM observations do not
            localize a memory package.
3. Obvious VBIOS corruption — confidence LOW
   For: Windows tooling reported BIOS unknown.
   Against: PCI ROM is readable, structurally plausible and identity-consistent.

NOT TESTED / LIMITATIONS
- No driver bind, telemetry, Vulkan, VRAM, compute, reset or MMIO access.
- Allocation offsets would not by themselves identify physical GDDR packages.
- Physical VRAM package: UNKNOWN.

NEXT SAFE TESTS
1. Preserve/import pstore or serial-console output from any independently
   approved probe boot.
2. Compare the ROM hash/parsed image chain with a sourced exact-board image.
3. Inspect board-level rails/signals with appropriate external equipment;
   do not infer a VRAM chip from this report.
```

## 6. VRAM Diagnostic Architecture

### 6.1 Grenzen des heutigen `memtest_vulkan`

- Das Tool erzeugt nützliche Stress- und Bitfehlerstatistik, sagt aber selbst,
  dass gemeldete Fehler nicht sicher zwischen VRAM-IC, GPU/Controller, PCIe,
  Treiber und Rechenfehler unterscheiden.
- Die Standardlaufzeit des Upstream-Tools ist auf thermisches Verhalten über
  mehrere Minuten ausgelegt; der heutige 60-s-Wrapper ist nur ein kurzer
  Screen, kein gleichwertiger Standardtest.
- Das ausgegebene Linux-Gerätekennzeichen enthält im heute geparsten Format
  weder PCI-Domain noch Function.
- gpu-triage erhält nur zusammengefasste Adressbereiche und Textstatistik,
  nicht für jeden Fehler erwartetes/gelesenes Wort und Wiederholbarkeit.
- Ein Vulkan-Allokationsoffset ist keine dokumentierte physische VRAM-Adresse.
- Große zusammenhängende Allokationen, Resizable BAR, Memory-Budget und
  Treiberverhalten begrenzen die tatsächlich getestete Speichermenge.

Übergang: `memtest_vulkan` bleibt vorübergehend ein klar markiertes
`LEGACY_SCREEN`. Es darf nur laufen, wenn ein separater Vulkan-Mapper genau ein
PhysicalDevice der vollständigen BDF zuordnet und in derselben ICD-Sicht kein
zweites Hardwaregerät die Indexzuordnung mehrdeutig macht. Andernfalls
`UNAVAILABLE: EXACT_DEVICE_MAPPING_NOT_PROVEN`. Der bestehende Test, der eine
nicht-null Domain trotzdem akzeptiert, wird in eine Abbruchregression gedreht.

### 6.2 Eigener Helper

Ein kleiner nativer Vulkan-Helper wird als auditiertes C17-Programm gebaut und
als versionsgebundene Binary ins Offline-Bundle gelegt. Shaderquellen und
reproduzierbar erzeugte SPIR-V-Dateien bleiben im Repository; auf dem
Diagnose-PC ist kein Compiler nötig.

Vor `vkCreateDevice`:

1. alle PhysicalDevices enumerieren;
2. `VK_EXT_pci_bus_info` abfragen und Domain/Bus/Device/Function exakt
   vergleichen;
3. zusätzlich Vendor-/Device-ID vergleichen;
4. falls PCI-Bus-Info fehlt, `VK_EXT_physical_device_drm` abfragen und
   Major/Minor über `/sys/dev/char` bzw. DRM-sysfs exakt zur BDF auflösen;
5. genau ein Match verlangen; null oder mehrere Matches brechen vor Allokation ab.

Der Helper schreibt versionierte JSON-Lines. Pro begrenztem Fehlerrecord:

```json
{"allocation":2,"offset":1048576,"width_bits":32,
 "expected":"0xaaaaaaaa","actual":"0xaaa8aaaa","xor":"0x00020000",
 "bits_0_to_1":[],"bits_1_to_0":[17],"pattern":"alternating_aa",
 "seed":1234,"pass":3,"reread":1,"timestamp_ms":8421,"temp_mC":68125}
```

Records werden gedeckelt; vollständige Summen bleiben erhalten. Der Hauptreport
zeigt nur Gesamtzahl, erste/letzte Offsets, XOR-/Bit-Histogramm, Cluster,
Stride-Kandidaten und Reproduzierbarkeitsquoten.

### 6.3 Pattern- und Wiederholungsplan

- all-zero / all-one;
- `0xAA`/`0x55` alternating und invertiert;
- walking-one / walking-zero pro Wort;
- allocation-offset-derived und inverse Muster;
- deterministischer PRNG mit gespeichertem Algorithmus, Seed und Version;
- sequenzielle und mehrere primzahl-/cachelinebezogene Strides;
- write-once/repeated-read;
- repeated-write/read;
- erneutes Lesen derselben logischen Adresse nach einem Fehler;
- neuer Pass in derselben Allokation;
- neue Allokation mit demselben logischen Offset.

„Gleicher Offset in neuer Allokation“ ist nur Reproduzierbarkeit im virtuellen
Testlayout; ohne dokumentierte physische Platzierung beweist es nicht dieselbe
Speicherzelle.

Limits sind explizit: Sekunden, maximale Bytes/VRAM-Anteil, maximale
Fehlerrecords und optionale read-only Temperatur-Abbruchschwelle. Fehlt
Temperaturtelemetrie, bleibt sie `UNAVAILABLE`; es wird nicht geraten. Ein
`VK_ERROR_DEVICE_LOST` ist ein eigener Status und niemals gleichbedeutend mit
`VRAM FAIL`.

### 6.4 Unabhängige Evidenzmatrix

| Experiment | Hauptpfade | Aussage bei Fehler | Wichtige Grenze |
| --- | --- | --- | --- |
| Host-fill -> transfer upload -> transfer download -> CPU compare | Host, PCIe/DMA, Copy, VRAM | Transfer-/Memory-Pfad fehlerhaft | isoliert PCIe nicht vollständig |
| GPU compute schreibt Pattern -> transfer download -> CPU compare | Compute, VRAM, Copy, PCIe | Compute- oder Memory-/Transferpfad | Vergleich mit anderen Experimenten nötig |
| GPU-local Copy A->B -> Download/Compare | Copy-Queue, VRAM, PCIe beim Readback | Copy-/Memory-Pfad | „Copy“ darf ohne Vendorbeleg nicht „SDMA“ heißen |
| kleiner Compute Known-Answer-Test in mehrfach gelesenen Buffern | Compute + wenig Speicher | Compute-Pfad verdächtig | Speicher ist nie vollständig aus dem Test entfernbar |
| wiederholtes Readback ohne Rewrite | VRAM-Storage/Read + Transfer | Persistenz/Read-Pfad verdächtig | Cache und Treiber können das Bild beeinflussen |

Die Klassifikation entsteht aus dem Muster mehrerer Experimente, nicht aus
einem monolithischen „VRAM bad“.

## 7. Physical Memory Package Mapping

### Ebene A: Memory-path fault likely

Reproduzierbare Datenabweichungen in exakt zugeordneten, erfolgreichen
Vulkan-Läufen erlauben `MEMORY_PATH_FAULT_LIKELY`. Sie erlauben nicht
`VRAM_CHIP_BAD`.

### Ebene B: Offset-/Bit-Lane-Evidenz

Generisch möglich sind XOR-Histogramme, 0→1/1→0-Verteilung, wiederkehrende
Bitindizes, logische Offset-Cluster, Strides und Reproduzierbarkeit. Diese Daten
sind Beobachtungen, keine Channel-Namen.

### Ebene C: ASIC Channel/Lane inference

Nur ein separates, versionsgebundenes ASIC-Mapping darf logische Evidenz in
Channel/Lane-Hypothesen übersetzen. Jede Regel braucht:

- ASIC-ID und Revisionen;
- genaue Definition, ob Eingabe virtuell, allokationsrelativ oder physisch ist;
- Primärquelle oder dokumentiertes Experiment;
- Validierungsfixtures und Known-Fault-Gegenproben;
- Mapping-Version und Confidence im Report.

Fehlt einer dieser Punkte, lautet das Ergebnis `Channel/Lane: UNKNOWN`.

### Ebene D: Board package mapping

Eine Board-Datenbank ist getrennt von ASIC-Regeln. Schlüssel mindestens:

- PCI Vendor/Device;
- Subsystem Vendor/Device;
- VBIOS-Hash oder ausreichend starke VBIOS-Identität;
- PCB-Revision, wenn Varianten dasselbe Subsystem teilen.

Ein Eintrag enthält `channel/lane -> package_ref`, Quelle, Foto-/Schematic-
Revision, Prüfer, Datum und Confidence. `HIGH` ist nur bei exakter
Boardidentität plus belegtem Mapping zulässig; `MEDIUM` bei plausibler, aber
nicht vollständiger Variantenidentität; `LOW` wird nur als Hypothese gezeigt.

Immer sichtbar bleiben Alternativen: Memory-Controller, GPU-BGA, Leiterbahn,
Versorgung und Signalintegrität. Ohne belastbaren Datenbanktreffer:

```text
Physical VRAM package: UNKNOWN
```

## 8. Files to Change

### Bestehende Dateien

- `go.sh`: Bootstrap-Erstmount in eine kleine, auch aus `BOOT.txt` nutzbare
  Mapper-first-Zeile überführen; Argumente unverändert weiterreichen.
- `start.sh`: read-only Preflight vor Runtime-Bootstrap routen; Safe- und
  Full-Runtimeprofile trennen.
- `scripts/bootstrap.sh`: zu reinem Offline-Runtime-Installer machen;
  sämtliche `modprobe`, unbind- und nouveau-Entfernungsschritte löschen.
- `offline/package-list.txt` und Bundle-Metadaten: Pakete in `safe-runtime` und
  `driver-bound-runtime` klassifizieren; fehlende `SHA256SUMS` künftig fail
  closed statt unverified install.
- `app/gpu_diag.py`: CLI und Orchestrator auf State Machine umstellen.
- `tools/prepare-usb.ps1`: Mapper-first `BOOT.txt`, `SAFE-BOOT.txt`, Doctor-
  Prüfung des tatsächlichen Inhalts statt nur der Existenz.
- `tools/sync-to-usb.ps1`: dieselbe korrigierte Einstiegshilfe ausgeben.
- `README.md`: Safe-Boot als zwingende Voraussetzung erklären, Supportclaims
  in „erkannt“ vs. „hardwarevalidiert“ trennen.
- `ROADMAP.md`: Phasen und Sicherheitsgates dieses Plans übernehmen.

### Neue, klar begrenzte Komponenten

- `app/triage_model.py`: Statuswerte, Stages, Result-/Evidence-Dataclasses.
- `app/collectors.py`: ausschließlich read-only Stage-0/1-Collector.
- `app/reporting.py`: Markdown/JSON, Checkpoints, atomare Writes.
- `app/rom_reader.py`: einziges ROM-State-Change-Modul mit garantiertem Cleanup.
- `app/driver_probe.py`: bereits gebundene Treiber, Telemetrie, Vulkan-Mapper;
  keine Bind-/Modprobe-Funktion.
- `docs/SAFE-BOOT.md`: offizielle ISO-Profile, gleiche Vendor-GPU-Grenze,
  Hard-Lock-Laborverfahren.
- `liveiso/`: späterer eigener ArchISO-Profile/Initramfs-Guard; erst nach
  erfolgreichem Proof-of-Concept freigeben.
- `vram-helper/`: C17-Quelle, Shader, JSONL-Schema, reproduzierbarer Build.
- `data/asics/` und `data/boards/`: zunächst leere, schemavalidierte
  Provenance-Datenbanken; keine spekulativen Mappings.
- `tests/fixtures/`: sysfs-, lspci-, Kernel-, pstore-, Vulkan-, memtest- und
  ROM-Fixtures.

### Vorgeschlagene kleine Commits

1. Sicherheitsregressionen zunächst rot hinzufügen.
2. Bootstrap von Treiberaktionen entkoppeln.
3. Mapper-first BOOT und Safe-Boot-Dokumentation.
4. Statusmodell und State Machine, noch ohne neue Messungen.
5. Stage-0/1-Collector und Raw-Sidecars.
6. kompakter Report und atomare Stage-Checkpoints.
7. expliziter ROM-Modus samt Parser/Cleanup-Tests.
8. Driver-bound-Probe und exakte Vulkan-BDF-Gates.
9. eigener VRAM-Helper und Evidenzmatrix.
10. erst danach ASIC- und Board-Datenmodelle.

## 9. Testing Strategy

### 9.1 Hardwarefreie Architektur

Alle OS-Zugriffe werden über injizierbare Roots und einen Command-Runner
geführt (`sysfs_root`, `proc_root`, `pstore_root`, `run_command`). Fixtures
enthalten Rohdateien; erwartete Reports werden als kompakte Golden-Snapshots
geprüft. Tests prüfen sowohl Status als auch verbotene Aufrufe.

### 9.2 Pflichtfixtures

| # | Fixture | Zwingende Erwartung |
| --- | --- | --- |
| 1 | gesunde AMD-GPU | Full-Matrix kann PASS erreichen |
| 2 | gesunde NVIDIA-GPU | BDF-spezifisches `nvidia-smi`, Full-PASS |
| 3 | nur iGPU | kein DUT; Exit 2, keine Autoselektion |
| 4 | mehrere dGPUs | `--gpu` zwingend |
| 5 | AMD ungebunden/Safe | Driver `NOT_RUN`, Last `UNSAFE_SKIPPED`, Overall `INCOMPLETE` |
| 6 | NVIDIA ungebunden/Safe | wie 5, ohne `modprobe` |
| 7 | BARs, kein Treiber, kein Safe-Intent | Driver `FAIL`, VRAM `BLOCKED` |
| 8 | Link degradiert | Link `WARN` oder `FAIL` nach definierter Erwartungsquelle, Messwerte sichtbar |
| 9 | Endpoint-AER | exakte Endpoint-Attribution |
| 10 | nur Upstream-AER | kein erfundener Endpoint-Fehler; Hierarchie sichtbar |
| 11 | DevSta gelatcht, AER 0/Delta 0 | Observation/WARN, nicht aktiver AER-Fail |
| 12 | Treiber gebunden, Telemetrie fehlt | Telemetrie `UNAVAILABLE`, andere Tests unabhängig |
| 13 | Vulkan-BDF nicht eindeutig | Abbruch vor Allokation |
| 14 | VRAM-Datenfehler | VRAM `FAIL`, Offsets/XOR/Bitstatistik erhalten |
| 15 | Vulkan Device Lost | `INCONCLUSIVE`/eigener Device-Lost-Befund, nicht automatisch „VRAM chip“ |
| 16 | VRAM nicht ausführbar | `UNAVAILABLE`/`BLOCKED`, nie PASS |
| 17 | Medium wird read-only | letzter Checkpoint bleibt gültig; klare Fallback-/Screen-Meldung |
| 18 | Ventoy Raw-Mount scheitert, Mapper klappt | BOOT-Pfad startet `go.sh` über Mapper |
| 19 | ROM fehlt | `UNAVAILABLE`, keine enable-Schreiboperation |
| 20 | ROM vorhanden, Lesen fehlschlägt | Cleanup trotzdem nachweislich ausgeführt |
| 21 | gültige 55aa/PCIR-Kette | Bilder, IDs, Längen, Last-Indicator und SHA korrekt |
| 22 | RX 6900 XT 1002:73af/1462:3955, x16, AER 0, kein Treiber | VRAM `NOT_RUN`, Memory fault `UNPROVEN`, Package `UNKNOWN`, Overall `INCOMPLETE` |

Zusätzliche Sicherheitsregressionen:

- Kein Safe-/Preflight-Test darf `modprobe`, `rmmod`, bind/unbind, remove,
  rescan, reset, `resourceN`-Write oder `/dev/mem` aufrufen.
- ROM-Test prüft jede Exception-Position und den exakten Cleanup-Callgraph.
- Nicht-null PCI-Domain und Function ungleich 0 müssen exakt matchen oder
  abbrechen.
- Korrigierbare, nonfatal und fatal AER-Deltas werden getrennt bewertet.
- Root-Port-Gesamtzähler werden nicht als eindeutiger Endpoint-Fehler ausgegeben.
- Hauptreport bleibt unter dem definierten Zeilenbudget; Raw-Dumps sind nur
  Sidecars.
- Jeder Statuspfad hat einen Test, der `PASS` bei fehlendem Pflichtresultat
  verhindert.
- PowerShell-Tests validieren Inhalt und Mapper-Reihenfolge in `BOOT.txt`.

### 9.3 Echte Hardware-Gegenproben

| Aufbau | Safe Boot | Preflight | Driver-bound | ROM | VRAM/Compute |
| --- | --- | --- | --- | --- | --- |
| Intel iGPU + Known-good AMD dGPU | AMD-Blacklist | ja | separater Normalboot | opt-in | ja |
| Intel iGPU + Known-good NVIDIA dGPU | NVIDIA-Blacklist | ja | separater Normalboot | opt-in | ja |
| AMD iGPU + AMD dGPU | BDF-Initramfs-Guard | ja | separater Normalboot | erst nach Guard-Validierung | ja |
| zwei dGPUs | je Ziel-BDF | explizite Auswahl | exakte Vulkan-Zuordnung | opt-in | jeweils getrennt |
| RX 6900 XT Praxisfall | AMD-Blacklist/Guard | ja, primärer Test | absichtlich nein | opt-in | `NOT_RUN` |

Jede Hardwarefreigabe dokumentiert Board, BIOS, Kernel, Bootcmdline, BDFs,
Treiber, Displayverkabelung und Ergebnis. Ein Known-bad-Test darf erst nach den
Known-good-Gegenproben stattfinden.

## 10. Implementation Phases

### Phase 0 — unmittelbarer Safety-Fix

- Treiberaktionen aus `bootstrap.sh` entfernen.
- `quick` darf ohne bereits gebundenen Treiber keine Last starten.
- BOOT/README auf Mapper-first korrigieren.
- Safe-Boot-Anleitung und klare Same-Vendor-Grenze ausliefern.

### Phase 1 — sichere Pre-Driver-Triage

- Stage 0/1, explizite BDF, Treiber-Intent, vollständige PCI-/AER-/Kernel-/
  pstore-Evidenz.
- Safe-Runtime-Profil ohne GPU-Treiberaktivierung.
- RX-6900-XT-Fixture und Verbots-Call-Tests.
- BDF-spezifischen Initramfs-Guard prototypisieren und erst nach praktischer
  Validierung als freigegeben markieren.

### Phase 2 — kompakter, crash-toleranter Report

- Statusmatrix, vier Evidenzebenen, 50–120 Zeilen.
- Raw-Sidecars, atomare Checkpoints und begrenztes `fsync` an Stage-Grenzen.
- Wenn das USB-Medium ausfällt: weiter in `/run/gpu-triage` spiegeln und den
  Verlust der Persistenz deutlich melden; keine falsche Crash-Garantie.

### Phase 3 — bereits gebundene Treiber und Isolation

- Driver-bound-Collector, Telemetrie, AMD-RAS-read-only, NVIDIA-Xid.
- Exakte Vulkan-BDF-Zuordnung.
- Legacy-`memtest_vulkan` nur unter strengem Eindeutigkeitsgate.
- Keine automatische Driver-Probe-Funktion.

### Phase 4 — eigener VRAM-/Compute-Helper

- Host/Transfer, GPU-local Copy, Compute-KAT und VRAM-Pattern als getrennte
  Experimente.
- Strukturierte Fehlerrecords, Re-Reads, Pass-/Allocation-Reproduzierbarkeit,
  Cluster/Stride und Temperatur/Zeitlimits.

### Phase 5 — ASIC Channel/Lane inference

- Erst nach belastbarer Forschung und Known-Fault-Validierung.
- Fehlt ein belegtes Mapping, bleibt jeder Channel/Lane-Wert `UNKNOWN`.

### Phase 6 — Board -> Package

- Provenance-Datenbank, exakte Boardidentität, Confidence und Quellen.
- Kein heuristischer Package-Name aus einem Offset allein.

## 11. Acceptance Criteria

### Phase 0

- Quellcode-Suche und Tests beweisen: Safe-/Preflight-Pfade enthalten keinen
  erreichbaren `modprobe`, bind/unbind, reset, remove oder rescan.
- Das generierte `BOOT.txt` versucht den aus dem Label-Link abgeleiteten
  Mapper-Knoten vor der Raw-Partition; Fixture 18 ist grün.
- README sagt ausdrücklich, dass nur ein Safe Boot den frühen Lock verhindern kann.

### Phase 1

- Ein einziger `triage --gpu 0000:03:00.0` erzeugt auf Fixture 22 einen
  verwertbaren Report ohne Treiber- oder Vulkanaufruf.
- Alle PCI-Identitätsfelder, Ancestors, Endpoint-/Upstream-AER, cmdline,
  Treiberzustand und pstore-Verfügbarkeit sind strukturiert.
- Target-BDF ist immer explizit; Mehr-GPU- und Display-Risk-Gates brechen ab.
- Safe Boot ist auf mindestens AMD- und NVIDIA-DUT mit fremdem iGPU-Vendor
  praktisch getestet; Same-Vendor bleibt bis Guard-Freigabe ehrlich `BLOCKED`.

### Phase 2

- Normalreport umfasst 50–120 kurze Zeilen und enthält keine Raw-Dumps.
- Nach künstlichem Fehler an jeder Stage ist der letzte atomare Report parsebar
  und als `INCOMPLETE` markiert.
- Bei read-only werdendem Medium gibt es keine Traceback-only-Ausgabe und kein
  beschädigtes finales JSON/Markdown.

### Phase 3

- Kein Vulkan-Test beginnt, bevor genau ein PhysicalDevice auf vollständige
  Domain/Bus/Device/Function oder eindeutig auf den DRM-Knoten gemappt ist.
- Gebundener AMD- und NVIDIA-Known-good-Aufbau erzeugt korrekte Telemetrie- und
  Kernel-Deltas.
- Ungebundene Targets führen zu null Modul-/Bind-Aufrufen.

### Phase 4

- Jeder Fehlerrecord enthält Allocation, Offset, expected, actual, XOR,
  Richtungsbits, Pattern/Seed/Pass und Wiederholungsinformation.
- Alle geforderten Patternfamilien besitzen deterministische Unit-/Shader-
  Tests und künstliche Fehler-Injection im Helper-Testmodus.
- PCIe/Transfer, VRAM und Compute erscheinen unabhängig in der Matrix.
- Zeit-, Speicher-, Fehlerrecord- und Temperaturgrenzen werden getestet.

### Phase 5

- Ohne belegtes ASIC-Profil lautet Channel/Lane immer `UNKNOWN`.
- Jedes Profil enthält Quelle, Version und Known-Fault-Validierung; der Report
  nennt Mapping-Version und Confidence.

### Phase 6

- Nur exakte Board-/VBIOS-/gegebenenfalls PCB-Treffer dürfen ein Package nennen.
- Jeder Package-Befund nennt Confidence und Quelle sowie Controller/BGA/
  Signalpfad als verbleibende Alternativen.
- Jeder nicht exakte Fall gibt wörtlich `Physical VRAM package: UNKNOWN` aus.

## 12. Open Questions / Research Needed

1. Sind `amdgpu`/`radeon` im exakt gepinnten ArchISO-Kernel garantiert Module
   und nicht built-in? Das muss Release-Metadatum und Test sein, nicht Annahme.
2. Welche Bootloader-Eingabe ist für das konkrete ArchISO/Ventoy-Paar am
   zuverlässigsten, und kann `prepare-usb.ps1` einen getesteten Safe-Menüeintrag
   statt nur Textanweisungen bereitstellen?
3. Läuft der vorgeschlagene BDF-`driver_override`-Initramfs-Hook garantiert vor
   allen relevanten uevents, und bleibt er bei Same-Vendor-iGPU praktisch stabil?
4. Welche minimale Safe-Runtime ist bereits im ArchISO vorhanden? Danach sind
   `safe-runtime`-Paketrollen und Bundlegröße festzulegen.
5. Welche Version von `memtest_vulkan` steckt reproduzierbar im Arch-Bundle,
   und ist deren Ausgabe/Indexreihenfolge exakt festgeschrieben? Bis zur Antwort
   bleibt sie Legacy und streng gegated.
6. Welche getesteten AMD-/NVIDIA-Treiber bieten `VK_EXT_pci_bus_info` und/oder
   `VK_EXT_physical_device_drm` auf den unterstützten Generationen?
7. Welche Vulkan-Queue führt GPU-local Copies tatsächlich aus? Ohne Vendorbeleg
   darf die Matrix nur „Copy path“, nicht „SDMA“, sagen.
8. Wie viel VRAM kann ohne Verdrängung/Systeminstabilität getestet werden?
   Defaultgrenzen müssen aus Known-good-Messungen entstehen.
9. Welche read-only Temperaturquelle ist pro Backend ausreichend zuverlässig
   für einen Abbruch, und was geschieht ohne Telemetrie?
10. Welche AMD-ATOM-Strukturen und Board-Strings sind pro Generation offiziell
    bzw. aus Kernelquellen belastbar parsebar? Freie Stringtreffer bleiben nur
    Beobachtungen.
11. Für welche Plattformen ist pstore im Arch-Kernel samt Backend tatsächlich
    verfügbar, und wie verhindern wir, dass fremde Dienste Evidenz vor dem Tool
    verschieben/löschen? gpu-triage selbst löscht nie pstore.
12. Welche PCIe-Linkbreite ist für ein Board „erwartet“? `max_link_width` des
    Endpoints allein erkennt Slot-/CPU-Limits nicht sicher; unbekannte Erwartung
    muss `UNKNOWN` bleiben.
13. Gibt es öffentlich belegte Navi-21-Adress-/Interleave-Mappings und exakte
    MSI-V395-PCB-Package-Zuordnungen? Ohne belastbare Quellen bleibt Ebene C/D
    deaktiviert.
14. Welche Lizenz soll der neue native Helper und die Boarddatenbank haben?
    Das Repository besitzt derzeit keine Lizenz.

## Technische Primärquellen für die Umsetzung

- Linux PCI sysfs, einschließlich `enable`-Zähler und ROM-Aktivierung:
  https://docs.kernel.org/PCI/sysfs-pci.html
- Linux AER-sysfs-ABI und Root-Port-Aggregate:
  https://docs.kernel.org/admin-guide/abi-testing.html#symbols-under-sys-bus-pci-devices
- Linux Driver Binding/`driver_override`:
  https://docs.kernel.org/driver-api/driver-model/binding.html
- Linux Kernelparameter, insbesondere `module_blacklist`:
  https://docs.kernel.org/admin-guide/kernel-parameters.html
- Linux pstore:
  https://docs.kernel.org/power/shutdown-debugging.html
- AMDGPU RAS (nur read-only sysfs-Teile verwenden):
  https://docs.kernel.org/gpu/amdgpu/ras.html
- Vulkan `VK_EXT_pci_bus_info`:
  https://registry.khronos.org/vulkan/specs/latest/man/html/VK_EXT_pci_bus_info.html
- Vulkan `VK_EXT_physical_device_drm`:
  https://registry.khronos.org/vulkan/specs/latest/man/html/VK_EXT_physical_device_drm.html
- UEFI PCI Option-ROM-/PCIR-Strukturen:
  https://uefi.org/specs/UEFI/2.10_A/14_Protocols_PCI_Bus_Support.html
- NVIDIA Xid-Dokumentation:
  https://docs.nvidia.com/deploy/xid-errors/introduction.html
- Upstream `memtest_vulkan`, einschließlich eigener Grenzen der Interpretation:
  https://github.com/GpuZelenograd/memtest_vulkan
