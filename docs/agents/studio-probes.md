# Studio probes

Part of the agent instructions: [AGENTS.md](../../AGENTS.md) links here from its index, and its rules apply here too.

Details and measurements: [Studio probes of 13.11.2026](../superpowers/probes/2026-11-13-studio-proben.md).
Apps do not automatically become MCP tools; only importing OpenAPI as an HTTP connector turns
operations into tools. App modules have no schedules (workflows do). The same instance ran for
20 calls each, including after 31 minutes idle; a later stop was not checked. SSE arrived through
the gateway before the stream ended, with one short pause in the first measurement. The Gmail
connector does not return complete headers in `get_thread`; newsletter detection therefore also
uses the label `CATEGORY_PROMOTIONS` for connector accounts (`CATEGORY_UPDATES` does not count,
as it also contains order confirmations and account notices). On 23.11.2026, ten Gmail Studio trash
requests failed confirmation after a label write returned HTTP 200. A first probe made four
label writes with HTTP 200 and no observed change. A second probe made seven writes. All seven
returned HTTP 200 with a missing-scope diagnostic in text and no tool error flag. Both read formats
confirmed zero changes. On 24.11.2026, after reauthentication of the same pinned credential,
read/unread, archive/restore and trash/restore worked on one synthetic delivery. Both read formats
confirmed each change and the original unread Inbox state returned with other labels unchanged.
The folder probe returned HTTP 200 without a positive receipt. Gmail Studio permits these
actions through a verified binding and checks the current exact message before recording success.
Gmail Studio point readbacks retry at approximately one and three seconds. A recognized
permission denial pauses the binding immediately. Without a receipt or permission diagnostic,
two consecutive unchanged readbacks from separate writes pause the binding. The second reports
`provider_permission_missing` on that action. A positive receipt
or a change to a requested label resets the streak. After reauthentication in Studio, the
account action allows another attempt.
A repeated Trash can finish a partial Gmail move when a current point read shows both
`TRASH` and `INBOX` on the exact message. It removes only `INBOX` and records the action and
Undo token only after the full destination is confirmed. Folder Undo checks the complete
folder-label set against the original action's destination and the intermediate state its own
writes can produce. It preserves unrelated labels and flags and accepts an already-restored
destination without writing. If another client has started restoring a trashed mail to the
Inbox and Undo would return it to Archive, Undo preserves both labels and keeps the token open.
Index-only corrections and partial provider changes have distinct feedback in the inbox,
chat confirmation cards and rule history. Group Undo reports unsupported tokens separately
from skipped tokens. Inbox permission guidance stays visible alongside newer action feedback
until dismissed. Action notices and Undo keep their own deadlines.
Spam, provider folder moves and provider label changes remain unavailable.
`send_email` takes `replyToMessageId`. The Outlook connector returned HTTP 401 in the earlier probe.
