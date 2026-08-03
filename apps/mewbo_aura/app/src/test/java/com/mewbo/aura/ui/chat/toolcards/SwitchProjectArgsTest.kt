package com.mewbo.aura.ui.chat.toolcards

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [SwitchProjectArgs] is the whole non-Compose half of `SwitchProjectToolCard`.
 *
 * The RESULT is the contract here, not the args: `switch_project` returns a named JSON shape with
 * fixed keys (mirrored by `SwitchProjectResult`), so this suite reads it as a schema rather than
 * matching a sentence. What it has to prove is that the parse is TOTAL over everything a wire can
 * deliver - a truncated payload, a shape from a newer backend, and above all a REFUSAL, which must
 * never render as a successful move.
 */
class SwitchProjectArgsTest {

    private fun parse(result: String?, args: String = """{"project":"acme/beacon"}""") =
        SwitchProjectArgs.parse(Json.parseToJsonElement(args), result)

    /** The tool's own result shape (`SwitchProjectTool._result`), every key present. */
    private val fullResult = """
        {
          "project": "acme/beacon",
          "name": "beacon",
          "kind": "repository",
          "cwd": "/srv/acme/beacon",
          "repo": "git.example.com/acme/beacon",
          "branch": "main",
          "description": "Telemetry ingest service",
          "previous_project": "assistant",
          "previous_cwd": "/srv/projects/assistant",
          "project_instructions_found": true,
          "bound_tools": 42,
          "skills": 3,
          "summary": "Switched to project 'acme/beacon' (repository)."
        }
    """.trimIndent()

    // --- the happy path: named keys, rendered as three rows ---

    @Test
    fun `reads the named keys off the tool's result shape`() {
        val args = parse(fullResult)

        assertEquals("acme/beacon", args?.projectKey)
        assertEquals("beacon", args?.displayName)
        assertEquals("/srv/acme/beacon", args?.directory)
        assertEquals("git.example.com/acme/beacon", args?.repo)
        assertEquals("main", args?.branch)
        assertEquals("assistant", args?.movedFrom)
    }

    @Test
    fun `the meta row is repo, branch and where it moved from, middot-joined`() {
        assertEquals("git.example.com/acme/beacon · main · from assistant", parse(fullResult)?.metaLine)
    }

    @Test
    fun `the human NAME is what the card shows, never a managed uuid key`() {
        val result = """{"project":"managed:9f3a1c","name":"scratch-worktree","cwd":"/srv/wt/9f3a"}"""

        val args = parse(result, args = """{"project":"managed:9f3a1c"}""")

        assertEquals("managed:9f3a1c", args?.projectKey)
        assertEquals("scratch-worktree", args?.displayName)
    }

    @Test
    fun `a blank name falls back to the key rather than rendering an empty line`() {
        // The catalogue sends "" rather than null for a name it does not have.
        assertEquals("acme/beacon", parse("""{"project":"acme/beacon","name":"","cwd":"/srv/x"}""")?.displayName)
    }

    @Test
    fun `the RESULT's project key wins over the argument - the resolver settles it, not the model`() {
        val result = """{"project":"acme/beacon","cwd":"/srv/acme/beacon"}"""

        assertEquals("acme/beacon", parse(result, args = """{"project":"  acme/beacon  "}""")?.projectKey)
    }

    @Test
    fun `a result that omits its own key falls back to the argument`() {
        assertEquals("acme/beacon", parse("""{"cwd":"/srv/acme/beacon"}""")?.projectKey)
    }

    // --- previous_project is null on a session's FIRST switch; previous_cwd always is not ---

    @Test
    fun `the first switch of a session reports the directory it came from, not a key`() {
        // Honest, not a gap: the loop is handed a directory at construction, never a catalogue key,
        // so before the first switch there IS no previous project key to report.
        val result = """
            {"project":"acme/beacon","name":"beacon","cwd":"/srv/acme/beacon",
             "previous_project":null,"previous_cwd":"/tmp/mewbo-session-4f21"}
        """.trimIndent()

        assertEquals("/tmp/mewbo-session-4f21", parse(result)?.movedFrom)
        assertEquals("from /tmp/mewbo-session-4f21", parse(result)?.metaLine)
    }

    @Test
    fun `a later switch prefers the previous project KEY over its directory`() {
        val result = """
            {"project":"acme/beacon","cwd":"/srv/acme/beacon",
             "previous_project":"assistant","previous_cwd":"/srv/projects/assistant"}
        """.trimIndent()

        assertEquals("assistant", parse(result)?.movedFrom)
    }

    // --- absent facts are dropped, never rendered as empty rows ---

