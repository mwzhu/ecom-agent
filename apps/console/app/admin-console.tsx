"use client";

import { useEffect, useMemo, useState, useTransition } from "react";
import type { AdminConsoleData, CaseDetail, CaseEvent, CaseStatus, EvalReviewItem, FopSummary } from "../lib/admin-data";

const POLL_INTERVAL_MS = 15_000;

const statusLabels: Record<CaseStatus, string> = {
  open: "Open",
  pending_approval: "Needs approval",
  executing: "Executing",
  resolved: "Resolved",
  failed: "Needs attention",
  canceled: "Canceled",
};

const typeLabels: Record<string, string> = {
  address_change_request: "Address change",
  damaged_in_transit: "Damaged item",
  delivered_not_received: "Missing delivery",
  fraud_triage: "Fraud review",
  inventory_conflict: "Inventory issue",
  item_change_request: "Item change",
  order_cancellation_request: "Cancellation",
  order_not_picked: "Warehouse delay",
  stuck_in_transit: "Shipping delay",
  wismo: "Tracking question",
};

type Decision = "approve" | "modify" | "reject";

type ActionState = {
  caseId: string | null;
  tone: "idle" | "success" | "error";
  message: string;
};

export function AdminConsole({ initialData }: { initialData: AdminConsoleData }) {
  const [data, setData] = useState(initialData);
  const [showAllCases, setShowAllCases] = useState(false);
  const [showPii, setShowPii] = useState(false);
  const [selectedCaseId, setSelectedCaseId] = useState(firstActionableCase(initialData.cases)?.id ?? initialData.cases[0]?.id ?? "");
  const [messageDrafts, setMessageDrafts] = useState<Record<string, string>>({});
  const [operatorNotes, setOperatorNotes] = useState<Record<string, string>>({});
  const [sendAfterApproval, setSendAfterApproval] = useState<Record<string, boolean>>({});
  const [correctionNotes, setCorrectionNotes] = useState<Record<string, string>>({});
  const [pollMessage, setPollMessage] = useState("");
  const [actionState, setActionState] = useState<ActionState>({ caseId: null, tone: "idle", message: "" });
  const [isPending, startTransition] = useTransition();

  useEffect(() => {
    if (data.source !== "api") {
      return;
    }

    const poll = async () => {
      try {
        const response = await fetch("/api/admin-data", { cache: "no-store" });
        if (!response.ok) {
          throw new Error(`Polling failed with ${response.status}`);
        }
        const payload = (await response.json()) as AdminConsoleData;
        setData(payload);
        setPollMessage(`Synced ${formatClock(payload.loadedAt)}`);
      } catch {
        setPollMessage("Live refresh paused");
      }
    };

    const interval = window.setInterval(poll, POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [data.source]);

  const cases = data.cases;
  const reviewCases = useMemo(() => cases.filter(isActionableCase), [cases]);
  const queueCases = showAllCases ? cases : reviewCases;
  const selectedCase = cases.find((item) => item.id === selectedCaseId) ?? queueCases[0] ?? cases[0];
  const fopIndex = useMemo(() => new Map(data.fops.map((fop) => [fop.id, fop])), [data.fops]);
  const failedCount = cases.filter((item) => item.status === "failed").length;
  const resolvedCount = cases.filter((item) => item.status === "resolved").length;

  function updateLocalDecision(caseId: string, status: CaseStatus, resolution: Record<string, unknown>) {
    setData((current) => ({
      ...current,
      cases: current.cases.map((item) =>
        item.id === caseId
          ? {
              ...item,
              status,
              resolution: { ...(item.resolution ?? {}), ...resolution },
              events: [
                ...item.events,
                {
                  id: `local-${caseId}-${Date.now()}`,
                  kind: "case.decision_submitted",
                  actor: "human",
                  created_at: new Date().toISOString(),
                  langsmith_run_id: null,
                  payload: resolution,
                },
              ],
            }
          : item,
      ),
      loadedAt: new Date().toISOString(),
    }));
  }

  function submitDecision(caseItem: CaseDetail, decision: Decision) {
    const proposal = proposalFor(caseItem);
    const note = operatorNotes[caseItem.id]?.trim();
    const customerMessage = messageDrafts[caseItem.id] ?? stringValue(proposal.customer_message);
    const shouldSend = sendAfterApproval[caseItem.id] ?? true;

    startTransition(async () => {
      setActionState({ caseId: caseItem.id, tone: "idle", message: "Submitting..." });
      const response = await fetch(`/api/cases/${caseItem.id}/decision`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Console-Fixture-Mode": data.source === "fixture" ? "true" : "false",
        },
        body: JSON.stringify({
          decision,
          source: "console",
          actor: "operator-console",
          note,
          modification:
            decision === "reject"
              ? note
                ? { operator_note: note }
                : null
              : {
                  operator_note: note,
                  customer_message: customerMessage,
                  send_customer_message: shouldSend,
                },
        }),
      });
      if (!response.ok) {
        setActionState({ caseId: caseItem.id, tone: "error", message: "Could not submit decision." });
        return;
      }
      const payload = (await response.json()) as { status?: CaseStatus };
      const status =
        payload.status ??
        (decision === "reject" ? "canceled" : decision === "modify" ? "pending_approval" : "executing");
      updateLocalDecision(caseItem.id, status, {
        decision,
        source: "console",
        actor: "operator-console",
        note,
        customer_message: customerMessage,
        send_customer_message: shouldSend,
      });
      setActionState({ caseId: caseItem.id, tone: "success", message: "Decision recorded." });
      setOperatorNotes((current) => ({ ...current, [caseItem.id]: "" }));
    });
  }

  function submitCorrection(caseItem: CaseDetail) {
    const notes = correctionNotes[caseItem.id]?.trim();
    if (!notes) {
      return;
    }
    startTransition(async () => {
      setActionState({ caseId: caseItem.id, tone: "idle", message: "Recording feedback..." });
      const response = await fetch(`/api/cases/${caseItem.id}/corrections`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Console-Fixture-Mode": data.source === "fixture" ? "true" : "false",
        },
        body: JSON.stringify({
          expected_resolution: {
            case_type: caseItem.type,
            operator_notes: notes,
          },
          notes,
        }),
      });
      if (!response.ok) {
        setActionState({ caseId: caseItem.id, tone: "error", message: "Could not record feedback." });
        return;
      }
      setActionState({ caseId: caseItem.id, tone: "success", message: "Feedback queued." });
      setCorrectionNotes((current) => ({ ...current, [caseItem.id]: "" }));
    });
  }

  return (
    <main className="consoleShell">
      <header className="appHeader">
        <div>
          <p className="eyebrow">Operator Console</p>
          <h1>Review agent decisions</h1>
          <p className="headerCopy">Start with the queue. Open a case, check the recommendation, then approve or ask for a change.</p>
        </div>
        <div className="headerActions">
          <span className={`modePill ${data.source}`}>{data.source === "fixture" ? "Simulation" : "Live"}</span>
          <label className="toggleLabel">
            <input checked={showPii} onChange={(event) => setShowPii(event.target.checked)} type="checkbox" />
            Reveal names
          </label>
        </div>
      </header>

      {data.apiError ? (
        <section className="errorBanner" aria-label="Live API connection error">
          <strong>Live API mode is on, but the console could not load real cases.</strong>
          <span>{data.apiError}</span>
        </section>
      ) : null}

      <section className="statusStrip" aria-label="Console status">
        <span>{reviewCases.length} need review</span>
        <span>{failedCount} need attention</span>
        <span>{resolvedCount} resolved</span>
        <span>{pollMessage || `Loaded ${formatClock(data.loadedAt)}`}</span>
      </section>

      <section className="reviewLayout" aria-label="Case review workspace">
        <aside className="queuePanel" aria-label="Case queue">
          <div className="queueHeader">
            <div>
              <h2>{showAllCases ? "All cases" : "Needs review"}</h2>
              <p>{showAllCases ? `${cases.length} total cases` : "Only cases that need a human."}</p>
            </div>
            <button className="textButton" type="button" onClick={() => setShowAllCases((current) => !current)}>
              {showAllCases ? "Show needs review" : "Show all"}
            </button>
          </div>
          <div className="caseList">
            {queueCases.length > 0 ? (
              queueCases.map((caseItem) => (
                <button
                  className={`caseListItem ${selectedCase?.id === caseItem.id ? "active" : ""}`}
                  key={caseItem.id}
                  type="button"
                  onClick={() => setSelectedCaseId(caseItem.id)}
                >
                  <span className={`statusDot ${caseItem.status}`} aria-hidden="true" />
                  <span>
                    <strong>{typeLabels[caseItem.type] ?? caseItem.type}</strong>
                    <small>{orderLabel(caseItem)}</small>
                  </span>
                  <em>{statusLabels[caseItem.status]}</em>
                </button>
              ))
            ) : (
              <EmptyState title="No cases waiting" detail="The agent does not need a human decision right now." />
            )}
          </div>
        </aside>

        {selectedCase ? (
          <CaseReviewPanel
            actionState={actionState.caseId === selectedCase.id ? actionState : null}
            caseItem={selectedCase}
            correctionNote={correctionNotes[selectedCase.id] ?? ""}
            evalReviews={data.evalReviews.filter((review) => review.case_id === selectedCase.id)}
            fopIndex={fopIndex}
            isPending={isPending}
            messageDraft={messageDrafts[selectedCase.id]}
            operatorNote={operatorNotes[selectedCase.id] ?? ""}
            onCorrectionNoteChange={(value) =>
              setCorrectionNotes((current) => ({ ...current, [selectedCase.id]: value }))
            }
            onDecision={submitDecision}
            onMessageDraftChange={(value) =>
              setMessageDrafts((current) => ({ ...current, [selectedCase.id]: value }))
            }
            onOperatorNoteChange={(value) =>
              setOperatorNotes((current) => ({ ...current, [selectedCase.id]: value }))
            }
            onSendAfterApprovalChange={(value) =>
              setSendAfterApproval((current) => ({ ...current, [selectedCase.id]: value }))
            }
            onSubmitCorrection={() => submitCorrection(selectedCase)}
            sendAfterApproval={sendAfterApproval[selectedCase.id] ?? true}
            showPii={showPii}
          />
        ) : (
          <EmptyState title="No case selected" detail="Choose a case from the queue to review it." />
        )}
      </section>
    </main>
  );
}

