# Umsetzungsplan: Stick-Vorbereitung radikal vereinfachen

Ziel dieses Plans ist, die Einrichtung von gpu-triage von einem mehrstufigen
Setup mit eigener Arch-Umgebung auf **ein einziges Kommando auf dem Windows-PC**
zu reduzieren — ohne die Eigenschaft aufzugeben, die den Ansatz trägt:
ISO, Kernel und Offline-Bundle bleiben hart aneinander gebunden.

## 1. Messlatte

| | heute | nach Umsetzung |
| --- | --- | --- |
| Schritte auf dem Vorbereitungs-PC | 7 | 1 |
| Zusätzliche Linux-Umgebung nötig | ja (Arch-VM oder WSL2) | nein |
| `pacstrap` / chroot nötig | ja | nein — beliebiges Container-Image genügt |
| ISO manuell suchen, benennen, prüfen | ja | nein |
| Bundle-Größe auf dem Stick | voller Closure: 659 MB | nur was das ISO **nicht** hat: 372 MB |
| Kommandos auf dem Diagnose-PC | 3 | 1 |
| „Passt ISO zu Bundle?" merkt man | beim Booten (Exit 3) | schon beim Vorbereiten |

Heutiger Ablauf, aus [README.md](README.md) und [TESTING-WINDOWS-VM.md](TESTING-WINDOWS-VM.md):

1. Ventoy installieren
2. Arch-ISO herunterladen, Dateiname exakt lassen
3. Repo klonen
4. **internetfähiges Arch-System aufsetzen** (VM oder WSL2) ← größte Hürde
5. ISO in diese Umgebung bringen
6. `sudo bash offline/build_bundle.sh <iso>` (root, `pacstrap`, GB-Download)
7. `.\tools\sync-to-usb.ps1 -Drive E: -IsoPath ...`

Zielablauf:

```powershell
.\tools\prepare-usb.ps1
```

## 2. Die eigentlichen Reibungspunkte

| # | Reibung | Ursache im Code |
| --- | --- | --- |
| R1 | Arch-Umgebung nötig | `build_bundle.sh` nutzt `pacstrap` → braucht Arch + root |
| R2 | Bundle muss lokal gebaut werden | es gibt kein veröffentlichtes Artefakt |
| R3 | Bundle unnötig groß | `pacstrap … base linux "${TARGETS[@]}"` lädt alles, was das ISO schon mitbringt |
| R4 | ISO manuell besorgen | `-IsoPath` ist Handarbeit, Dateiname ist semantisch relevant |
| R5 | Laufwerksbuchstabe raten | `-Drive` ist Pflichtparameter |
| R6 | Ventoy separat installieren | gar nicht im Toolchain abgebildet |
| R7 | 3 Kommandos beim Booten | Datenpartition wird nicht automatisch gemountet |

R1–R3 hängen zusammen und werden in Phase 1+2 gemeinsam aufgelöst. Das ist der
Hebel mit der größten Wirkung — alles andere ist Komfort obendrauf.

## 3. Zielarchitektur

```text
GitHub Actions (monatlich / bei neuem Arch-ISO)
   │  lädt offizielles ISO, liest arch/pkglist.x86_64.txt
   │  löst Closure gegen Archive-Snapshot auf (ohne root, ohne pacstrap)
   │  zieht ab, was das ISO schon enthält
   ↓
Release-Asset: gpu-triage-bundle-<isodate>.zip  (+ manifest.env, SHA256SUMS)
   │
   ↓                              offline/release.json  ← eine Datei pinnt alles
prepare-usb.ps1 (Windows, kein Linux)
   │  Stick finden → ISO laden+prüfen → Bundle laden+prüfen → Repo spiegeln
   ↓
Ventoy-Stick
   ↓
Diagnose-PC:  bash /run/media/…/gpu-triage/go.sh      (ein Kommando)
```

Der Kern: **`offline/release.json` wird die einzige Wahrheit** über das Paar aus
ISO und Bundle.

