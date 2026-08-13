# gpu-triage Roadmap

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
