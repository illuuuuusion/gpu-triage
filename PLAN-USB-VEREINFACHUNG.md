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
  "schema": 1,
  "generated": "2026-08-16T19:31:50Z",
  "iso_date": "2026.08.01",
  "iso_name": "archlinux-2026.08.01-x86_64.iso",
  "iso_sha256": "…",
  "iso_size": 1597014016,
  "iso_urls": [
    "https://geo.mirror.pkgbuild.com/iso/2026.08.01/archlinux-2026.08.01-x86_64.iso",
    "https://archive.archlinux.org/iso/2026.08.01/archlinux-2026.08.01-x86_64.iso"
  ],
  "expected_kernel": "7.1.5-arch1-2",
  "release_tag": "bundle-2026.08.01",
  "bundle_name": "gpu-triage-bundle-2026.08.01.zip",
  "bundle_url": "https://github.com/<owner>/gpu-triage/releases/download/bundle-2026.08.01/gpu-triage-bundle-2026.08.01.zip",
  "bundle_sha256": "…",
  "bundle_size": 389537369,
  "bundle_packages": 25
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

## Phase 2 — Bundle in CI bauen und veröffentlichen ✅ umgesetzt

**Warum:** Erst hiermit verschwindet die Linux-Anforderung auf dem Vorbereitungs-PC
vollständig. Phase 1 macht es möglich, Phase 2 macht es nutzbar.

> **Stand nach Umsetzung.** Neu sind [.github/workflows/bundle.yml](.github/workflows/bundle.yml)
> und [offline/release_meta.py](offline/release_meta.py). Der Bau im Container ist
> gegen das echte ISO `archlinux-2026.08.01` nachgestellt worden (176 → 25 Pakete,
> 372 MB), ebenso jeder Shell-Schritt des Workflows.
>
> Zwei Abweichungen vom Planentwurf, beide unten erklärt:
> 1. **Der Job läuft nicht *im* `archlinux`-Container**, sondern startet ihn (2.1).
> 2. **`iso_urls` enthält zusätzlich `archive.archlinux.org`** — ohne diesen
>    Eintrag verfällt eine `release.json` nach einem Monat (2.3).
>
> Zwei Fehler sind erst beim Nachstellen aufgefallen und behoben (2.5).

### 2.1 Workflow `.github/workflows/bundle.yml`

- Trigger: `workflow_dispatch` (mit `iso_version` und `force`) + `schedule`
  (`0 6 2 * *`, also einen Tag nach dem üblichen Arch-ISO-Release).
- Zwei Jobs: `resolve` bestimmt das ISO und prüft, ob es das Release schon gibt;
  `build` läuft nur, wenn es etwas zu tun gibt. Ein Monatslauf ohne neues ISO
  kostet damit Sekunden statt eines 1,6-GB-Downloads.

**Korrektur gegenüber dem Planentwurf: der Job läuft nicht *im* Container.**
`container: archlinux:latest` hätte auch `actions/checkout`, `git`, `gh` und
`python3` in die Arch-Umgebung verlegt — dort fehlt jedes davon außer `python3`
nicht standardmäßig, und `gh` gar nicht erst. Stattdessen läuft der Job auf
`ubuntu-latest` und startet für genau einen Schritt

```bash
docker run --rm -v "$PWD":/repo -v "$RUNNER_TEMP/iso":/iso:ro -w /repo \
  -e GPU_TRIAGE_OWNER="$(id -u):$(id -g)" archlinux:latest \
  bash offline/build_bundle.sh "/iso/$ISO_NAME"
```

Das ist wörtlich das Kommando, das die README schon für den manuellen Weg nennt —
CI und Handarbeit laufen nachweislich durch denselben Pfad. `GPU_TRIAGE_OWNER`
gibt die im Container als root erzeugten Dateien an den Runner-Benutzer zurück,
sonst könnte der Commit-Schritt sie nicht anfassen.

Schrittfolge: ISO auflösen → Release-Gate → ISO laden und prüfen → Container-Bau
→ Artefakt prüfen → Release `bundle-<isodate>` → `release.json` → PR.

### 2.2 ISO-Datum bestimmen

`offline/release_meta.py iso [version|latest]` liest
`https://archlinux.org/releng/releases/json/`. Kein Scraping, kein Raten des
Dateinamens.

**Die `sha256sums.txt`-Prüfung sitzt eine Stufe früher als geplant.** Der Entwurf
wollte den Download dagegen prüfen; das allein bliebe aber eine Selbstbestätigung,
wenn Hash und Datei aus derselben Quelle stammen. Umgesetzt ist deshalb: Die
Release-Liste (eine Django-View) und die neben dem ISO liegende
`sha256sums.txt` (mit den Images erzeugt) müssen sich über denselben Hash **einig
sein** — sonst bricht der Lauf ab, bevor ein Byte geladen wird. Erst der so
bestätigte Wert prüft dann den Download. Zwei unabhängige Quellen statt einer.