```json
{
  "iso_date": "2026.08.01",
  "iso_name": "archlinux-2026.08.01-x86_64.iso",
  "iso_sha256": "…",
  "iso_urls": ["https://geo.mirror.pkgbuild.com/iso/2026.08.01/archlinux-2026.08.01-x86_64.iso"],
  "expected_kernel": "6.16.1-arch1-1",
  "bundle_url": "https://github.com/<owner>/gpu-triage/releases/download/bundle-2026.08.01/gpu-triage-bundle-2026.08.01.zip",
  "bundle_sha256": "…"
}
```

Ein ISO-Update ist damit **ein Commit**, der diese Datei ersetzt — erzeugt von CI,
nicht von Hand.

---

## Phase 1 — `build_bundle.sh` ohne `pacstrap`, ohne Ballast ✅ umgesetzt

**Warum zuerst:** Solange der Bundle-Bau `pacstrap` braucht, ist er an eine echte
Arch-Installation gebunden. Damit ist weder CI noch Container noch ein schlankes
WSL sinnvoll nutzbar. Diese Phase ist die Voraussetzung für alles Weitere.

> **Stand nach Umsetzung.** Gemessen am ISO `archlinux-2026.08.01`: Closure 176
> Pakete / 659 MB → ausgeliefert 25 Pakete / **372 MB**. Der Bau läuft in einem
> unveränderten `archlinux`-Container ohne `--privileged`.
>
> Drei Annahmen dieses Plans haben der Umsetzung nicht standgehalten; die
> Abschnitte unten sind entsprechend korrigiert:
> 1. **„ohne root" war nicht erreichbar** (siehe 1.1).
> 2. **Die Größenschätzung war zu hoch** — das alte Bundle war 659 MB, nicht ~2 GB.
> 3. **Das pkglist-Format** ist `name version`, nicht `repo/name version`.
>
> Dazu kam ein im Plan nicht vorhergesehener Blocker: Paketdateien mit Epoch
> tragen einen Doppelpunkt im Namen (siehe 1.5).

### 1.1 `pacstrap` → `pacman -Sw` (Download-only)

```bash
pacman --config "$PACCONF" --dbpath "$TMP/db" --root "$TMP/root" \
       --cachedir "$DL_CACHE" --logfile "$TMP/pacman.log" -Sw --noconfirm "${ALL_TARGETS[@]}"
```

- `-w` lädt herunter und installiert nichts → **kein chroot, keine Mount-Rechte,
  kein `--privileged`**. Ein gewöhnliches `archlinux`-Image genügt.
- Ein **leeres** `--dbpath` ist der entscheidende Punkt: pacman sieht eine leere
  lokale Datenbank und löst deshalb die *vollständige* Closure auf, statt zu
  überspringen, was auf dem Build-Host zufällig installiert ist. Genau dafür
  brauchte die alte Fassung den leeren pacstrap-Root.
- `--logfile` und `--cachedir` halten den Bau aus `/var` heraus.

**Korrektur gegenüber dem Planentwurf: root bleibt nötig.** pacman prüft bei
`-Sy`/`-Sw` schlicht `getuid() == 0`, unabhängig davon, wohin seine Pfade zeigen.
Die Auswege wurden geprüft und verworfen:

| Ausweg | Ergebnis |
| --- | --- |
| `unshare -r` (User-Namespace) | von Dockers Default-Seccomp für Nicht-Root blockiert |
| `fakeroot` | steckt in `base-devel`, dessen Installation selbst root braucht |
| Downloads selbst per curl | verlangt, pacmans Signaturprüfung nachzubauen: das Archiv liefert **kein** `core.db.sig`, der Vertrauensanker ist also die Signatur *jedes einzelnen Pakets* |

Eine nachgebaute Signaturprüfung wäre der Preis dafür gewesen, auf dem Rechner
des Maintainers ein `sudo` zu sparen — ein schlechter Tausch. pacman behält die
Verantwortung, das Skript eskaliert per `sudo` und gibt die erzeugten Dateien
danach an den aufrufenden Benutzer zurück (`GPU_TRIAGE_OWNER`, sonst `SUDO_UID`).
Für Phase 2 und 3 ist das folgenlos: Container laufen ohnehin als root, und der
Windows-Nutzer führt dieses Skript nach Phase 3 gar nicht mehr aus.

### 1.2 `EXPECTED_KERNEL` ohne Bundle-Root ermitteln

