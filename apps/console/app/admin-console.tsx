"use client";

import { useMemo, useState, useTransition } from "react";
import type { AdminConsoleData, CaseDetail, CaseStatus, EvalReviewItem } from "../lib/admin-data";

const statusLabels: Record<CaseStatus, string> = {
  open: "Open",
  pending_approval: "Needs approval",
  executing: "Executing",
  resolved: "Resolved",
  failed: "Failed",
  canceled: "Canceled",
};

const typeLabels: Record<string, string> = {
  address_change_request: "Address change request",
  damaged_in_transit: "Damaged in transit",
  delivered_not_received: "Delivered not received",
  fraud_triage: "Fraud triage",
  inventory_conflict: "Inventory conflict",
  item_change_request: "Item change request",
  order_cancellation_request: "Order cancellation request",
  order_not_picked: "Order not picked",
  stuck_in_transit: "Stuck in transit",
  wismo: "WISMO",
};

type ActionState = {
  tone: "idle" | "success" | "error";
  message: string;
};

export function AdminConsole({ initialData }: { initialData: AdminConsoleData }) {
  const [cases, setCases] = useState(initialData.cases);
  const [selectedCaseId, setSelectedCaseId] = useState(initialData.cases[0]?.id ?? "");
  const [merchantId, setMerchantId] = useState(initialData.merchants[0]?.id ?? "all");
  const [filter, setFilter] = useState<CaseStatus | "all">("all");
  const [modifyNote, setModifyNote] = useState("");
  const [correctionNotes, setCorrectionNotes] = useState("");
  const [actionState, setActionState] = useState<ActionState>({ tone: "idle", message: "" });
  const [isPending, startTransition] = useTransition();

  const visibleCases = useMemo(
    () =>
      cases.filter((item) => {
        const merchantMatches = merchantId === "all" || item.merchant_id === merchantId;
        const statusMatches = filter === "all" || item.status === filter;
        return merchantMatches && statusMatches;
      }),
    [cases, filter, merchantId],
  );
  const selectedCase = cases.find((item) => item.id === selectedCaseId) ?? visibleCases[0];
  const metrics = useMemo(() => caseMetrics(cases), [cases]);
  const selectedEvalReviews = useMemo(
    () => initialData.evalReviews.filter((review) => review.case_id === selectedCase?.id),
    [initialData.evalReviews, selectedCase?.id],
  );
  const evalReviewCaseIds = useMemo(
    () => new Set(initialData.evalReviews.map((review) => review.case_id)),
    [initialData.evalReviews],
  );

  function updateCaseStatus(caseId: string, status: CaseStatus, resolution: Record<string, unknown>) {
    setCases((current) =>
      current.map((item) =>
        item.id === caseId
          ? {
              ...item,
              status,
              resolution,
              events: [
                ...item.events,
                {
                  id: `local-${Date.now()}`,
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
    );
  }

  function submitDecision(decision: "approve" | "modify" | "reject") {
    if (!selectedCase) {
      return;
    }
    const note = decision === "modify" ? modifyNote : undefined;
    startTransition(async () => {
      setActionState({ tone: "idle", message: "Submitting decision..." });
      const response = await fetch(`/api/cases/${selectedCase.id}/decision`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Console-Fixture-Mode": initialData.source === "fixture" ? "true" : "false",
        },
        body: JSON.stringify({
          decision,
          actor: "internal-console",
          note,
          modification: note ? { operator_note: note } : null,
        }),
      });
      if (!response.ok) {
        setActionState({ tone: "error", message: "Decision could not be submitted." });
        return;
      }
      const payload = (await response.json()) as { status?: CaseStatus };
      const status =
        payload.status ??
        (decision === "reject" ? "canceled" : decision === "modify" ? "pending_approval" : "executing");
      updateCaseStatus(selectedCase.id, status, { decision, actor: "internal-console", note });
      setActionState({ tone: "success", message: "Decision recorded." });
      setModifyNote("");
    });
  }

  function submitCorrection() {
    if (!selectedCase) {
      return;
    }
    startTransition(async () => {
      setActionState({ tone: "idle", message: "Recording correction..." });
      const response = await fetch(`/api/cases/${selectedCase.id}/corrections`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Console-Fixture-Mode": initialData.source === "fixture" ? "true" : "false",
        },
        body: JSON.stringify({
          expected_resolution: {
            case_type: selectedCase.type,
            operator_notes: correctionNotes,
          },
          notes: correctionNotes,
        }),
      });
      if (!response.ok) {
        setActionState({ tone: "error", message: "Correction could not be queued." });
        return;
      }
      setActionState({ tone: "success", message: "Correction queued for evals." });
      setCorrectionNotes("");
    });
  }

  return (
    <main className="adminShell">
      <header className="topbar" aria-label="Console header">
        <div>
          <p className="eyebrow">Internal admin</p>
          <h1>Order Exception Console</h1>
        </div>
        <div className="topbarActions">
          <span className={`sourcePill ${initialData.source}`}>{initialData.source}</span>
          <a className="iconButton textButton" href={`${initialData.apiBaseUrl}/health`}>
            API Health
          </a>
        </div>
      </header>

      <section className="metricBand" aria-label="Case metrics">
        <Metric label="Open" value={metrics.open} />
        <Metric label="Needs approval" value={metrics.pending_approval} />
        <Metric label="Executing" value={metrics.executing} />
        <Metric label="Resolved" value={metrics.resolved} />
        <Metric label="Eval review" value={initialData.evalReviews.length} />
      </section>

      <section className="controlStrip" aria-label="Console filters">
        <div className="segmented" aria-label="Merchant switcher">
          <button
            className={merchantId === "all" ? "active" : ""}
            type="button"
            onClick={() => setMerchantId("all")}
          >
            All merchants
          </button>
          {initialData.merchants.map((merchant) => (
            <button
              className={merchantId === merchant.id ? "active" : ""}
              key={merchant.id}
              type="button"
              onClick={() => setMerchantId(merchant.id)}
            >
              {merchant.name}
            </button>
          ))}
        </div>
        <label className="selectLabel">
          Status
          <select value={filter} onChange={(event) => setFilter(event.target.value as CaseStatus | "all")}>
            <option value="all">All</option>
            <option value="open">Open</option>
            <option value="pending_approval">Needs approval</option>
            <option value="executing">Executing</option>
            <option value="resolved">Resolved</option>
            <option value="failed">Failed</option>
            <option value="canceled">Canceled</option>
          </select>
        </label>
      </section>

      <section className="workspace" aria-label="Case review workspace">
        <aside className="caseQueue" aria-label="Case queue">
          <div className="queueHeader">
            <div>
              <p className="eyebrow">Queue</p>
              <h2>{visibleCases.length} cases</h2>
            </div>
          </div>
          <div className="caseList">
            {visibleCases.map((item) => (
              <button
                className={`caseRow ${selectedCase?.id === item.id ? "active" : ""}`}
                key={item.id}
                type="button"
                onClick={() => setSelectedCaseId(item.id)}
              >
                <span className={`statusDot ${item.status}`} aria-hidden="true" />
                <span>
                  <strong>{typeLabels[item.type] ?? item.type}</strong>
                  <small>{String(item.subject_ref.order_name ?? item.subject_ref.order_id ?? item.id)}</small>
                </span>
                <em>{evalReviewCaseIds.has(item.id) ? "Eval review" : statusLabels[item.status]}</em>
              </button>
            ))}
          </div>
        </aside>

        {selectedCase ? (
          <CaseReview
            actionState={actionState}
            caseItem={selectedCase}
            correctionNotes={correctionNotes}
            evalReviews={selectedEvalReviews}
            fopYaml={initialData.fopYaml}
            isPending={isPending}
            modifyNote={modifyNote}
            onCorrectionNotesChange={setCorrectionNotes}
            onDecision={submitDecision}
            onModifyNoteChange={setModifyNote}
            onSubmitCorrection={submitCorrection}
          />
        ) : (
          <section className="emptyState">
            <h2>No cases match the current filters.</h2>
          </section>
        )}
      </section>
    </main>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <article className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function CaseReview({
  actionState,
  caseItem,
  correctionNotes,
  evalReviews,
  fopYaml,
  isPending,
  modifyNote,
  onCorrectionNotesChange,
  onDecision,
  onModifyNoteChange,
  onSubmitCorrection,
}: {
  actionState: ActionState;
  caseItem: CaseDetail;
  correctionNotes: string;
  evalReviews: EvalReviewItem[];
  fopYaml: string;
  isPending: boolean;
  modifyNote: string;
  onCorrectionNotesChange: (value: string) => void;
  onDecision: (decision: "approve" | "modify" | "reject") => void;
  onModifyNoteChange: (value: string) => void;
  onSubmitCorrection: () => void;
}) {
  const latestProposal = [...caseItem.events]
    .reverse()
    .find((event) => event.kind.includes("proposal"));
  return (
    <section className="caseDetail" aria-label="Case detail">
      <div className="detailHeader">
        <div>
          <p className="eyebrow">{caseItem.merchant_name}</p>
          <h2>{typeLabels[caseItem.type] ?? caseItem.type}</h2>
          <p className="subjectLine">
            {String(caseItem.subject_ref.order_name ?? caseItem.subject_ref.order_id ?? caseItem.id)}
          </p>
        </div>
        <span className={`statusBadge ${caseItem.status}`}>{statusLabels[caseItem.status]}</span>
      </div>

      <div className="reviewGrid">
        <article className="decisionPanel">
          <p className="eyebrow">Recommendation</p>
          <h3>{String(latestProposal?.payload.summary ?? "No proposal yet")}</h3>
          <p>{String(latestProposal?.payload.recommendation ?? "Waiting for agent output.")}</p>
          <div className="buttonRow">
            <button disabled={isPending} type="button" onClick={() => onDecision("approve")}>
              Approve
            </button>
            <button disabled={isPending} type="button" onClick={() => onDecision("reject")}>
              Reject
            </button>
          </div>
          <label className="fieldLabel">
            Modification
            <textarea
              onChange={(event) => onModifyNoteChange(event.target.value)}
              placeholder="Change the resolution, customer message, or tool plan."
              value={modifyNote}
            />
          </label>
          <button disabled={isPending || !modifyNote.trim()} type="button" onClick={() => onDecision("modify")}>
            Submit modification
          </button>
          {actionState.message ? (
            <p className={`actionMessage ${actionState.tone}`}>{actionState.message}</p>
          ) : null}
        </article>

        <article className="tracePanel">
          <p className="eyebrow">LangSmith trace</p>
          {caseItem.langsmith_trace_url ? (
            <iframe src={caseItem.langsmith_trace_url} title="LangSmith trace" />
          ) : (
            <div className="tracePlaceholder">
              <strong>{caseItem.langgraph_thread_id ?? "No thread yet"}</strong>
              <span>Trace link appears after a shared LangSmith run is attached.</span>
            </div>
          )}
        </article>
      </div>

      <section className="timelineSection" aria-label="Audit timeline">
        <div className="sectionTitle">
          <p className="eyebrow">Audit log</p>
          <h3>{caseItem.events.length} events</h3>
        </div>
        <div className="timeline">
          {caseItem.events.map((event) => (
            <article className="timelineItem" key={event.id}>
              <div>
                <strong>{event.kind}</strong>
                <span>{event.actor}</span>
              </div>
              <time>{formatTime(event.created_at)}</time>
              <pre>{JSON.stringify(event.payload, null, 2)}</pre>
            </article>
          ))}
        </div>
      </section>

      <section className="lowerGrid" aria-label="Case support panels">
        <article className="correctionPanel">
          <p className="eyebrow">Correct this</p>
          <label className="fieldLabel">
            Ground truth
            <textarea
              onChange={(event) => onCorrectionNotesChange(event.target.value)}
              placeholder="Record the expected resolution for evals."
              value={correctionNotes}
            />
          </label>
          <button disabled={isPending || !correctionNotes.trim()} type="button" onClick={onSubmitCorrection}>
            Queue correction
          </button>
        </article>
        <article className="evalReviewPanel">
          <p className="eyebrow">Eval review</p>
          {evalReviews.length > 0 ? (
            <div className="evalReviewList">
              {evalReviews.map((review) => (
                <div className="evalReviewItem" key={review.id}>
                  <div>
                    <strong>{review.score}/5</strong>
                    <span>{review.passed ? "Passed" : "Needs review"}</span>
                  </div>
                  <p>{review.reason}</p>
                  <pre>{JSON.stringify(review.payload, null, 2)}</pre>
                </div>
              ))}
            </div>
          ) : (
            <p className="mutedText">No queued judge findings for this case.</p>
          )}
        </article>
        <article className="fopPanel">
          <p className="eyebrow">Phase 0 FOP YAML</p>
          <pre>{fopYaml}</pre>
        </article>
      </section>
    </section>
  );
}

function caseMetrics(cases: CaseDetail[]) {
  return cases.reduce(
    (counts, item) => ({ ...counts, [item.status]: counts[item.status] + 1 }),
    { open: 0, pending_approval: 0, executing: 0, resolved: 0, failed: 0, canceled: 0 },
  );
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
