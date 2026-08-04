# Get Started

## Set up Mewbo on your phone

<video muted loop playsinline preload="auto" style="width: 100%; max-width: 960px; height: auto; display: block; margin: 0 auto 1.5rem;">
  <source src="../assets/videos/Mewbo-Aura-0-Banner.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

Aura puts your Mewbo assistant in your pocket. It runs on a server you host, so a session you start on the move opens later from any other client.

The terminal and web console are built for the desk. Aura is for the moments away from it.

- **Let the assistant act on your phone.** With your permission it checks the time and battery, sets alarms and timers, and reads or sends text messages.
- **Talk instead of type.** Dictate a message, hear the reply read back as it streams, and raise the assistant by voice from anywhere on the device.
- **Watch the work, not just the answer.** Tool calls, plans, and sub-agent activity appear in the transcript as they happen.
- **Keep your work.** Every conversation is a session. Reopen a recent one, rename it, or archive it.

## Prerequisites {#prerequisites}

Aura is a client, not a standalone app. You need three things before it is useful.

| Requirement | Notes |
|-------------|-------|
| A running Mewbo server | Aura connects to a Mewbo API server that you host. See [Get Started](../getting-started.md) to stand one up. |
| An API key | Issue one from the console under Settings, then API Keys. See [MCP Server](../clients-mcp.md#authentication-required). |
| An Android phone | Voice needs a microphone, and reading replies aloud needs a text to speech engine. Both ship on standard Android devices. |

> [!NOTE] Your server, your data
> Aura only ever talks to the server you point it at. There is no Aura cloud service in between. The conversations, the model, and the tools are all yours.

## Your first query {#first-query}

1. **Install the app.** Download a prebuilt APK or build it from source. See [Install](install.md).
2. **Connect it to your server.** Open Settings on first launch and enter your server's base URL and API key. Aura checks the connection before it saves. See [Install](install.md#connect).
3. **Ask something.** Type in the composer and send. The reply streams in, and the conversation is saved to your recent chats.

## Next steps { .ms-h2-icon data-icon="rocket" }

<div class="ms-grid ms-grid--4">

<a class="ms-card" href="install/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:download" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Install</span>
  <span class="ms-card__body">Get the APK or build from source, choose a build flavor, and connect to your server.</span>
</a>

<a class="ms-card" href="voice/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:mic" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Voice</span>
  <span class="ms-card__body">Dictation, replies read aloud, and a voice overlay you can raise from anywhere.</span>
</a>

<a class="ms-card" href="chat/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:messages-square" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Chat and Sessions</span>
  <span class="ms-card__body">Streaming chat, tool activity, the session rail, attachments, and the model and scope pickers.</span>
</a>

<a class="ms-card" href="device-tools/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:smartphone-nfc" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Device Tools</span>
  <span class="ms-card__body">What the assistant can do on your phone, and how permissions gate it.</span>
</a>

</div>
