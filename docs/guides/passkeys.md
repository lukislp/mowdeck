# Signing in with a passkey

A passkey lets you sign in without typing your password: your phone, laptop or password manager confirms it is you with Face ID, Touch ID, Windows Hello or your device PIN. Your password keeps working, so you can always fall back to it. Your server's operator must have switched passkeys on ([Passkeys](../ops/passkeys.md)).

## Add a passkey

1. Open MowDeck in your browser (Safari, Chrome, Edge or Firefox) at its usual address.
2. Go to **Einstellungen**, section **Konto**, panel **Passkeys**, and choose **Passkey hinzufügen**.
3. Enter your current password and a name (for example "iPhone"), then **Passkey anlegen**.
4. Your device asks to save the passkey; confirm with Face ID, Touch ID or your PIN.

The list shows each passkey with its name, the provider or browser it was made with, when it was added and last used, and whether it is synced (iCloud Keychain, Google Password Manager, a password manager) or bound to one device. You can rename and remove passkeys there. In the iPhone or Android app the panel tells you to add passkeys in the browser; a synced passkey then works in the app too.

## Sign in

- **Browser:** on the sign-in page choose **Mit Passkey anmelden**, or tap the username field and pick your passkey from the suggestions. No username or password needed.
- **App:** choose **Mit Passkey anmelden**. Safari (iPhone) or Chrome (Android) opens your MowDeck sign-in page; sign in with your passkey there. You are taken back to the app, signed in. If the browser stays open, tap **App öffnen**.

## Lost a device?

Remove its passkey under **Passkeys** after signing in with your password or another passkey. An admin can remove the passkeys of sub-users under **Benutzer**, **Passkeys**.
