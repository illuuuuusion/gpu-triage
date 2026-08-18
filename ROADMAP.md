# gpu-triage Roadmap

## Phase 0 — Safety-Fix (implementiert)

- Bootstrap ohne `modprobe`, bind/unbind, remove, rescan oder reset
- `quick` als deprecated Alias des adaptiven sicheren `triage`-Pfads
- Mapper-first `BOOT.txt`
- Safe-Boot-Anleitung mit expliziter Same-Vendor-Grenze

## Phase 1 — sichere Pre-Driver-Triage (implementiert, Hardwarevalidierung offen)

- explizite vollständige BDF und Display-Risk-Gates
- Stage 0/1 mit Treiber-Intent und beobachtetem Bindungszustand
- PCI-Identität, Topologie, BAR/Link, Endpoint-/Upstream-AER
- DMI, cmdline, Module, vollständiger Kernel-Sidecar und pstore-Kopien
- Safe-Runtime-Rolle ohne GPU-Treiberaktivierung
- BDF-spezifischer Initramfs-Guard als **nicht freigegebener Prototyp**
- Same-Vendor bleibt `BLOCKED: SAFE_BOOT_NOT_PROVEN`

Die AMD-/NVIDIA-Known-good-Gegenproben auf echter Hardware sind noch nicht
dokumentiert. Daher ist dies eine Implementierungs-, keine Hardwarefreigabe.

## Phase 2 — kompakter, crash-toleranter Report (implementiert)

- feste Statusmatrix und getrennte Messungen, Beobachtungen, Interpretation und Hypothesen
- Markdown-Primärreport mit erzwungenem Budget von 50–120 Zeilen ohne Raw-Dumps
- begrenzte PCI-, Kernel- und pstore-Sidecars
- atomare Markdown-/JSON-Checkpoints mit `fsync` an jeder Stage-Grenze
- fortlaufender Runtime-Spiegel unter `/run/gpu-triage`
- failover bei ausfallendem Primärmedium mit sichtbarem Persistenzverlust
- künstliche Stage- und Read-only-Fehler als hardwarefreie Regressionstests

Der `/run`-Spiegel ist flüchtig und ausdrücklich keine Garantie gegen Stromausfall
oder einen vollständigen System-Lockup. Er hält den letzten synchronisierten
Stage-Checkpoint und erlaubt die Fortsetzung, wenn nur das USB-Medium ausfällt.

## Phase 3 — bereits gebundene Treiber und Isolation (implementiert, Hardwarevalidierung offen)

- adaptiver Übergang nur bei bereits beobachtetem `amdgpu`/`nvidia`
- AMD-hwmon und vorhandene AMD-RAS-Zähler read-only
- BDF-spezifisches `nvidia-smi` mit Rückprüfung der Geräteadresse
- Kernel- und Endpoint-/Upstream-AER-Deltas über das Driver-bound-/Lastfenster
- exakte Vulkan-Zuordnung über vollständige PCI-BDF plus IDs oder DRM-Major/Minor
- Legacy-`memtest_vulkan` nur bei genau einem Hardwaregerät in derselben ICD-Sicht
- keine automatische Treiberprobe, kein Modul-Laden und keine Bindungsänderung

Die Pfade sind mit AMD-/NVIDIA-, Domain-/Function-, DRM-, Mehrgeräte-, Xid- und
AER-Fixtures hardwarefrei getestet. Die im Plan verlangten Known-good-
Gegenproben auf echter AMD- und NVIDIA-Hardware sind noch nicht dokumentiert;
dies ist daher weiterhin keine allgemeine Hardwarefreigabe.

## Phase 4+ — siehe PLAN-SAFE-TRIAGE.md

ROM-Opt-in und der eigene VRAM-/Compute-Helper folgen in den dort definierten
Phasen. Die älteren Versionsziele unten bleiben historische Produktideen, sind
aber nicht der aktuelle Sicherheitsplan.

## v0.1 — Repo-first Offline MVP

- ein physischer USB-Stick
- offizielles Arch-ISO via Ventoy
- Repo separat vom ISO aktualisierbar
- reproduzierbares Offline-Paketbundle passend zum ISO-Datum
- Kernel-/Bundle-Kompatibilitätscheck vor Installation
- PCI Enumeration / Target Selection
- Driver + DRM State
- PCIe Link/BAR Evidence
- Endpoint + Upstream AER Snapshot/Delta
- AMD hwmon Telemetry
- NVIDIA `nvidia-smi` Telemetry
- Kernel Error Evidence
- bounded Vulkan VRAM test via `memtest_vulkan`
- JSON/Text/Log Reporting direkt auf den USB

## v0.2 — essentielle Isolation

- eigener kleiner nativer Vulkan-Helper
- Host → VRAM → Host Correctness
- PCIe Transfer Bandwidth
- GPU-local Compute Known-Answer-Test
- Device-Lost Watchdog
- unabhängige Ergebnis-Matrix: PCIe / VRAM / Compute

## v0.3 — reparaturorientierte VRAM-Evidenz

- strukturierte fehlerhafte Offsets und Datenwörter
- XOR / Bit-Lane / 0→1 / 1→0 Statistik
- Wiederholbarkeit pro Fehleradresse
- Stride-/Cluster-Inferenz
- VRAM Channel/Lane Hypothese

## v0.4 — physische Package-Zuordnung

- Board Identity: PCI IDs + Subsystem IDs + VBIOS Hash
- Board-Layout-Datenbank
- Channel → Package Mapping pro Board
- Confidence-bewertete Chip-Empfehlung

## später, nur wenn durch Praxisfälle gerechtfertigt

- Offscreen Graphics Known-Answer-Test
- Texture/Sampler/ROP-Isolation
- Video Engines
- langer Thermal-/Power-Soak
- Vendor-spezifische RAS-Details
- fertiges eigenes gpu-triage-Live-ISO als Release-Artefakt
- Windows Backend
- Intel dGPU Support
