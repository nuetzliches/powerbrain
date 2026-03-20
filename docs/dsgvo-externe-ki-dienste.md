# DSGVO: Personenbezogene Daten und externe KI-Dienste

## Frage

Ist eine Datenschutzverletzung nach DSGVO gegeben, wenn personenbezogene
Daten an claude.ai oder vergleichbare externe LLM-Dienste übermittelt werden?

---

## Kurzantwort

**Ja** — bei claude.ai (Consumer) immer. Bei der Anthropic API unter
bestimmten Voraussetzungen rechtlich argumentierbar, aber mit Restrisiko.

---

## Die drei Voraussetzungen

### 1. Auftragsverarbeitungsvertrag (Art. 28 DSGVO)

Wer personenbezogene Daten an einen Dritten zur Verarbeitung weitergibt,
**muss** einen AVV abschließen. Ohne AVV ist die Übermittlung ein Verstoß —
unabhängig vom Empfänger.

| Dienst | AVV verfügbar? |
|--------|----------------|
| Anthropic API (Enterprise/Team) | Ja, über die Konsole abschließbar |
| claude.ai (Consumer) | **Nein** — Anthropic ist dort eigener Verantwortlicher, nicht Auftragsverarbeiter |

Für **claude.ai** gibt es strukturell keinen AVV-Pfad. Das allein
schließt regelkonformen Einsatz für Daten mit Personenbezug aus.

### 2. Drittlandtransfer USA (Art. 44–49 DSGVO)

Anthropic ist ein US-Unternehmen. Seit *Schrems II* (EuGH, Juli 2020) braucht
jeder Transfer in die USA eine eigene Rechtsgrundlage:

**EU-US Data Privacy Framework (DPF)**
Angemessenheitsbeschluss der EU-Kommission seit Juli 2023. US-Unternehmen,
die zertifiziert sind, dürfen Daten ohne SCCs empfangen.
→ Zu prüfen: [dataprivacyframework.gov](https://www.dataprivacyframework.gov)
→ Stand August 2025: Anthropic-Zertifizierung **nicht bestätigt** — vor Einsatz
aktuell prüfen.

**Standardvertragsklauseln (SCCs)**
Anthropic bietet im API-Enterprise-Kontext SCCs an. Seit *Schrems II* reichen
SCCs allein formal nicht aus — ein **Transfer Impact Assessment (TIA)** ist
erforderlich, das die tatsächliche US-Behördenzugriffsmöglichkeit bewertet
(CLOUD Act, FISA 702). Ein *Schrems III* ist nicht ausgeschlossen.

**Art. 49 Ausnahmen** (explizite Einwilligung, Vertragserfüllung, etc.) sind
für systematische KI-Nutzung nicht tragfähig.

### 3. Datenverwendung für Training

claude.ai (Consumer) behält sich in den AGB vor, Konversationen für
Modell-Training zu verwenden. Selbst wenn der Transfer rechtlich wäre:
Die Weitergabe zu Trainingszwecken wäre ein Zweckentfremdungsverstoß
(Art. 5 Abs. 1 lit. b) und erfordert eine neue Rechtsgrundlage, die für
die ursprünglichen Daten typischerweise nicht existiert.

---

## Bewertungsmatrix

| Szenario | Bewertung |
|----------|-----------|
| claude.ai (Browser/Consumer) mit Personenbezug | **Klarer Verstoß** — kein AVV möglich, Trainingsdaten-Risiko |
| Anthropic API ohne AVV + SCCs | **Verstoß** — fehlende Vertragsgrundlage |
| Anthropic API mit AVV + SCCs, ohne TIA | **Wahrscheinlich Verstoß** |
| Anthropic API mit AVV + SCCs + TIA + kein Training | Rechtlich argumentierbar, Restrisiko durch CLOUD Act |
| Dieses Projekt (Ollama lokal) | ✅ Kein Transfer, kein Problem |

---

## Relevanz für den Entwicklungs-Workflow

Dieses Projekt verwendet bewusst **Ollama lokal** — der Architekturentscheid
„Embeddings lokal" schließt externe Datenübermittlung strukturell aus.

Konsequenterweise gilt das auch für den Entwicklungs-Workflow:
**Keine Echtdaten in Prompts an externe KI-Assistenten** (Code-Assistenten,
Chats), auch nicht als Testdaten in Schemas oder als Beispiele in Codekommentaren.

Wenn Claude Code oder vergleichbare Tools zur Entwicklung eingesetzt werden:
- Nur anonymisierte/synthetische Beispieldaten im Projektkontext verwenden
- Keine Produktions-Dumps, echte Kundennummern oder Namen als Testdaten

---

## Rechtliche Einordnung: „Unter den Teppich" vs. Dokumentieren

→ Separate Behandlung in [architektur.md §4.5](architektur.md) und
[006_privacy_incidents.sql](../init-db/006_privacy_incidents.sql).

Kurzfassung: Das Nicht-Protokollieren schafft keinen Schutz:
- Die 72h-Frist (Art. 33) läuft ab Bekanntwerden, nicht ab Entscheidung
- Behörden (BfDI, LfDI) verhängen bei nachträglicher Entdeckung 2–4× höhere
  Bußgelder als bei proaktiver Meldung
- §42 BDSG: persönliche Strafbarkeit (bis 3 Jahre) bei vorsätzlicher Nicht-Meldung
- Art. 5(2) Rechenschaftspflicht erfordert aktive Nachweisführung

---

## Quellen und Nachweise

- DSGVO Art. 5, 6, 28, 33, 34, 44–49, 83
- BDSG §42, §43
- EuGH C-311/18 (*Schrems II*, 16. Juli 2020)
- EU-Kommission Durchführungsbeschluss (EU) 2023/1795 (EU-US DPF, 10. Juli 2023)
- EDPB Recommendations 01/2020 on supplementary measures for transfers
- BfDI: Orientierungshilfe KI-Sprachmodelle (jeweils aktuelle Fassung prüfen)