    @Test
    fun `a configured project with no repo, branch or origin renders the directory alone`() {
        val result = """
            {"project":"notes","name":"notes","kind":"configured","cwd":"/srv/projects/notes",
             "repo":null,"branch":null,"previous_project":null,"previous_cwd":null}
        """.trimIndent()

        val args = parse(result, args = """{"project":"notes"}""")

        assertEquals("/srv/projects/notes", args?.directory)
        assertNull(args?.repo)
        assertNull(args?.branch)
        assertNull("no repo, no branch, nowhere to have come from ⇒ no meta row at all", args?.metaLine)
    }

    @Test
    fun `a worktree reports its branch without a repo slug`() {
        val result = """
            {"project":"managed:9f3a","name":"login-fix","kind":"worktree","cwd":"/srv/wt/9f3a",
             "repo":null,"branch":"feat/login","previous_project":"assistant"}
        """.trimIndent()

        assertEquals("feat/login · from assistant", parse(result, args = """{"project":"managed:9f3a"}""")?.metaLine)
    }

    // --- a REFUSAL must never render as a successful move ---

    @Test
    fun `the error envelope is refused, whichever way it is spelled`() {
        // Today a refusal arrives as the Python-repr envelope the loop's reclassifier reads, which
        // is not JSON and fails the decode. The explicit `error`-key guard is the belt to that
        // brace: it holds if the envelope is ever respelled as real JSON.
        assertNull(parse("""{'error': {'code': 'not_found', 'message': "Project 'nope' is not known."}}"""))
        assertNull(parse("""{"error":{"code":"not_found","message":"Project 'nope' is not known."}}"""))
        assertNull(parse("""{"error":{"code":"auto_sentinel","message":"'auto' is not a project"}}"""))
        assertNull(parse("""{"error":{"code":"rebind_failed","message":"You are still in the previous project."}}"""))
    }

    @Test
    fun `a refusal is refused even when the args name a perfectly good project`() {
        // The args say what the model ASKED for. Rendering them for a call that was refused is
        // exactly the card lying about the session's state.
        assertNull(parse("""{"error":{"code":"no_checkout","message":"registered but has no local checkout"}}"""))
    }

    // --- totality: every shape a wire can deliver ---

    @Test
    fun `a missing or empty result degrades - an unread outcome is not a rendered one`() {
        assertNull(parse(null))
        assertNull(parse(""))
        assertNull(parse("   "))
    }

    @Test
    fun `a truncated result degrades rather than throwing`() {
        // The result is capped server-side; a cut-off payload is invalid JSON.
        assertNull(parse("""{"project":"acme/beacon","cwd":"/srv/acme/be"""))
    }

    @Test
    fun `every other malformed shape degrades too`() {
        assertNull(parse("""[]"""))                                  // an array, not an object
        assertNull(parse(""""Switched to acme/beacon""""))           // a bare string (the OLD prose result)
        assertNull(parse("""null"""))                                // JSON null
        assertNull(parse("""not json at all"""))                     // not JSON
        assertNull(parse("""{}""", args = """{}"""))                 // no key anywhere
        assertNull(parse("""{"project":null}""", args = """{}"""))   // null key, no argument to fall back to
        assertNull(parse("""{"project":"   "}""", args = """{}"""))  // blank key
    }

    @Test
    fun `a wrongly-typed field degrades instead of half-rendering`() {
        // kotlinx.serialization throws on a type mismatch; the parse must absorb it.
        assertNull(parse("""{"project":"acme/beacon","cwd":{"path":"/srv/x"}}"""))
        assertNull(parse("""{"project":["acme/beacon"],"cwd":"/srv/x"}"""))
        assertNull(parse("""{"project":"acme/beacon","bound_tools":"lots"}"""))
    }

    @Test
    fun `a key the tool adds later is additive, not a decode failure`() {
        // ignoreUnknownKeys: a newer backend must not silently drop every card to the generic one.
        val result = """{"project":"acme/beacon","name":"beacon","cwd":"/srv/acme/beacon","container_id":"c-77"}"""

        assertEquals("/srv/acme/beacon", parse(result)?.directory)
    }

    @Test
    fun `malformed ARGS are survivable as long as the result reads`() {
        // The args are the fallback, not the contract - a nested object where a string belongs must
        // not throw on the way past.
        assertEquals("acme/beacon", SwitchProjectArgs.parse(null, fullResult)?.projectKey)
        assertEquals("acme/beacon", parse(fullResult, args = """{"project":{"key":"x"}}""")?.projectKey)
        assertEquals("acme/beacon", parse(fullResult, args = """[]""")?.projectKey)
    }
}
