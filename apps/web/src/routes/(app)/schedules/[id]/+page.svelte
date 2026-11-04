<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<script lang="ts">
  import { page } from '$app/state';

  import ScheduleEditor from '#lib/components/ScheduleEditor.svelte';

  const id = $derived(page.params.id ?? '');
</script>

{#key id}
  <ScheduleEditor {id} />
{/key}
