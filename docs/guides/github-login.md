# Signing in with GitHub

You can sign in to MowDeck with your GitHub account once you have linked it to your MowDeck user. Your password keeps working, so you can always fall back to it. Your server's operator must have switched GitHub on ([Sign in with GitHub](../ops/github-login.md)); otherwise you see neither the button nor the settings panel.

## Link your GitHub account

1. Open MowDeck in your browser (Safari, Chrome, Edge or Firefox) and sign in with your password.
2. Go to **Einstellungen**, section **Konto**, panel **GitHub**, and choose **Mit GitHub verknüpfen**.
3. Enter your current password and choose **Weiter zu GitHub**.
4. GitHub asks whether MowDeck may read your public profile; choose **Authorize**. You come back to the settings, which now show "Verknüpft mit GitHub als <your GitHub name>".

MowDeck stores only your GitHub account's number and name. It never reads your e-mail address and never links accounts by it: a GitHub account signs in only to the MowDeck user who linked it. One GitHub account can be linked to one MowDeck user.

In the iPhone or Android app the panel tells you to link GitHub in the browser; after that the app can sign you in with GitHub too.

## Sign in

- **Browser:** on the sign-in page choose **Mit GitHub anmelden**. If you are signed in to GitHub already, you are back in MowDeck right away; otherwise sign in at GitHub first.
- **App:** choose **Mit GitHub anmelden**. Safari (iPhone) or Chrome (Android) opens your MowDeck sign-in page; choose **Mit GitHub anmelden** there, sign in at GitHub and confirm **In der App weiter**. Only confirm when you started the sign-in from the app yourself.

"Anmeldung mit GitHub nicht möglich" means that this GitHub account is not linked to a MowDeck user: sign in with your password and link it first.

## Unlink

**Einstellungen**, **Konto**, **GitHub**, **Verknüpfung lösen**. GitHub then no longer signs you in; your password does. You can link again at any time. To also remove MowDeck's access on GitHub's side, revoke it under GitHub, **Settings**, **Applications**, **Authorized OAuth Apps**.
