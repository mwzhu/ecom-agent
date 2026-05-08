"use client";

import { useEffect, useMemo, useState, useTransition } from "react";
import type { ReactNode } from "react";
import type {
  AdminConsoleData,
  CaseDetail,
  CaseEvent,
  CaseStatus,
  EvalReviewItem,
  FopSummary,
  IntegrationHealth,
} from "../lib/admin-data";
import { readJsonResponse } from "../lib/http-json";

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
type ProviderId = "shopify" | "stripe" | "gorgias";

type ActionState = {
  caseId: string | null;
  tone: "idle" | "success" | "error";
  message: string;
};

type OnboardingState = {
  provider: ProviderId | null;
  tone: "idle" | "success" | "error";
  message: string;
};

export function AdminConsole({ initialData }: { initialData: AdminConsoleData }) {
  const [data, setData] = useState(initialData);
  const [showAllCases, setShowAllCases] = useState(false);
  const [showSyntheticOnly, setShowSyntheticOnly] = useState(false);
  const [showPii, setShowPii] = useState(false);
  const [selectedCaseId, setSelectedCaseId] = useState(firstActionableCase(initialData.cases)?.id ?? initialData.cases[0]?.id ?? "");
  const [messageDrafts, setMessageDrafts] = useState<Record<string, string>>({});
  const [operatorNotes, setOperatorNotes] = useState<Record<string, string>>({});
  const [sendAfterApproval, setSendAfterApproval] = useState<Record<string, boolean>>({});
  const [correctionNotes, setCorrectionNotes] = useState<Record<string, string>>({});
  const [shopifyShop, setShopifyShop] = useState("");
  const [gorgiasAccount, setGorgiasAccount] = useState("");
  const [stripeRestrictedKey, setStripeRestrictedKey] = useState("");
  const [stripeAccountId, setStripeAccountId] = useState("");
  const [pollMessage, setPollMessage] = useState("");
  const [actionState, setActionState] = useState<ActionState>({ caseId: null, tone: "idle", message: "" });
  const [onboardingState, setOnboardingState] = useState<OnboardingState>({
    provider: null,
    tone: "idle",
    message: "",
  });
  const [isPending, startTransition] = useTransition();

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const setupStatus = params.get("setup");
    if (!setupStatus) {
      return;
    }

    const provider = toProviderId(params.get("provider"));
    if (setupStatus === "connected") {
      setOnboardingState({
        provider,
        tone: "success",
        message: provider ? `${providerLabel(provider)} is connected.` : "Provider connected.",
      });
      refreshIntegrationHealth();
    }
    if (setupStatus === "error") {
      setOnboardingState({
        provider,
        tone: "error",
        message: params.get("message") ?? "Setup did not finish. Please try connecting again.",
      });
    }
    window.history.replaceState(null, "", window.location.pathname);
  }, []);

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
        const payload = (await readJsonResponse(response)) as AdminConsoleData;
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
  const visibleCases = useMemo(
    () => (showSyntheticOnly ? cases.filter(isSyntheticCase) : cases),
    [cases, showSyntheticOnly],
  );
  const reviewCases = useMemo(() => visibleCases.filter(isActionableCase), [visibleCases]);
  const queueCases = showAllCases ? visibleCases : reviewCases;
  const selectedCase = visibleCases.find((item) => item.id === selectedCaseId) ?? queueCases[0] ?? visibleCases[0] ?? cases[0];
  const fopIndex = useMemo(() => new Map(data.fops.map((fop) => [fop.id, fop])), [data.fops]);
  const failedCount = visibleCases.filter((item) => item.status === "failed").length;
  const resolvedCount = visibleCases.filter((item) => item.status === "resolved").length;
  const syntheticCount = cases.filter(isSyntheticCase).length;
  const missingScopeHealth = data.integrationHealth.filter((item) => item.missing_scopes.length > 0);
  const connectedProviders = data.integrationHealth.filter(isConnectedHealth).length;

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
      const payload = (await readJsonResponse(response)) as { status?: CaseStatus };
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

  function refreshIntegrationHealth() {
    startTransition(async () => {
      try {
        const response = await fetch("/api/integrations/health", { cache: "no-store" });
        if (!response.ok) {
          throw new Error(`Health refresh failed with ${response.status}`);
        }
        const integrationHealth = (await readJsonResponse(response)) as IntegrationHealth[];
        setData((current) => ({
          ...current,
          integrationHealth,
          loadedAt: new Date().toISOString(),
        }));
      } catch {
        setOnboardingState({
          provider: null,
          tone: "error",
          message: "Could not refresh connection health.",
        });
      }
    });
  }

  function startProviderInstall(provider: ProviderId) {
    const body: { shop?: string; account?: string } =
      provider === "shopify"
        ? { shop: shopifyShop.trim() }
        : provider === "gorgias"
          ? { account: gorgiasAccount.trim() }
          : {};
    if (provider === "shopify" && !body.shop) {
      setOnboardingState({ provider, tone: "error", message: "Enter your Shopify shop domain." });
      return;
    }
    if (provider === "gorgias" && !body.account) {
      setOnboardingState({ provider, tone: "error", message: "Enter your Gorgias account subdomain." });
      return;
    }

    startTransition(async () => {
      setOnboardingState({ provider, tone: "idle", message: "Opening secure provider authorization..." });
      const response = await fetch(`/api/integrations/${provider}/install`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = (await readJsonResponse(response)) as { install_url?: string; detail?: string };
      if (!response.ok || !payload.install_url) {
        setOnboardingState({
          provider,
          tone: "error",
          message: payload.detail ?? `Could not start ${providerLabel(provider)} connection.`,
        });
        return;
      }
      window.location.assign(payload.install_url);
    });
  }

  function installStripeRestrictedKey() {
    const accessToken = stripeRestrictedKey.trim();
    const accountId = stripeAccountId.trim();
    if (!accessToken || !accountId) {
      setOnboardingState({
        provider: "stripe",
        tone: "error",
        message: "Enter both the restricted key and Stripe account ID.",
      });
      return;
    }
    startTransition(async () => {
      setOnboardingState({ provider: "stripe", tone: "idle", message: "Checking and saving Stripe key..." });
      const response = await fetch("/api/integrations/stripe/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          access_token: accessToken,
          metadata: {
            stripe_account_id: accountId,
            scope: "charges:read,disputes:read,refunds:write",
            installed_by: "console_restricted_key",
          },
        }),
      });
      const payload = (await readJsonResponse(response)) as { detail?: string; health_status?: string; missing_scopes?: string[] };
      if (!response.ok) {
        setOnboardingState({
          provider: "stripe",
          tone: "error",
          message: payload.detail ?? "Could not save Stripe key.",
        });
        return;
      }
      setStripeRestrictedKey("");
      setOnboardingState({
        provider: "stripe",
        tone: payload.missing_scopes?.length ? "error" : "success",
        message: payload.missing_scopes?.length
          ? `Connected, but missing scopes: ${payload.missing_scopes.join(", ")}`
          : "Stripe is connected.",
      });
      refreshIntegrationHealth();
    });
  }

  function disconnectProvider(provider: ProviderId) {
    startTransition(async () => {
      setOnboardingState({ provider, tone: "idle", message: `Disconnecting ${providerLabel(provider)}...` });
      const response = await fetch(`/api/integrations/${provider}`, { method: "DELETE" });
      const payload = (await readJsonResponse(response)) as { detail?: string };
      if (!response.ok) {
        setOnboardingState({
          provider,
          tone: "error",
          message: payload.detail ?? `Could not disconnect ${providerLabel(provider)}.`,
        });
        return;
      }
      setOnboardingState({ provider, tone: "success", message: `${providerLabel(provider)} disconnected.` });
      refreshIntegrationHealth();
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
            <input checked={showSyntheticOnly} onChange={(event) => setShowSyntheticOnly(event.target.checked)} type="checkbox" />
            Synthetic only
          </label>
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

      {missingScopeHealth.length > 0 ? <IntegrationScopeBanner health={missingScopeHealth} /> : null}

      <OnboardingPanel
        connectedCount={connectedProviders}
        gorgiasAccount={gorgiasAccount}
        health={data.integrationHealth}
        isPending={isPending}
        onDisconnect={disconnectProvider}
        onGorgiasAccountChange={setGorgiasAccount}
        onRefresh={refreshIntegrationHealth}
        onShopifyShopChange={setShopifyShop}
        onStartInstall={startProviderInstall}
        onStripeAccountIdChange={setStripeAccountId}
        onStripeRestrictedKeyChange={setStripeRestrictedKey}
        onStripeRestrictedKeyInstall={installStripeRestrictedKey}
        setup={data.setup}
        shopifyShop={shopifyShop}
        state={onboardingState}
        stripeAccountId={stripeAccountId}
        stripeRestrictedKey={stripeRestrictedKey}
      />

      <section className="statusStrip" aria-label="Console status">
        <span>{reviewCases.length} need review</span>
        <span>{failedCount} need attention</span>
        <span>{resolvedCount} resolved</span>
        <span>{syntheticCount} synthetic</span>
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
                    <SyntheticBadge caseItem={caseItem} />
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

function OnboardingPanel({
  connectedCount,
  gorgiasAccount,
  health,
  isPending,
  onDisconnect,
  onGorgiasAccountChange,
  onRefresh,
  onShopifyShopChange,
  onStartInstall,
  onStripeAccountIdChange,
  onStripeRestrictedKeyChange,
  onStripeRestrictedKeyInstall,
  setup,
  shopifyShop,
  state,
  stripeAccountId,
  stripeRestrictedKey,
}: {
  connectedCount: number;
  gorgiasAccount: string;
  health: IntegrationHealth[];
  isPending: boolean;
  onDisconnect: (provider: ProviderId) => void;
  onGorgiasAccountChange: (value: string) => void;
  onRefresh: () => void;
  onShopifyShopChange: (value: string) => void;
  onStartInstall: (provider: ProviderId) => void;
  onStripeAccountIdChange: (value: string) => void;
  onStripeRestrictedKeyChange: (value: string) => void;
  onStripeRestrictedKeyInstall: () => void;
  setup: AdminConsoleData["setup"];
  shopifyShop: string;
  state: OnboardingState;
  stripeAccountId: string;
  stripeRestrictedKey: string;
}) {
  const shopify = providerHealth(health, "shopify");
  const stripe = providerHealth(health, "stripe");
  const gorgias = providerHealth(health, "gorgias");
  const ready = connectedCount === 3 && health.every((item) => item.missing_scopes.length === 0);

  return (
    <section className="onboardingPanel" aria-label="Customer onboarding">
      <div className="onboardingHeader">
        <div>
          <p className="eyebrow">Self-serve setup</p>
          <h2>Connect your store, payments, and helpdesk</h2>
          <p>
            {ready
              ? "All core providers are connected and ready for automation."
              : `${connectedCount} of 3 core providers connected.`}
          </p>
        </div>
        <button className="secondaryButton" disabled={isPending} type="button" onClick={onRefresh}>
          Refresh checks
        </button>
      </div>

      <div className="providerGrid">
        <ProviderCard
          detail="Orders, fulfillment, refunds, order edits, and Shopify webhooks."
          health={shopify}
          isPending={isPending}
          onDisconnect={() => onDisconnect("shopify")}
          onPrimary={() => onStartInstall("shopify")}
          primaryLabel="Connect Shopify"
          provider="shopify"
          state={state}
        >
          <SetupHint label="Allowed redirect URL" value={setup.shopifyRedirectUri} />
          <label className="fieldLabel">
            Shopify shop
            <input
              onChange={(event) => onShopifyShopChange(event.target.value)}
              placeholder="your-shop.myshopify.com"
              value={shopifyShop}
            />
          </label>
        </ProviderCard>

        <ProviderCard
          detail="Disputes, charges, payment failures, refunds, and Stripe webhooks."
          health={stripe}
          isPending={isPending}
          onDisconnect={() => onDisconnect("stripe")}
          onPrimary={() => onStartInstall("stripe")}
          primaryLabel="Connect Stripe"
          provider="stripe"
          state={state}
        >
          <SetupHint label="Connect redirect URL" value={setup.stripeRedirectUri} />
          <details className="inlineDetails">
            <summary>Use restricted key instead</summary>
            <div className="stackedFields">
              <label className="fieldLabel">
                Restricted key
                <input
                  onChange={(event) => onStripeRestrictedKeyChange(event.target.value)}
                  placeholder="rk_live_..."
                  type="password"
                  value={stripeRestrictedKey}
                />
              </label>
              <label className="fieldLabel">
                Account ID
                <input
                  onChange={(event) => onStripeAccountIdChange(event.target.value)}
                  placeholder="acct_..."
                  value={stripeAccountId}
                />
              </label>
              <button className="secondaryButton" disabled={isPending} type="button" onClick={onStripeRestrictedKeyInstall}>
                Save key
              </button>
            </div>
          </details>
        </ProviderCard>

        <ProviderCard
          detail="Tickets, customer lookup, draft replies, and Gorgias HTTP events."
          health={gorgias}
          isPending={isPending}
          onDisconnect={() => onDisconnect("gorgias")}
          onPrimary={() => onStartInstall("gorgias")}
          primaryLabel="Connect Gorgias"
          provider="gorgias"
          state={state}
        >
          <SetupHint label="OAuth redirect URL" value={setup.gorgiasRedirectUri} />
          <label className="fieldLabel">
            Gorgias account
            <input
              onChange={(event) => onGorgiasAccountChange(event.target.value)}
              placeholder="your-subdomain"
              value={gorgiasAccount}
            />
          </label>
        </ProviderCard>
      </div>
    </section>
  );
}

function SetupHint({ label, value }: { label: string; value: string }) {
  return (
    <div className="setupHint">
      <span>{label}</span>
      <code>{value}</code>
    </div>
  );
}

function ProviderCard({
  children,
  detail,
  health,
  isPending,
  onDisconnect,
  onPrimary,
  primaryLabel,
  provider,
  state,
}: {
  children: ReactNode;
  detail: string;
  health: IntegrationHealth | null;
  isPending: boolean;
  onDisconnect: () => void;
  onPrimary: () => void;
  primaryLabel: string;
  provider: ProviderId;
  state: OnboardingState;
}) {
  const connected = isConnectedHealth(health);
  const hasMissingScopes = Boolean(health?.missing_scopes.length);
  const message = state.provider === provider ? state.message : "";

  return (
    <article className={`providerCard ${connected ? "connected" : "notConnected"}`}>
      <div className="providerTopline">
        <div>
          <h3>{providerLabel(provider)}</h3>
          <p>{detail}</p>
        </div>
        <span className={`connectionPill ${connectionTone(health)}`}>{connectionLabel(health)}</span>
      </div>
      {children}
      {health?.provider_account_id ? <p className="accountHint">{health.provider_account_id}</p> : null}
      {hasMissingScopes ? (
        <p className="scopeHint">Missing scopes: {health?.missing_scopes.join(", ")}</p>
      ) : null}
      {message ? <p className={`actionMessage ${state.tone}`}>{message}</p> : null}
      <div className="providerActions">
        <button disabled={isPending || connected} type="button" onClick={onPrimary}>
          {connected ? "Connected" : primaryLabel}
        </button>
        <button className="secondaryButton" disabled={isPending || !connected} type="button" onClick={onDisconnect}>
          Disconnect
        </button>
      </div>
    </article>
  );
}

function IntegrationScopeBanner({ health }: { health: IntegrationHealth[] }) {
  return (
    <section className="warningBanner" aria-label="Integration scope warnings">
      <strong>Some provider tools are disabled until scopes are restored.</strong>
      <span>
        {health
          .map((item) => `${providerLabel(item.provider)}: ${item.missing_scopes.join(", ")}`)
          .join(" · ")}
      </span>
    </section>
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
  const synthetic = syntheticInfo(caseItem);

  return (
    <section className="reviewPanel" aria-label="Selected case">
      <div className="caseHeader">
        <div>
          <p className="eyebrow">{statusLabels[caseItem.status]}</p>
          <h2>{typeLabels[caseItem.type] ?? caseItem.type}</h2>
          <p>{orderLabel(caseItem)} · {maskedCustomer(caseItem, showPii)}</p>
        </div>
        <div className="caseHeaderBadges">
          {synthetic ? <span className="syntheticPill">{syntheticLabel(synthetic)}</span> : null}
          <span className={`riskPill ${riskLevel(caseItem)}`}>{riskLevel(caseItem)} risk</span>
        </div>
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

function SyntheticBadge({ caseItem }: { caseItem: CaseDetail }) {
  const synthetic = syntheticInfo(caseItem);
  return synthetic ? <small className="syntheticInline">{syntheticLabel(synthetic)}</small> : null;
}

function firstActionableCase(cases: CaseDetail[]) {
  return cases.find(isActionableCase);
}

function isActionableCase(caseItem: CaseDetail) {
  return caseItem.status === "pending_approval" || caseItem.status === "failed";
}

function isSyntheticCase(caseItem: CaseDetail) {
  return syntheticInfo(caseItem) !== null;
}

function syntheticInfo(caseItem: CaseDetail): { runTag: string; scenarioId: string; shopIndex: string } | null {
  const execution = objectValue(caseItem.resolution?.execution);
  const graph = objectValue(caseItem.resolution?.graph);
  const eventSynthetic =
    caseItem.events.map((event) => objectValue(event.payload.synthetic)).find((item) => stringValue(item.run_tag)) ?? {};
  const runTag =
    stringValue(execution.synthetic_run_tag) ||
    stringValue(graph.synthetic_run_tag) ||
    stringValue(eventSynthetic.run_tag);
  if (!runTag) {
    return null;
  }
  return {
    runTag,
    scenarioId: stringValue(eventSynthetic.scenario_id),
    shopIndex: stringValue(eventSynthetic.shop_index),
  };
}

function syntheticLabel(synthetic: { runTag: string; scenarioId: string; shopIndex: string }) {
  const scenario = synthetic.scenarioId ? ` · ${synthetic.scenarioId}` : "";
  const shop = synthetic.shopIndex ? ` · shop ${synthetic.shopIndex}` : "";
  return `Synthetic${shop}${scenario}`;
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

function providerLabel(value: string) {
  if (value === "gorgias") {
    return "Gorgias";
  }
  if (value === "shopify") {
    return "Shopify";
  }
  if (value === "stripe") {
    return "Stripe";
  }
  return labelize(value);
}

function providerHealth(health: IntegrationHealth[], provider: ProviderId) {
  const current = health.find((item) => item.provider === provider) ?? null;
  if (current?.status === "unknown" && !hasCredentialSignal(current)) {
    return null;
  }
  return current;
}

function connectionLabel(health: IntegrationHealth | null) {
  if (!health) {
    return "Not connected";
  }
  if (health.missing_scopes.length > 0) {
    return "Needs scopes";
  }
  if (health.status === "healthy") {
    return "Ready";
  }
  return labelize(health.status);
}

function connectionTone(health: IntegrationHealth | null) {
  if (!health) {
    return "empty";
  }
  return health.missing_scopes.length > 0 || health.status !== "healthy" ? "warn" : "ready";
}

function isConnectedHealth(health: IntegrationHealth | null) {
  if (!health || health.status === "auth_failed") {
    return false;
  }
  return health.status !== "unknown" || hasCredentialSignal(health);
}

function hasCredentialSignal(health: IntegrationHealth) {
  return Boolean(health.provider_account_id || health.granted_scopes.length > 0 || health.checked_at || health.error);
}

function toProviderId(value: string | null): ProviderId | null {
  if (value === "shopify" || value === "stripe" || value === "gorgias") {
    return value;
  }
  return null;
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
