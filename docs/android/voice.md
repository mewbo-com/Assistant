# Voice

## Talk to Mewbo hands free

<video controls preload="metadata" width="1800" height="1350" style="width: 100%; max-width: 720px; height: auto; display: block; margin: 1.5rem auto;">
  <source src="../../assets/videos/Mewbo-Aura-2-Overlay.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

A voice turn runs through the same Mewbo server and the same session as a typed one. Nothing about the conversation changes because you spoke it.

> [!NOTE] Voice runs on speech built into your phone
> Dictation uses your phone's speech recognizer, and reading replies aloud uses its text to speech engine. Both ship on standard Android devices. Emulators without them fall back to test stand ins, so use a real phone to try voice.

## Dictate a message {#dictation}

Tap the microphone button labeled `Dictate` in the chat composer and speak. The first time, Aura asks for microphone permission. The composer becomes a live view while it listens, and your words settle into text as they are recognized.

When you finish, the recognized text drops into the input field, ready to edit or send. **Stop** ends listening without sending and keeps whatever was recognized. If the recognizer hears nothing, it returns to rest with no error to dismiss.

Dictation in chat starts only when you tap the microphone. It never listens on its own.

> [!TIP] Dictating switches on the spoken reply for that turn
> Send a message you dictated and Aura reads the reply back as it streams. Type it and the reply stays silent. Choosing voice or text is how you choose whether to hear the answer.

## Have replies read aloud {#read-aloud}

Start a turn by voice and Aura speaks the reply sentence by sentence as it streams, so you hear the answer without waiting for it to finish. The reading is smoothed for speech. It reads the words rather than the markup, and skips code blocks and tool activity.

Every completed answer also carries a speaker button that reads it aloud on demand. Starting a new voice turn, tapping stop, or dismissing the conversation stops the current reading immediately. While Aura speaks it briefly lowers other audio on the phone, then restores it.

## The voice overlay {#overlay}

Set Aura as your device's default assistant app and you can raise it from any screen with your usual assistant gesture, without opening the app first. Aura's Settings has the option, which opens the Android setting for that choice.

### What happens when you raise it {#overlay-flow}

A compact overlay slides up over whatever you are doing and Aura starts listening right away, with nothing to tap first once you have granted microphone access. Stop speaking and it sends the turn automatically. The answer streams into a card and is read aloud, without leaving the screen you were on. Stay silent for a few seconds and listening cancels, returning the overlay to rest.

The response card lets you toggle the spoken reply, stop the streaming reply, or expand into the full conversation. Swiping the card up does the same.

Only the first exchange happens in place. From your next message onward, Aura hands the conversation to the full app so a longer exchange gets the room it needs. After the first reply, a plain microphone button starts another spoken turn.

The overlay also offers a shortcut to continue your most recent conversation. Swiping the composer up hands off to the app at any point, carrying anything you have typed. Pressing back or swiping down dismisses the overlay.

### The brand mark {#brand-mark}

A small spark pulses at the bottom of the transcript while the assistant works, and a blue glow along the bottom edge signals that it is live. Both appear in chat and in the overlay.

## Next steps

- [Chat and Sessions](chat.md). The transcript, session rail, and attachments a voice turn lands in.
- [Device Tools](device-tools.md). What the assistant can do on the phone once you can talk to it.
