# Device Tools

## Let a session use your phone

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-aura-04-device-tools.png" alt="Aura chat turn asking for the phone's battery level, with the tool-activity fold expanded to show the device_get_battery call and a checkmark, followed by the assistant's answer using that result" style="width: 100%; max-width: 720px; height: auto;" />
</div>

Aura lets the assistant take real actions on your phone. During a conversation it can check the time and battery, set alarms and timers, get your attention, and read or send text messages.

## How a device tool call works {#round-trip}

Nothing runs on the phone unless the assistant asks for it during a live conversation. When a session starts, Aura tells the server which device tools it can offer, and that list depends on what your phone currently allows. The server delivers a call down the session's live stream, Aura runs it locally, and Aura posts the result back. The [Device Tool Bridge](../api/device-tools.md) owns that wire contract.

Whichever surface is following the conversation runs the call, so a question asked from the assist overlay is served exactly as it is in the app.

Set alarm, set timer, and dismiss alarm are the one exception. Each hands a job to the clock app, so they need Aura on screen, either as the app or as the assist overlay. Android only lets an app open another app's screen while it stays visible.

## Permissions are the only gate {#permissions}

What your phone permits is exactly what the assistant can reach.

- There is no separate consent screen inside the app, no allowlist, and no extra confirmation layer. Aura adds no approval step on top of Android.
- A tool that needs a permission you have not granted is not offered to the server. The assistant cannot call a tool it was never told about.
- Granting the Android permission enables the tool. Revoking it takes the tool away again.

Only messaging needs a permission. Grant the Android SMS permissions from Aura's Settings, or by accepting the system prompt when it appears.

> [!NOTE] You are in control through Android
> Manage what the assistant can reach the same way you manage any app, in Android's own permission settings.

## Available tools {#tools}

The catalog of every tool this build carries lives in [`DeviceToolCatalog.kt`](repo:apps/mewbo_aura/app/src/main/java/com/mewbo/aura/data/device/DeviceToolCatalog.kt).

| Tool | What it does | Permission |
|------|--------------|------------|
| Get time | Reports the phone's current local date, time, and timezone. | None |
| Get battery | Reports the battery level and whether the phone is charging. | None |
| Set alarm | Asks the phone's clock app to set an alarm for a given time. | None |
| Set timer | Asks the phone's clock app to start a countdown timer. | None |
| Get next alarm | Reports the single next alarm scheduled to fire. Android only exposes the soonest one, not every alarm. | None |
| Dismiss alarm | Asks the clock app to dismiss an alarm by time, by label, the next one, or all of them. | None |
| Wake | Rings and vibrates the phone briefly to get your attention. | None |
| Read latest texts | Reads your most recent incoming text messages, optionally filtered by sender. | Read SMS |
| Send a text | Sends a text message to a recipient. | Send SMS |

> [!WARNING] Sending a text is irreversible
> The Send a text tool sends a real SMS, which cannot be recalled and may cost money through your carrier. Never grant the Android Send SMS permission and the assistant is never offered the tool.

## Next steps

- [Chat and Sessions](chat.md). Where tool activity appears during a conversation.
- [Device Tool Bridge](../api/device-tools.md). How the server delivers these calls to the app.