function CaseReviewPanel({
  actionState,
  caseItem,
  correctionNote,
  evalReviews,
  fopIndex,
  isPending,
  messageDraft,
  operatorNote,
  onCorrectionNoteChange,
  onDecision,
  onMessageDraftChange,
  onOperatorNoteChange,
  onSendAfterApprovalChange,
  onSubmitCorrection,
  sendAfterApproval,
  showPii,
}: {
  actionState: ActionState | null;
  caseItem: CaseDetail;
  correctionNote: string;
  evalReviews: EvalReviewItem[];
  fopIndex: Map<string, FopSummary>;
  isPending: boolean;
  messageDraft?: string;
  operatorNote: string;
  onCorrectionNoteChange: (value: string) => void;
  onDecision: (caseItem: CaseDetail, decision: Decision) => void;
  onMessageDraftChange: (value: string) => void;
  onOperatorNoteChange: (value: string) => void;
  onSendAfterApprovalChange: (value: boolean) => void;
  onSubmitCorrection: () => void;
  sendAfterApproval: boolean;
  showPii: boolean;
}) {
  const proposal = proposalFor(caseItem);
  const policy = matchedPolicyText(caseItem, fopIndex);
  const customerMessage = messageDraft ?? stringValue(proposal.customer_message);

  return (
    <section className="reviewPanel" aria-label="Selected case">
      <div className="caseHeader">
        <div>
          <p className="eyebrow">{statusLabels[caseItem.status]}</p>
          <h2>{typeLabels[caseItem.type] ?? caseItem.type}</h2>
          <p>{orderLabel(caseItem)} · {maskedCustomer(caseItem, showPii)}</p>
        </div>
        <span className={`riskPill ${riskLevel(caseItem)}`}>{riskLevel(caseItem)} risk</span>
      </div>

      <section className="answerGrid" aria-label="Case essentials">
        <AnswerCard title="What happened" value={stringValue(proposal.summary) || "The agent has not summarized this case yet."} />
        <AnswerCard title="Agent recommends" value={stringValue(proposal.action_label) || stringValue(proposal.recommendation)} />
        <AnswerCard title="Why" value={approvalReason(proposal)} />
      </section>

      <section className="simpleCard">
        <h3>Customer and order</h3>
        <dl className="factsList">
          <InfoRow label="Customer" value={maskedCustomer(caseItem, showPii)} />
          <InfoRow label="Order" value={orderLabel(caseItem)} />
          <InfoRow label="Value" value={stringValue(caseItem.subject_ref.value)} />
          <InfoRow label="Fulfillment" value={stringValue(caseItem.subject_ref.fulfillment_status)} />
          <InfoRow label="Refunds in 60d" value={stringValue(caseItem.subject_ref.refund_count_60d)} />
          <InfoRow label="Risk note" value={riskFactorsFor(caseItem)[0] ?? "No major risk note"} />
        </dl>
      </section>

      {policy ? (
        <section className="policyNote">
          <strong>Matched policy</strong>
          <span>{policy}</span>
        </section>
      ) : null}

      <section className="simpleCard">
        <h3>Customer message</h3>
        <textarea
          className="messageBox"
          onChange={(event) => onMessageDraftChange(event.target.value)}
          placeholder="The agent has not drafted a message yet."
          value={customerMessage}
        />
        <label className="toggleLabel">
          <input
            checked={sendAfterApproval}
            onChange={(event) => onSendAfterApprovalChange(event.target.checked)}
            type="checkbox"
          />
          Send this message after approval
        </label>
      </section>

      <section className="decisionCard">
        <label>
          Note or change request
          <textarea
            onChange={(event) => onOperatorNoteChange(event.target.value)}
            placeholder="Optional: explain a change, rejection, or escalation."
            value={operatorNote}
          />
        </label>
        <div className="buttonRow">
          <button disabled={isPending} type="button" onClick={() => onDecision(caseItem, "approve")}>
            Approve
          </button>
          <button disabled={isPending || !operatorNote.trim()} type="button" onClick={() => onDecision(caseItem, "modify")}>
            Ask for change
          </button>
          <button className="dangerButton" disabled={isPending} type="button" onClick={() => onDecision(caseItem, "reject")}>
            Reject
          </button>
        </div>
        {actionState?.message ? <p className={`actionMessage ${actionState.tone}`}>{actionState.message}</p> : null}
      </section>

      <details className="moreDetails">
        <summary>More details</summary>
        <div className="detailsGrid">
          <ToolSummary caseItem={caseItem} />
          <ActivitySummary events={caseItem.events} />
          <FeedbackBox
            correctionNote={correctionNote}
            evalReviews={evalReviews}
            isPending={isPending}
            onCorrectionNoteChange={onCorrectionNoteChange}
            onSubmitCorrection={onSubmitCorrection}
          />
        </div>
      </details>
    </section>
  );
}

