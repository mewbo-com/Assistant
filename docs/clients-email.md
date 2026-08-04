# Email

## Reply from your inbox

<div style="display: flex; justify-content: center;">
  <img src="../assets/img/mewbo-email-01.png" alt="Mewbo email thread in Gmail" style="width: 100%; max-width: 720px; height: auto;" />
</div>

Email Mewbo and it replies. Send a message to the configured mailbox, get a styled HTML reply, and keep the thread going to stay in the same session.

## How it works

Mewbo polls the mailbox over IMAP and runs each new email through the same [channel pipeline](core-orchestration.md#channel-adapters) as every other adapter. Replies go back over SMTP as multipart messages, a styled HTML body plus a plain text fallback.

## Access control

- **`allowed_senders`** is the only gate. It lets through listed addresses and silently ignores and marks read everything else.
- An email addressed to you alone is always processed. No mention keyword is needed.
- A thread with multiple `To` or `Cc` recipients only gets a reply when `@Mewbo` appears in the body.

## Session model

Threads map to sessions through the `References` and `In-Reply-To` headers.

- A **new email**, one with no `In-Reply-To` header, creates a new session tagged `email:thread:<sender>:<Message-ID>`.
- A **reply**, one carrying `In-Reply-To` or `References`, resolves the existing session through the thread root's `Message-ID`.

Outbound replies carry `In-Reply-To` and `References` too, so mail clients group them into the same thread.

## Slash commands

Send the command as the email body.

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/usage` | Show session context usage and token budget |
| `/new` | Start a fresh conversation (clears context) |
| `/switch-project <name>` | Switch active project context |

## Configuration

Add to `configs/app.json` under `channels`.

```json title="configs/app.json"
"email": {
  "enabled": true,
  "imap_host": "imap.example.com",
  "imap_port": 993,
  "imap_ssl": true,
  "smtp_host": "smtp.example.com",
  "smtp_port": 587,
  "smtp_starttls": true,
  "username": "mewbo@example.com",
  "password": "app-password-here",
  "from_address": "Mewbo <mewbo@example.com>",
  "mailbox": "INBOX",
  "poll_interval_seconds": 30,
  "allowed_senders": ["alice@example.com", "bob@example.com"]
}
```

What the example above does not show.

| Field | Description |
|-------|-------------|
| `username` / `password` | One credential pair serves both IMAP and SMTP. Typically an app password. |
| `from_address` | Display name and address for outbound mail. Defaults to `username`. |
| `mailbox` | IMAP folder to poll. Default `INBOX`. |
| `poll_interval_seconds` | Seconds between mailbox checks. Default 30, minimum 5. |
| `smtp_ssl` | Implicit TLS instead of `smtp_starttls`. |

## Response rendering

The adapter converts the agent's markdown to HTML styled with inline CSS, which is what Gmail, Outlook and Apple Mail need. Headings, bold, italic and links render as written. Code blocks get a monospace font on a light background, and tables get borders and padding. The raw markdown rides along as a plain text fallback for clients that cannot render HTML.

## Limitations

- **Attachments** are not processed on inbound email. File metadata is ignored too.
- **Rate limiting** does not exist on the poller.
- **Polling latency** is bounded by `poll_interval_seconds`. IMAP IDLE push is not supported.

> [!NOTE] How it works internally
> See [Architecture Overview → Channel adapters](core-orchestration.md#channel-adapters).
