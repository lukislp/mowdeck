import { render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setLocale } from '#lib/paraglide/runtime.js';

import InboxImage from './InboxImage.svelte';

beforeEach(() => {
  setLocale('en', { reload: false });
  vi.stubGlobal(
    'URL',
    Object.assign(URL, { createObjectURL: () => 'blob:photo', revokeObjectURL: vi.fn() }),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe('InboxImage', () => {
  it('shows the photo once it is loaded', async () => {
    const load = vi.fn().mockResolvedValue(new Blob(['x'], { type: 'image/jpeg' }));
    render(InboxImage, { props: { id: 'n1', load } });
    const img = await screen.findByTestId('inbox-photo');
    expect(img.getAttribute('src')).toBe('blob:photo');
    expect(load).toHaveBeenCalledWith('n1');
  });

  it('shows nothing when the photo is gone', async () => {
    const load = vi.fn().mockRejectedValue(new Error('404'));
    render(InboxImage, { props: { id: 'n1', load } });
    await waitFor(() => expect(load).toHaveBeenCalled());
    expect(screen.queryByTestId('inbox-photo')).toBeNull();
  });
});
