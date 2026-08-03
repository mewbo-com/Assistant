package com.mewbo.aura.ui.settings

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import com.mewbo.aura.data.model.ComposerScope
import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.ui.common.AuraListBottomSheet
import com.mewbo.aura.ui.common.AutoRowCaption
import com.mewbo.aura.ui.common.AutoRowLabel
import com.mewbo.aura.ui.common.ProjectRowKind
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * Default-project picker - mirrors [com.mewbo.aura.ui.chat.ModelPickerSheet]'s
 * anatomy (`surfaceInput` container, `radiusBubble` top corners, 56dp rows, trailing
 * `accentPrimary` check on selected).
 *
 * Two SYNTHETIC rows lead, above the divider that sets them apart from real, saved projects:
 * "Temporary directory" (empty [selectedKey]) and "Auto" ([ComposerScope.AUTO_PROJECT_KEY]).
 * Neither names a directory the operator saved - one is a throwaway cwd, the other defers the
 * choice to Mewbo - so they share a group, and Temporary keeps the leading position it has always
 * had rather than being displaced by the newcomer.
 *
 * `projects == null` (offline/error, [SettingsViewModel.loadProjectsIfNeeded] already fired its own
 * notice before this ever opens) degrades to a single current-selection row, same shape as
 * [com.mewbo.aura.ui.chat.ModelPickerSheet]'s own offline degrade - never crashes.
 */
@Composable
fun ProjectPickerSheet(
    projects: List<ProjectSummary>?,
    selectedKey: String,
    onSelect: (String) -> Unit,
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
) {
    AuraListBottomSheet(onDismiss = onDismiss, modifier = modifier) {
        if (projects == null) {
            item {
                ProjectPickerRow(
                    label = when {
                        selectedKey.isBlank() -> "Temporary directory"
                        selectedKey == ComposerScope.AUTO_PROJECT_KEY -> AutoRowLabel
                        else -> selectedKey
                    },
                    kind = ProjectRowKind.of(selectedKey),
                    selected = true,
                    onClick = onDismiss,
                )
            }
        } else {
            item {
                ProjectPickerRow(
                    label = "Temporary directory",
                    kind = ProjectRowKind.Temporary,
                    selected = selectedKey.isBlank(),
                    onClick = { onSelect("") },
                )
            }
            item {
                ProjectPickerRow(
                    label = AutoRowLabel,
                    caption = AutoRowCaption,
                    kind = ProjectRowKind.Auto,
                    selected = selectedKey == ComposerScope.AUTO_PROJECT_KEY,
                    onClick = { onSelect(ComposerScope.AUTO_PROJECT_KEY) },
                )
            }
            item {
                // The two synthetic rows above are set apart from the real, saved projects below by
                // a divider: neither is one of your projects - one is a throwaway scratch context,
                // the other defers the choice to Mewbo.
                HorizontalDivider(color = AuraColors.outlineHairline)
            }
            items(projects, key = { it.contextKey }) { project ->
                ProjectPickerRow(
                    label = project.name,
                    kind = ProjectRowKind.Saved,
                    selected = project.contextKey == selectedKey,
                    onClick = { onSelect(project.contextKey) },
                )
            }
        }
    }
}

@Composable
private fun ProjectPickerRow(
    label: String,
    kind: ProjectRowKind,
    selected: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    caption: String? = null,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        // Leading scope glyph + tint come from the row's KIND ([ProjectRowKind]), shared with the
        // composer's own picker. Filled slot on every row, so this is NOT the §7.13 empty-icon-slot
        // indent regression (that was an EMPTY reserved box).
        Icon(
            imageVector = kind.glyph,
            contentDescription = null,
            tint = kind.tint,
            modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
        )
        Spacer(modifier = Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
        Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.Center) {
            Text(text = label, style = AuraType.listItem, color = AuraColors.textPrimary)
            // A caption only where the label alone can't carry the meaning (the Auto row) - the
            // DrillInRow label+caption shape, not a second line on every row.
            caption?.let { Text(text = it, style = AuraType.caption) }
        }
        if (selected) {
            Icon(
                imageVector = Icons.Filled.Check,
                contentDescription = "Selected",
                tint = AuraColors.accentPrimary,
                modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
            )
        }
    }
}
