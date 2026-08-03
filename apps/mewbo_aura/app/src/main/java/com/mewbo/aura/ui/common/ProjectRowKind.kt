package com.mewbo.aura.ui.common

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import com.mewbo.aura.data.model.ComposerScope
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.theme.AuraColors

/**
 * What a project-picker row IS, and therefore how it is marked. The two pickers - the app-wide
 * default (`ui/settings/ProjectPickerSheet`) and the per-chat one (`ui/composer/ComposerOptionsSheet`)
 * - render structurally different lists over the same three kinds, and each used to carry its own
 * copy of the glyph-and-tint decision. One copy drifting from the other is a real risk rather than a
 * theoretical one: they already sit in different packages and are edited for different reasons.
 *
 * The glyph and tint are MEMBERS, not a `when` at each call site, for the same reason: a fourth kind
 * costs one row here and nothing anywhere else.
 */
enum class ProjectRowKind(val glyph: ImageVector, val tint: Color) {

    /** A real, saved project - operator-configured or Mewbo-managed. */
    Saved(ChatIcons.ProjectScope, AuraColors.scopeProject),

    /** The ephemeral temp-dir cwd. Muted: it is a throwaway scratch context, not one of your
     * projects. Its clock glyph is deliberately not a trash/auto-delete one, which next to a
     * selectable row misreads as "tap to delete". */
    Temporary(ChatIcons.TemporaryProjectScope, AuraColors.textSecondary),

    /** Auto-select ([ComposerScope.AUTO_PROJECT_KEY]): no project is fixed and Mewbo picks one,
     * moving the session between projects as the task needs. Wears the PROJECT tint rather than
     * Temporary's muted one - a session in auto mode ends up in a real project, it just is not in
     * one yet. */
    Auto(ChatIcons.AutoProjectScope, AuraColors.scopeProject),

    ;

    companion object {
        /** The kind a stored `context.project` value denotes. Blank is Temporary (the wire omits
         * the field entirely), the sentinel is Auto, anything else names a project. */
        fun of(projectKey: String?): ProjectRowKind = when {
            projectKey.isNullOrBlank() -> Temporary
            projectKey == ComposerScope.AUTO_PROJECT_KEY -> Auto
            else -> Saved
        }
    }
}

/** The auto-select row's label, shared verbatim by both pickers - two spellings of the same mode
 * would read as two different modes. */
internal const val AutoRowLabel = "Auto"

/** The auto-select row's supporting line. States who picks and that the pick is per-task, which is
 * the part a user cannot infer from the word "Auto" alone. */
internal const val AutoRowCaption = "Mewbo picks the project for each task"
