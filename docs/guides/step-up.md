# Confirming critical actions

For actions that are hard to undo or that change who can get in, MowDeck asks you to confirm it is really you, even though you are signed in. A sheet opens: **Confirm with passkey** (Face ID, Touch ID, Windows Hello, your phone) or type your **password**. After that your action simply continues.

## When does it ask?

- deleting a mower or removing a linked Mammotion account,
- creating an API token,
- creating or removing users, changing roles and admin rights, setting a sub-user's password,
- removing a passkey, unlinking GitHub or Apple, adding a passkey or linking one,
- deleting the account.

Your confirmation counts for **5 minutes** (your administrator can change that). Signing in counts as a confirmation too, so right after signing in it does not ask again.

## In the iOS and Android app

Passkeys cannot be used inside the app, so the sheet only asks for your **password** there.

## API tokens

API tokens, for example the one Home Assistant uses, cannot confirm anything. These actions are refused for them with the message that an interactive sign-in is needed; do them in the web app.
