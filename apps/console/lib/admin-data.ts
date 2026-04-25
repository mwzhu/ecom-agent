import { readFile } from "node:fs/promises";
import path from "node:path";

export type CaseStatus = "open" | "pending_approval" | "resolved" | "canceled";

export type CaseEvent = {
  id: string;
  kind: string;
  actor: "agent" | "human" | "webhook" | "system" | string;
  created_at: string;
  langsmith_run_id: string | null;
  payload: Record<string, unknown>;
};

export type CaseDetail = {
  id: string;
  merchant_id: string;
  merchant_name: string;
  type: string;
  status: CaseStatus;
  subject_ref: Record<string, unknown>;
  langgraph_thread_id: string | null;
  langsmith_trace_url: string | null;
  resolution: Record<string, unknown> | null;
  events: CaseEvent[];
};

export type MerchantOption = {
  id: string;
  name: string;
};

export type EvalReviewItem = {
  id: string;
  case_id: string;
  merchant_id: string;
  langsmith_run_id: string | null;
  score: number;
  passed: boolean;
  reason: string;
  payload: Record<string, unknown>;
  status: "queued" | "reviewed" | "dismissed" | string;
  created_at: string;
};

export type AdminConsoleData = {
  source: "api" | "fixture";
  apiBaseUrl: string;
  cases: CaseDetail[];
  merchants: MerchantOption[];
  fopYaml: string;
  evalReviews: EvalReviewItem[];
};

type ApiCaseSummary = {
  id: string;
  merchant_id: string;
  type: string;
  status: CaseStatus;
  subject_ref: Record<string, unknown>;
};

type ApiCaseDetail = ApiCaseSummary & {
  langgraph_thread_id: string | null;
  resolution: Record<string, unknown> | null;
  events: CaseEvent[];
};

const API_BASE_URL =
  process.env.INTERNAL_API_BASE_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export async function loadAdminConsoleData(): Promise<AdminConsoleData> {
  const fopYaml = await readPhase0Fops();
  const apiCases = await loadApiCases();
  if (apiCases.length > 0) {
    const evalReviews = await loadApiEvalReviews();
    return {
      source: "api",
      apiBaseUrl: API_BASE_URL,
      cases: apiCases,
      merchants: uniqueMerchants(apiCases),
      fopYaml,
      evalReviews,
    };
  }

  const fixtures = fixtureCases();
  return {
    source: "fixture",
    apiBaseUrl: API_BASE_URL,
    cases: fixtures,
    merchants: uniqueMerchants(fixtures),
    fopYaml,
    evalReviews: fixtureEvalReviews(fixtures),
  };
}

async function loadApiCases(): Promise<CaseDetail[]> {
  const token = process.env.INTERNAL_CONSOLE_BEARER_TOKEN;
  if (!token) {
    return [];
  }
  try {
    const list = await apiFetch<ApiCaseSummary[]>("/v1/cases", token);
    const details = await Promise.all(
      list.map((item) => apiFetch<ApiCaseDetail>(`/v1/cases/${item.id}`, token)),
    );
    return details.map((item) => ({
      ...item,
      merchant_name: String(item.subject_ref.merchant_name ?? "Current merchant"),
      langsmith_trace_url: traceUrl(item.events),
    }));
  } catch {
    return [];
  }
}

async function loadApiEvalReviews(): Promise<EvalReviewItem[]> {
  const token = process.env.INTERNAL_CONSOLE_BEARER_TOKEN;
  if (!token) {
    return [];
  }
  try {
    return await apiFetch<EvalReviewItem[]>("/v1/evals/review-queue", token);
  } catch {
    return [];
  }
}

async function apiFetch<T>(pathname: string, token: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${pathname}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`API request failed with ${response.status}`);
  }
  return (await response.json()) as T;
}

async function readPhase0Fops(): Promise<string> {
  const filePath = path.join(process.cwd(), "..", "agents", "fops", "demo-merchant.yaml");
  try {
    return await readFile(filePath, "utf8");
  } catch {
    return "merchant_slug: demo-merchant\nfops: []\n";
  }
}

