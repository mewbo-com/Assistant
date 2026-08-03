package com.mewbo.aura.ui.common

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins the fix for a regression that shipped five separate times: `ModalBottomSheet`'s content
 * slot is a bare `ColumnScope` with no height cap and no scrolling, so a caller that drops a plain
 * `Column` into it silently clips overflow content off-screen, lets a vertical drag fall through to
 * the sheet's own `AnchoredDraggable` instead of a scroll, and skips the navigationBars/statusBars
 * inset the sheet needs to clear the system bars. Every sheet in the app re-derived its own
 * container from scratch and got some subset of that wrong — the bug was never in any one sheet's
 * own logic, it was in there being five independent containers at all.
 *
 * This module has no `androidTest` source set, no Compose UI-test dependency, and no emulator in
 * CI (`app/build.gradle.kts`'s test deps are plain JUnit4 + coroutines-test + turbine +
 * mockito-core), so nothing here can assert that a sheet actually scrolls, clips, or insets
 * correctly on a real layout pass. What this test asserts instead is the architectural invariant
 * that makes the bug unrepeatable: exactly one file is allowed to call `ModalBottomSheet(` directly
 * (the shared container in `ui/common/`) — every other sheet must go through it. A future sheet
 * that reaches for `ModalBottomSheet` directly, the exact shortcut that produced this bug five
 * times, fails the build here instead of shipping a sixth broken sheet.
 */
class SheetContainerContractTest {

    @Test
    fun `only the shared sheet container calls ModalBottomSheet directly`() {
        val sourceRoot = findSourceRoot()
        val ktFiles = sourceRoot.walkTopDown().filter { it.isFile && it.extension == "kt" }.toList()

        // A tree-walk that silently finds zero (or a suspiciously small number of) files is worse
        // than useless — it would report "no violations" and pass for the wrong reason. Fail loudly
        // instead of trusting an unverified working directory.
        assertTrue(
            "Expected well over $MIN_PLAUSIBLE_KT_FILE_COUNT Kotlin source files under $sourceRoot, " +
                "found only ${ktFiles.size}. The source-root resolution is almost certainly wrong " +
                "(started the walk-up from ${File(".").absoluteFile}) — fix findSourceRoot() before " +
                "trusting anything else this test reports.",
            ktFiles.size > MIN_PLAUSIBLE_KT_FILE_COUNT,
        )

        val allowedContainer = File(sourceRoot, ALLOWED_CONTAINER_RELATIVE_PATH).canonicalFile
        val offenders = ktFiles
            .filterNot { it.canonicalFile == allowedContainer }
            .filter { file -> file.readLines().any { line -> line.substringBefore("//").contains(CALL_MARKER) } }
            .sortedBy { it.path }

        assertTrue(
            buildString {
                appendLine(
                    "${offenders.size} file(s) call `$CALL_MARKER` directly instead of going through " +
                        "the shared sheet container. Use `AuraBottomSheet`/`AuraListBottomSheet` from " +
                        "`ui/common/` instead — a hand-rolled `ModalBottomSheet` call re-opens the " +
                        "unbounded-height / no-scroll / no-system-bar-inset regression this test exists " +
                        "to catch. Offending file(s):",
                )
                offenders.forEach { appendLine("  - ${it.path}") }
            },
            offenders.isEmpty(),
        )
    }

    /**
     * The Gradle test task's working directory is the module dir (`app/`), so
     * [RELATIVE_SOURCE_ROOT] normally resolves directly. The walk-up is a fallback for any OTHER
     * working directory (a different IDE test runner, a future Gradle version) — this must always
     * either FIND the real source tree or fail loudly, never silently scan an empty/wrong one.
     */
    private fun findSourceRoot(): File {
        val direct = File(RELATIVE_SOURCE_ROOT)
        if (direct.isDirectory) return direct

        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val candidate = File(dir, RELATIVE_SOURCE_ROOT)
            if (candidate.isDirectory) return candidate
            dir = dir.parentFile
        }

        throw AssertionError(
            "Could not locate `$RELATIVE_SOURCE_ROOT` from the test working directory " +
                "(${File(".").absoluteFile}) or any ancestor directory. Fix findSourceRoot() rather " +
                "than letting this test silently scan zero files and pass for the wrong reason.",
        )
    }

    private companion object {
        const val RELATIVE_SOURCE_ROOT = "src/main/java/com/mewbo/aura"
        const val ALLOWED_CONTAINER_RELATIVE_PATH = "ui/common/AuraBottomSheet.kt"
        const val CALL_MARKER = "ModalBottomSheet("
        const val MIN_PLAUSIBLE_KT_FILE_COUNT = 50
    }
}
