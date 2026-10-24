<script lang="ts">
  import { goto } from '$app/navigation';

  import { session } from '#lib/auth/session.svelte.js';

  $effect(() => {
    if (session.status === 'authenticated') void goto('/mowers', { replace: true });
    else if (session.status === 'anonymous') void goto('/login', { replace: true });
  });
</script>
