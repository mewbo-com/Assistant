package com.mewbo.aura.ui.aurora

import android.graphics.RuntimeShader
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.State
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.graphics.ShaderBrush
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.unit.Dp
import com.mewbo.aura.ui.orb.GlslNoise
import com.mewbo.aura.ui.orb.rememberShaderTimeSeconds
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.GradientStop
import com.mewbo.aura.ui.theme.LocalAssistantExtras
import kotlin.math.PI
import kotlin.math.sin

// AGSL (RuntimeShader, API 33+). Bottom bloom: a bottom-anchored exponential falloff, no radial
// mask, no plateau - ignition grows [decayLength] from near-zero to its resting depth so every
// column lights at the same rate, and the continuous decay leaves no flat region for an opaque
// layer above it (the composer pill) to crop a line across. Dither (below,
// [GlslNoise.ditherPremul]) removes 8-bit banding. Hue is a two-stop blend by the bloom's own
// intensity, never by angle - no phase uniform to drift out of sync. Horizontal center-weighting
// and a slow x/time waviness (GlslNoise) keep it from reading as a flat static band.
//
// Listening reads WIDE (reach into the bottom corners); Thinking contracts to hug the pill with
// no corner reach. Both peak at the same bottom-edge position - only reach (decayLength) and
// corner suppression (centerWeight) differ, via [EdgeGlowUniformMath.decayDepthDp]/
// [EdgeGlowUniformMath.centerWeightStrength]. Decay length alone can't suppress corners (they sit
// on the same distFromBottom=0 row as the peak), so Thinking also raises centerWeight.
private val AURORA_EDGE_SHADER_SRC = """
uniform float2 iResolution;
uniform float iDecayLengthPx;
uniform float iIgniteProgress;
uniform float iIntensity;
uniform float iCenterWeight;
uniform float iTopFadeFraction;
uniform float iWaveAmplitude;
uniform float iWaveFreqX;
uniform float iWaveSpeedHz;
uniform float iTime;
uniform float iVisible;
uniform float iPerimeterBloom;
uniform float iPerimeterFloor;
uniform float iHueDrift;
uniform float3 iColorLight;
uniform float3 iColorDeep;
uniform float3 iAuroraLightB;
uniform float3 iAuroraDeepB;
uniform float3 iAuroraLightC;
uniform float3 iAuroraDeepC;

${GlslNoise.GLSL_CORE}

half4 main(float2 fragCoord) {
    float2 res = iResolution;

    // Low-frequency drift over (x, time) modulates the bloom's reach so it breathes rather than
    // reading as a frozen gradient. The noise x-argument is offset by iTime so the undulation
    // reads as one wave flowing across the screen, not shimmer in place; the second (coarse+fine)
    // octave breaks the silhouette so the boundary reads as liquid rather than one sine wave.
    // Both terms are iTime × rate - phase-continuous, never reset mid-state.
    float waveCoarse = valueNoise(float2(
        fragCoord.x * iWaveFreqX + iTime * iWaveSpeedHz,
        iTime * iWaveSpeedHz * 0.37)) - 0.5;
    float waveFine = valueNoise(float2(
        fragCoord.x * iWaveFreqX * 2.3 + 7.31 + iTime * iWaveSpeedHz * 1.9,
        iTime * iWaveSpeedHz * 0.71 + 3.17)) - 0.5;
    float wave = waveCoarse * 0.65 + waveFine * 0.35;
    float igniteFloor = 0.02; // keeps the decay length nonzero so exp() never divides by zero
    float decayLength = iDecayLengthPx * max(iIgniteProgress, igniteFloor) *
        (1.0 + wave * 2.0 * iWaveAmplitude);

    // Scaling decayLength by iIgniteProgress IS the whole ignition animation: at progress ~0 only
    // a thin sliver at the bottom edge is lit, widening to the resting reach - no radial mask, so
    // nothing crops against the corners at any progress value.
    float distFromBottom = max(res.y - fragCoord.y, 0.0);
    float verticalGlow = exp(-distFromBottom / max(decayLength, 0.5));

    // Horizontal center-weighting: brightest under/around the composer, fainter but never fully
    // zero toward the corners.
    float distFromCenterX = abs(fragCoord.x - res.x * 0.5);
    float horizontalWeight = 1.0 - iCenterWeight * smoothstep(0.0, res.x * 0.5, distFromCenterX);

    float vGlow = clamp(verticalGlow * horizontalWeight, 0.0, 1.0);

    // A large caller reachScale (the in-app chat's 5.5x) leaves the bottom-anchored falloff
    // clearly nonzero at the TOP draw bound, hard-stopping into a seam where the surface clips.
    // Feather to EXACTLY 0 over the top iTopFadeFraction of the surface height. Applies to the
    // BOTTOM-ANCHORED term ONLY - the perimeter terms below are edge-anchored and PEAK at the draw
    // bounds by construction, so there is no mid-falloff value for a clip edge to slice through.
    // No-op for the small-reach overlay.
    vGlow *= smoothstep(0.0, res.y * iTopFadeFraction, fragCoord.y);

    // Invocation perimeter bloom: per-row/per-column exponential falloffs from the side and top
    // edges (edge-anchored, no radial mask - same construction as the bottom term, rotated),
    // weighted by a bottom bias so the light reads as EMANATING from the pill. sideDecay derives
    // from decayLength, so the perimeter's inner boundary undulates with zero extra noise terms.
    // iPerimeterFloor keeps the perimeter edge-lit after iPerimeterBloom settles to 0 -
    // max(iPerimeterBloom, iPerimeterFloor) means the bloom wins whenever it's live. The per-row
    // sideLevel wave below makes the edge reach visibly rise and fall along the screen edges.
    float sideLevel = valueNoise(float2(
        fragCoord.y * iWaveFreqX * 3.5 + iTime * iWaveSpeedHz * 1.4,
        iTime * iWaveSpeedHz * 0.53 + 11.7)) - 0.5;
    float sideDecay = max(decayLength * 0.6 * (1.0 + sideLevel * 2.0 * iWaveAmplitude), 0.5);
    float sideGlow = exp(-fragCoord.x / sideDecay) + exp(-(res.x - fragCoord.x) / sideDecay);
    float heightFrac = fragCoord.y / res.y;
    float bottomBias = mix(0.45, 1.0, heightFrac * heightFrac);
    float topGlow = exp(-fragCoord.y / max(sideDecay * 0.5, 0.5)) * 0.5;
    float perimeter = clamp((sideGlow * bottomBias + topGlow) * max(iPerimeterBloom, iPerimeterFloor), 0.0, 1.0);

    float glow = clamp(vGlow + perimeter, 0.0, 1.0);

    // Hue drifts through three families (A blue -> B violet -> C ember) via a bounded, aperiodic
    // value-noise field, never an angle/phase term (a rotating hue can periodically let the wrong
    // color dominate). INTENSITY still selects light-vs-deep within the sampled family and drives
    // alpha. iHueDrift = 0 collapses to the pure A pair and skips the noise sample entirely - the
    // chat byte-path.
    float hueT = 0.0;
    if (iHueDrift > 0.0) {
        float hueNoise = valueNoise(float2(
            fragCoord.x * iWaveFreqX * 0.55 + iTime * iWaveSpeedHz * 0.63,
            fragCoord.y * iWaveFreqX * 0.35 + iTime * iWaveSpeedHz * 0.29 + 5.43));
        hueT = clamp(hueNoise, 0.0, 1.0) * iHueDrift;
    }
    // Written branch-free (never a ternary on a float3): a ternary-with-vector form risks a
    // runtime-compiler failure that renders the whole shader black. segAB runs A->B as hueT climbs
    // 0->0.5, segBC then runs B->C over 0.5->1 - bit-for-bit the same result as the two-branch form.
    float segAB = clamp(hueT * 2.0, 0.0, 1.0);
    float segBC = clamp((hueT - 0.5) * 2.0, 0.0, 1.0);
    float3 hueLight = mix(mix(iColorLight, iAuroraLightB, segAB), iAuroraLightC, segBC);
    float3 hueDeep = mix(mix(iColorDeep, iAuroraDeepB, segAB), iAuroraDeepC, segBC);
    float3 col = mix(hueDeep, hueLight, glow);

    // The bottom-edge sample (#6D85B9 over the #2A2A2E scrim) back-solves to a near-opaque peak.
    // Stripe-avoidance comes from the continuous exponential falloff, not from a low peak alpha -
    // capping alpha low here would render the effect invisible behind the overlay's bottom chrome.
    float peakAlpha = 0.9;
    float alpha = clamp(glow * iIntensity * iVisible, 0.0, 1.0) * peakAlpha;

    // Dither must apply to the PREMULTIPLIED colour (col * alpha) - the space actually quantized
    // to 8 bits. Dithering `col` before the `* alpha` scales the perturbation BY alpha, fading it
    // out precisely where the ramp is flattest and banding is worst. Alpha itself stays
    // un-dithered: the compositor's `premul + dst*(1-alpha)` keeps a 0.5/255 alpha error's visible
    // impact under 0.03 LSB against this family's near-black canvas.
    return half4(ditherPremul(col * alpha, alpha, fragCoord), alpha);
}
"""

