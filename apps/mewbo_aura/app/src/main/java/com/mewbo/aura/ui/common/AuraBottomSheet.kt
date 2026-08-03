package com.mewbo.aura.ui.common

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.mewbo.aura.ui.theme.AuraColors

/*
 * The ONE `ModalBottomSheet` container in the app. Nothing else may call `ModalBottomSheet`
 * directly — `SheetContainerContractTest` fails the build if it does.
 *
 * Why this exists, when the chrome next door in `ActionSheet` deliberately did NOT wrap the
 * container: that earlier call was made on the reasoning that a shared container "would only be a
 * lambda pass-through". It was wrong, and five sheets grew the same defect independently.
 * `ModalBottomSheet`'s content slot is a `ColumnScope` that caps nothing and scrolls nothing. A
 * plain `Column` dropped into it lays its children out at full intrinsic height, so once content
 * outgrew the sheet the rows past the fold were still laid out — just clipped off-screen and
 * permanently unreachable. With no scrollable child to claim the drag, a vertical swipe fell
 * through to the sheet's own anchored-drag and translated the whole card instead of scrolling it;
 * and because nothing consumed that gesture past touch slop, a scroll ATTEMPT landed on a row's
 * `clickable` and silently committed a different selection. Bounding, scrolling and system-bar
 * insets are shared, load-bearing behavior — not pass-through. They live here now.
 *
 * Two entry points over one private frame, because the safe shape differs with the content:
 * `AuraBottomSheet` for content of bounded, known size, `AuraListBottomSheet` for anything driven
 * by backend data. Picking the list door for a list is the whole discipline — a `LazyColumn` must
 * never be nested inside `AuraBottomSheet`'s scrolling `Column` (an infinite height constraint),
 * which is why the list case gets its own entry point rather than a caller-supplied modifier.
 */

/**
 * Sheet frame: owns the sheet state, the top-corners-only [SheetShape], the container fill, and the
 * two insets that keep the card inside the display.
 *
 * The height cap is `Modifier.weight(1f, fill = false)`, applied by each entry point, NOT a dp
 * ceiling: the content slot already arrives bounded to the sheet's own height, so weight resolves
 * against real space while `fill = false` lets a SHORT sheet stay short. A `heightIn(max = …)`
 * computed off the window would need a token, would fight the sheet's partial-expand anchor, and
 * still would not make short content wrap.
 *
 * **Both system-bar insets are spent as MODIFIERS, and the sheet's own `contentWindowInsets` is
 * zeroed** so there is exactly one place each inset is applied. `statusBarsPadding()` sits on the
 * sheet itself, so a full-height sheet's FILL and its rounded top corners stop below the clock
 * rather than painting behind it. `navigationBarsPadding()` sits on the scrolling child, which ends
 * the scroll VIEWPORT above the gesture bar — deliberately chosen over `contentPadding`, which
 * would let rows travel behind an opaque gesture bar on the way past. The card ends above the
 * display perimeter on both edges; nothing is ever drawn under a system bar.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SheetFrame(
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.() -> Unit,
) {
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(),
        containerColor = AuraColors.surfaceInput,
        shape = SheetShape,
        contentWindowInsets = { WindowInsets(0, 0, 0, 0) },
        modifier = modifier.statusBarsPadding(),
        content = content,
    )
}

/**
 * A bottom sheet whose content is of bounded, known size — action rows, a header, a small pane
 * machine. It still scrolls if that content outgrows the display (a long header, a large font
 * scale, a landscape window), so "bounded" never means "allowed to overflow".
 *
 * Use [AuraListBottomSheet] instead whenever the rows come from backend data.
 */
@Composable
fun AuraBottomSheet(
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.() -> Unit,
) {
    SheetFrame(onDismiss = onDismiss, modifier = modifier) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f, fill = false)
                .navigationBarsPadding()
                .verticalScroll(rememberScrollState()),
            content = content,
        )
    }
}

/**
 * A bottom sheet over a data-driven list. The `LazyColumn` both bounds the sheet and consumes the
 * vertical drag as scroll — which is what keeps a scroll gesture from ever reaching a row's
 * `clickable`.
 *
 * [header] is an OPTIONAL pinned band above the list: it stays put while the list scrolls under it.
 * It exists for a drill-in pane whose back/refresh affordances must remain reachable no matter how
 * far the list has travelled (`ComposerOptionsSheet`'s Project/Tools panes) — a header placed in
 * the list instead would scroll away with the first row. It sits ABOVE the scroll viewport, so it
 * is not part of what scrolls and must stay small; anything list-shaped belongs in [content].
 */
@Composable
fun AuraListBottomSheet(
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
    header: @Composable (ColumnScope.() -> Unit)? = null,
    content: LazyListScope.() -> Unit,
) {
    SheetFrame(onDismiss = onDismiss, modifier = modifier) {
        header?.invoke(this)
        LazyColumn(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f, fill = false)
                .navigationBarsPadding(),
            content = content,
        )
    }
}
