# Studio-Proben (2026-11-13)

Ergebnisse von Task 2 des Umsetzungsplans
(`docs/superpowers/plans/2026-11-13-oberflaeche-und-mail-aktionen.md`). Die Proben liefen in der
eigenen Test-App `liam-e-probe` (ohne Datenbank, aus dem Scratchpad mit
`mistral deploy --from-disk` ausgeliefert). Die echte App `liam-e` blieb unberührt.

## Entscheidungen

| Frage | Ergebnis | Folge im Plan |
|---|---|---|
| SSE durch das Gateway | Kommt ungepuffert an: erstes Ereignis nach 184 ms bzw. 83 ms, Ende rund 80–180 ms nach der Serverzeit; in der ersten Messung stockten einmal 7 Ereignisse etwa 0,85 s, in der zweiten gar nicht | Task 17 Variante A (SSE) |
| Werden App-Operationen automatisch MCP-Tools? | Nein. Erst ein Import der OpenAPI als HTTP-Connector macht Tools daraus. | Task 19 Variante A |
| Ausnahme einzelner Operationen | Kein Mechanismus in Paketen, CLI oder `mistral.json`; nur Abwählen einzelner Tools in Studio nach dem Import, oder `include_in_schema=False` (fehlt dann auch im generierten Client) | Variante A reicht; keine Ausnahme nötig |
| Zeitpläne für Apps | Keine. Zeitpläne gibt es nur für Workflows (`ScheduleDefinition`), nicht für App-Module | Task 10 Step 8 entfällt; Abruf beim Öffnen |
| Mehrere Instanzen, Leerlauf-Stopp | Eine Instanz bei je 20 Aufrufen; nach 31 min Leerlauf lief noch derselbe Prozess (kein Neustart). Ob die Plattform nach längerer Zeit stoppt, ist nicht gemessen; die Plattform führt `available_replicas`/`ready_replicas` | keine Codeänderung; Hinweis in AGENTS.md (Task 28) |
| `get_thread` liefert Kopfzeilen | Nein. `FULL_CONTENT` liefert keine Kopfzeilen, `MINIMAL` nur Subject, From, To, Cc, Date | Task 8: `list_unsubscribe = ""` für Connector-Konten; Task 14 erkennt Newsletter über `CATEGORY_*`-Labels |
| `TRASH` per Label | Ja. `label_message.labelIds` nennt `TRASH` ausdrücklich als System-Label | Task 12: Papierkorb für Connector-Konten über `label_message` |

## Belege

### MCP und Connectoren

- `GET /v1/connectors` (lesend, über `api.mail.connectors.list_connectors` mit dem lokalen
  Schlüssel) listet 14 Connectoren des Workspace; weder `liam-e` noch `liam-e-probe` ist dabei,
  obwohl beide als App deployt sind.
- `mistral deploy --json` meldet für die Test-App `"mcpUrls": []`.
- `mistralai_capabilities/fastapi/openapi.py:8-11`: „the same document, as served, is what a
  Studio HTTP connector imports to turn each operation into an MCP tool named after its
  operationId“.
- Doku https://docs.mistral.ai/studio/connectors/http-connectors: Ein HTTP-Connector verbindet
  eine HTTP-API und macht ihre Endpunkte zu Tools; einzelne Tools lassen sich auf der Seite des
  Connectors an- und abschalten (Zusammenfassung, nicht wörtlich).