`release_meta.py` benutzt ausschließlich die Standardbibliothek: das Skript läuft
auf dem Runner, nicht im Arch-Container, also ist `python3` sicher vorhanden und
`jq` als Abhängigkeit nicht nötig.

### 2.3 `release.json` als einzige Kopplung

Erzeugt von CI, im Repo eingecheckt, gelesen von `prepare-usb.ps1`. Wer bewusst
bei einem älteren ISO bleiben will, checkt schlicht den passenden Commit aus —
die Kopplung ISO↔Bundle↔Kernel ist damit versioniert statt mündlich.

Das Schema steht in Abschnitt 3. Drei Felder mehr als im Entwurf, jedes mit
einem Abnehmer:

- **`iso_size` / `bundle_size`** — Phase 3.1 Schritt 2 will den Platz prüfen
  *„vor dem ersten Byte Download"*. Ohne die Größen im `release.json` ginge das nicht.
- **`bundle_packages`** — erlaubt dem Doctor-Modus (3.2) die Aussage „14 von 25
  Paketen auf dem Stick" statt bloß „irgendwas fehlt".
- **`schema`** — damit ein künftiges `prepare-usb.ps1` ein zu neues `release.json`
  benennen kann, statt an einem fehlenden Feld zu scheitern.

**`iso_urls` führt zusätzlich `archive.archlinux.org`.** Der Entwurf listete nur
einen Mirror. Mirrors halten aber nur das *aktuelle* ISO vor: Sobald der nächste
Monatsrelease erscheint, ist die URL im eingecheckten `release.json` tot — und
genau dann, einen Monat später, wird die Datei benutzt. Das Archiv hält jedes ISO
dauerhaft. Der Mirror steht deshalb vorn (schnell), das Archiv dahinter
(haltbar), und `release_meta.py check` verweigert ein `release.json` ohne diese
Rückfallebene.

`release_meta.py` hat drei Unterkommandos, alle auch von Hand benutzbar:
`iso` (Metadaten auflösen), `write` (`release.json` schreiben) und `check`
(bestehendes `release.json` validieren, auf Wunsch gegen die Bundle-Datei).
`write` bricht ab, wenn `manifest.env` ein anderes `ARCHISO_DATE` trägt als die
ISO-Metadaten — ein `release.json`, das ein nicht zusammengehöriges Paar
beschreibt, kann so gar nicht erst entstehen.

### 2.4 Was beim Nachstellen aufgefallen ist

Beide Fehler wären erst im Betrieb sichtbar geworden, keiner davon laut:

1. **Der PR wäre beim allerersten Lauf ausgeblieben.** Die Abbruchbedingung war
   `git diff --quiet -- offline/release.json`. `git diff` meldet für eine
   *unversionierte* Datei keine Änderung — beim ersten Lauf existiert
   `release.json` aber noch gar nicht im Repo. Das Release wäre veröffentlicht
   worden, der PR, der es pinnt, nicht. Ersetzt durch `git status --porcelain`.
2. **`pipefail` fehlte.** GitHubs Standard-Shell ist `bash -e` *ohne* `pipefail`.
   In `release_meta.py iso … | tee -a "$GITHUB_OUTPUT"` hätte ein Fehlschlag des
   Skripts vom erfolgreichen `tee` verdeckt werden können — der Build-Job wäre
   mit leeren ISO-Variablen weitergelaufen. Ein `defaults: run: shell: bash`
   erzwingt `-eo pipefail` für jeden Schritt.

### 2.5 Akzeptanzkriterien Phase 2 — Ergebnis

| Kriterium | Ergebnis |
| --- | --- |
| Workflow ist syntaktisch gültig | ✅ YAML geparst, alle 8 `run`-Blöcke gegen `bash -n` |
| ISO-Auflösung gegen die echte Release-Liste | ✅ `latest` und gepinnt (`2026.08.01`); unbekannte Version bricht mit Exit 2 ab |
| Hash aus zwei unabhängigen Quellen | ✅ releng-JSON == `sha256sums.txt` |
| ISO-Download: Mirror-Ausfall und Hash-Fehler | ✅ Fallback aufs Archiv greift; falscher Hash beendet den Schritt mit Exit 1 |
| Container-Bau wie im Workflow aufgerufen | ✅ gegen das echte ISO: 176 → 25 Pakete, 372 MB, Kernel `7.1.5-arch1-2` |
| Dateien gehören danach dem Runner-Benutzer | ✅ via `GPU_TRIAGE_OWNER` |
| Artefaktprüfung (Entpacken + `SHA256SUMS`) | ✅ 25/25, `ARCHISO_DATE` im Manifest passt |
| `bundle_sha256` aus `release.json` == Asset | ✅ gegen `sha256sum` und gegen die `.sha256` des Bau-Skripts |
| `release.json` wird bei Nichtübereinstimmung verweigert | ✅ 4 Schreib- und 8 Prüf-Fehlerfälle je Exit 2 |
| PR-Schritt: Erstlauf, Zweitlauf, offener PR | ✅ Branch angelegt / force-aktualisiert / kein Duplikat |
| Release-Gate (`force`, Release existiert) | ✅ alle drei Kombinationen |
| Commit enthält nur `release.json` | ✅ Pakete, `dist/`, Cache bleiben durch `.gitignore` draußen |
| Bestehende Regressionstests unverändert grün | ✅ 39 unittests + Stamp-Tests |

