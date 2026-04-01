# MCP-Server Integration Checklist

Checkliste bei Anbindung eines neuen MCP-Servers an den pb-proxy.

## PII & Datenschutz

- [ ] **Datenquellen identifizieren**: Welche externen APIs/Datenbanken greift der MCP zu?
- [ ] **PII-Klassifikation pro Tool**:
  - Tools die in Powerbrain suchen (`search_knowledge`) liefern pseudonymisierte Daten
  - Tools die externe APIs direkt aufrufen liefern Rohdaten mit potentiell PII
  - Gemischte Server brauchen eine explizite Tool-Liste
- [ ] **`pii_status` in `mcp_servers.yaml` setzen**:
  - `scanned` — alle Tool-Results bereits pseudonymisiert (z.B. Powerbrain MCP)
  - `unscanned` — Tool-Results enthalten potentiell PII (Default, fail-safe)
  - `mixed` — per-Tool Deklaration via `pii_scanned_tools`
- [ ] **`pii_scanned_tools` pflegen** (nur bei `mixed`): Liste der Tool-Namen (unprefixed), deren Results bereits pseudonymisiert sind
- [ ] **Ingestion-Pfad pruefen**: Werden Daten ueber `ingest_data` eingespeist? Falls ja: PII-Scanner greift automatisch bei der Ingestion (Presidio + OPA Policy)
- [ ] **Tool-Results pruefen**: Enthalten Results Personennamen, E-Mails, Beschreibungen mit PII? Als `unscanned` deklarieren

## Authentifizierung

- [ ] **Auth-Modus waehlen**: `bearer` (User-Token forwarding), `static` (fester Token aus Env-Var), `none`
- [ ] **`forward_headers` definieren**: Welche Client-Headers muss der Proxy an den MCP weiterleiten? (z.B. `X-TC-PAT` fuer TimeCockpit)
- [ ] **OPA-Policy erweitern**: Server in `proxy.rego` `mcp_servers_allowed` aufnehmen (welche Rollen duerfen zugreifen?)

## Allgemein

- [ ] **`prefix` festlegen**: Eindeutiger Namespace fuer Tool-Namen (z.B. `tc_`, `jira_`, `slack_`)
- [ ] **`required` festlegen**: Muss der Server beim Proxy-Start erreichbar sein? (`true` = Proxy startet nicht ohne diesen Server)
- [ ] **`tool_whitelist` pruefen**: Sollen nur bestimmte Tools exponiert werden? (Default: alle Tools des Servers)
- [ ] **Docker Networking**: Server muss im `proxy-net` Netzwerk erreichbar sein
- [ ] **Health Check**: Server sollte auf Tool-Discovery reagieren (MCP `list_tools`)

## Referenz: mcp_servers.yaml Beispiel

```yaml
servers:
  - name: neuer-mcp
    url: http://neuer-mcp:3000/mcp
    auth: static
    auth_token_env: NEUER_MCP_TOKEN
    prefix: neu
    required: false
    pii_status: unscanned        # oder: scanned, mixed
    # pii_scanned_tools:         # nur bei mixed
    #   - tool_das_powerbrain_sucht
    forward_headers:
      - X-Custom-Auth
```