Früher wurde die Kernelversion aus dem pacstrap-Root gelesen — ohne `pacstrap`
gibt es das Verzeichnis nicht mehr. Ersatz: direkt aus dem heruntergeladenen
Kernelpaket.

```bash
mapfile -t KERNEL_DIRS < <(bsdtar -tf "$DL_CACHE/$LINUX_FILE" \
  | sed -n 's|^usr/lib/modules/\([^/]*\)/.*|\1|p' | sort -u)
```

Das ist exakt der String, den `uname -r` im Live-System liefert (`7.1.5-arch1-2`
gegenüber der Paketversion `7.1.5.arch1-2`) — keine Versionsstring-Heuristik
nötig. Das Kernelpaket wird dafür in den Cache geladen und landet nicht im
ausgelieferten Bundle, weil das ISO es ohnehin mitbringt.

`sort -u` statt `head -n1`: `head` schließt die Pipe früh, was `bsdtar` ein
SIGPIPE einträgt und unter `set -o pipefail` den ganzen Lauf mit Exit 141
abbricht — genau so ist die erste Fassung gescheitert. Nebenbei erzwingt `sort -u`
die Prüfung, dass das Kernelpaket wirklich nur einen Modulbaum enthält.

### 1.3 Nur ausliefern, was das ISO nicht schon hat

**Verifiziert.** Das offizielle Arch-ISO enthält `arch/pkglist.x86_64.txt`, und
`bsdtar` extrahiert die Datei direkt aus dem ISO9660-Image über ihren
Rock-Ridge-Namen (im reinen ISO9660-Namensraum heißt sie `PKGLIST_X86_64.TXT;1`):

```bash
bsdtar -xOf "$ISO_PATH" arch/pkglist.x86_64.txt > "$TMP/isopkgs.txt"
```

Das Format ist `name version` — **ohne** `repo/`-Präfix, anders als im
Planentwurf angenommen. Für `archlinux-2026.08.01` sind das 456 Einträge.

Regeln für die Subtraktion:

- Abgezogen wird **nur bei exakter Übereinstimmung von Name *und* Version**.
- Weicht eine Version ab, bleibt das Paket im Bundle (Sicherheit vor Größe).
- `offline/excluded.txt` hält fest, was warum fehlt.

**Korrektur: kein harter Abbruch bei Versionsversatz.** Der Planentwurf wollte bei
einer Häufung abbrechen. Das ist falsch herum gedacht: Ein behaltenes Paket ist
per Konstruktion unschädlich — schlimmstenfalls wird das Bundle größer. Gefährlich
wäre nur das Gegenteil, und das kann nicht eintreten, weil die Liste vom ISO
selbst stammt. Umgesetzt ist deshalb eine Warnung mit Anzahl. **Hart abgebrochen
wird nur bei abweichender `linux`-Version**, denn dort ist der Versatz tatsächlich
fatal: `nvidia-open` liefert Module für genau einen Kernel. Für das ISO vom
2026.08.01 liegt der Versatz bei 0 von 151 — ISO-Datum und Archive-Snapshot decken
sich exakt.

Als Rückfallebene bleibt `--no-iso-subtract` (liefert die vollen 176 Pakete /
659 MB, also das alte Verhalten).

**Folge für das Bundle:** Es ist jetzt nur noch auf genau diesem ISO
installierbar. Das deckt sich mit der bestehenden Kernel-Schranke, führt aber zu
einem neuen Fehlerbild, wenn jemand es anderswo einspielt — `pacman -U` bricht
dann mit einer unerklärten Wand aus `unable to satisfy dependency` ab. `bootstrap.sh`
fängt das jetzt ab und benennt die Ursache samt Auswegen.

### 1.4 Bundle als Artefakt verpacken

Neuer Schritt am Ende des Skripts: `offline/dist/gpu-triage-bundle-<isodate>.zip`
mit `packages/`, `manifest.env`, `SHA256SUMS`, `excluded.txt`.

**ZIP, nicht `tar.zst`** — bewusst: `Expand-Archive` ist in jedem Windows-PowerShell
vorhanden, ein zstd-Tar nicht. Die Pakete sind bereits komprimiert, daher
`-0` / Store-Modus; die ZIP-Hülle kostet nichts.