/**
 * Pure uniform math per [EdgeGlowState] - same discipline as the orb's uniform-math objects: one
 * exhaustive `when(state)` per uniform. [reducedMotion] is threaded through as a parameter rather
 * than a second shader/composable (M8 here means "feed static values", not "swap shaders" - there
 * is no lower-fidelity fallback shape to fall back to, unlike the orb/spark silhouettes).
 */
internal object EdgeGlowUniformMath {
    private val BREATHE_HZ = 1000f / AuraMotion.listeningBreathePeriodMs // M2

    // M2: "loosely synced to RMS" - rms never overrides the 1.2s breathe cadence, it scales how
    // big that swing gets, up to this fraction on top of the named base amplitude at full loudness.
    private const val RMS_AMPLITUDE_BOOST_MAX = 0.5f
    private const val RMS_NORMALIZATION_DB = 10f // matches OrbUniformMath's SpeechRecognizer range

    private const val THINKING_INTENSITY = 1.3f // M3: "intensify" - no named numeric token, own tuning constant

    // Dismissal plays the ignite growth in reverse at AuraMotion.dismissSpeedMultiplier - derived
    // from those two tokens rather than a private literal, since no separate duration is named for
    // Hidden<->active transitions outside that growth itself.
    val VISIBILITY_TRANSITION_MS = (AuraMotion.edgeSweepMs / AuraMotion.dismissSpeedMultiplier).toInt()

