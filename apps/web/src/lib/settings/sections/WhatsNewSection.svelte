<!--
  Neuigkeiten: the history of "Was ist neu", every release of this build up to the running
  version, newest first. Always shown (before About), for every user.
-->
<script lang="ts">
  import { m } from '#lib/paraglide/messages.js';
  import { whatsNew } from '#lib/whats-new/whats-new.svelte.js';
  import WhatsNewList from '#lib/whats-new/WhatsNewList.svelte';

  $effect(() => {
    void whatsNew.load();
  });
</script>

<div class="news" data-testid="whats-new-history">
  {#if whatsNew.history.length}
    <WhatsNewList releases={whatsNew.history} />
  {:else if whatsNew.loaded}
    <p class="empty">{m.whatsnew_empty()}</p>
  {/if}
</div>

<style>
  .news {
    padding: 0 var(--md-gutter);
  }

  .empty {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }
</style>
