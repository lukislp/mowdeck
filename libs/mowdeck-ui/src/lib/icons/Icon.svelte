<!--
  One glyph per concept, drawn on a 24 px grid with square line ends (technical, not playful).
  Decorative by default (aria-hidden); pass `label` when the icon carries meaning alone.
-->
<script lang="ts" module>
  export const ICON_PATHS = {
    mower:
      'M3 15h13l3-5h2M5 15v-3h8l2 3M7 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM17 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z',
    users:
      'M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM2.5 20c.6-3.6 3.2-6 6.5-6s5.9 2.4 6.5 6M16 4.3a3.5 3.5 0 0 1 0 6.4M18 14.5c2 .9 3.2 2.9 3.5 5.5',
    user: 'M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM4 21c.7-4.2 3.9-7 8-7s7.3 2.8 8 7',
    key: 'M14 10a4 4 0 1 0-3.5 4L9 15.5V18H6.5v2.5H3V17l6.5-6.5M15.5 8.5h.01',
    settings:
      'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1',
    back: 'M15 5l-7 7 7 7',
    chevron: 'M9 5l7 7-7 7',
    plus: 'M12 4v16M4 12h16',
    minus: 'M4 12h16',
    fit: 'M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5M10 10h4v4h-4z',
    crosshair: 'M12 2v5M12 17v5M2 12h5M17 12h5M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z',
    layers: 'M12 3l9 5-9 5-9-5 9-5ZM3 12.5l9 5 9-5M3 17l9 5 9-5',
    route: 'M4 20V5h5v14h5V5h6v15',
    expand: 'M14 4h6v6M10 20H4v-6M20 4l-6.5 6.5M4 20l6.5-6.5',
    collapse: 'M20 10h-6V4M4 14h6v6M14 10l6.5-6.5M10 14l-6.5 6.5',
    close: 'M5 5l14 14M19 5L5 19',
    check: 'M4 12.5l5 5L20 6.5',
    alert: 'M12 3l10 18H2L12 3ZM12 10v5M12 18h.01',
    bolt: 'M13 2L4 14h7l-1 8 9-12h-7l1-8Z',
    play: 'M7 4.5v15L19 12 7 4.5Z',
    pause: 'M7 4h3.5v16H7zM13.5 4H17v16h-3.5z',
    dock: 'M3 11l9-7 9 7M5 9.5V20h14V9.5M9.5 20v-5h5v5',
    signal: 'M4 20v-3M9 20v-7M14 20v-11M19 20V4',
    link: 'M10 14l4-4M8.5 11.5l-2 2a3.5 3.5 0 0 0 5 5l2-2M15.5 12.5l2-2a3.5 3.5 0 0 0-5-5l-2 2',
    sun: 'M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4',
    moon: 'M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z',
    globe:
      'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM3 12h18M12 3c2.5 2.5 3.5 5.5 3.5 9s-1 6.5-3.5 9c-2.5-2.5-3.5-5.5-3.5-9s1-6.5 3.5-9Z',
    download: 'M12 3v12M7 10l5 5 5-5M4 20h16',
    logout: 'M14 4h5v16h-5M10 8l-4 4 4 4M6 12h11',
    list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
    copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
    blade: 'M12 12m-8 0a8 8 0 1 0 16 0 8 8 0 1 0-16 0M12 4v4M12 16v4M4 12h4M16 12h4',
    speed: 'M4 18a8 8 0 1 1 16 0M12 18l4-6',
    trash: 'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13',
    bell: 'M6 16V10a6 6 0 1 1 12 0v6l2 2H4l2-2ZM10 21h4',
    send: 'M3 11l18-8-8 18-2-8-8-2ZM11 13l10-10',
    phone: 'M7 2h10v20H7zM11 18h2',
    inbox: 'M3 13l3-9h12l3 9M3 13v7h18v-7M3 13h5l1.5 3h5l1.5-3h5',
    offline:
      'M2 2l20 20M8.5 16.5a5 5 0 0 1 7 0M5 13a10 10 0 0 1 4-2.4M19 13a10 10 0 0 0-2.3-1.6M12 20h.01',
    calendar: 'M4 6h16v14H4zM4 10.5h16M8.5 3v5M15.5 3v5M8 14h2M12 14h2M16 14h.01M8 17h2M12 17h2',
    clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 7v5.5l3.5 2',
    rain: 'M7 15a4 4 0 0 1-.6-7.96A6 6 0 0 1 17.8 8.2 3.5 3.5 0 0 1 17 15H7ZM8.5 18l-1 3M12.5 18l-1 3M16.5 18l-1 3',
    skip: 'M5 5l9 7-9 7V5ZM18 5v14',
    pin: 'M12 21s-7-6.2-7-11.5a7 7 0 1 1 14 0C19 14.8 12 21 12 21ZM12 12a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z',
    joystick:
      'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21',
    pulse: 'M2 12h4l2.5-6 4 12 3-9 1.5 3H22',
    stop: 'M6 6h12v12H6z',
    camera: 'M3 7h12v10H3zM15 10.5l6-3.5v10l-6-3.5',
    sound: 'M4 9h4l5-4v14l-5-4H4zM16.5 9a4 4 0 0 1 0 6M19 6.5a7.5 7.5 0 0 1 0 11',
    mute: 'M4 9h4l5-4v14l-5-4H4zM16 9.5l5 5M21 9.5l-5 5',
    shield: 'M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6l8-3ZM8.5 12l2.5 2.5 4.5-5',
    fleet: 'M3 6l6-2.5 6 2.5 6-2.5v15L15 21l-6-2.5L3 21V6ZM9 3.5v15M15 6v15',
    chart: 'M3 20h18M6 16.5v-5M10.5 16.5V6M15 16.5v-8M19.5 16.5v-3',
    more: 'M5 11h2v2H5zM11 11h2v2h-2zM17 11h2v2h-2z',
    site: 'M3 21h18M5 21V4h9v17M14 9h5v12M8 8h3M8 12h3M8 16h3',
    group: 'M8 8h12v12H8zM4 16V4h12',
    table: 'M3 5h18v14H3zM3 10h18M3 14.5h18M9 5v14',
    edit: 'M4 20h4L19 9l-4-4L4 16v4ZM13 7l4 4',
  } as const;
  export type IconName = keyof typeof ICON_PATHS;
</script>

<script lang="ts">
  interface Props {
    name: IconName;
    size?: number;
    label?: string;
  }
  let { name, size = 20, label }: Props = $props();
</script>

<svg
  class="md-icon"
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.75"
  stroke-linecap="square"
  stroke-linejoin="miter"
  role={label ? 'img' : undefined}
  aria-label={label}
  aria-hidden={label ? undefined : 'true'}
  focusable="false"
>
  <path d={ICON_PATHS[name]} />
</svg>

<style>
  .md-icon {
    flex: none;
    display: block;
  }
</style>