    // Listening keeps corners at ~50% of vertical-only brightness; Thinking suppresses to ~15%,
    // since a contracted decay length alone can't suppress corners (they sit on the same
    // distFromBottom=0 row as the peak).
    private const val LISTENING_CENTER_WEIGHT_STRENGTH = 0.5f
    private const val THINKING_CENTER_WEIGHT_STRENGTH = 0.85f

    // Fraction of surface HEIGHT, from the top edge, over which the bloom feathers to exactly 0 -
    // kills the seam a large reachScale (the in-app 5.5x) would otherwise hard-stop into at the top
    // draw bound. A no-op for small-reach bottom-anchored callers.
    const val TOP_FADE_FRACTION = 0.18f

    // Thinking's decay length as a fraction of AuraColors.auroraOverlayBloomDecayDepth (the
    // measured WIDE/Listening reach).
    private const val THINKING_DECAY_FRACTION = 0.22f

    // Overlay session-resting pool: fractions of the ONE measured reach token
    // (auroraOverlayBloomDecayDepth) and of the active baseline.
    private const val OVERLAY_RESTING_DECAY_FRACTION = 0.45f
    private const val OVERLAY_RESTING_INTENSITY = 0.55f

    // Persistent edge-lit floors so the perimeter keeps a faint side/top presence after the bloom
    // settles, instead of collapsing to zero. Ratios of the bloom's full perimeter strength;
    // Thinking stays lowest since its contracted, corner-suppressed profile is a designed state,
    // not an edge-lit one.
    private const val LISTENING_PERIMETER_FLOOR = 0.35f
    private const val RESTING_PERIMETER_FLOOR = 0.30f
    private const val THINKING_PERIMETER_FLOOR = 0.15f