function AnswerCard({ title, value }: { title: string; value: string }) {
  return (
    <article className="answerCard">
      <span>{title}</span>
      <strong>{value || "Not recorded"}</strong>
    </article>
  );
}

function ToolSummary({ caseItem }: { caseItem: CaseDetail }) {
  const calls = toolCallsFor(caseItem);
  return (
    <section className="detailBlock">
      <h3>Tools</h3>
      {calls.length > 0 ? (
        <div className="compactList">
          {calls.map((call, index) => (
            <div className="compactItem" key={`${call.tool}-${index}`}>
              <strong>{call.tool}</strong>
              <span>{call.status}</span>
            </div>
          ))}
        </div>
      ) : (
        <p>No tool calls recorded.</p>
      )}
    </section>
  );
}

function ActivitySummary({ events }: { events: CaseEvent[] }) {
  return (
    <section className="detailBlock">
      <h3>Activity</h3>
      <div className="compactList">
        {[...events].slice(-5).reverse().map((event) => (
          <div className="compactItem" key={event.id}>
            <strong>{plainEventLabel(event)}</strong>
            <span>{formatClock(event.created_at)}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function FeedbackBox({
  correctionNote,
  evalReviews,
  isPending,
  onCorrectionNoteChange,
  onSubmitCorrection,
}: {
  correctionNote: string;
  evalReviews: EvalReviewItem[];
  isPending: boolean;
  onCorrectionNoteChange: (value: string) => void;
  onSubmitCorrection: () => void;
}) {
  return (
    <section className="detailBlock">
      <h3>Agent feedback</h3>
      {evalReviews.length > 0 ? <p>{evalReviews[0]?.reason}</p> : <p>No evaluator finding for this case.</p>}
      <textarea
        onChange={(event) => onCorrectionNoteChange(event.target.value)}
        placeholder="Optional: tell the agent team what should have happened."
        value={correctionNote}
      />
      <button disabled={isPending || !correctionNote.trim()} type="button" onClick={onSubmitCorrection}>
        Send feedback
      </button>
    </section>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value || "Not recorded"}</dd>
    </>
  );
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <article className="emptyState">
      <h3>{title}</h3>
      <p>{detail}</p>
    </article>
  );
}

function firstActionableCase(cases: CaseDetail[]) {
  return cases.find(isActionableCase);
}

function isActionableCase(caseItem: CaseDetail) {
  return caseItem.status === "pending_approval" || caseItem.status === "failed";
}

function proposalFor(caseItem: CaseDetail): Record<string, unknown> {
  const graph = objectValue(caseItem.resolution?.graph);
  const latestProposal = [...caseItem.events].reverse().find((event) => event.kind.includes("proposal"));
  return { ...graph, ...(latestProposal?.payload ?? {}) };
}

function matchedPolicyText(caseItem: CaseDetail, fopIndex: Map<string, FopSummary>) {
  const id = stringList(proposalFor(caseItem).matched_fop_ids)[0];
  if (!id) {
    return "";
  }
  return fopIndex.get(id)?.nl_text ?? id;
}

function toolCallsFor(caseItem: CaseDetail) {
  const proposalCalls = arrayValue(proposalFor(caseItem).tool_calls).map(normalizeToolCall);
  const eventCalls = caseItem.events.flatMap((event) => arrayValue(event.payload.tool_calls).map(normalizeToolCall));
  const all = [...proposalCalls, ...eventCalls].filter((call) => call.tool);
  const seen = new Set<string>();
  return all.filter((call) => {
    const key = `${call.tool}-${call.status}`;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function normalizeToolCall(value: unknown) {
  if (typeof value === "string") {
    return { tool: value, status: "planned" };
  }
  const payload = objectValue(value);
  return {
    tool: stringValue(payload.tool),
    status: stringValue(payload.status) || "planned",
  };
}

function riskLevel(caseItem: CaseDetail) {
  const proposalRisk = stringValue(proposalFor(caseItem).risk_level).toLowerCase();
  if (proposalRisk === "high" || proposalRisk === "medium" || proposalRisk === "low") {
    return proposalRisk;
  }
  if (caseItem.status === "failed" || currencyNumber(caseItem.subject_ref.value) > 500) {
    return "high";
  }
  if ((numberValue(caseItem.subject_ref.refund_count_60d) ?? 0) > 0) {
    return "medium";
  }
  return "low";
}

function riskFactorsFor(caseItem: CaseDetail): string[] {
  const explicit = stringList(proposalFor(caseItem).risk_factors);
  if (explicit.length > 0) {
    return explicit;
  }
  return [stringValue(caseItem.subject_ref.risk_flag) || "No major risk note"];
}

function approvalReason(proposal: Record<string, unknown>) {
  const explicit = stringValue(proposal.approval_reason);
  if (explicit) {
    return explicit;
  }
  const approvals = stringList(proposal.required_approvals);
  return approvals.length > 0 ? `Requires ${approvals.map(labelize).join(", ")} approval.` : "No approval reason recorded.";
}

function maskedCustomer(caseItem: CaseDetail, showPii: boolean) {
  const name = stringValue(caseItem.subject_ref.customer_name) || "Customer";
  const email = stringValue(caseItem.subject_ref.customer_email);
  if (showPii || !email) {
    return email ? `${name} · ${email}` : name;
  }
  const [prefix, domain] = email.split("@");
  return `${name} · ${prefix?.slice(0, 2) ?? ""}***@${domain ?? "masked"}`;
}

function orderLabel(caseItem: CaseDetail) {
  return stringValue(caseItem.subject_ref.order_name) || stringValue(caseItem.subject_ref.order_id) || caseItem.id;
}

function plainEventLabel(event: CaseEvent) {
  if (event.kind.includes("proposal")) {
    return "Agent proposed an action";
  }
  if (event.kind.includes("decision")) {
    return "Human decision recorded";
  }
  if (event.kind.includes("tool")) {
    return "Tool activity recorded";
  }
  if (event.kind.includes("webhook")) {
    return "New event received";
  }
  if (event.kind.includes("customer")) {
    return "Customer message received";
  }
  return labelize(event.kind);
}

function labelize(value: string) {
  return value
    .replaceAll("_", " ")
    .replaceAll(".", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatClock(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.length > 0) : [];
}

function stringValue(value: unknown) {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "";
}

function numberValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const parsed = Number(value.replace(/[$,]/g, ""));
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function currencyNumber(value: unknown) {
  return numberValue(value) ?? 0;
}
