<!--
  The camera debug overlay (`?camdebug=1`, `lib/camera/debug.ts`): once a second the readings of
  every shown tile (the <video> element, its tracks and media events, every frame signal, the
  freeze watch, the Agora numbers and the receiver's inbound-rtp statistics) as short lines over
  the picture. "Copy" puts the full readings on the clipboard as JSON, to send along with a
  report from a phone. It never takes touches except on its button, so driving still works.
-->
<script lang="ts">
  import { debugLines, type DebugInput } from '$lib/camera/probe';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    /** The current readings. */
    read: () => Promise<DebugInput>;
  }

  let { read }: Props = $props();

  let input = $state.raw<DebugInput | null>(null);
  let copied = $state(false);

  $effect(() => {
    let alive = true;
    const tick = async (): Promise<void> => {
      const next = await read();
      if (alive) input = next;
    };
    void tick();
    const timer = setInterval(() => void tick(), 1_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  });

  async function copy(): Promise<void> {
    if (!input) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(input, null, 1));
      copied = true;
      setTimeout(() => (copied = false), 1_500);
    } catch {
      copied = false;
    }
  }
</script>

<div class="camdebug" data-testid="camera-debug">
  <pre class="mono">{input ? debugLines(input).join('\n') : ''}</pre>
  <button type="button" class="copy" data-testid="camera-debug-copy" onclick={() => void copy()}
    >{copied ? m.camera_debug_copied() : m.camera_debug_copy()}</button
  >
</div>

<style>
  .camdebug {
    position: absolute;
    z-index: 6;
    inset: 0 0 auto;
    max-height: 70%;
    overflow: auto;
    padding: var(--md-space-1) var(--md-space-2);
    background: color-mix(in srgb, var(--md-color-ink) 72%, transparent);
    color: var(--md-color-ink-inverse);
    pointer-events: none;
  }

  pre {
    margin: 0;
    font-size: var(--md-font-size-11);
    line-height: 1.35;
    white-space: pre-wrap;
    word-break: break-all;
  }

  .copy {
    position: absolute;
    top: var(--md-space-1);
    right: var(--md-space-1);
    padding: var(--md-space-0_5) var(--md-space-2);
    border: 1px solid var(--md-color-ink-inverse);
    border-radius: var(--md-radius-1);
    background: transparent;
    color: inherit;
    font-size: var(--md-font-size-11);
    pointer-events: auto;
  }
</style>