### 1.5 Epoch-Doppelpunkt in Dateinamen (im Plan nicht vorhergesehen)

Pakete mit Epoch heißen im Dateisystem `mesa-1:26.1.6-1-x86_64.pkg.tar.zst`. Der
Doppelpunkt ist auf **exFAT und NTFS ein illegales Zeichen** — die Ventoy-
Datenpartition ist exFAT. Solche Dateien lassen sich weder per robocopy auf den
Stick kopieren noch per `Expand-Archive` auspacken. Im Bundle vom 2026.08.01
betrifft das 6 der 25 Pakete, darunter `mesa` und `vulkan-radeon`.

Das ist kein neuer Fehler — die alte Fassung erzeugte dieselben Namen —, aber es
hätte das Artefakt aus 1.4 auf Windows unbrauchbar gemacht. Umgesetzt: Beim
Kopieren wird `:` durch `_` ersetzt, die Signaturdatei wandert mit, und eine
Kollisionsprüfung sichert die Abbildung ab. Nachgewiesen, dass das zulässig ist:
`pacman -Qp` und `pacman -U` lesen Name und Version aus den Paketmetadaten, nicht
aus dem Dateinamen; eine umbenannte Datei installiert sich als `mesa 1:26.1.6-1`.

`SHA256SUMS` führt die bereinigten Namen, und `bootstrap.sh` leitet seine
Paketpfade genau daraus ab — dort war deshalb keine Änderung nötig.

### 1.6 Was sonst noch mitgezogen werden musste

- `tools/sync-to-usb.ps1` schließt jetzt `.dlcache` und `dist` aus. Ohne das
  landeten 659 MB Cache plus eine 372-MB-ZIP-Zweitkopie mit auf dem Stick — der
  Größengewinn aus 1.3 wäre mehr als aufgezehrt worden.
- `.gitignore` deckt die neuen erzeugten Pfade ab, `offline/.bundle-root/` entfällt.
- Der alte `pacman -Sy … archlinux-keyring`-Aufruf auf dem Build-Host ist ersatzlos
  entfallen — er widerspräche der Zusage, das Build-System nicht anzufassen. Ein
  veralteter Keyring wird stattdessen beim Fehlschlag benannt.

### 1.7 Akzeptanzkriterien Phase 1 — Ergebnis

| Kriterium | Ergebnis |
| --- | --- |
| Lauf im unveränderten `archlinux`-Container ohne `--privileged` | ✅ (root im Container, siehe 1.1) |
| `EXPECTED_KERNEL` korrekt | ✅ `7.1.5-arch1-2`, gegengeprüft gegen den Modulbaum in `nvidia-open` |
| `sha256sum -c SHA256SUMS` | ✅ 25/25 |
| Bundle messbar kleiner | ✅ 659 MB → 372 MB (−43 %) |
| Keine Windows-verbotenen Zeichen in Dateinamen | ✅ 0 |
| ZIP-Artefakt intakt, vollständig, mit `.sha256` | ✅ |
| `--no-iso-subtract` liefert das alte Verhalten | ✅ 176 Pakete / 659 MB |
| Reduziertes Bundle installiert mit voller Abhängigkeitsprüfung gegen ein Root, das nur die ISO-Pakete hält | ✅ |
| `bootstrap.sh` end-to-end gegen das neue Bundle | ✅ inkl. Stamp und Skip beim zweiten Lauf |
| Bestehende Regressionstests | ✅ 39 unittests + Stamp-Tests |

Offen und bewusst Phase 5 zugeordnet: automatisierte Regressionstests für die
Subtraktions- und Umbenennungslogik. Bisher ist sie durch die Läufe oben belegt,
nicht durch eine Testdatei.

**Aufwand tatsächlich:** ~0,5 Tag.

---

## Phase 2 — Bundle in CI bauen und veröffentlichen

**Warum:** Erst hiermit verschwindet die Linux-Anforderung auf dem Vorbereitungs-PC
vollständig. Phase 1 macht es möglich, Phase 2 macht es nutzbar.

### 2.1 Workflow `.github/workflows/bundle.yml`