Zwei Kriterien lassen sich **nur auf GitHub** abschließen und stehen noch aus:
der tatsächlich ausgelöste Lauf (Release, Assets, PR gegen die echte API) und
der Nachweis, dass ein *aus dem Release gezogenes* Bundle in der VM bootstrappt
wie ein lokal gebautes. Nachgestellt ist davon alles, was ohne GitHub-Token und
ohne Testhardware nachstellbar ist; die API-Aufrufe liefen gegen ein `gh`-Stub.

`offline/release.json` liegt bewusst **noch nicht** im Repository: Die Datei
entsteht im ersten CI-Lauf. Ein von Hand eingecheckter Inhalt würde auf ein
Release-Asset zeigen, das es nicht gibt — genau die mündliche Kopplung, die
diese Phase abschafft. Phase 3 setzt darauf auf und braucht deshalb einen
gelaufenen Workflow.

**Aufwand tatsächlich:** ~0,5 Tag. Risiko wie erwartet niedrig.
**Zur offenen Frage Asset-Größe:** 372 MB gegen GitHubs 2-GB-Grenze — erledigt.
Ohne Phase 1.3 wären es 659 MB gewesen, also noch im Rahmen, aber ohne Reserve.

---

## Phase 3 — `prepare-usb.ps1`: ein Kommando auf Windows ✅ umgesetzt

Neues Skript neben dem bestehenden [tools/sync-to-usb.ps1](tools/sync-to-usb.ps1);
`sync-to-usb.ps1` bleibt als reiner Repo-Sync erhalten und wird von
`prepare-usb.ps1` intern aufgerufen (kein Duplikat der robocopy-Logik).

> **Stand nach Umsetzung.** Neu sind [tools/prepare-usb.ps1](tools/prepare-usb.ps1)
> und [tools/ventoy-release.json](tools/ventoy-release.json). Getestet wurde gegen
> Windows PowerShell 5.1 auf einem per `subst` erzeugten Laufwerk mit einem
> synthetischen `release.json`: Erstlauf, Zweitlauf, `-Check`, `-Force`, sieben
> Fehlerbilder und ein echter Download von GitHub.
>
> Drei Abweichungen vom Planentwurf, alle unten begründet:
> 1. **Die Schrittreihenfolge 5/6 war falsch herum** — so wie geplant hätte der
>    Repo-Spiegel das gerade entpackte Bundle wieder gelöscht (3.1).
> 2. **Entpackt wird ins Repository, nicht auf den Stick** — Folge aus 1.
> 3. **Ein Zustandsvermerk auf dem Stick** ist nötig, damit der zweite Lauf das
>    Versprechen „lädt nichts erneut" auch beim Prüfen einlöst (3.1).

### 3.1 Ablauf

| Schritt | Verhalten |
| --- | --- |
| 1. `release.json` lesen | Pflichtfelder, Schema, Hash-Format. Fehlt die Datei, nennt das Skript den CI-Workflow und den manuellen Weg. |
| 2. Stick finden | `Get-Volume -FileSystemLabel Ventoy`. Genau einer → nehmen. Mehrere → Auswahlliste mit Modell/Größe. Keiner → Hinweis auf `-InstallVentoy`. `-Drive` bleibt als Override. |
| 3. Bestand aufnehmen | Liegt das ISO schon geprüft auf dem Stick? Ist ein zum ISO passendes Bundle schon entpackt? |
| 4. Platz prüfen | Stick, Cache-Laufwerk und Repo getrennt, **vor** dem ersten Byte Download — und nur für das, was Schritt 3 wirklich als fehlend gemeldet hat. |
| 5. ISO besorgen | Liegt es schon auf dem Stick und stimmt der SHA256 → überspringen. Sonst nach `%LOCALAPPDATA%\gpu-triage\cache` laden, prüfen, auf den Stick kopieren, **die Kopie erneut prüfen**. |
| 6. Bundle besorgen | Analog, per Hash gecached. Entpackt wird nach `offline/` **im Repository** (siehe unten). |
| 7. Repo spiegeln | `sync-to-usb.ps1` (robocopy `/MIR`) — trägt das Bundle mit auf den Stick. |
| 8. `BOOT.txt` schreiben | Stick-Root: das eine Boot-Kommando im Klartext. |
| 9. Verifizieren | Derselbe Code wie `-Check`, gegen den fertigen Stick. |

