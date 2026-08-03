package com.mewbo.aura.ui.chat

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
import androidx.compose.material.icons.filled.AutoAwesome
import androidx.compose.material.icons.filled.Check
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.painterResource
import com.mewbo.aura.data.model.ModelCatalog
import com.mewbo.aura.ui.common.AuraListBottomSheet
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * Model picker (task brief W1-A): [ModelCatalog.popular]'s pinned rows always visible, everything
 * else ([ModelCatalog.more]) behind an expanding "More models" row (spec anatomy per the reference
 * plus-menu/tools-sheet captures: drag handle, ~56dp rows, icon/label rhythm - reused here as
 * [AuraSpacing.DrawerRow] tokens rather than inventing new ones).
 *
 * `models == null` (offline/error - [ChatScreen] already fired the "Couldn't load models" notice
 * via [ChatViewModel.loadModelsIfNeeded] before this ever opens) degrades to a single
 * current-selection row instead of a list; never crashes.
 *
 * Rides [AuraListBottomSheet], not a bare `Column`: [ModelCatalog.more] carries whatever
 * `GET api/models` returned, with no client-side ceiling, so this is the app's canonical
 * unbounded sheet. The expanded list is the case that used to render past the display with every
 * row below the fold unreachable — and, with no scroll container to claim the drag past touch
 * slop, a scroll ATTEMPT would land on a row and silently switch the user's model.
 *
 * The expand/collapse `AnimatedVisibility` that used to wrap the extra rows is gone rather than
 * ported: inside a lazy list it would animate each row independently on expand, and the
 * per-item alternative introduces motion that would then have to be reduced-motion-gated. The
 * rows appear directly.
 */
@Composable
fun ModelPickerSheet(
    models: ModelCatalog?,
    selectedModel: String?,
    onSelect: (String?) -> Unit,
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var moreExpanded by remember { mutableStateOf(false) }
    val more = models?.more().orEmpty()

    AuraListBottomSheet(onDismiss = onDismiss, modifier = modifier) {
        if (models == null) {
            item {
                ModelRow(
                    id = selectedModel ?: "Core",
                    label = selectedModel?.let(ModelCatalog::normalize) ?: "Core",
                    selected = true,
                    onClick = onDismiss,
                )
            }
        } else {
            items(models.popular(), key = { it }) { id ->
                ModelRow(
                    id = id,
                    label = models.displayName(id),
                    selected = models.isSelected(id, selectedModel),
                    onClick = { onSelect(id) },
                )
            }

            if (more.isNotEmpty()) {
                item(key = MoreModelsRowKey) {
                    MoreModelsRow(expanded = moreExpanded, onClick = { moreExpanded = !moreExpanded })
                }
                if (moreExpanded) {
                    items(more, key = { it }) { id ->
                        ModelRow(
                            id = id,
                            label = models.displayName(id),
                            selected = models.isSelected(id, selectedModel),
                            onClick = { onSelect(id) },
                        )
                    }
                }
            }
        }
    }
}

/** Stable key for the More/Fewer toggle so it keeps its slot as the list below it grows and
 * shrinks — a model id can never collide with it. */
private const val MoreModelsRowKey = "more-models-toggle"

@Composable
private fun ModelRow(id: String, label: String, selected: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        val providerIconRes = ModelProviderIcons.iconFor(id)
        val providerLabel = ModelProviderIcons.providerNameFor(id)
        if (providerIconRes != null) {
            Icon(
                painter = painterResource(providerIconRes),
                contentDescription = providerLabel,
                tint = AuraColors.iconPrimary,
                modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
            )
        } else {
            // No brand match (unrecognized/self-hosted model id) - material-icons-extended's
            // generic "spark" glyph, not a hand-drawn fallback (orchestrator directive).
            Icon(
                imageVector = Icons.Filled.AutoAwesome,
                contentDescription = providerLabel,
                tint = AuraColors.iconPrimary,
                modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
            )
        }
        Spacer(Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
        Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.Center) {
            Text(text = label, style = AuraType.listItem, color = AuraColors.textPrimary)
            Text(text = id, style = AuraType.caption, color = AuraColors.textSecondary)
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

@Composable
private fun MoreModelsRow(expanded: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.DrawerRow.sectionHeaderTopPad / 2),
    ) {
        Text(text = if (expanded) "Fewer models" else "More models", style = AuraType.sectionHeader)
    }
}
