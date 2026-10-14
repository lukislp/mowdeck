<!--
  An old route of a settings section (`/account`, `/settings/users`, ...): forwards to its
  anchor on the one settings page and keeps the query and an anchor of its own.
-->
<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';

  import SlowLoading from '$lib/components/SlowLoading.svelte';
  import { builtIn } from '$lib/shell/registry.svelte';

  import { SETTINGS_ROUTE, forwardTarget } from './sections';

  $effect(() => {
    const target =
      forwardTarget(builtIn, page.url.pathname, page.url.search, page.url.hash) ?? SETTINGS_ROUTE;
    void goto(target, { replaceState: true });
  });
</script>

<p class="pad" role="status"><SlowLoading /></p>

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
    color: var(--md-color-ink-muted);
  }
</style>
