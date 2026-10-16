/**
 * Capacitor configuration of the MowDeck app shells (ADR 0036).
 *
 * The shells load the web app from the owner's MowDeck server, not from a bundled copy: the web
 * app and the API share one origin behind Traefik, so the HttpOnly refresh cookie keeps working
 * and every server update reaches the apps at once. `server.url` is therefore NOT set here; the
 * native code (`ios/App/App/MainViewController.swift`, `android/.../MainActivity.java`) sets it at
 * start from the address the user entered on the bundled first-launch screen (`www/index.html`).
 * Without a stored address the shell shows that screen; `offline.html` is shown when the server
 * cannot be reached.
 */
import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'app.mowdeck.mobile',
  appName: 'MowDeck',
  webDir: 'www',
  // Lets the server and its logs tell the shells from a browser (the web app detects the shell
  // through the injected `window.Capacitor` bridge, not through this).
  appendUserAgent: 'MowDeckApp',
  server: {
    androidScheme: 'https',
    errorPath: 'offline.html',
  },
  ios: {
    // Edge to edge: the web view fills the screen and the web app keeps clear of the status bar,
    // the notch and the home indicator itself (viewport-fit=cover and env(safe-area-inset-*):
    // body padding, the pinned top bar, the bottom bars). 'always' insets the scroll view
    // instead, which gives the page a scroll range of the inset height even when its content
    // fits, so a drag pulled the top bar under the status bar.
    contentInset: 'never',
  },
  android: {
    allowMixedContent: false,
    // No @capacitor/push-notifications on Android: it needs Firebase Cloud Messaging. Android
    // gets local notifications while the app runs and ntfy in the background (ADR 0036).
    includePlugins: [
      '@capacitor/app',
      '@capacitor/local-notifications',
      '@capacitor/splash-screen',
      '@capawesome/capacitor-badge',
    ],
  },
  plugins: {
    // Android: pad the web view by the system bars natively (edge-to-edge since Android 15).
    SystemBars: {
      insetsHandling: 'native',
      style: 'DEFAULT',
    },
    SplashScreen: {
      launchAutoHide: true,
      launchShowDuration: 600,
      showSpinner: false,
    },
    LocalNotifications: {
      smallIcon: 'ic_stat_mowdeck',
      iconColor: '#1f7a35',
    },
    PushNotifications: {
      // How an APNs message is shown while the app is in the foreground (iOS).
      presentationOptions: ['badge', 'sound', 'alert'],
    },
    // The web app talks to its own origin with fetch; no native HTTP patching.
    CapacitorHttp: {
      enabled: false,
    },
  },
};

export default config;