- Trigger: `workflow_dispatch` + `schedule` (monatlich, kurz nach dem üblichen
  Arch-ISO-Release am Monatsersten).
- Läuft im Container `archlinux:latest` auf `ubuntu-latest`.
- Schritte: ISO-Datum ermitteln → ISO laden → **offizielle `sha256sums.txt` prüfen**
  → `build_bundle.sh` → ZIP → Release `bundle-<isodate>` anlegen → `release.json`
  erzeugen und als Commit/PR ins Repo zurückschreiben.

### 2.2 ISO-Datum bestimmen

`https://archlinux.org/releng/releases/json/` liefert die Release-Liste inkl.
Datum und SHA256. Kein Scraping, kein Raten des Dateinamens.

### 2.3 `release.json` als einzige Kopplung

Erzeugt von CI, im Repo eingecheckt, gelesen von `prepare-usb.ps1`. Wer bewusst
bei einem älteren ISO bleiben will, checkt schlicht den passenden Commit aus —
die Kopplung ISO↔Bundle↔Kernel ist damit versioniert statt mündlich.

### 2.4 Akzeptanzkriterien Phase 2

- Manuell ausgelöster Workflow erzeugt Release + Assets + `release.json`-PR.
- `bundle_sha256` aus `release.json` stimmt mit dem Asset überein.
- Ein aus dem Release gezogenes Bundle bootstrappt in der VM genauso wie ein lokal gebautes.

**Aufwand:** ~0,5 Tag. Risiko niedrig.
**Offene Frage:** Asset-Größe. GitHub erlaubt 2 GB pro Datei — nach Phase 1.3
unkritisch, **ohne** Phase 1.3 grenzwertig. Deshalb ist 1.3 keine Kür.

---

## Phase 3 — `prepare-usb.ps1`: ein Kommando auf Windows

Neues Skript neben dem bestehenden [tools/sync-to-usb.ps1](tools/sync-to-usb.ps1);
`sync-to-usb.ps1` bleibt als reiner Repo-Sync erhalten und wird von
`prepare-usb.ps1` intern aufgerufen (kein Duplikat der robocopy-Logik).

### 3.1 Ablauf

| Schritt | Verhalten |
| --- | --- |
| 1. Stick finden | `Get-Volume -FileSystemLabel Ventoy`. Genau einer → nehmen. Mehrere → Auswahlliste mit Modell/Größe. Keiner → Hinweis auf `-InstallVentoy`. `-Drive` bleibt als Override. |
| 2. Platz prüfen | ISO + Bundle + Repo gegen freien Speicher, **vor** dem ersten Byte Download. |
| 3. `release.json` lesen | ISO-Name, Hashes, Bundle-URL. |
| 4. ISO besorgen | Liegt es schon auf dem Stick und stimmt der SHA256 → überspringen. Sonst nach `%LOCALAPPDATA%\gpu-triage\cache` laden, prüfen, auf den Stick kopieren. |
| 5. Bundle besorgen | Analog, per Hash gecached. Entpacken nach `gpu-triage/offline/`. |
| 6. Repo spiegeln | `sync-to-usb.ps1` (robocopy `/MIR`, `.git`/`reports` ausgeschlossen). |
| 7. Verifizieren | `manifest.env` vorhanden, `SHA256SUMS` deckt alle Paketdateien, ISO-Datum == `manifest.env`-Datum. |
| 8. `BOOT.txt` schreiben | Stick-Root: das eine Boot-Kommando im Klartext. |

Alle Downloads sind **hash-verifiziert und idempotent** — ein zweiter Lauf lädt
nichts erneut und ist damit auch der schnelle Weg, nur den Code zu aktualisieren.

### 3.2 `-Check` (Doctor-Modus)

