/**
 * The account list of the operator console: Identity's accounts of the installation
 * (`GET /identity/platform/accounts`: name, kind, status, owner, created) joined by id with
 * Entitlements' plan of each account (`GET /entitlements/accounts`: the stored assignment and the
 * plan that applies). Both lists are paged oldest first; the page of Entitlements at the same
 * offset usually holds the same accounts, the rest are fetched one by one
 * (`GET /entitlements/accounts/{id}` also answers for accounts without an assignment). The search
 * reads Identity's whole list (up to `SEARCH_CAP` accounts) because Identity has no search
 * parameter. Neither service returns mower data, and nothing here asks for any.
 */
import { entitlementsApi, identityApi, unwrap } from '#lib/api/client.js';
import type { components } from '#lib/generated/identity.js';
import type { Assigned, ResolvedPlan } from '#lib/plan/types.js';

export type PlatformAccount = components['schemas']['PlatformAccountOut'];

export interface AccountPlanInfo {
  assigned: Assigned | null;
  effective: ResolvedPlan;
}

export interface ConsoleAccount extends PlatformAccount {
  /** Null when Entitlements did not answer for this account. */
  plan: AccountPlanInfo | null;
}

export interface AccountPage {
  rows: ConsoleAccount[];
  total: number;
  offset: number;
  limit: number;
  /** Null when Entitlements did not answer. */
  billingEnabled: boolean | null;
}

export const PAGE_SIZE = 25;

/** The most accounts a search reads (8 requests of 200). */
export const SEARCH_CAP = 1600;

/** Join the Identity accounts with the plans found (missing plans stay null). */
export function joinAccounts(
  accounts: PlatformAccount[],
  plans: ReadonlyMap<string, AccountPlanInfo>,
): ConsoleAccount[] {
  return accounts.map((account) => ({ ...account, plan: plans.get(account.id) ?? null }));
}

/** Rows whose name, owner, id, kind or plan contains every word of `query` (case blind). */
export function filterAccounts(rows: ConsoleAccount[], query: string): ConsoleAccount[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return rows;
  return rows.filter((row) => {
    const text = [
      row.name,
      row.owner_username ?? '',
      row.id,
      row.kind,
      row.status,
      row.plan?.assigned?.plan ?? '',
      row.plan?.effective.plan ?? '',
    ]
      .join(' ')
      .toLowerCase();
    return words.every((word) => text.includes(word));
  });
}

/** Run `work` over `items` with at most `limit` calls at a time. */
async function mapLimited<T, R>(items: T[], limit: number, work: (item: T) => Promise<R>) {
  const results: R[] = new Array<R>(items.length);
  let next = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const index = next;
      next += 1;
      results[index] = await work(items[index]!);
    }
  });
  await Promise.all(runners);
  return results;
}

/** The plans of `accounts`: Entitlements' page `hint` first, the rest one by one. Null when
 * Entitlements does not answer at all. */
async function plansOf(
  accounts: PlatformAccount[],
  hint: { offset: number; limit: number } | null,
): Promise<{ plans: Map<string, AccountPlanInfo>; billingEnabled: boolean | null } | null> {
  const plans = new Map<string, AccountPlanInfo>();
  let billingEnabled: boolean | null = null;
  if (hint) {
    const page = await unwrap(
      entitlementsApi.GET('/api/v1/entitlements/accounts', {
        params: { query: { offset: hint.offset, limit: hint.limit } },
      }),
    ).catch(() => null);
    if (!page) return null;
    billingEnabled = page.billing_enabled;
    for (const item of page.items) {
      plans.set(item.account_id, { assigned: item.assigned, effective: item.effective });
    }
  }
  const missing = accounts.filter((a) => !plans.has(a.id));
  let answered = hint !== null;
  await mapLimited(missing, 4, async (account) => {
    try {
      const detail = await unwrap(
        entitlementsApi.GET('/api/v1/entitlements/accounts/{account_id}', {
          params: { path: { account_id: account.id } },
        }),
      );
      answered = true;
      billingEnabled ??= detail.effective.billing_enabled;
      plans.set(account.id, { assigned: detail.assigned ?? null, effective: detail.effective });
    } catch {
      // stays without plan
    }
  });
  return answered || accounts.length === 0 ? { plans, billingEnabled } : null;
}

/** One page of the account list with the plans. Throws when Identity fails. */
export async function loadAccountPage(offset: number, limit = PAGE_SIZE): Promise<AccountPage> {
  const accounts = await unwrap(
    identityApi.GET('/api/v1/identity/platform/accounts', {
      params: { query: { offset, limit } },
    }),
  );
  const found = await plansOf(accounts.items, { offset, limit });
  return {
    rows: joinAccounts(accounts.items, found?.plans ?? new Map()),
    total: accounts.total,
    offset: accounts.offset,
    limit: accounts.limit,
    billingEnabled: found?.billingEnabled ?? null,
  };
}

/** Every account of the installation from Identity, 200 per request, at most `cap`. */
export async function allPlatformAccounts(cap = SEARCH_CAP): Promise<PlatformAccount[]> {
  const all: PlatformAccount[] = [];
  for (;;) {
    const page = await unwrap(
      identityApi.GET('/api/v1/identity/platform/accounts', {
        params: { query: { offset: all.length, limit: 200 } },
      }),
    );
    all.push(...page.items);
    if (page.items.length === 0 || all.length >= page.total || all.length >= cap) return all;
  }
}

/** Search over the whole installation (Identity has no search parameter): every account is read
 * and matched by name, owner, id, kind and status; the first `limit` matches get their plans. */
export async function searchAccounts(query: string, limit = PAGE_SIZE): Promise<AccountPage> {
  const everyone = await allPlatformAccounts();
  const matches = filterAccounts(joinAccounts(everyone, new Map()), query);
  const shown = matches.slice(0, limit);
  const found = await plansOf(shown, null);
  return {
    rows: joinAccounts(shown, found?.plans ?? new Map()),
    total: matches.length,
    offset: 0,
    limit,
    billingEnabled: found?.billingEnabled ?? null,
  };
}

/** The Identity record of one account (Identity has no single-account operator endpoint).
 * Null when it is not found. */
export async function findPlatformAccount(id: string): Promise<PlatformAccount | null> {
  return (await allPlatformAccounts()).find((a) => a.id === id) ?? null;
}
