> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../CLAUDE.md)

# Aura DI — di/

Scope: `di/` — the Hilt `SingletonComponent` modules. `DataModule`, `DeviceModule`, `HapticsModule`,
`NotifyModule` ship in every variant; the fake-voice and mock-backend bindings live in variant-scoped
`src/debug` / `src/release` mirrors.

## The seam law — a cross-layer dependency is a narrow interface DOWN + a binding HERE

Every place a higher layer needs a lower one to call UP (a repo notifying the notification service, a
device handler launching an activity) is a narrow `fun interface` declared in the CONSUMER's own lower
package, with the implementation `@Binds`/`@Provides`-bound here. **There is never an upward import**;
the interface plus this module are the only seam.

- `DeviceToolDispatch` ← `DeviceToolExecutor` (`DeviceModule.bindDeviceToolDispatch`) is the ONE place
  the executor is wired to anything. `RunRepository.live()` attaches it, and **nothing else may inject
  it**.
- `RunNotifications` ← `RunNotificationLauncher` (`NotifyModule`) — same shape, for the FGS start.
- `DeviceToolGate` / `DevicePermissionChecker` / `DeviceClock` / `AppForegroundChecker` /
  `DeviceToolResultReporter` are `@Provides` lambdas so `data/device/` stays plain-JVM unit-testable.
  **Do NOT inject `SettingsStore`/`Context`/`AuraApi` into that package directly.**

## DataModule — one network stack, URL editable at runtime

- Retrofit is built ONCE against a placeholder base URL; **`BaseUrlInterceptor` rewrites every
  request's scheme/host/port** to the current `SettingsStore.baseUrl`, which is what makes the server
  user-editable with no Retrofit/OkHttp rebuild plumbing. `SessionStreamClient` builds its own URL, so
  the rewrite is a harmless no-op for SSE.
- `AuthInterceptor` sends `X-API-Key`, `X-Mewbo-Surface: android`, and `X-Mewbo-Capabilities` — a
  comma-joined list with `apps` always present and `stlite` only while `streamlitWidgetsEnabled` is on.
  **Advertise and render are keyed on the same flag**, so the app never advertises what it will not
  service. Flags are read via blocking `first()`: DataStore caches in memory, and this runs off-main on
  OkHttp's dispatcher.
- `provideJson`: `ignoreUnknownKeys = true` AND **`explicitNulls = false`**. The second is load-bearing
  for the DTO round-trip contract — a test asserting a null field is DROPPED from an encoded body must
  match this `Json` exactly.
- `provideEventSourceFactory` = `okHttpClient.newBuilder()` with `readTimeout(0)`/`callTimeout(0)`,
  since SSE is long-lived on 15s server heartbeats. **`newBuilder()` COPIES the interceptor chain**,
  which is why the debug mock interceptor transparently covers SSE too.
- `debugOnlyInterceptors: Set<Interceptor>` is a multibinding — empty in release, the mock interceptor
  in debug — added LAST, so a short-circuiting debug interceptor sees the exact final request shape the
  real network would.
- `@ApplicationScope` = `SupervisorJob + Dispatchers.IO`, app-process-lifetime. It backs
  `RunRepository`'s per-session `shareIn` multicast and `DeviceToolExecutor`'s dispatch job — both must
  outlive any single collector, or a `stop()`/nav event tears down servicing for a still-live run.
  `SupervisorJob` so one session's stream failure cannot cancel a sibling's.

## DeviceModule / HapticsModule / NotifyModule

- `DeviceModule` provides the handler LIST out of `DeviceToolExecutor`'s constructor, so its unit tests
  need no `Context`-backed handlers. `AppForegroundChecker` ORs process importance with
  `AssistOverlayPresence.visible` → `canStartActivityNow`.
- `HapticsModule` is the ONE `Vibrator` resolution (`VibratorManager`, minSdk 33), wrapped in
  `runCatching { … }.getOrNull` so a device with no vibrator degrades to a no-op rather than crashing.
- `NotifyModule` binds `RunNotifications` ← `RunNotificationLauncher`.
