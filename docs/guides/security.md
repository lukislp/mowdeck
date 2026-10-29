# Your security page

**Settings → Sicherheit** shows in one place how your account is protected. Only you see it: an administrator of your household cannot open your sessions here.

## The overview

Five gauges at the top, each a jump to its part:

- **Password**: set or not, and when you last changed it (unknown for passwords set before this page existed). Change it in your profile.
- **Passkeys**: how many you have. Add, rename and remove them right below, see [Signing in with a passkey](passkeys.md).
- **Sign-in services**: GitHub and Apple, if linked, see [GitHub](github-login.md) and [Apple](apple-login.md). MowDeck does not let you unlink the last way to sign in.
- **Access tokens**: your active personal access tokens for scripts, see [Scripting MowDeck](scripting.md). You can revoke one right here.
- **Devices**: where you are signed in now.

## Signed-in devices

Every browser and every app installation you are signed in with is one row: the browser and system (for example *Firefox on Windows* or *MowDeck app on iOS*), where it signed in from, how (password, passkey, GitHub, Apple), since when and when it was last active. **This device** marks the one you are using.

MowDeck asks no location service. The address is shortened to its network (`203.0.113.x`); addresses of your own network are shown as *home network*.

- **Sign out** ends one device. **Sign out everywhere else** ends all but this one.
- Both ask you to confirm with your passkey or password first ([Confirming critical actions](step-up.md)), so somebody who got hold of a signed-in device cannot throw you out.
- A signed-out device cannot renew its access. MowDeck's account functions refuse it at once; the mower functions accept the access it already has for at most 10 more minutes, then it has to sign in again.

## Alerts about new sign-ins

When somebody signs in to your account from a browser or app MowDeck has not seen for you before, you get a notification **Neue Anmeldung bei MowDeck / New sign-in to MowDeck**: in the app's inbox and as a push message on the devices you set up for notifications, also during your quiet hours. It names the browser and system and the shortened address.

Was it not you? Tap the notification: the security page opens with that sign-in flagged. **Sign this device out**, then change your password.

Good to know:

- Your very first sign-in, and your first one after this feature arrived, never alert: there was no device to compare with.
- A device is recognised by a random id in a cookie, not by fingerprinting. A private window or deleted cookies make the same browser a new device again.
- The app taking over a sign-in you just made in the browser does not alert again.
- You can turn the alerts off with the switch on the security page. Turning them off asks for a confirmation, so a stolen session cannot silence them.

## Security events

The last 30 days of what happened to your account, newest first: sign-ins and failed sign-ins, new devices, signed-out devices, passkeys, linked services, password changes and access tokens. Failed attempts are marked red, things worth a look amber.