- Sichtbarkeit eines Connectors: `private`, `shared_workspace`, `shared_org`
  (https://docs.mistral.ai/studio/connectors/management).
- `mistral.json` kennt `"mcp": true` an einer Route. Das gilt für Module, die selbst MCP-Server
  sind, nicht für die Umwandlung der OpenAPI.
- Die Test-Route `probe_write_hidden` nutzt `include_in_schema=False`. Damit fehlt sie in der
  ausgelieferten OpenAPI und damit auch in jedem Import. Für liam-e kommt das nicht in Frage, weil
  der generierte Web-Client dieselbe OpenAPI liest.

### Zeitpläne und Instanzen

- Workflows: „Run workflows on a recurring cron, interval, or calendar schedule“
  (https://docs.mistral.ai/studio/workflows/building-workflows/scheduling). App-Module haben
  `serve` = `static`, `server` oder `worker`, ohne Zeitplan (CLI v0.18.1).
- Die Modul-Statusabfrage der CLI enthält `available_replicas` und `ready_replicas`; eine
  Einstellung für Replikate oder Mindestinstanzen ist nicht dokumentiert.

### Gmail-Connector (lesend)

- `get_thread.messageFormat`: `MESSAGE_FORMAT_UNSPECIFIED`, `MINIMAL`, `FULL_CONTENT`
  (Standard `FULL_CONTENT`). `MINIMAL`: „Returns message snippets and key headers (Subject, From,
  To, Cc, Date)“.
- Felder einer Nachricht in `get_thread` (`FULL_CONTENT`) und `search_threads`: `id`, `date`,
  `sender`, `subject`, `snippet`, `toRecipients`, `ccRecipients`, `labelIds`, `plaintextBody`,
  `htmlBody`, `attachmentIds`, `attachments`. Keine Kopfzeilen.
- `date` hat die Form `2026-11-13T09:41:07-05:00` (ISO 8601 mit Offset).
- `sender` war in der Probe eine bloße Adresse ohne Anzeigenamen. Der Parser muss beide Formen
  lesen (`Name <adresse>` und `adresse`).
- `label_message`/`unlabel_message`: `labelIds` „Can be a system label ID (e.g., 'INBOX',
  'TRASH', 'SPAM', 'STARRED', 'UNREAD', 'IMPORTANT') or a user-defined label ID“.
- `send_email` nimmt `replyToMessageId` für Antworten im selben Gespräch.
- Werkzeuge: `create_draft`, `send_email`, `send_draft`, `list_drafts`, `get_thread`,
  `search_threads`, `label_thread`, `unlabel_thread`, `list_labels`, `label_message`,
  `unlabel_message`, `create_label`, `update_label`, `delete_label`.
- `outlook`: `GET /v1/connectors/outlook/tools` antwortet mit HTTP 401.

#### Follow-up on 23 November 2026

The tool catalog still listed 14 tools. Its system label examples describe input values and do not
prove the connected grant permits changes. Ten trash requests failed confirmation. In the first
probe, four writes returned HTTP 200 and produced zero observed changes. In the second probe,
seven writes returned HTTP 200 with a missing-scope diagnostic in text and no tool error flag.
MINIMAL and FULL_CONTENT both confirmed zero changes. The tested grant lacks permission to modify
mail. No send was tested in these two probes.

### Fixture-Vorlage für Task 8 (ausgedachte Werte)

```json
{
  "id": "18c2f0a9b7d41e55",
  "messages": [
    {
      "id": "18c2f0a9b7d41e55",
      "date": "2026-11-12T07:15:00+01:00",
      "sender": "news@gartenmarkt.example",
      "subject": "Nur heute: 20 % auf Pflanzkübel",
      "snippet": "Hallo Mira, nur heute gibt es 20 % auf alle Pflanzkübel …",
      "toRecipients": ["mira.beispiel@example.org"],
      "ccRecipients": null,
      "labelIds": ["CATEGORY_PROMOTIONS", "UNREAD", "INBOX"],
      "plaintextBody": "Hallo Mira,\n\nnur heute gibt es 20 % auf alle Pflanzkübel.\n",
      "htmlBody": "<p>Hallo Mira,</p><p>nur heute gibt es 20 % auf alle Pflanzkübel.</p>",
      "attachmentIds": null,
      "attachments": null
    }
  ]
}
```

### SSE und Instanzen

Die Probe-Seite liest `GET /api/v1/probe/sse` (20 Ereignisse im Abstand von 100 ms) per `fetch`
und ruft danach 20-mal `GET /api/v1/probe/instance` auf. Gemessen im angemeldeten Browser des
Owners, weil das Gateway ohne Anmeldung 401 antwortet.

Messung 2026-11-13T18:08:49Z (Server-ms / Ankunft im Browser in ms):

| Ereignis | 0 | 1 | 2 | 3–9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Server | 0 | 100 | 200 | 300–902 | 1003 | 1103 | 1204 | 1304 | 1404 | 1504 | 1604 | 1705 | 1805 | 1906 |
| Browser | 184 | 281 | 406 | 1251 | 1282 | 1282 | 1383 | 1488 | 1605 | 1684 | 1864 | 1884 | 2070 | 2085 |

- Erstes Ereignis nach 184 ms (Grenze 500 ms), danach meist etwa 100 ms Abstand. Die Ereignisse
  3 bis 9 kamen gesammelt nach einer Stockung von etwa 0,85 s. Das Gateway puffert also nicht bis
  zum Ende. Die Schreibanimation im Browser (3 Zeichen je 24 ms) überbrückt solche Stockungen.
- Instanzen: alle 20 Aufrufe von `16cb45bf`, Prozessstart 1794592470 (Unix-Zeit,
  2026-11-13T17:54:30Z).

Zweite Messung 2026-11-13T18:40:28Z, 31 min nach der ersten, dazwischen kein Aufruf der Test-App
(Server-ms / Ankunft im Browser in ms):

| Ereignis | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|---|
| Server | 0 | 100 | 201 | 301 | 402 | 502 | 603 | 703 | 804 | 904 |
| Browser | 83 | 181 | 281 | 384 | 482 | 586 | 684 | 785 | 885 | 983 |

| Ereignis | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 |
|---|---|---|---|---|---|---|---|---|---|---|
| Server | 1004 | 1104 | 1204 | 1305 | 1404 | 1504 | 1605 | 1705 | 1806 | 1906 |
| Browser | 1086 | 1185 | 1284 | 1384 | 1484 | 1584 | 1718 | 1820 | 1885 | 1986 |

- Erstes Ereignis nach 83 ms, Abstände zwischen 65 und 134 ms, keine Stockung.
- Instanzen: wieder alle 20 Aufrufe von `16cb45bf` mit demselben Prozessstart. Nach 31 min
  Leerlauf lief also derselbe Prozess weiter; ein Kaltstart war nicht zu sehen. Für liam-e heißt
  das: Zustand im Prozess (etwa ein Abruf, der gerade läuft) übersteht kurze Pausen, ist aber
  nicht dauerhaft. Was dauerhaft gelten muss, gehört in die Datenbank (`mail_job`, Task 6).