function uniqueMerchants(cases: CaseDetail[]): MerchantOption[] {
  const seen = new Set<string>();
  return cases.flatMap((item) => {
    if (seen.has(item.merchant_id)) {
      return [];
    }
    seen.add(item.merchant_id);
    return [{ id: item.merchant_id, name: item.merchant_name }];
  });
}

function traceUrl(events: CaseEvent[]): string | null {
  const traceEvent = [...events].reverse().find((event) => event.langsmith_run_id);
  if (!traceEvent?.langsmith_run_id) {
    return null;
  }
  return `https://smith.langchain.com/public/${traceEvent.langsmith_run_id}/r`;
}

function fixtureCases(): CaseDetail[] {
  const merchantId = "demo-merchant";
  return [
    {
      id: "case_demo_fraud",
      merchant_id: merchantId,
      merchant_name: "Demo Merchant",
      type: "fraud_triage",
      status: "pending_approval",
      subject_ref: { order_id: "gid://shopify/Order/1", order_name: "#1001", value: "$742.00" },
      langgraph_thread_id: "thread_demo_fraud",
      langsmith_trace_url: null,
      resolution: null,
      events: [
        fixtureEvent("agent.proposal", "agent", {
          summary: "Fraud score 85 is above the cancel threshold.",
          recommendation: "Cancel the order and issue the refund after approval.",
          matched_fop_ids: ["fop_fraud_score_cancel"],
        }),
        fixtureEvent("tool.plan", "agent", {
          tool_calls: ["shopify_cancel_order", "shopify_create_refund"],
          requires_human: true,
        }),
      ],
    },
    {
      id: "case_demo_address",
      merchant_id: merchantId,
      merchant_name: "Demo Merchant",
      type: "address_validation",
      status: "open",
      subject_ref: { order_id: "gid://shopify/Order/3", order_name: "#1003", value: "$128.00" },
      langgraph_thread_id: "thread_demo_address",
      langsmith_trace_url: null,
      resolution: null,
      events: [
        fixtureEvent("webhook.order_updated", "webhook", {
          provider: "shopify",
          topic: "orders/updated",
        }),
        fixtureEvent("agent.proposal", "agent", {
          summary: "Address validation found a missing apartment number.",
          recommendation: "Hold fulfillment and send the customer a confirmation draft.",
        }),
      ],
    },
    {
      id: "case_demo_inventory",
      merchant_id: merchantId,
      merchant_name: "Demo Merchant",
      type: "inventory_conflict",
      status: "resolved",
      subject_ref: { order_id: "gid://shopify/Order/4", order_name: "#1004", value: "$214.50" },
      langgraph_thread_id: "thread_demo_inventory",
      langsmith_trace_url: null,
      resolution: { decision: "approve", actor: "ops@example.com" },
      events: [
        fixtureEvent("agent.proposal", "agent", {
          summary: "Inventory context shows one out-of-stock line.",
          recommendation: "Partial ship with a customer message.",
        }),
        fixtureEvent("case.decision_submitted", "human", {
          decision: "approve",
          actor: "ops@example.com",
        }),
      ],
    },
  ];
}

function fixtureEvalReviews(cases: CaseDetail[]): EvalReviewItem[] {
  const fraudCase = cases.find((item) => item.type === "fraud_triage");
  if (!fraudCase) {
    return [];
  }
  return [
    {
      id: "eval-review-demo-fraud",
      case_id: fraudCase.id,
      merchant_id: fraudCase.merchant_id,
      langsmith_run_id: "run_demo_low_confidence",
      score: 3,
      passed: false,
      reason: "Judge wants an operator to confirm the cancellation note before approval.",
      payload: {
        scenario_id: "fraud_high_score_cancel_refund",
        fop_violations: [],
        unsafe_actions: [],
      },
      status: "queued",
      created_at: new Date("2026-04-17T18:05:00.000Z").toISOString(),
    },
  ];
}

function fixtureEvent(kind: string, actor: CaseEvent["actor"], payload: Record<string, unknown>) {
  return {
    id: `${kind}-${Math.random().toString(16).slice(2)}`,
    kind,
    actor,
    created_at: new Date("2026-04-17T18:00:00.000Z").toISOString(),
    langsmith_run_id: null,
    payload,
  };
}
