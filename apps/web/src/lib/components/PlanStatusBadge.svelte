<!-- A plan status (active, trial, grace, expired) as a state word with glyph in the state colours. -->
<script lang="ts">
  import { statusLabel, statusTone } from '$lib/plan/labels';
  import type { PlanStatus } from '$lib/plan/types';

  import ToneBadge from './ToneBadge.svelte';

  interface Props {
    status: PlanStatus;
    testid?: string;
  }
  let { status, testid }: Props = $props();
  const look = $derived(statusTone(status));
</script>

<ToneBadge
  tone={look.tone}
  glyph={look.glyph}
  label={statusLabel(status)}
  value={status}
  {testid}
/>
