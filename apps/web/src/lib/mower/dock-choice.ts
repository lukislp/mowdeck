/**
 * Whether the return to the dock asks to end or keep the running job. Only a mower whose
 * connection keeps a paused job on the way home (`capabilities.dock_keeps_job`) offers the
 * choice; every other mower goes home with a plain `return_to_dock`, whatever it does with its
 * job. Decided from the capabilities alone, never from the vendor.
 */
export function dockChoiceOffered(
  capabilities: object | null | undefined,
  jobActive: boolean,
): boolean {
  const keeps = (capabilities as { dock_keeps_job?: unknown } | null | undefined)?.dock_keeps_job;
  return jobActive && keeps === true;
}
