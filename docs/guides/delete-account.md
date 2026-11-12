# Deleting your account or yourself

You can delete your data inside MowDeck, in the browser and in the iOS and Android app: **Settings -> Profile**, the last section, below **Sign out**. What it offers depends on your role in the account, and MowDeck explains each step before anything happens.

## You are a member of someone else's account: "Delete me"

- You are removed **at once and for good**. There is no waiting period and no way back; an administrator of the account can create a new user for you later.
- Deleted: your user (name, e-mail address, password, passkeys, sign-ins), your notification settings and devices, your own AI provider keys. Your user name and e-mail address are free again.
- The mowers, sites, schedules and the record of commands belong to the account and stay, without your name. Schedules you own stop until someone else in the account turns them on again. Share links you created stop working.
- After you confirm (type your user name, then your password or passkey if MowDeck asks), you are signed out and back on the sign-in page.

## You own the account and are its only member: "Delete account"

- The whole account and everything in it is deleted, in every part of MowDeck, **after a waiting period of 7 days** (your administrator can change it). The dialog shows the exact date.
- Until that date you keep full access: your mowers keep running on their schedules and you can stop them at any time. A banner on every page shows the date and has **Cancel deletion**.
- API tokens stop working at once, and until the date no members or tokens can be added.
- If this server sells subscriptions: when the deletion starts, the **unused part of the current paid period is refunded** to the original payment method, and your subscriptions end. The amount is worked out on that day, from what you paid for the period and the days left of it. Some payment methods (a bank transfer, for example) take a few days to show the refund. Your customer record at the payment provider is deleted; invoices and payment records stay with the payment provider.

## The account has other members

The owner cannot delete an account that still has other members. Remove them first under **Settings -> Users**: deleting the account never removes other people. Each member can also delete themselves.

## What may remain for a while

- After "Delete me": your name in the internal record of sent commands for up to 24 hours and in the record of camera sessions for up to 30 days; then it is removed automatically.
- After an account deletion: technical notices without content, for up to 30 days.
- Backups made by the operator of your MowDeck server may still contain the data until they are replaced. How long they are kept is up to the operator; ask them.
- Outside MowDeck: what the push service of a browser or an ntfy server already holds, requests already sent to a cloud AI provider, and what a mower manufacturer keeps in its own cloud stay with them under their terms. MowDeck deletes the stored logins of linked manufacturer accounts and the pictures and keys it stores itself.

## In the public demo

The demo deletes nothing: the section is shown but locked. Its sandbox accounts are removed automatically.