    /** Per-state persistent perimeter presence (0 = bottom-only). Scaled by the
     * composable's caller knob (`perimeterPresence`: overlay 1f, chat default 0f → byte-path). */
    fun perimeterFloor(state: EdgeGlowState): Float = when (state) {
        EdgeGlowState.Hidden -> 0f
        is EdgeGlowState.Igniting -> LISTENING_PERIMETER_FLOOR
        is EdgeGlowState.Listening -> LISTENING_PERIMETER_FLOOR
        EdgeGlowState.Thinking -> THINKING_PERIMETER_FLOOR
        EdgeGlowState.Resting -> RESTING_PERIMETER_FLOOR
    }

    // Low-frequency spatial+temporal noise modulates the decay length by up to this fraction,
    // drifting slowly - a wobble, never a hard geometric change.
    const val WAVE_AMPLITUDE = 0.22f
    private const val WAVE_SPATIAL_CYCLES_ACROSS_WIDTH = 1.5f
    private const val WAVE_DRIFT_HZ = 0.035f

    /** Discriminates the state *kind* (ignoring [EdgeGlowState.Listening]'s rms payload) so the
     * caller can detect "we just entered Listening" without retriggering on every rms tick - same
     * pattern as [com.mewbo.aura.ui.orb.OrbState]'s error-pulse tracking. */
    fun transitionKey(state: EdgeGlowState): Int = when (state) {
        EdgeGlowState.Hidden -> 0
        is EdgeGlowState.Igniting -> 1
        is EdgeGlowState.Listening -> 2
        EdgeGlowState.Thinking -> 3
        EdgeGlowState.Resting -> 4
    }

    fun targetVisible(state: EdgeGlowState): Float = when (state) {
        EdgeGlowState.Hidden -> 0f
        is EdgeGlowState.Igniting -> 1f
        is EdgeGlowState.Listening -> 1f
        EdgeGlowState.Thinking -> 1f
        EdgeGlowState.Resting -> 1f
    }

    /** The ignition growth front: 1f once ignition is complete or state has moved past it entirely -
     * only [EdgeGlowState.Igniting] carries a genuine sub-1 value, animated by the caller. Feeds the
     * shader's [AURORA_EDGE_SHADER_SRC] `decayLength` scale directly. */
    fun igniteProgress(state: EdgeGlowState, reducedMotion: Boolean): Float {
        if (reducedMotion) return 1f // static bloom frame, no growth animation
        return when (state) {
            EdgeGlowState.Hidden -> 1f
            is EdgeGlowState.Igniting -> state.progress.coerceIn(0f, 1f)
            is EdgeGlowState.Listening -> 1f
            EdgeGlowState.Thinking -> 1f
            EdgeGlowState.Resting -> 1f
        }
    }

    /** [EdgeGlowState.Igniting] targets Listening's WIDE reach (it is the entry animation
     * immediately before Listening begins), while [EdgeGlowState.Thinking] contracts to hug the
     * pill. [Hidden]'s value is unreachable in practice (alpha targets 0 there) but still must
     * resolve to something for the exhaustive `when`. [EdgeGlowState.Resting] (overlay-scoped)
     * sits between the two: a session-lifetime pool wider than Thinking's hug but narrower than
     * Listening's full reach. */
    fun decayDepthDp(state: EdgeGlowState): Dp = when (state) {
        EdgeGlowState.Hidden -> AuraColors.auroraOverlayBloomDecayDepth
        is EdgeGlowState.Igniting -> AuraColors.auroraOverlayBloomDecayDepth
        is EdgeGlowState.Listening -> AuraColors.auroraOverlayBloomDecayDepth
        EdgeGlowState.Thinking -> AuraColors.auroraOverlayBloomDecayDepth * THINKING_DECAY_FRACTION
        EdgeGlowState.Resting -> AuraColors.auroraOverlayBloomDecayDepth * OVERLAY_RESTING_DECAY_FRACTION
    }

