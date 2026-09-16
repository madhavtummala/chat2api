# Prompt audit — what is filling the prompt

A browser chat UI accepts one free-text turn, and the site caps its length
(~100k characters on the ExpressAI composer). chat2api flattens a whole
OpenAI-style conversation into that single string, so an agent client can blow
the cap without anything explaining *why*: the system prompt, every MCP server's
tool schemas, injected reminders and a couple of fat tool outputs all land in
the same turn.

The audit records every inbound request and attributes every character of the
prompt we are about to send.

## Turn it on

```bash
CHAT2API_AUDIT_PROMPTS=true python -m src.main
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `CHAT2API_AUDIT_PROMPTS` | `false` | Enable the audit middleware. |
| `CHAT2API_AUDIT_DIR` | `logs/audit` | Where `audit.jsonl` and `bodies/` are written. |
| `CHAT2API_AUDIT_SAVE_BODIES` | `true` | Also keep each raw request body. |
| `CHAT2API_AUDIT_LIMIT_CHARS` | `100000` | Prompt length to flag as over the limit (`0` = off). |

The middleware is read-only — it never alters the request or the response, and
an audit failure is logged rather than raised, so an audited server behaves
exactly like an unaudited one. It covers `/v1/chat/completions` and
`/v1/responses`; Responses bodies are normalised to the same shape so the
numbers are comparable.

> `logs/` is gitignored: saved bodies contain the entire conversation, including
> whatever the client injected. Treat the directory as sensitive.

## What it writes

`logs/audit/` gets three things per request:

| File | Contents |
| --- | --- |
| `prompts.log` | The log you actually read: timestamp, the analysis, and **the exact prompt chat2api typed into the composer**, verbatim. |
| `audit.jsonl` | One machine-readable summary line per request (counts and category totals, no prompt text). |
| `bodies/<ts>-<id>.json` | The raw client request, so a breakdown can be re-derived later. |

The prompt in `prompts.log` is not a reconstruction: a browser provider submits
exactly `flatten_messages(request.messages)` (see `BrowserChatSession.send`), and
that is the string recorded. Watch it live with:

```bash
tail -f logs/audit/prompts.log
```

An entry looks like this — analysis first, then the prompt it describes:

```
══════════════════════════════════════════════════════════════════
2026-09-15 19:41:01  c95a35d7b1b5  /v1/chat/completions  model=expressai/Qwen3.8 27B  stream=False
══════════════════════════════════════════════════════════════════
── prompt audit c95a35d7b1b5 · /v1/chat/completions · model=expressai/Qwen3.8 27B
   prompt: 134,918 chars (~33,729 tokens) · 5 messages · 6 tools
   !! OVER LIMIT by 34,918 chars (limit 100,000)
   ... breakdown ...
── prompt sent to the provider (134,918 chars) ────────────────────
System: You have access to the tools listed below.
...
Assistant:
── end c95a35d7b1b5 ───────────────────────────────────────────────
```

## Reading the output

The same breakdown also goes to the server log, biggest contributor first:

```
── prompt audit f7cc5ef16861 · /v1/chat/completions · model=expressai/Qwen3.8 27B
   prompt: 134,918 chars (~33,729 tokens) · 5 messages · 6 tools
   !! OVER LIMIT by 34,918 chars (limit 100,000)
   by category:
        62,623   46.4%  tool-output/Bash
        41,642   30.9%  system-prompt
        13,935   10.3%  injected/system-reminder
         8,894    6.6%  tools/mcp:notion
   largest 12 items:
        62,623   46.4%  tool-output/Bash · msg[3] tool Bash
        16,814   12.5%  system-prompt · msg[0] system § Tool policy
```

Categories:

| Category | What it is |
| --- | --- |
| `system-prompt` | System messages, split at their markdown headings once they are long enough to be worth splitting. |
| `user-messages` / `assistant/text` | Ordinary conversation turns. |
| `assistant/tool-calls` | Prior assistant turns re-rendered as `⟦tool_call⟧` blocks. |
| `tool-output/<name>` | A tool result, per tool — usually the top line in an agent session. |
| `tools/mcp:<server>` | Tool schemas from one MCP server, priced per tool. |
| `tools/builtin` | Tool schemas the client defined itself. |
| `tools/preamble-boilerplate` | The fixed instructions that introduce the tool list. |
| `injected/system-reminder` | `<system-reminder>` blocks the client slipped into a message. |
| `skills/commands` | `<command-*>` payloads (skills / slash commands). |
| `envelope` | Role labels and blank lines added by flattening. |
| `attachments` | Decoded file/image bytes (uploaded, not part of the prompt text). |

Tool schemas are the contributor clients least expect to pay for: they are sent
as structured JSON and never appear in the conversation, yet every one of them
is rendered into the prompt text. Each tool is measured as the exact block it
contributes to the preamble, so the number is directly comparable to a message.

## Summarising a session

```bash
python -m src.audit_report                       # aggregate logs/audit
python -m src.audit_report --limit 100000 --top 20
python -m src.audit_report logs/audit/bodies/20260915T192835-f7cc5ef16861.json
python -m src.audit_report logs/audit/bodies/…json --prompt   # + the prompt itself
```

The first two forms answer "across this session, which categories keep costing
me, and which requests blew the limit". Passing a saved body re-derives that one
request's full per-segment breakdown; add `--prompt` to print the composed
prompt with it.

## Acting on it

Typical findings, in the order they usually show up:

1. **Tool output dominates.** Cap what the client feeds back (truncate long
   command output, page large file reads) — this is nearly always the biggest
   single win.
2. **MCP servers you aren't using.** Each connected server costs its whole
   catalogue on *every* request. Disconnect the ones this task doesn't need.
3. **The system prompt.** The heading-level split shows which section grew.
4. **Injected reminders.** Memory and skill text repeats on every turn.
