package com.mewbo.aura.data.repo

import com.mewbo.aura.data.device.DeviceToolDefinition
import com.mewbo.aura.data.device.toContextEntry
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/**
 * The `context` object shape shared by session creation AND every `/query` call (data/CLAUDE.md,
 * `apps/mewbo_api/CLAUDE.md`'s `SessionQuery.post`): the backend independently re-resolves
 * `project` and re-defaults `model` back to the config default from EACH request's own context -
 * neither is sticky across turns server-side - so callers must resend both on every send, not just
 * at session creation. `mcp_tools` is omitted entirely when the caller hasn't narrowed anything
 * (task brief) so the backend's own default - bind every tool - stays in effect.
 *
 * `device_tools` follows the SAME resend law as `model`/`project`/`mcp_tools`, but
 * ONLY [RunRepository.sendQuery] passes it - `device_tools` isn't part of session-creation's own
 * context (task brief scopes it to "re-enumerated FRESH on every `/query`" specifically), so
 * [deviceTools] defaults to `null`/omitted for [SessionRepository.createSession]'s call site.
 *
 * **[project] carries a RESERVED value as well as real keys**, and this function is the one place
 * that decision reaches the wire. Three states, all expressed through this one field:
 * `null`/blank omits it entirely (a throwaway temp-dir cwd),
 * [com.mewbo.aura.data.model.ComposerScope.AUTO_PROJECT_KEY] sends the auto-select sentinel (no
 * project fixed - Mewbo picks one, and may move the session between projects mid-run), and anything
 * else is a catalogue key (bare name or `managed:<id>`). The sentinel needs no arm of its own here
 * BY DESIGN: it travels as an ordinary non-blank string, so the resolver on the far side owns the
 * whole meaning of it and this side cannot develop a second opinion about what "auto" resolves to.
 */
internal fun buildSessionContext(
    model: String?,
    project: String?,
    mcpTools: List<String>?,
    deviceTools: List<DeviceToolDefinition>? = null,
): JsonObject = buildJsonObject {
    put("client", JsonPrimitive("aura-android"))
    if (!model.isNullOrBlank()) put("model", JsonPrimitive(model))
    if (!project.isNullOrBlank()) put("project", JsonPrimitive(project))
    if (!mcpTools.isNullOrEmpty()) put("mcp_tools", JsonArray(mcpTools.map { JsonPrimitive(it) }))
    if (!deviceTools.isNullOrEmpty()) put("device_tools", JsonArray(deviceTools.map { it.toContextEntry() }))
}
