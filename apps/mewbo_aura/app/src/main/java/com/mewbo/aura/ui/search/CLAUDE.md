> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Search Chats — ui/search/

Scope: `ui/search/` — `SearchChatsScreen` + the pure `SessionSearchFilter`. **There is NO backend
search call**: this is a client-side filter over the drawer's already-cached list.

- **`SessionSearchFilter`** is a pure `internal object` (unit-testable without Compose). A blank query
  returns the FULL list unchanged, not empty. A non-blank query matches ONLY `SessionSummary.title`
  (`contains`, ignore-case), so a `null`-titled session is dropped from every search. Message bodies
  are not searched.
- **`SearchChatsScreen` reuses the SAME `SessionsViewModel` as the drawer** (`hiltViewModel()`), so it
  inherits that VM's loading/error/offline states and issues no fetch of its own. The filter runs every
  keystroke inside `remember(sessions, query)`. It is a separate PUSHED route, without the
  `ModalNavigationDrawer` chrome. The empty state is two-way: "No matches" for a non-blank query vs
  "No chats yet" for a blank one.
