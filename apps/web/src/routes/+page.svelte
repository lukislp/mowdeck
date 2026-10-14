<script lang="ts">
  import { goto } from '$app/navigation';

  import { session } from '$lib/auth/session.svelte';

  $effect(() => {
    if (session.status === 'authenticated') void goto('/mowers', { replaceState: true });
    else if (session.status === 'anonymous') void goto('/login', { replaceState: true });
  });
</script>