    /** Horizontal center-weight strength per state. [Resting] reuses Listening's strength - it is
     * presence, not a corner-suppression concern like Thinking. */
    fun centerWeightStrength(state: EdgeGlowState): Float = when (state) {
        EdgeGlowState.Hidden -> LISTENING_CENTER_WEIGHT_STRENGTH
        is EdgeGlowState.Igniting -> LISTENING_CENTER_WEIGHT_STRENGTH
        is EdgeGlowState.Listening -> LISTENING_CENTER_WEIGHT_STRENGTH
        EdgeGlowState.Thinking -> THINKING_CENTER_WEIGHT_STRENGTH
        EdgeGlowState.Resting -> LISTENING_CENTER_WEIGHT_STRENGTH
    }

    private fun rmsNormalized(rmsDb: Float): Float = (rmsDb / RMS_NORMALIZATION_DB).coerceIn(0f, 1f)

    fun intensity(state: EdgeGlowState, timeSeconds: Float, reducedMotion: Boolean): Float = when (state) {
        EdgeGlowState.Hidden -> 0f
        is EdgeGlowState.Igniting -> 1f
        is EdgeGlowState.Listening -> if (reducedMotion) {
            1f
        } else {
            val amplitude = AuraMotion.listeningBreatheAmplitude *
                (1f + rmsNormalized(state.rmsDb) * RMS_AMPLITUDE_BOOST_MAX)
            1f + amplitude * sin(2f * PI.toFloat() * BREATHE_HZ * timeSeconds)
        }
        EdgeGlowState.Thinking -> THINKING_INTENSITY
        // Static - no time term, unlike Listening's breathe: a session-lifetime presence pool
        // must not oscillate (it would read as still "doing something").
        EdgeGlowState.Resting -> OVERLAY_RESTING_INTENSITY
    }

    /** Waviness spatial frequency in shader-space (cycles per pixel), scaled so
     * [WAVE_SPATIAL_CYCLES_ACROSS_WIDTH] full cycles span the draw width regardless of device size. */
    fun waveFreqX(widthPx: Float): Float =
        if (widthPx > 0f) WAVE_SPATIAL_CYCLES_ACROSS_WIDTH / widthPx else 0f

    fun waveSpeedHz(reducedMotion: Boolean): Float = if (reducedMotion) 0f else WAVE_DRIFT_HZ
}

/**
 * Draw-phase-only "did the state kind just change" tracker, read exclusively inside `onDrawBehind`.
 * A `LaunchedEffect(transitionKey)` alternative races [rememberShaderTimeSeconds]'s own internal
 * `LaunchedEffect` - both launch on the same composition pass with no guaranteed relative order, so
 * it can read `timeSeconds` before the frame clock's first tick and capture 0 instead of the true
 * current time. Comparing inside the draw phase is race-free: draw strictly follows that frame's
 * `timeSeconds` update, always.
 */
private class EdgeGlowTransitionTracker {
    private var lastTransitionKey = Int.MIN_VALUE
    private var enteredAt = 0f
    private var heldState: EdgeGlowState = EdgeGlowState.Hidden
    private var heldSpeed: Float = 1f

    /** The wave speed matching the most recent [resolveUniformState] call - read straight after it. */
    var resolvedSpeed: Float = 1f
        private set

    /**
     * The state whose uniforms (reach/intensity/center-weight/transition-key) should drive the shader
     * THIS frame. Normally the live [state]; but during an opt-in dismiss fade ([holdOnDismiss] &&
     * [state] == [EdgeGlowState.Hidden]) it resolves to the LAST ACTIVE state instead - Hidden's own
     * uniforms can't fade (intensity 0f zeroes alpha instantly; reach is wide and would pop the
     * bloom), so the frame is HELD and only the alpha envelope (`iVisible`) ramps to 0. The
     * matching wave speed is held too (via [resolvedSpeed]) so the drift neither jumps nor slows,
     * and phase never resets mid-fade either. Mutated + read EXCLUSIVELY inside `onDrawBehind`.
     */
    fun resolveUniformState(state: EdgeGlowState, speedScale: Float, holdOnDismiss: Boolean): EdgeGlowState {
        if (state != EdgeGlowState.Hidden) {
            heldState = state
            heldSpeed = speedScale
        }
        val hold = holdOnDismiss && state == EdgeGlowState.Hidden
        resolvedSpeed = if (hold) heldSpeed else speedScale
        return if (hold) heldState else state
    }

