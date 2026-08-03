package com.mewbo.aura.data.model

/**
 * The ONE authority for "this tool_result becomes its own top-level [ChatItem.ToolCard] instead of
 * folding into the turn's [ChatItem.ToolCallGroup]" (Gemini-style action-card promotion). Membership
 * here is a transcript-STRUCTURE decision made in data/ - which items exist - never a presentation
 * decision; which composable draws a given [ChatItem.ToolCard] is ui/'s renderer registry to own.
 * Adding an id here with no matching ui/ renderer is safe: ui/ falls back to a generic card for any
 * unrecognized [ToolCall.toolId], so this allowlist and ui/'s registry can evolve independently.
 */
object PromotedTools {
    /**
     * `switch_project` earns its place beside the three device actions for the same reason they
     * did: it is a state CHANGE the user has to be able to see. It re-points the working directory
     * of the whole session, so every relative path, shell command and file edit after it - and
     * every sub-agent spawned after it - resolves somewhere else. Buried inside a collapsed
     * "Used N tools" fold, the one event that explains why the rest of the turn is operating on
     * different files is the one event the user never sees.
     */
    val IDS: Set<String> = setOf("device_set_alarm", "device_set_timer", "device_send_sms", "switch_project")

    fun isPromoted(toolId: String): Boolean = toolId in IDS
}