Alle Downloads sind **hash-verifiziert und idempotent** — ein zweiter Lauf lädt
nichts erneut und ist damit auch der schnelle Weg, nur den Code zu aktualisieren.

**Korrektur: Bundle entpacken und Repo spiegeln standen in der falschen
Reihenfolge.** Der Entwurf wollte erst das Bundle auf den Stick entpacken (5) und
danach das Repo spiegeln (6). `sync-to-usb.ps1` spiegelt mit `robocopy /MIR`, und
`/MIR` löscht im Ziel alles, was in der Quelle fehlt — die eben entpackten
Pakete wären Sekunden später wieder verschwunden, weil `offline/packages/` im
frisch geklonten Repo leer ist. Die Reihenfolge einfach zu tauschen genügt aber
auch nicht: dann entscheidet der Lauf vor dem Spiegeln über einen Zustand, den
das Spiegeln danach zerstört. Umgesetzt ist deshalb: **das Bundle wird in
`offline/` des Repositories entpackt** — also genau dorthin, wo `build_bundle.sh`
es hinlegen würde — und der Spiegel trägt es mit auf den Stick. Damit bleibt
robocopy der einzige Schreiber von `gpu-triage/` auf dem Stick, und der manuelle
Bau-Weg und der Release-Weg münden nachweislich in denselben Zustand.

**Ergänzung: `.gpu-triage-state.json` im Stick-Root.** Schritt 5 „stimmt der
SHA256 → überspringen" heißt wörtlich genommen, bei *jedem* Lauf 1,6 GB vom Stick
zu lesen; der schnelle zweite Lauf wäre damit keiner. Der Vermerk hält fest,
welche Datei (Name, Größe, mtime) wann gegen welchen Hash geprüft wurde. Er liegt
im Stick-Root und nicht unter `gpu-triage/`, weil `/MIR` ihn dort bei jedem Lauf
löschen würde. `-Force` ignoriert ihn, `-Check` ebenfalls — ein Doktor, der einem
Vermerk glaubt, ist keiner.

### 3.2 `-Check` (Doctor-Modus)

