> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Device Tools — data/device/

Scope: `data/device/` — the `device_*` tool catalog, handlers, executor, dispatch, ledger, and the
platform readers behind them. This package PROVIDES the executor/catalog; the seam that advertises AND
answers them is `RunRepository.live()` ([`data/repo/CLAUDE.md`](../repo/CLAUDE.md)) — never a ViewModel.

Handlers: `time`, `battery`, `setAlarm`, `setTimer`, `wake`, `readLatestSms`, `sendSms`,
`getNextAlarm`, `dismissAlarm`.

## Two-layer gate, one set of ids

Every tool is gated at BOTH layers so a stale server cannot slip one through:

- **Advertise:** `DeviceToolCatalog.availableTools` (`suspend`) intersects the `DeviceToolGate`
  disabled-id set AND runtime-permission availability, so a disabled or ungranted tool is never in
  `context.device_tools`.
- **Answer:** `DeviceToolExecutor.executeOutcome` refuses a disabled tool with a `tool_disabled` error,
  checked BEFORE handler lookup so it never reads as `unknown_tool`.

`disabledDeviceToolIds` is stored at tool-id level even though the Settings UI shows
`DeviceToolToggles.GROUPS` clusters — the gate is exact. An empty set means every tool is enabled.

## Plain-JVM testability is the reason for every injected collaborator

`DevicePermissionChecker`, `DeviceClock`, `DeviceToolGate`, `AppForegroundChecker`,
`DeviceToolResultReporter`, `SmsInboxReader`, `NextAlarmReader` are narrow `fun interface`s bound in
[`di/DeviceModule`](../../di/CLAUDE.md). **Never inject `SettingsStore`/`Context`/`AuraApi` into this
package directly** — that is what keeps `DeviceToolCatalog`/`DeviceToolExecutor` unit-testable without
Robolectric. `DeviceToolExecutor` is bound to `DeviceToolDispatch` at exactly ONE place; nothing else
may inject the executor.

## Laws

- **A subscriber is NOT an executor.** Dispatch lives and dies with `live()`'s upstream, not as a
  second subscriber; correctness comes from backlog-replay + `DeviceToolCallLedger.recordIfNew`, never
  from a terminal frame that may be dropped ([`data/repo/CLAUDE.md`](../repo/CLAUDE.md)).
- **The activity-launch gate is not a process-importance test.** `canStartActivityNow` ORs process
  importance with `AssistOverlayPresence.visible`, because a showing `VoiceInteractionSession` pins the
  process at `IMPORTANCE_FOREGROUND_SERVICE` (125), never `IMPORTANCE_FOREGROUND` (100), while the
  launch itself is genuinely permitted. **A BAL-blocked start is a SILENT no-op** — `START_ABORTED` is
  mapped to `START_SUCCESS` before the caller sees it — so a stale-`true` flag reports
  `handed_to_clock_app: true` for an alarm the user never got. Every ambiguous path errs toward
  `false`. Full chain: [`voice/CLAUDE.md`](../../voice/CLAUDE.md) § "Device tools from the overlay".
- **Args are model output — a black box.** Handler arg parsing must be TOTAL and degrade honestly.
- **`DeviceToolResultReporter` never retries** — the wire forbids retrying a result POST
  (200/403/404/409/network are all terminal). It closes both `Response<ResponseBody>` body variants;
  the body is raw rather than a converted DTO, so no JSON conversion is attempted against a
  legitimately-empty 200 body.
- **`SetAlarmHandler`/`SetTimerHandler` results carry no confirming "note" field — the handoff IS the
  success signal.** A note claims certainty the handler does not have, and competes with the
  promoted-tool card the reducer builds from the success. `device_set_alarm`/`device_set_timer`/
  `device_send_sms` are in `PromotedTools` ([`data/model/CLAUDE.md`](../model/CLAUDE.md)), so their
  result renders as an action card ([`toolcards`](../../ui/chat/toolcards/CLAUDE.md)) with no editorial
  prose in the payload.