    fun elapsedSinceTransition(transitionKey: Int, timeSeconds: Float): Float {
        if (transitionKey != lastTransitionKey) {
            lastTransitionKey = transitionKey
            enteredAt = timeSeconds
        }
        return (timeSeconds - enteredAt).coerceAtLeast(0f)
    }
}

/**
 * Bottom-anchored edge glow. [AuraColors.auroraOverlayBloom]'s two stops (pale/deep blue) are
 * blended by the bloom's own intensity, never by angle or position - see
 * [AURORA_EDGE_SHADER_SRC]'s header. [EdgeGlowState.Igniting]'s progress is driven by the caller
 * (mirrors [OrbState]'s externally-driven `rmsDb` pattern) - this composable only turns it into
 * pixels.
 */
@Composable
fun AuroraEdgeGlow(
    state: EdgeGlowState,
    modifier: Modifier = Modifier,
    /** Multiplies the state's decay length. 1f = the overlay's capture-measured reach; the in-app
     * chat passes a larger scale since that reach reads tiny under the app's taller bottom stack. */
    reachScale: Float = 1f,
    /** Replaces the state's horizontal center-weighting when non-null. The state defaults produce a
     * center-anchored bloom (correct for the overlay, whose glow radiates from the pill); the
     * in-app chat passes a near-zero value so the bloom covers the whole bottom edge evenly. */
    centerWeightOverride: Float? = null,
    /** Scales the final alpha on top of the state's intensity envelope. The in-app chat runs
     * softer than the overlay's capture-measured peak. */
    alphaScale: Float = 1f,
    /** Multiplies the wave-drift speed ONLY (`iWaveSpeedHz`) - leaves reach/center-weight/alpha and
     * the breathe cadence untouched. 1f = the state's own pace; the in-app chat passes a >1 value
     * for the ACTIVE (Thinking) glow so a live run reads livelier. Reduced-motion is unaffected
     * (base speed is already 0 there).
     *
     * This scales the RATE, NEVER the accumulated phase: `iTime` is
     * [EdgeGlowTransitionTracker]'s `elapsedInState`, which resets to 0 at every state transition,
     * and a transition is the ONLY moment `speedScale` changes - so `iTime * iWaveSpeedHz` is 0
     * at the scale change regardless of scale, and the phase can never jump mid-flight. */
    speedScale: Float = 1f,
    /** Opt-in gentle fade-OFF duration (ms) for the run-end / dismiss transition to
     * [EdgeGlowState.Hidden]. `null` (the overlay default) keeps the quick
     * [EdgeGlowUniformMath.VISIBILITY_TRANSITION_MS] dismiss. The in-app chat passes a long value
     * ([com.mewbo.aura.ui.theme.AuraMotion.edgeRestFadeMs], >=3s) so the glow eases off instead of
     * snapping to black on an abrupt on->off luminance change (a photosensitivity trigger). While
     * a fade is in flight the LAST active state's whole frame (reach, intensity, wave phase AND
     * speed) is HELD and only the alpha envelope ([iVisible]) ramps to 0 - snapping to [Hidden]'s
     * own uniforms can't fade (intensity 0f zeroes alpha instantly; reach is WIDE and would pop
     * the bloom). Holding the speed too is load-bearing: a wave-rate change while `iTime` is
     * already large would jump the drift. */
    dismissFadeMs: Int? = null,
    /** The two-stop color pair, blended by the glow's own intensity, never angle. Default = the
     * chat-darkened [AuraColors.auroraOverlayBloom]; the overlay passes
     * [AuraColors.auroraOverlayLiveBloom] (brighter, visible). */
    colors: List<GradientStop> = AuraColors.auroraOverlayBloom,
    /** Perimeter-bloom envelope (0..1), caller-animated - the composable only turns it into
     * pixels. Also lerps the color pair from [AuraColors.auroraIgnitionBloom] (bloom=1) down to
     * the caller's [colors] pair (bloom=0). Read in the draw phase only. Forced 0 under reduced
     * motion (no traveling light). */
    perimeterBloom: State<Float>? = null,
    /** 0..1 amount of the aurora hue-drift field (A blue -> B violet -> C ember). Default 0 =
     * the pure-[colors] two-stop path (the shader skips the hue-noise sample entirely) - chat's
     * byte-path; the overlay passes 1f. Reduced motion keeps the FIELD as a static multi-hue
     * frame: all drift speeds are already 0 there. */
    hueDriftAmount: Float = 0f,
    /** Scales [EdgeGlowUniformMath.perimeterFloor] (persistent edge-lit side/top presence in the
     * live states). Default 0 = bottom-only (chat byte-path); overlay passes 1f. */
    perimeterPresence: Float = 0f,
) {
    val extras = LocalAssistantExtras.current

    val target = EdgeGlowUniformMath.targetVisible(state)
    // Only when easing to Hidden (target 0). Only the alpha ENVELOPE ([visible]) is animated here;
    // the held-frame resolution that lets Hidden fade in place lives in
    // EdgeGlowTransitionTracker.resolveUniformState, mutated in the DRAW phase.
    val fadeOffMs = if (target == 0f) dismissFadeMs else null
    // Kept as an un-destructured State<Float> (no `by`) so reading it doesn't subscribe THIS
    // composable to recompose on every animation frame - only `onDrawBehind` below reads `.value`.
    // The one place this composable needs to react in COMPOSITION (deciding whether to render at
    // all) goes through `derivedStateOf` instead, which only recomposes when the boolean flips.
    val visible: State<Float> = animateFloatAsState(
        targetValue = target,
        animationSpec = when {
            extras.reducedMotion -> snap()
            // >=3s LINEAR ease-off (photosensitivity: a constant rate with no fast segment). The
            // quick dismiss below stays for the fade-IN and callers that pass no override.
            fadeOffMs != null -> tween(fadeOffMs, easing = LinearEasing)
            else -> tween(EdgeGlowUniformMath.VISIBILITY_TRANSITION_MS)
        },
        label = "aurora-edge-visible",
    )
    val shouldRender by remember(target) { derivedStateOf { visible.value > 0f || target > 0f } }

    if (!shouldRender) return

    val shader = remember { RuntimeShader(AURORA_EDGE_SHADER_SRC) }
    val timeSeconds = rememberShaderTimeSeconds()
    val transitionTracker = remember { EdgeGlowTransitionTracker() }

    Box(
        modifier = modifier
            .fillMaxSize()
            .drawWithCache {
                val brush = ShaderBrush(shader)
                val waveFreqX = EdgeGlowUniformMath.waveFreqX(size.width)
                onDrawBehind {
                    val t = timeSeconds.floatValue
                    // During an opt-in dismiss fade this is the LAST ACTIVE state, else the live
                    // one - keying the uniforms off it means a fade-off holds the active frame and
                    // never resets phase or pops the reach mid-fade.
                    val uniformState = transitionTracker.resolveUniformState(
                        state = state,
                        speedScale = speedScale,
                        holdOnDismiss = dismissFadeMs != null,
                    )
                    val elapsedInState = transitionTracker.elapsedSinceTransition(
                        transitionKey = EdgeGlowUniformMath.transitionKey(uniformState),
                        timeSeconds = t,
                    )
                    // Forced 0 under reduced motion (no traveling light). The ignition color pair
                    // is lerped CPU-side (bloom=1 → the brand ignition pair, bloom=0 → the caller's
                    // own [colors] pair) so the shader stays a two-stop intensity blend, never a 3rd
                    // stop. Read in the draw phase so reading perimeterBloom's per-frame value
                    // never subscribes THIS composable to recompose.
                    val bloom = if (extras.reducedMotion) 0f else (perimeterBloom?.value ?: 0f)
                    val colorLight = lerp(colors[0].color, AuraColors.auroraIgnitionBloom[0].color, bloom)
                    val colorDeep = lerp(colors[1].color, AuraColors.auroraIgnitionBloom[1].color, bloom)
                    // The B/C hue families lerp toward the SAME ignition pair as the A pair above,
                    // so a bloom unifies the whole multi-hue field into brand ignition light and
                    // drifts back apart as it settles.
                    val violetLight = lerp(AuraColors.auroraOverlayVioletBloom[0].color, AuraColors.auroraIgnitionBloom[0].color, bloom)
                    val violetDeep = lerp(AuraColors.auroraOverlayVioletBloom[1].color, AuraColors.auroraIgnitionBloom[1].color, bloom)
                    val emberLight = lerp(AuraColors.auroraOverlayEmberBloom[0].color, AuraColors.auroraIgnitionBloom[0].color, bloom)
                    val emberDeep = lerp(AuraColors.auroraOverlayEmberBloom[1].color, AuraColors.auroraIgnitionBloom[1].color, bloom)
                    val decayLengthPx = EdgeGlowUniformMath.decayDepthDp(uniformState).toPx() * reachScale
                    shader.setFloatUniform("iResolution", size.width, size.height)
                    shader.setFloatUniform("iDecayLengthPx", decayLengthPx)
                    shader.setFloatUniform(
                        "iIgniteProgress",
                        EdgeGlowUniformMath.igniteProgress(uniformState, extras.reducedMotion),
                    )
                    shader.setFloatUniform(
                        "iIntensity",
                        EdgeGlowUniformMath.intensity(uniformState, t, extras.reducedMotion) * alphaScale,
                    )
                    shader.setFloatUniform(
                        "iCenterWeight",
                        centerWeightOverride ?: EdgeGlowUniformMath.centerWeightStrength(uniformState),
                    )
                    shader.setFloatUniform("iTopFadeFraction", EdgeGlowUniformMath.TOP_FADE_FRACTION)
                    shader.setFloatUniform(
                        "iWaveAmplitude",
                        if (extras.reducedMotion) 0f else EdgeGlowUniformMath.WAVE_AMPLITUDE,
                    )
                    shader.setFloatUniform("iWaveFreqX", waveFreqX)
                    shader.setFloatUniform(
                        "iWaveSpeedHz",
                        EdgeGlowUniformMath.waveSpeedHz(extras.reducedMotion) * transitionTracker.resolvedSpeed,
                    )
                    shader.setFloatUniform("iTime", elapsedInState)
                    shader.setFloatUniform("iVisible", visible.value)
                    shader.setFloatUniform("iPerimeterBloom", bloom)
                    // Neither the floor nor the hue-drift amount is reduced-motion-gated: both are
                    // static presence, not travel - only the drift SPEEDS are already 0 there.
                    shader.setFloatUniform(
                        "iPerimeterFloor",
                        EdgeGlowUniformMath.perimeterFloor(uniformState) * perimeterPresence,
                    )
                    shader.setFloatUniform("iHueDrift", hueDriftAmount)
                    shader.setFloatUniform("iColorLight", colorLight.red, colorLight.green, colorLight.blue)
                    shader.setFloatUniform("iColorDeep", colorDeep.red, colorDeep.green, colorDeep.blue)
                    shader.setFloatUniform("iAuroraLightB", violetLight.red, violetLight.green, violetLight.blue)
                    shader.setFloatUniform("iAuroraDeepB", violetDeep.red, violetDeep.green, violetDeep.blue)
                    shader.setFloatUniform("iAuroraLightC", emberLight.red, emberLight.green, emberLight.blue)
                    shader.setFloatUniform("iAuroraDeepC", emberDeep.red, emberDeep.green, emberDeep.blue)
                    drawRect(brush = brush)
                }
            },
    )
}
