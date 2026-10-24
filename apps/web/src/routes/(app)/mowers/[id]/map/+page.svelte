<!--
  Theft alarms link to `/mowers/<id>/map?lat=..&lon=..` (Notifications' `_where`). The map lives
  on the detail page, so this route forwards there and keeps the reported position.
-->
<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';

  import SlowLoading from '#lib/components/SlowLoading.svelte';

  $effect(() => {
    const id = encodeURIComponent(page.params.id ?? '');
    void goto(`/mowers/${id}${page.url.search}`, { replace: true });
  });
</script>

<p class="pad" role="status"><SlowLoading /></p>

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
    color: var(--md-color-ink-muted);
  }
</style>
