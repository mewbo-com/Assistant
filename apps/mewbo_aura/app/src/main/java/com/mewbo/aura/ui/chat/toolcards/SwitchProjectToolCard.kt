package com.mewbo.aura.ui.chat.toolcards

import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import com.mewbo.aura.data.model.ToolCall
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * `switch_project` - the card for the one call that moves the session's working directory.
 *
 * Three rows at most: the project's name, the directory the session is now in, and one quiet meta
 * line carrying repo, branch and where it moved FROM. The copy states the move and stops there; it
 * does not narrate what the move implies - the turn's own prose renders right underneath, and this
 * card is never the whole answer ([ToolActionCard]'s KDoc).
 *
 * A result the card cannot read degrades to [GenericToolCard], same as [AlarmToolCard].
 */
@Composable
fun SwitchProjectToolCard(call: ToolCall, modifier: Modifier = Modifier) {
    val args = remember(call.inputJson, call.detail) { SwitchProjectArgs.parse(call.inputJson, call.detail) }
    if (args == null) {
        GenericToolCard(call = call, modifier = modifier)
        return
    }

    ToolActionCard(icon = ChatIcons.ProjectScope, label = "Project", modifier = modifier) {
        Text(
            text = args.displayName,
            style = AuraType.toolCardDisplay,
            color = AuraColors.textPrimary,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        args.directory?.let { directory ->
            Text(
                text = directory,
                style = AuraType.bodyMessage,
                color = AuraColors.textSecondary,
                maxLines = 1,
                // A path is identified by its TAIL - the leaf directory is what two sibling
                // worktrees differ by, and an end-ellipsis would cut off exactly that.
                overflow = TextOverflow.MiddleEllipsis,
                modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
            )
        }
        args.metaLine?.let { meta ->
            Text(
                text = meta,
                style = AuraType.caption,
                color = AuraColors.textSecondary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
            )
        }
    }
}

/**
 * `switch_project`'s displayable content, kept pure so it is unit-testable without a device
 * (`SwitchProjectArgsTest`) - the whole non-Compose half of [SwitchProjectToolCard].
 *
 * **The RESULT is the contract, not the args.** The tool returns a named JSON shape
 * ([SwitchProjectResult]) whose keys are ALWAYS present - `null` when unknown rather than omitted -
 * precisely so a renderer branches on VALUES and never on key existence, and never has to read a
 * human sentence. The tool's prose lives in that shape's own `summary` field, and this card
 * deliberately does not echo it: the card's job is the structure, the turn's narration underneath is
 * the prose.
 *
 * Four things that are easy to get wrong here:
 * - **`cwd`, not `path`.** `path` is `list_projects`' word for a catalogue ENTRY, one kind of which
 *   (a registered repository with no checkout) legitimately has none. `cwd` is where the agent IS.
 * - **`previous_project` is `null` on a session's FIRST switch, and that is honest rather than a
 *   gap to paper over** - the loop is handed a directory at construction, never a catalogue key, so
 *   before the first switch there is no key to report. `previous_cwd` is always populated, so
 *   [movedFrom] falls back to it.
 * - **A result this cannot read returns `null`** (⇒ `GenericToolCard`) rather than being
 *   half-rendered from the args. Now that the result is a contract, an unreadable one means the card
 *   does not know what happened, and a confident "Project: X" for an outcome it could not read is
 *   the card guessing.
 * - **A refused switch must never render as a successful one.** Two independent guards: the reducer
 *   only promotes a call whose `success` is true (the failure path the loop's own error-envelope
 *   reclassification drives), and [parse] additionally refuses any payload carrying an `error` key.
 *   Belt and braces on purpose - a refusal is currently spelled as a Python-repr envelope
 *   (`{'error': …}`, single-quoted) which is not JSON and fails the decode anyway, but that is a
 *   property of today's SPELLING and not something a card should stake correctness on.
 */
internal data class SwitchProjectArgs(
    val projectKey: String,
    val displayName: String,
    val directory: String?,
    val repo: String?,
    val branch: String?,
    val movedFrom: String?,
) {

    /**
     * The single quiet meta row under the directory: repo, branch, and where the session moved from,
     * middot-joined (the composer scope row's own facet idiom). `null` when none of the three is
     * known, so the card renders two rows rather than an empty third.
     */
    val metaLine: String?
        get() = listOfNotNull(repo, branch, movedFrom?.let { "from $it" })
            .takeIf { it.isNotEmpty() }
            ?.joinToString(" · ")

    companion object {
        fun parse(input: JsonElement?, result: String?): SwitchProjectArgs? {
            val decoded = decodeResult(result) ?: return null
            // The result's own key wins over the argument: the args are what the model ASKED for,
            // the result is what the resolver actually settled on.
            val key = (decoded.project ?: argumentProjectKey(input)).clean() ?: return null
            return SwitchProjectArgs(
                projectKey = key,
                // `name` is what a human calls it; a key can be `managed:<uuid>`, which is an
                // identifier and reads as noise on a card.
                displayName = decoded.name.clean() ?: key,
                directory = decoded.cwd.clean(),
                repo = decoded.repo.clean(),
                branch = decoded.branch.clean(),
                movedFrom = decoded.previousProject.clean() ?: decoded.previousCwd.clean(),
            )
        }

        /**
         * The result text as the named shape, or `null` for anything this cannot trust: absent,
         * truncated, not a JSON object, or an error envelope. TOTAL - a tool result is model-facing
         * output that crossed a wire, so a decode that threw would take the transcript down.
         */
        private fun decodeResult(result: String?): SwitchProjectResult? {
            val text = result?.trim()?.takeIf { it.isNotEmpty() } ?: return null
            val payload = runCatching { Decoder.parseToJsonElement(text) }.getOrNull() as? JsonObject ?: return null
            if (payload.containsKey(ErrorKey)) return null
            return runCatching { Decoder.decodeFromJsonElement(SwitchProjectResult.serializer(), payload) }.getOrNull()
        }

        /** The `project` ARGUMENT, for a result that omitted its own key. `as? JsonPrimitive`, never
         * the `jsonPrimitive` accessor: that one THROWS on a nested object/array. */
        private fun argumentProjectKey(input: JsonElement?): String? {
            val obj = input as? JsonObject ?: return null
            val primitive = obj["project"] as? JsonPrimitive ?: return null
            // isString rejects a bare number/boolean, which would otherwise render as a "key".
            return if (primitive.isString) primitive.content else null
        }

        /** Blank is absent. `name`/`description` arrive as `""` rather than `null` when the catalogue
         * has none, so a bare null check would render an empty row instead of no row. */
        private fun String?.clean(): String? = this?.trim()?.takeIf { it.isNotEmpty() }

        private const val ErrorKey = "error"

        /** `ignoreUnknownKeys` so a key the tool adds later is additive, rather than a decode failure
         * that would silently drop every card back to the generic one. */
        private val Decoder = Json { ignoreUnknownKeys = true }
    }
}

/**
 * `switch_project`'s result shape, mirroring `mewbo_core`'s `SwitchProjectTool._result`
 * field-for-field. Every field is DEFAULTED so a payload missing one decodes rather than throwing:
 * the shape promises all keys are present, and this type does not stake the card on that promise.
 *
 * `kind`, `description`, `project_instructions_found`, `bound_tools`, `skills` and `summary` are
 * decoded but deliberately unrendered - `summary` is prose written for the MODEL (the turn's own
 * narration is the user's prose), and the rest are facts about the workspace rather than about the
 * move. They are declared anyway so the mirror stays a mirror and the next card change needs no wire
 * archaeology.
 */
@Serializable
private data class SwitchProjectResult(
    val project: String? = null,
    val name: String? = null,
    val kind: String? = null,
    val cwd: String? = null,
    val repo: String? = null,
    val branch: String? = null,
    val description: String? = null,
    @SerialName("previous_project") val previousProject: String? = null,
    @SerialName("previous_cwd") val previousCwd: String? = null,
    @SerialName("project_instructions_found") val projectInstructionsFound: Boolean = false,
    @SerialName("bound_tools") val boundTools: Int = 0,
    val skills: Int = 0,
    val summary: String? = null,
)
