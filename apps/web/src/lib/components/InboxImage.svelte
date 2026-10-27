<script lang="ts" module>
  import { notificationsApi, unwrap } from '#lib/api/client.js';

  /** The camera photo of an error or theft notification, fetched with the session token. */
  export function loadInboxImage(id: string): Promise<Blob> {
    return unwrap(
      notificationsApi.GET('/api/v1/notifications/inbox/{notification_id}/image', {
        params: { path: { notification_id: id } },
        parseAs: 'blob',
      }),
    ) as Promise<Blob>;
  }
</script>

<script lang="ts">
  import { m } from '#lib/paraglide/messages.js';

  let { id, load = loadInboxImage }: { id: string; load?: (id: string) => Promise<Blob> } =
    $props();

  let url = $state<string | null>(null);

  // The photo needs the bearer token, so it cannot be an <img src>: fetch it and show a blob
  // URL. A photo that is gone (a day) or fails to load simply leaves no image.
  $effect(() => {
    let cancelled = false;
    let made: string | null = null;
    url = null;
    void load(id)
      .then((blob) => {
        if (cancelled) return;
        made = URL.createObjectURL(blob);
        url = made;
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      if (made) URL.revokeObjectURL(made);
    };
  });
</script>

{#if url}
  <img class="photo" src={url} alt={m.notif_photo_alt()} data-testid="inbox-photo" loading="lazy" />
{/if}

<style>
  .photo {
    display: block;
    width: 100%;
    max-width: 360px;
    aspect-ratio: 4 / 3;
    margin-top: var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-2, 4px);
    background: var(--md-color-surface-sunken);
    object-fit: cover;
  }
</style>