Prüft ohne zu schreiben: Stick gefunden, ISO vorhanden und Hash korrekt, Bundle
vollständig, `manifest.env` passend zum ISO, `reports/` beschreibbar. Gibt eine
Ampel aus. Das verschiebt den heutigen Exit-Code 3
([bootstrap.sh:65](scripts/bootstrap.sh#L65)) vom Diagnose-PC an den Schreibtisch —
dorthin, wo man ihn beheben kann.

Umgesetzt wie geplant, mit zwei Ergänzungen: `manifest.env` wird zusätzlich gegen
`expected_kernel` aus `release.json` geprüft (nicht nur gegen das ISO-Datum), und
die Anzahl der Einträge in `SHA256SUMS` gegen `bundle_packages` — das ist der
Abnehmer, für den Phase 2.3 das Feld aufgenommen hat. Was `-Check` bewusst *nicht*
tut: die 372 MB Paketdateien nachrechnen. Das erledigt `bootstrap.sh` auf dem
Diagnose-PC ohnehin vor jeder Installation; hier zählt, dass alle Dateien da sind.

### 3.3 `-InstallVentoy` (opt-in, destruktiv)

Umgesetzt wie geplant. Die CLI-Aufrufform wurde vorher an der offiziellen
Dokumentation und an den Zeichenketten in `Ventoy2Disk.exe` selbst verifiziert:

```text
Ventoy2Disk.exe VTOYCLI /I /PhyDrive:<n> /FS:exFAT
```

- **Nicht** Teil des Standardlaufs, und ohne erhöhte Rechte bricht das Skript ab,
  bevor es überhaupt Laufwerke aufzählt.
- Zeigt Nummer, Modell, Seriennummer, Größe und Bustyp und verlangt die exakt
  getippte Zeichenfolge `ERASE DISK <n>` — kein `[J/N]`, und
  Groß-/Kleinschreibung zählt.
- Kandidaten sind nur Laufwerke mit `BusType -eq 'USB'`, ohne `IsBoot`/`IsSystem`
  und unterhalb `-MaxDiskSizeGB` (Standard 256). Am Testrechner filtert genau das
  eine angeschlossene 1,8-TB-USB-SSD heraus — der Schutz greift also nicht nur
  theoretisch.
- `/NOUSBCheck` wird **nicht** übergeben: Ventoys eigene Weigerung, ein
  Nicht-USB-Laufwerk anzufassen, bleibt als zweite, unabhängige Schranke stehen.
- Version und SHA256 sind in [tools/ventoy-release.json](tools/ventoy-release.json)
  gepinnt (1.1.17). Der Hash stammt aus der offiziellen `sha256.txt` des Releases
  **und** wurde gegen die heruntergeladene Datei nachgerechnet.

**Zum Erfolgsnachweis.** Ventoys CLI startet einen Kindprozess, der Exit-Code des
gestarteten Prozesses sagt deshalb nichts. Ausgewertet wird die dokumentierte
Marke `cli_done.txt` (Inhalt `0` = Erfolg); bei Misserfolg werden die letzten
Zeilen aus `cli_log.txt` ausgegeben.

### 3.4 Was beim Testen aufgefallen ist

Fünf Fehler, alle vor dem ersten echten Stick gefunden und behoben:

1. **`return ,$entries` lieferte eine Liste mit einem Element**, das die
   eigentliche Liste enthielt. Das Bundle galt dadurch immer als unvollständig
   („SHA256SUMS listet 1 Paket, release.json sagt 25"). Das Komma-Idiom schützt
   einelementige Rückgaben vor dem Auspacken — hier hat es die Liste eingepackt.
2. **`$LASTEXITCODE -ne 0` nach `sync-to-usb.ps1` wäre immer wahr gewesen.**
   robocopy meldet `1` für „Dateien wurden kopiert", also den Normalfall.
   `sync-to-usb.ps1` unterscheidet das bereits korrekt (`>= 8`); die zweite
   Prüfung war schlicht falsch und ist entfallen.
3. **`$input` als Variablenname** im Download überschreibt eine automatische
   PowerShell-Variable. Umbenannt in `$stream`.
4. **`-Force` löschte den Cache-Eintrag vor dem Download.** Ein `-Force`-Lauf mit
   Netzproblem stand danach ohne die Datei da, die er vorher hatte. Jetzt bleibt
   die alte Datei liegen, bis die neue heruntergeladen *und* geprüft ist.
5. **`BOOT.txt` wurde nach der Verifikation geschrieben**, die es folglich als
   fehlend meldete und die der Lauf dann selbst behob — ein Lauf, der sich
   erfolgreich beschwert. Reihenfolge getauscht.

### 3.5 Akzeptanzkriterien Phase 3 — Ergebnis

| Kriterium | Ergebnis |
| --- | --- |
| Skript parst unter Windows PowerShell 5.1 | ✅ `[Parser]::ParseFile`, keine Diagnosen |
| Erstlauf auf leerem Stick | ✅ ISO kopiert und geprüft, Bundle entpackt, Spiegel, `BOOT.txt`, Exit 0 |
| Zweitlauf lädt und prüft nichts erneut | ✅ ISO über den Zustandsvermerk, Bundle über `manifest.env` erkannt |
| Platzprüfung vor dem ersten Byte | ✅ Reihenfolge belegt; Verweigerung bei zu wenig Platz gegengeprüft |
| Download echt, mit Redirect und Hash | ✅ 16 MB von GitHub geladen, SHA256 bestätigt |
| Download mit falschem Hash / HTTP-Fehler | ✅ verworfen bzw. alle URLs mit Sammelmeldung |
| `-Check` auf gesundem Stick | ✅ 6× OK, Exit 0 |
| `-Check`: ISO fehlt / falsche Größe / falscher Hash | ✅ je FAIL, Exit 1 |
| `-Check`: Paketdatei fehlt / Bundle vom falschen ISO | ✅ je FAIL mit Ursache im Klartext |
| `-Check`: leerer Stick | ✅ 2× FAIL, 2× WARN, Exit 1 |
| `release.json` fehlt / Schema zu neu | ✅ je eine Klartextmeldung ohne PowerShell-Stacktrace, Exit 1 |
| Beschädigtes ISO wird im Normallauf ersetzt | ✅ erkannt, neu kopiert, erneut geprüft |
| `-Check` und `-InstallVentoy` zusammen | ✅ abgewiesen |
| `-InstallVentoy` ohne Adminrechte | ✅ bricht ab, bevor Laufwerke aufgezählt werden |
| `-InstallVentoy` Größen-/Bustyp-Schranke | ✅ gegen echte Laufwerke geprüft, 1,8-TB-USB-SSD gefiltert |
| Bestehende Regressionstests | ✅ 39 unittests + Stamp-Tests |

Nicht nachstellbar und deshalb offen: ein Lauf gegen einen **echten** Ventoy-Stick
(exFAT, Wechselmedium) und der tatsächliche Ventoy-Schreibvorgang. Beides braucht
Hardware; die Logik davor ist einzeln geprüft.

**Aufwand tatsächlich:** ~0,5 Tag.

---

## Phase 4 — Ein Kommando auf dem Diagnose-PC ✅ umgesetzt (Stufe 1+2)

Statt drei Zeilen:

```bash
mkdir -p /mnt/ventoy
mount /dev/disk/by-label/Ventoy /mnt/ventoy
bash /mnt/ventoy/gpu-triage/start.sh
```

nur noch die eine Zeile aus `BOOT.txt`:

```bash
m=/mnt/v; mkdir -p $m; mount /dev/disk/by-label/Ventoy $m; bash $m/gpu-triage/go.sh list
```

Der ehrliche Teil: **das erste Mounten kann man nicht wegoptimieren**, solange das
Skript auf der Datenpartition liegt. Realistisch sind drei Stufen, aufsteigend im
Aufwand:

1. **Umgesetzt:** die kopierbare Einzelzeile in `BOOT.txt` auf dem Stick-Root
   (geschrieben von `prepare-usb.ps1`) und in der README. Sie endet auf `go.sh
   list`, mountet also nicht nur, sondern liefert direkt die GPU-Liste.
2. **Umgesetzt:** [go.sh](go.sh) neben `start.sh`. Es ist ein Wrapper, kein zweiter
   Pfad — es endet immer in `exec bash start.sh "$@"`.
3. **Verifiziert und verworfen** — siehe 4.2.

### 4.1 Was `go.sh` tatsächlich löst

Beim Umsetzen stellte sich heraus, dass Stufe 2 mehr ist als Tipparbeit sparen.
Zwei Dinge gehen ohne sie schief, beide mitten im Lauf statt am Anfang:

- **Schreibschutz.** Hat das Live-System die Partition read-only eingehängt,
  fällt das erst auf, wenn der Report geschrieben werden soll. `go.sh` prüft das
  vorher und remountet. Dabei braucht ein Bind-Mount `remount,rw,bind` statt
  `remount,rw` — der einfache Aufruf lässt das ro-Flag stehen. Beide Formen
  werden probiert; genau dieser Fall ist im Test zuerst fehlgeschlagen.
- **Ventoys Device-Mapper.** Ventoy blendet die Partition aus, auf der das
  gebootete ISO liegt: sie ist aus dem Live-System heraus über `/dev/sdX1` nicht
  zuverlässig lesbar, wohl aber über `/dev/mapper/sdX1`
  ([Ventoy-Dokumentation](https://www.ventoy.net/en/doc_linux_remount.html), seit
  1.1.01). `go.sh` stellt den Mapper-Knoten in der Kandidatenliste **vor** das
  rohe Gerät. Die bisherige README-Zeile kannte diesen Fall nicht.

Ablauf: neben `start.sh` und beschreibbar → sofort starten. Read-only →
remounten. Sonst → nach einem bereits gemounteten Ventoy-Dateisystem suchen.
Sonst → selbst mounten, Mapper-Knoten zuerst, und nur akzeptieren, wenn dort
wirklich `gpu-triage/start.sh` liegt. Scheitert alles, nennt die Fehlermeldung
die gefundenen Geräte und den manuellen Weg.

Nach root eskaliert wird erst, wenn wirklich gemountet werden muss — `go.sh list`
auf einem schon gemounteten Stick läuft ohne `sudo`, genau wie `start.sh list`.

### 4.2 Stufe 3: geprüft, negativ

**Der `script=`-Parameter existiert.** Nachgesehen wurde nicht in der Doku,
sondern in der Implementierung: `configs/releng/airootfs/root/.automated_script.sh`
im archiso-Repository, aufgerufen aus `~/.zlogin`, wenn die Anmeldung auf `tty1`
erfolgt. Ein lokaler Pfad wird nach `/tmp/startup_script` kopiert und ausgeführt.

**Trotzdem funktioniert der Weg nicht.** Zum Zeitpunkt des `zlogin` ist die
Ventoy-Datenpartition **nicht gemountet** — das ist ja gerade das Problem, das
Stufe 3 lösen sollte. Der `cp` schlägt fehl, und das Skript wird stillschweigend
übersprungen. Die URL-Variante scheidet aus: Der Diagnose-PC ist per Definition
offline.

Der verbleibende Weg wäre, das Startskript per Ventoy-Injection in die Live-Umgebung
zu legen und die Kernel-Zeile per `conf_replace` zu ersetzen. `conf_replace` tauscht
eine Konfigurationsdatei *im ISO* gegen eine auf dem Stick — also
`/loader/entries/01-archiso-linux.conf` für UEFI und die syslinux-Variante für BIOS,
beide ISO-versionsspezifisch. Damit hinge die Kopplung ISO↔Stick wieder an
handgepflegten Dateien, deren Bruch sich erst beim Booten zeigt — das Gegenteil
dessen, was `release.json` in Phase 2 erreicht hat.

**Ergebnis: Stufe 3 entfällt.** Der Plan hatte das vorgesehen („Fällt die Prüfung
negativ aus, bleibt es bei Stufe 2, und der Plan verliert nichts Wesentliches") —
es bleibt bei einer getippten Zeile pro Boot.

### 4.3 Akzeptanzkriterien Phase 4 — Ergebnis

| Kriterium | Ergebnis |
| --- | --- |
| `go.sh` startet `start.sh` von einer beschreibbaren Kopie | ✅ Argumente werden durchgereicht (`--help`, `list`) |
| Read-only-Erkennung und Remount | ✅ gegen einen ro-Bind-Mount im Mount-Namespace |
| Bind-Mount braucht `remount,rw,bind` | ✅ als Fehler gefunden und behoben |
| Kein Ventoy-Gerät vorhanden | ✅ Exit 2 mit Gerätliste und manuellem Ausweg |
| Keine Root-Eskalation ohne Mount-Bedarf | ✅ |
| `bash -n` sauber | ✅ |
| `BOOT.txt` wird geschrieben und enthält die Zeile | ✅ Teil des `prepare-usb.ps1`-Laufs |
| Stufe 3 verifiziert | ✅ negativ, mit Begründung (4.2) |

Nicht nachstellbar: das Mounten eines echten Ventoy-Sticks samt Mapper-Knoten.
Die Kandidatenermittlung ist einzeln geprüft, das Mounten selbst braucht Hardware.

**Aufwand tatsächlich:** ~2 Stunden inklusive der Verifikation von Stufe 3.

---

## Phase 5 — Dokumentation und Tests nachziehen ✅ umgesetzt

> **Stand nach Umsetzung.** Die vier neuen Regressionstest-Bereiche sind als
> eigenständige, hardwarefreie Suiten vorhanden. Die Windows-Suite läuft unter
> Windows PowerShell 5.1 gegen ein temporäres `subst`-Laufwerk; ISO und Bundle
> sind synthetisch und werden vollständig über ihre echten Hashes geprüft.
> README und VM-Anleitung führen jetzt zuerst über den Release-Weg, während der
> manuelle Bundle-Bau als Rückfallweg am Dokumentende erhalten bleibt.

### 5.1 README umbauen

Teilweise mit Phase 3/4 vorweggenommen, weil ein Werkzeug, das in der README nicht
vorkommt, nicht existiert. Abschließend umgesetzt:

- ✅ „Der kurze Weg: ein Kommando" mit Schaltertabelle vor dem manuellen Weg.
- ✅ „Verwendung" führt die Einzeilen-Boot-Zeile und `go.sh`, inklusive des
  Device-Mapper-Hinweises aus 4.1.
- ✅ Projektstruktur um `go.sh`, `tools/prepare-usb.ps1`,
  `tools/ventoy-release.json`, `offline/release.json`, `offline/release_meta.py`
  und den CI-Workflow ergänzt; Stick-Layout um `BOOT.txt` und `go.sh`.

- ✅ Ein echter Abschnitt **„Schnellstart"** steht unmittelbar unter dem Titel:
  vier Schritte vom Checkout bis zur Zeile aus `BOOT.txt`.
- ✅ Der manuelle Weg steht am Ende des Dokuments und ist aus dem primären
  Einrichtungsfluss verschwunden.

### 5.2 Tests

Zu den bestehenden Tests in [tests/](tests/) kamen dazu:

- ✅ `test_bundle_subtract.sh` — synthetische `pkglist.x86_64.txt` + synthetische
  Paketliste; prüft, dass exakte Versionstreffer abgezogen und Abweichungen
  behalten **und gemeldet** werden. Zusätzlich ist die Epoch-Umbenennung für
  Windows-Dateisysteme abgedeckt. Die echte Buildlogik liegt dafür in
  `offline/bundle_helpers.sh` und wird sowohl vom Builder als auch vom Test
  verwendet.
- ✅ `test_release_json.sh` — Schema, Pflichtfelder, langlebige Archive-URL und
  die Bindung von Name, Größe und SHA256 an das Release-Artefakt.
- ✅ `test_prepare_usb.ps1` — `subst` als Laufwerk, synthetisches `release.json`
  und synthetisches Bundle im Cache: Erstlauf, Zweitlauf ohne Download, `-Check`
  sowie die Fehlerbilder fehlendes/falsches ISO, fehlendes Paket, falsches
  Bundle-Datum, leerer Stick, fehlende Datei und zu neues Schema.
- ✅ `test_go.sh` — Argumentweitergabe, echter Read-only-Bind-Mount mit Remount,
  Kandidatenauswahl über ein kontrolliertes `lsblk` und der Hardware-fehlende
  Fehlerpfad in einem privaten Mount-Namespace.

### 5.3 `TESTING-WINDOWS-VM.md`

✅ Neuer „Weg 0": `prepare-usb.ps1` zieht das Bundle aus dem Release statt es zu
bauen. Der VM-Test kann damit ohne WSL2 direkt über einen vorbereiteten
Ventoy-Stick beginnen.

### 5.4 Akzeptanzkriterien Phase 5 — Ergebnis

| Kriterium | Ergebnis |
| --- | --- |
| README beginnt mit vierstufigem Schnellstart | ✅ |
| Manueller Bundle-Bau steht am Dokumentende | ✅ |
| Exakte ISO-Pakete abziehen, Drift behalten und melden | ✅ synthetisch geprüft |
| Epoch-Doppelpunkt für Windows bereinigen | ✅ synthetisch geprüft |
| `release.json` vollständig und artefaktgebunden | ✅ 7 Positiv-/Fehlerfälle |
| `prepare-usb.ps1`: Erstlauf, Zweitlauf, `-Check` | ✅ Windows PowerShell 5.1, `subst` |
| `prepare-usb.ps1`: zentrale Fehlerbilder aus 3.5 | ✅ 9 Fehlerfälle |
| `go.sh`: Read-only-Remount und Kandidatenwahl | ✅ privater Mount-Namespace |
| Bestehende Regressionstests | ✅ 39 unittests + Stamp-Tests |

**Aufwand:** ~0,5 Tag.

---

## Reihenfolge, Abhängigkeiten, Aufwand

| Phase | Ergebnis | Abhängig von | Aufwand | Risiko |
| --- | --- | --- | --- | --- |
| ~~1~~ | ✅ Bundle-Bau klein und containerfähig | — | 0,5 d (erledigt) | — |
| ~~2~~ | ✅ Bundle als Release-Artefakt + `release.json` | 1 | 0,5 d (erledigt) | — |
| ~~3.1/3.2~~ | ✅ `prepare-usb.ps1` + `-Check` | 2 | 0,5 d (erledigt) | — |
| ~~4 (1+2)~~ | ✅ ein Kommando beim Booten | — | 2 h (erledigt) | — |
| ~~3.3~~ | ✅ `-InstallVentoy` | 3.1 | in 3.1 enthalten | **hoch**, opt-in |
| ~~4 (3)~~ | ⛔ Auto-Start via Boot-Parameter | 4.2 | 2 h Verifikation | verworfen, siehe 4.2 |
| ~~5~~ | ✅ Doku + Tests | 1–4 | 0,5 d (erledigt) | — |

**Ergebnis:** Die volle Vereinfachung steht — 7 Schritte auf dem Vorbereitungs-PC
sind einer geworden, eine Linux-Umgebung braucht nur noch, wer das Bundle selbst
bauen will. Auf dem Diagnose-PC ist aus drei Zeilen eine geworden; die verbleibende
Zeile ist die, die sich nicht wegoptimieren lässt (4.2).

Alle geplanten Phasen sind damit umgesetzt. Zwei Nachweise hängen weiterhin an
Dingen, die dieses Repository nicht selbst herstellen kann: der erste echte
CI-Lauf (Phase 2) und ein Lauf gegen echte Hardware (Phase 3.3, 4.1).

## Risiken und offene Verifikationen

| Risiko | Auswirkung | Umgang |
| --- | --- | --- |
| ~~`arch/pkglist.x86_64.txt` liegt nicht wie erwartet im ISO~~ | — | ✅ erledigt: am ISO 2026.08.01 verifiziert, `--no-iso-subtract` bleibt als Rückfallebene |
| ~~Versionsversatz zwischen ISO und Archive-Snapshot~~ | — | ✅ erledigt: abweichende Pakete werden behalten, nicht abgezogen; gemessener Versatz 0/151. Hart abgebrochen wird nur bei `linux` |
| ~~`pacman -Sw` liefert eine andere Closure als `pacstrap`~~ | — | ✅ erledigt: leeres `--dbpath` erzwingt dieselbe vollständige Auflösung; das reduzierte Bundle installiert nachweislich mit voller Abhängigkeitsprüfung |
| Release-Asset zu groß / Bandbreite | Vorbereitung dauert | entschärft: 372 MB, weit unter GitHubs 2-GB-Grenze; lokaler Hash-Cache verhindert Mehrfachdownloads |
| Bundle ist an genau ein ISO gebunden | Einspielen anderswo scheitert | bewusst so; `bootstrap.sh` benennt die Ursache, `--no-iso-subtract` erzeugt ein autarkes Bundle |
| ~~`-InstallVentoy` erwischt die falsche Platte~~ | **Datenverlust** | ✅ umgesetzt: opt-in, Adminrechte, nur `BusType USB` ohne `IsBoot`/`IsSystem`, Größenschwelle, Modell+Seriennummer im Klartext, getippte Bestätigung `ERASE DISK <n>`, dazu Ventoys eigene USB-Prüfung als zweite Schranke |
| GitHub als Abhängigkeit im Standardweg | ohne Netz keine Vorbereitung | manueller Weg bleibt vollständig dokumentiert und lauffähig |
| ~~Ventoy-Partition ist aus dem Live-System nicht mountbar~~ | Boot-Zeile schlägt fehl | ✅ erledigt: `go.sh` probiert den Device-Mapper-Knoten vor dem rohen Gerät (4.1) |
| Robocopy `/MIR` löscht das entpackte Bundle wieder | Stick ohne Pakete | ✅ erledigt: entpackt wird ins Repository, der Spiegel trägt es weiter (3.1) |

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