Prüft ohne zu schreiben: Stick gefunden, ISO vorhanden und Hash korrekt, Bundle
vollständig, `manifest.env` passend zum ISO, `reports/` beschreibbar. Gibt eine
Ampel aus. Das verschiebt den heutigen Exit-Code 3
([bootstrap.sh:65](scripts/bootstrap.sh#L65)) vom Diagnose-PC an den Schreibtisch —
dorthin, wo man ihn beheben kann.

### 3.3 `-InstallVentoy` (opt-in, destruktiv)

- **Nicht** Teil des Standardlaufs. Wer keinen Ventoy-Stick hat, muss den Schalter
  bewusst setzen.
- Zeigt vor dem Schreiben Modell, Seriennummer und Größe des Ziellaufwerks und
  verlangt eine getippte Bestätigung — kein `[J/N]`.
- Lädt Ventoy aus dem offiziellen GitHub-Release, prüft den Hash gegen einen im
  Repo gepinnten Wert, ruft `Ventoy2Disk.exe` im CLI-Modus.
- Weigert sich bei Laufwerken über einer Größenschwelle oder ohne Wechselmedien-Flag.

**Aufwand:** ~1–1,5 Tage (3.1/3.2 ~1 Tag, 3.3 ~0,5 Tag).
Risiko: 3.1/3.2 niedrig, **3.3 hoch** — deshalb opt-in und zuletzt.

---

## Phase 4 — Ein Kommando auf dem Diagnose-PC

Statt drei Zeilen:

```bash
mkdir -p /mnt/ventoy
mount /dev/disk/by-label/Ventoy /mnt/ventoy
bash /mnt/ventoy/gpu-triage/start.sh
```

nur noch:

```bash
bash /mnt/ventoy/gpu-triage/go.sh    # bzw. das, was in BOOT.txt steht
```

Der ehrliche Teil: **das erste Mounten kann man nicht wegoptimieren**, solange das
Skript auf der Datenpartition liegt. Realistisch sind drei Stufen, aufsteigend im
Aufwand:

1. **Sofort umsetzbar:** eine kopierbare Einzelzeile in `BOOT.txt` auf dem
   Stick-Root und in der README:
   `m=/mnt/v; mkdir -p $m; mount /dev/disk/by-label/Ventoy $m; bash $m/gpu-triage/start.sh`
2. **Klein:** `go.sh` neben `start.sh`, das die Ventoy-Partition selbst sucht
   (auch wenn sie schon woanders gemountet ist), remountet falls nötig und
   `start.sh` aufruft. Reduziert die Tipparbeit nach dem einmaligen Mount auf null.
3. **Zu prüfen, optional:** Ventoy-`ventoy_grub.cfg` mit einem eigenen Menüeintrag,
   der dem Arch-Kernel eine Boot-Option mitgibt, die das Skript automatisch startet.
   Das hängt davon ab, ob das aktuelle archiso einen `script=`-Parameter unterstützt
   — **erst verifizieren, dann versprechen**. Fällt die Prüfung negativ aus, bleibt
   es bei Stufe 2, und der Plan verliert nichts Wesentliches.

**Aufwand:** Stufe 1+2 ~2 Stunden. Stufe 3 zuerst eine Zeitbox von 2 Stunden zur Verifikation.

---

## Phase 5 — Dokumentation und Tests nachziehen

### 5.1 README umbauen

- Neuer Abschnitt **„Schnellstart"** ganz oben: Stick einstecken → `prepare-usb.ps1`
  → booten → `go.sh`. Vier Zeilen.
- Der heutige Abschnitt „Einrichtung" wird zu **„Manueller Weg / Bundle selbst
  bauen"** und rutscht nach hinten. Er bleibt vollständig — wer offline oder ohne
  GitHub arbeitet, braucht ihn weiterhin.
- Projektstruktur um `tools/prepare-usb.ps1`, `offline/release.json`, `go.sh` ergänzen.

### 5.2 Tests

Zu den bestehenden Tests in [tests/](tests/) kommen dazu:

- `test_bundle_subtract.sh` — synthetische `pkglist.x86_64.txt` + synthetische
  Paketliste; prüft, dass exakte Versionstreffer abgezogen und Abweichungen
  behalten **und gemeldet** werden. Das ist die risikoreichste neue Logik.
- `test_release_json.sh` — Schema und Pflichtfelder.
- Pester- oder zumindest `-WhatIf`-Trockenlauf für `prepare-usb.ps1` gegen ein
  Fake-Verzeichnis statt eines echten Sticks.

### 5.3 `TESTING-WINDOWS-VM.md`

Neuer „Weg 0": Bundle aus dem Release ziehen statt bauen — für alle, die nur die
App-Logik testen wollen, entfällt WSL2 damit ganz.

**Aufwand:** ~0,5 Tag.

---

## Reihenfolge, Abhängigkeiten, Aufwand

| Phase | Ergebnis | Abhängig von | Aufwand | Risiko |
| --- | --- | --- | --- | --- |
| ~~1~~ | ✅ Bundle-Bau klein und containerfähig | — | 0,5 d (erledigt) | — |
| 2 | Bundle als Release-Artefakt + `release.json` | 1 | 0,5 d | niedrig |
| 3.1/3.2 | `prepare-usb.ps1` + `-Check` | 2 | 1 d | niedrig |
| 4 (1+2) | ein Kommando beim Booten | — | 2 h | niedrig |
| 5 | Doku + Tests | 1–4 | 0,5 d | niedrig |
| 3.3 | `-InstallVentoy` | 3.1 | 0,5 d | **hoch** |
| 4 (3) | Auto-Start via Boot-Parameter | 4.2 | Zeitbox 2 h | offen |

**Empfehlung:** Phase 1 → 2 → 3.1/3.2 → 4(1+2) → 5 liefert bereits die volle
Vereinfachung (7 Schritte → 1, keine Linux-Umgebung mehr). Phase 3.3 und 4(3) sind
Komfort mit ungünstigem Risiko-Nutzen-Verhältnis und gehören ans Ende — oder gar
nicht.

Phase 4(1+2) hängt an nichts und lässt sich sofort vorziehen, wenn ein schneller
sichtbarer Gewinn gewünscht ist.

## Risiken und offene Verifikationen

| Risiko | Auswirkung | Umgang |
| --- | --- | --- |
| ~~`arch/pkglist.x86_64.txt` liegt nicht wie erwartet im ISO~~ | — | ✅ erledigt: am ISO 2026.08.01 verifiziert, `--no-iso-subtract` bleibt als Rückfallebene |
| ~~Versionsversatz zwischen ISO und Archive-Snapshot~~ | — | ✅ erledigt: abweichende Pakete werden behalten, nicht abgezogen; gemessener Versatz 0/151. Hart abgebrochen wird nur bei `linux` |
| ~~`pacman -Sw` liefert eine andere Closure als `pacstrap`~~ | — | ✅ erledigt: leeres `--dbpath` erzwingt dieselbe vollständige Auflösung; das reduzierte Bundle installiert nachweislich mit voller Abhängigkeitsprüfung |
| Release-Asset zu groß / Bandbreite | Vorbereitung dauert | entschärft: 372 MB, weit unter GitHubs 2-GB-Grenze; lokaler Hash-Cache verhindert Mehrfachdownloads |
| Bundle ist an genau ein ISO gebunden | Einspielen anderswo scheitert | bewusst so; `bootstrap.sh` benennt die Ursache, `--no-iso-subtract` erzeugt ein autarkes Bundle |
| `-InstallVentoy` erwischt die falsche Platte | **Datenverlust** | opt-in, getippte Bestätigung, Größen-/Wechselmedien-Prüfung, Modell+Seriennummer anzeigen |
| GitHub als Abhängigkeit im Standardweg | ohne Netz keine Vorbereitung | manueller Weg bleibt vollständig dokumentiert und lauffähig |

## Was bewusst unangetastet bleibt

- **Die Kernel-/Bundle-Kopplung.** `manifest.env` vs. `uname -r` und Exit-Code 3
  ([bootstrap.sh:55-66](scripts/bootstrap.sh#L55-L66)) bleiben genau so. Der Plan
  macht den Fehler nur früher sichtbar, er entschärft ihn nicht.
- **Ventoy statt Rufus/Balena** und die Trennung Repo ↔ Live-ISO — beide
  Designentscheidungen bleiben gültig und werden durch den Plan gestützt.
- **`start.sh` als einziger Einstiegspunkt.** `go.sh` ist ein Wrapper davor, kein
  zweiter Pfad.
- **Der manuelle Bau-Weg.** Er wird nach hinten sortiert, nicht entfernt.
- **Die Diagnoselogik** in [app/gpu_diag.py](app/gpu_diag.py). Dieser Plan fasst
  ausschließlich Vorbereitung und Installation an.
