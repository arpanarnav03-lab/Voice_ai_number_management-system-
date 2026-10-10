import { PHONE_RE } from "./api";

/** The Test call button is enabled only when there is a selection, a test agent and a valid reviewer phone. */
export function canStartTest(o: { selectedCount: number; agentChosen: boolean; reviewerPhone: string; busy?: boolean }): boolean {
  return o.selectedCount > 0 && o.agentChosen && PHONE_RE.test(o.reviewerPhone.trim()) && !o.busy;
}
