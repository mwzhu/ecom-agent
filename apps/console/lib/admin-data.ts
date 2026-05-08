import { readFile } from "node:fs/promises";
import path from "node:path";
import { getConsoleApiAuth } from "./console-auth";
import { serverEnv } from "./server-env";

export type CaseStatus = "open" | "pending_approval" | "executing" | "resolved" | "failed" | "canceled";

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
  created_at?: string;
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

export type FopSummary = {
  id: string;
  merchant_id: string;
  version: number;
  nl_text: string;
  structured: Record<string, unknown>;
  status: "draft" | "active" | "disabled" | "superseded" | string;
  created_by: string;
  created_at: string;
};

export type IntegrationHealth = {
  provider: "shopify" | "stripe" | "gorgias" | string;
  status: string;
  provider_account_id: string | null;
  granted_scopes: string[];
  missing_scopes: string[];
  checked_at: string | null;
  error: Record<string, unknown> | null;
};

export type AdminConsoleData = {
  source: "api" | "fixture";
  mode: "auto" | "api" | "fixture";
  apiBaseUrl: string;
  cases: CaseDetail[];
  merchants: MerchantOption[];
  fopYaml: string;
  fops: FopSummary[];
  evalReviews: EvalReviewItem[];
  integrationHealth: IntegrationHealth[];
  setup: {
    shopifyRedirectUri: string;
    stripeRedirectUri: string;
    gorgiasRedirectUri: string;
    publicApiBaseUrl: string;
  };
  loadedAt: string;
  apiError?: string;
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

type ApiFop = {
  id: string;
  merchant_id: string;
  version: number;
  nl_text: string;
  structured: Record<string, unknown>;
  status: string;
  created_by: string;
  created_at: string;
};

const API_BASE_URL =
  serverEnv("INTERNAL_API_BASE_URL") ?? serverEnv("NEXT_PUBLIC_API_BASE_URL") ?? "http://localhost:8000";
const CONSOLE_DATA_MODE = consoleDataMode(serverEnv("CONSOLE_DATA_MODE"));

const DEMO_MERCHANT_ID = "demo-merchant";
const DEMO_TIME = "2026-04-17T18:00:00.000Z";

export async function loadAdminConsoleData(): Promise<AdminConsoleData> {
  const fopYaml = await readOrderExceptionFops();
  const fixtureFops = parseFopsFromYaml(fopYaml);

  if (CONSOLE_DATA_MODE === "fixture") {
    return fixtureData({ fopYaml, fixtureFops });
  }

  try {
    const auth = await getConsoleApiAuth();
    if (auth.token === null) {
      throw new Error(auth.detail);
    }
    const apiCases = await loadApiCases(auth.token);
    if (apiCases.length > 0 || CONSOLE_DATA_MODE === "api") {
      const [evalReviews, apiFops, integrationHealth] = await Promise.all([
        loadApiEvalReviews(auth.token),
        loadApiFops(auth.token),
        loadApiIntegrationHealth(auth.token),
      ]);
      return {
        source: "api",
        mode: CONSOLE_DATA_MODE,
        apiBaseUrl: API_BASE_URL,
        cases: apiCases,
        merchants: uniqueMerchants(apiCases),
        fopYaml,
        fops: apiFops.length > 0 ? apiFops : fixtureFops,
        evalReviews,
        integrationHealth,
        setup: setupInfo(),
        loadedAt: new Date().toISOString(),
      };
    }
  } catch (error) {
    if (CONSOLE_DATA_MODE === "api") {
      return {
        source: "api",
        mode: CONSOLE_DATA_MODE,
        apiBaseUrl: API_BASE_URL,
        cases: [],
        merchants: [],
        fopYaml,
        fops: fixtureFops,
        evalReviews: [],
        integrationHealth: [],
        setup: setupInfo(),
        loadedAt: new Date().toISOString(),
        apiError: error instanceof Error ? error.message : "API data could not be loaded.",
      };
    }
  }

  return fixtureData({ fopYaml, fixtureFops });
}

function fixtureData({
  fopYaml,
  fixtureFops,
}: {
  fopYaml: string;
  fixtureFops: FopSummary[];
}): AdminConsoleData {
  const fixtures = fixtureCases();
  return {
    source: "fixture",
    mode: CONSOLE_DATA_MODE,
    apiBaseUrl: API_BASE_URL,
    cases: fixtures,
    merchants: uniqueMerchants(fixtures),
    fopYaml,
    fops: fixtureFops,
    evalReviews: fixtureEvalReviews(fixtures),
    integrationHealth: [],
    setup: setupInfo(),
    loadedAt: new Date().toISOString(),
  };
}

function setupInfo(): AdminConsoleData["setup"] {
  const publicApiBaseUrl = serverEnv("API_BASE_URL") ?? API_BASE_URL;
  return {
    publicApiBaseUrl,
    shopifyRedirectUri: `${publicApiBaseUrl}/v1/integrations/shopify/callback`,
    stripeRedirectUri: `${publicApiBaseUrl}/v1/integrations/stripe/connect/callback`,
    gorgiasRedirectUri: `${publicApiBaseUrl}/v1/integrations/gorgias/callback`,
  };
}

async function loadApiCases(token: string): Promise<CaseDetail[]> {
  const list = await apiFetch<ApiCaseSummary[]>("/v1/cases", token);
  const details = await Promise.all(
    list.map((item) => apiFetch<ApiCaseDetail>(`/v1/cases/${item.id}`, token)),
  );
  return details.map((item) => ({
    ...item,
    merchant_name: String(item.subject_ref.merchant_name ?? "Current merchant"),
    langsmith_trace_url: traceUrl(item.events),
    created_at: firstEventTime(item.events),
  }));
}

async function loadApiEvalReviews(token: string): Promise<EvalReviewItem[]> {
  try {
    return await apiFetch<EvalReviewItem[]>("/v1/evals/review-queue", token);
  } catch {
    return [];
  }
}

async function loadApiFops(token: string): Promise<FopSummary[]> {
  try {
    const fops = await apiFetch<ApiFop[]>("/v1/cases/-/fops", token);
    return fops.map((fop) => ({
      ...fop,
      status: fop.status,
    }));
  } catch {
    return [];
  }
}

async function loadApiIntegrationHealth(token: string): Promise<IntegrationHealth[]> {
  try {
    return await apiFetch<IntegrationHealth[]>("/v1/integrations/health", token);
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
    const body = await response.text();
    throw new Error(`API request ${pathname} failed with ${response.status}: ${body}`);
  }
  return (await response.json()) as T;
}

function consoleDataMode(value: string | undefined): AdminConsoleData["mode"] {
  if (value === "api" || value === "fixture" || value === "auto") {
    return value;
  }
  return "auto";
}

/*
 * The block below intentionally keeps the fixture simulator in this same file:
 * local demos can still run with CONSOLE_DATA_MODE=fixture, while
 * CONSOLE_DATA_MODE=api now refuses to silently fall back to fake data.
 */

async function readOrderExceptionFops(): Promise<string> {
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

function firstEventTime(events: CaseEvent[]): string | undefined {
  return events[0]?.created_at;
}

function parseFopsFromYaml(yaml: string): FopSummary[] {
  const blocks = yaml.split(/\n  - id:\s*/).slice(1);
  return blocks.map((block) => {
    const [idLine, ...rest] = block.split("\n");
    const body = rest.join("\n");
    const scope = yamlValue(body, "scope");
    return {
      id: idLine.trim(),
      merchant_id: DEMO_MERCHANT_ID,
      version: 1,
      nl_text: yamlValue(body, "nl_text") ?? "No natural-language policy text found.",
      structured: scope ? { scope } : {},
      status: "active",
      created_by: "demo-seed",
      created_at: DEMO_TIME,
    };
  });
}

function yamlValue(block: string, key: string): string | undefined {
  const match = block.match(new RegExp(`^\\s{4}${key}:\\s*(.+)$`, "m"));
  return match?.[1]?.trim();
}

function fixtureCases(): CaseDetail[] {
  return [
    makeCase({
      id: "case_demo_fraud",
      type: "fraud_triage",
      status: "pending_approval",
      orderName: "#1001",
      value: "$742.00",
      customerName: "Sarah Chen",
      customerEmail: "sarah@example.com",
      context: {
        ltv: "$1,420",
        prior_orders: 7,
        refund_count_60d: 0,
        chargebacks: 0,
        vip: true,
        risk_flag: "High fraud score",
        payment_status: "Captured",
        fulfillment_status: "Unfulfilled",
      },
      proposal: {
        summary: "Fraud score 85 is above the cancel threshold.",
        recommendation: "Cancel the order and issue the refund after approval.",
        action_label: "Cancel order and refund $742.00",
        approval_reason: "Money movement and cancellation require a human gate for high-value fraud triage.",
        confidence: 0.91,
        risk_level: "high",
        risk_factors: ["Fraud score 85", "High order value", "Billing and shipping distance mismatch"],
        matched_fop_ids: ["fop_fraud_score_cancel"],
        required_approvals: ["cancel_order", "refund_payment"],
        customer_message:
          "Hi Sarah, we were unable to verify this order and have canceled it for your protection. A refund will be returned to your original payment method.",
        tone: "firm",
        tool_calls: [
          toolCall("shopify_cancel_order", "pending", { reason: "fraud_risk" }, null, 128),
          toolCall("shopify_create_refund", "pending", { amount: "742.00" }, null, 141),
        ],
      },
      extraEvents: [
        event("case_demo_fraud-risk", 1, "webhook.order_created", "webhook", {
          provider: "shopify",
          topic: "orders/create",
          risk_score: 85,
        }),
      ],
    }),
    makeCase({
      id: "case_demo_address_change",
      type: "address_change_request",
      status: "pending_approval",
      orderName: "#1003",
      value: "$128.00",
      customerName: "Maya Patel",
      customerEmail: "maya@example.com",
      context: {
        ltv: "$336",
        prior_orders: 3,
        refund_count_60d: 0,
        chargebacks: 0,
        vip: false,
        risk_flag: "Normal",
        payment_status: "Paid",
        fulfillment_status: "Open pick ticket",
      },
      proposal: {
        summary: "Customer requested a pre-shipment address change and provided a complete replacement address.",
        recommendation: "Hold fulfillment, update the shipping address, and confirm the change back to the customer.",
        action_label: "Hold fulfillment and update address",
        approval_reason: "Address edits are customer-visible and must be confirmed before fulfillment resumes.",
        confidence: 0.87,
        risk_level: "low",
        risk_factors: ["Pre-shipment", "Complete replacement address", "No fraud signals"],
        matched_fop_ids: ["fop_address_change_pre_ship"],
        required_approvals: ["address_edit", "customer_message"],
        customer_message:
          "Hi Maya, we updated the shipping address on your order and paused fulfillment while the change syncs. We will send tracking as soon as it ships.",
        tone: "helpful",
        tool_calls: [
          toolCall("shopify_hold_fulfillment", "pending", { order_id: "#1003" }, null, 88),
          toolCall("shopify_update_shipping_address", "pending", { order_id: "#1003" }, null, 173),
          toolCall("gorgias_draft_reply", "pending", { tone: "helpful" }, null, 94),
        ],
      },
      extraEvents: [
        event("case_demo_address-ticket", 8, "customer.message_received", "system", {
          channel: "gorgias",
          intent: "address_change_request",
        }),
      ],
    }),
    makeCase({
      id: "case_demo_inventory",
      type: "inventory_conflict",
      status: "resolved",
      orderName: "#1004",
      value: "$214.50",
      customerName: "Ana Lopez",
      customerEmail: "ana@example.com",
      context: {
        ltv: "$782",
        prior_orders: 5,
        refund_count_60d: 1,
        chargebacks: 0,
        vip: false,
        risk_flag: "Normal",
        payment_status: "Paid",
        fulfillment_status: "Partially fulfillable",
      },
      proposal: {
        summary: "Inventory context showed one out-of-stock line.",
        recommendation: "Partial ship the available items and send a customer message about the delayed line.",
        action_label: "Partial ship available items",
        approval_reason: "Partial shipments require customer messaging before fulfillment changes.",
        confidence: 0.82,
        risk_level: "medium",
        risk_factors: ["One SKU oversold", "Customer has prior refund", "Shipment can still be split"],
        matched_fop_ids: ["fop_inventory_oos_partial_ship"],
        required_approvals: ["partial_ship", "customer_message"],
        customer_message:
          "Hi Ana, one item in your order is delayed. We can ship the available items now and send the remaining item separately as soon as it is back in stock.",
        tone: "apologetic",
        tool_calls: [toolCall("shopify_create_partial_fulfillment", "succeeded", { order_id: "#1004" }, { partial: true }, 231)],
      },
      decision: { decision: "approve", actor: "ops@example.com", source: "console" },
    }),
    makeCase({
      id: "case_demo_dnr",
      type: "delivered_not_received",
      status: "pending_approval",
      orderName: "#1005",
      value: "$89.20",
      customerName: "Jordan Miles",
      customerEmail: "jordan@example.com",
      context: {
        ltv: "$218",
        prior_orders: 2,
        refund_count_60d: 1,
        chargebacks: 0,
        vip: false,
        risk_flag: "One prior missing-package claim",
        payment_status: "Paid",
        fulfillment_status: "Delivered",
        tracking: "Delivered 2 days ago",
      },
      proposal: {
        summary: "Carrier marks the package delivered, but the customer reports it missing.",
        recommendation: "Ask for address confirmation and hold refund/replacement until claim history is reviewed.",
        action_label: "Send missing-package review reply",
        approval_reason: "Delivered-not-received claims require review before refund or replacement.",
        confidence: 0.74,
        risk_level: "medium",
        risk_factors: ["Delivered scan exists", "Prior missing-package claim", "Low order value"],
        matched_fop_ids: ["fop_delivered_not_received_review"],
        required_approvals: ["customer_message", "refund_payment"],
        customer_message:
          "Hi Jordan, I am sorry the package has not turned up. The carrier marked it delivered, so we are reviewing the delivery details and your address before we decide the next step.",
        tone: "apologetic",
        tool_calls: [
          toolCall("shipstation_get_tracking", "succeeded", { order_id: "#1005" }, { status: "delivered" }, 182),
          toolCall("gorgias_draft_reply", "pending", { ticket_id: "ticket_1005" }, null, 76),
        ],
      },
    }),
    makeCase({
      id: "case_demo_damage",
      type: "damaged_in_transit",
      status: "pending_approval",
      orderName: "#1006",
      value: "$56.00",
      customerName: "Nina Brooks",
      customerEmail: "nina@example.com",
      context: {
        ltv: "$912",
        prior_orders: 9,
        refund_count_60d: 0,
        chargebacks: 0,
        vip: true,
        risk_flag: "Photo evidence attached",
        payment_status: "Paid",
        fulfillment_status: "Delivered",
      },
      proposal: {
        summary: "Customer attached photo evidence of transit damage.",
        recommendation: "Approve a replacement shipment and send an apology.",
        action_label: "Create replacement shipment",
        approval_reason: "Replacement shipment changes inventory and requires operator approval.",
        confidence: 0.93,
        risk_level: "low",
        risk_factors: ["Photo evidence attached", "VIP customer", "No refund abuse history"],
        matched_fop_ids: ["fop_damaged_in_transit_review"],
        required_approvals: ["customer_message", "replacement_order"],
        customer_message:
          "Hi Nina, I am sorry your item arrived damaged. We can send a replacement right away and you do not need to return the damaged item.",
        tone: "apologetic",
        tool_calls: [
          toolCall("gorgias_get_ticket", "succeeded", { ticket_id: "ticket_1006" }, { attachments: 2 }, 115),
          toolCall("shopify_create_replacement_order", "pending", { order_id: "#1006" }, null, 205),
        ],
      },
    }),
    makeCase({
      id: "case_demo_item_change",
      type: "item_change_request",
      status: "pending_approval",
      orderName: "#1007",
      value: "$67.40",
      customerName: "Theo Carter",
      customerEmail: "theo@example.com",
      context: {
        ltv: "$67",
        prior_orders: 1,
        refund_count_60d: 0,
        chargebacks: 0,
        vip: false,
        risk_flag: "Normal",
        payment_status: "Paid",
        fulfillment_status: "Unfulfilled",
      },
      proposal: {
        summary: "Customer wants a same-price size swap before shipment.",
        recommendation: "Stage a Shopify order edit, hold fulfillment, and confirm the swap.",
        action_label: "Stage zero-delta order edit",
        approval_reason: "Order edits are write actions and require confirmation before fulfillment.",
        confidence: 0.89,
        risk_level: "low",
        risk_factors: ["Zero payment delta", "Pre-shipment", "Same product family"],
        matched_fop_ids: ["fop_item_change_zero_delta"],
        required_approvals: ["order_edit", "customer_message"],
        customer_message:
          "Hi Theo, we can swap the size on your order before it ships. I have staged the change and will confirm once it is applied.",
        tone: "helpful",
        tool_calls: [toolCall("shopify_apply_order_edit", "pending", { delta: 0 }, null, 167)],
      },
    }),
    makeCase({
      id: "case_demo_cancel",
      type: "order_cancellation_request",
      status: "pending_approval",
      orderName: "#1008",
      value: "$144.10",
      customerName: "Priya Singh",
      customerEmail: "priya@example.com",
      context: {
        ltv: "$144",
        prior_orders: 1,
        refund_count_60d: 0,
        chargebacks: 0,
        vip: false,
        risk_flag: "Normal",
        payment_status: "Captured",
        fulfillment_status: "Unfulfilled",
      },
      proposal: {
        summary: "Customer requested cancellation before shipment.",
        recommendation: "Cancel the order, refund the payment, restock the inventory, and send confirmation.",
        action_label: "Cancel and refund $144.10",
        approval_reason: "Cancellation plus refund is a money-movement action.",
        confidence: 0.9,
        risk_level: "low",
        risk_factors: ["Pre-shipment", "Customer-requested", "No prior refund abuse"],
        matched_fop_ids: ["fop_pre_ship_cancellation"],
        required_approvals: ["cancel_order", "refund_payment"],
        customer_message:
          "Hi Priya, your order has been canceled and refunded to your original payment method. The refund should appear within 5-10 business days.",
        tone: "neutral",
        tool_calls: [
          toolCall("shopify_cancel_order", "pending", { order_id: "#1008", restock: true }, null, 144),
          toolCall("shopify_create_refund", "pending", { amount: "144.10" }, null, 139),
        ],
      },
    }),
    makeCase({
      id: "case_demo_pick_sla",
      type: "order_not_picked",
      status: "open",
      orderName: "#1009",
      value: "$98.75",
      customerName: "Chris Nguyen",
      customerEmail: "chris@example.com",
      context: {
        ltv: "$431",
        prior_orders: 4,
        refund_count_60d: 0,
        chargebacks: 0,
        vip: false,
        risk_flag: "3PL pick SLA breach",
        payment_status: "Paid",
        fulfillment_status: "Awaiting pick",
      },
      proposal: {
        summary: "3PL pick ticket is older than the 24-hour SLA.",
        recommendation: "Check the latest 3PL status and prepare a proactive shipping-delay update.",
        action_label: "Prepare proactive delay update",
        approval_reason: "Customer message should be checked once the 3PL status is confirmed.",
        confidence: 0.68,
        risk_level: "medium",
        risk_factors: ["Pick SLA breached", "No carrier scan yet", "Customer has not contacted support"],
        matched_fop_ids: ["fop_pick_sla_breach_proactive_update"],
        required_approvals: ["customer_message"],
        customer_message:
          "Hi Chris, your order is taking longer than expected to leave the warehouse. We are checking the latest fulfillment status and will send tracking as soon as it moves.",
        tone: "proactive",
        tool_calls: [toolCall("shipbob_get_order_status", "pending", { order_id: "#1009" }, null, 97)],
      },
    }),
    makeCase({
      id: "case_demo_stuck",
      type: "stuck_in_transit",
      status: "failed",
      orderName: "#1010",
      value: "$173.35",
      customerName: "Ava Reed",
      customerEmail: "ava@example.com",
      context: {
        ltv: "$504",
        prior_orders: 3,
        refund_count_60d: 0,
        chargebacks: 0,
        vip: false,
        risk_flag: "Carrier API timeout",
        payment_status: "Paid",
        fulfillment_status: "In transit",
        tracking: "No scan for 5 days",
      },
      proposal: {
        summary: "Tracking has not updated for five days, but the carrier lookup failed.",
        recommendation: "Retry carrier lookup before sending a refund or replacement proposal.",
        action_label: "Retry tracking lookup",
        approval_reason: "The agent needs fresh carrier data before committing to the customer.",
        confidence: 0.51,
        risk_level: "medium",
        risk_factors: ["No scan for 5 days", "Carrier API timeout", "Refund not yet justified"],
        matched_fop_ids: ["fop_stuck_in_transit_customer_update"],
        required_approvals: ["customer_message"],
        customer_message:
          "Hi Ava, we are checking the latest carrier status because tracking has not updated recently. I will follow up as soon as we confirm the next scan.",
        tone: "reassuring",
        tool_calls: [
          toolCall(
            "shipstation_get_tracking",
            "failed",
            { tracking_number: "9400-demo" },
            { error: "Carrier API timeout", suggested_next_step: "Retry carrier lookup" },
            3000,
            2,
          ),
        ],
      },
      extraEvents: [
        event("case_demo_stuck-error", 45, "tool.failed", "system", {
          tool: "shipstation_get_tracking",
          error: "Carrier API timeout",
          suggested_next_step: "Retry carrier lookup",
        }),
      ],
    }),
    makeCase({
      id: "case_demo_wismo",
      type: "wismo",
      status: "resolved",
      orderName: "#1011",
      value: "$38.99",
      customerName: "Sam Rivera",
      customerEmail: "sam@example.com",
      context: {
        ltv: "$93",
        prior_orders: 2,
        refund_count_60d: 0,
        chargebacks: 0,
        vip: false,
        risk_flag: "Normal",
        payment_status: "Paid",
        fulfillment_status: "Shipped",
        tracking: "Out for delivery",
      },
      proposal: {
        summary: "Customer asked where the order is; latest shipment status is out for delivery.",
        recommendation: "Send a concise tracking reply.",
        action_label: "Send tracking reply",
        approval_reason: "Auto-resolved because the reply is informational and grounded in tracking data.",
        confidence: 0.96,
        risk_level: "low",
        risk_factors: ["Out for delivery", "No refund requested", "Informational reply only"],
        matched_fop_ids: ["fop_wismo_tracking_reply"],
        required_approvals: [],
        customer_message:
          "Hi Sam, your order is out for delivery today. You can follow the latest status from the tracking link in your shipping email.",
        tone: "concise",
        tool_calls: [toolCall("gorgias_send_reply", "succeeded", { ticket_id: "ticket_1011" }, { sent: true }, 121)],
      },
      decision: { decision: "auto_resolved", actor: "agent", source: "console" },
    }),
  ];
}

function makeCase({
  id,
  type,
  status,
  orderName,
  value,
  customerName,
  customerEmail,
  context,
  proposal,
  decision,
  extraEvents = [],
}: {
  id: string;
  type: string;
  status: CaseStatus;
  orderName: string;
  value: string;
  customerName: string;
  customerEmail: string;
  context: Record<string, unknown>;
  proposal: Record<string, unknown>;
  decision?: Record<string, unknown>;
  extraEvents?: CaseEvent[];
}): CaseDetail {
  const baseEvents = [
    ...extraEvents,
    event(`${id}-proposal`, 12, "agent.proposal", "agent", proposal, "run_demo_trace"),
    event(`${id}-tool-plan`, 13, "tool.plan", "agent", {
      tool_calls: proposal.tool_calls,
      requires_human: status === "pending_approval",
    }),
  ];
  const decisionActor = decision?.decision === "auto_resolved" ? "agent" : "human";
  const events = decision
    ? [...baseEvents, event(`${id}-decision`, 18, "case.decision_submitted", decisionActor, decision)]
    : baseEvents;

  return {
    id,
    merchant_id: DEMO_MERCHANT_ID,
    merchant_name: "Demo Merchant",
    type,
    status,
    subject_ref: {
      order_id: `gid://shopify/Order/${orderName.replace("#", "")}`,
      order_name: orderName,
      value,
      customer_name: customerName,
      customer_email: customerEmail,
      ...context,
    },
    langgraph_thread_id: `thread_${id}`,
    langsmith_trace_url: null,
    resolution: decision ?? null,
    events,
    created_at: events[0]?.created_at,
  };
}

function fixtureEvalReviews(cases: CaseDetail[]): EvalReviewItem[] {
  const fraudCase = cases.find((item) => item.type === "fraud_triage");
  const stuckCase = cases.find((item) => item.type === "stuck_in_transit");
  return [fraudCase, stuckCase].flatMap((item, index) => {
    if (!item) {
      return [];
    }
    return [
      {
        id: `eval-review-demo-${item.type}`,
        case_id: item.id,
        merchant_id: item.merchant_id,
        langsmith_run_id: index === 0 ? "run_demo_low_confidence" : "run_demo_tool_failure",
        score: index === 0 ? 3 : 2,
        passed: false,
        reason:
          index === 0
            ? "Judge wants an operator to confirm the cancellation note before approval."
            : "Tool failure left the case unresolved and should be reviewed.",
        payload: {
          scenario_id: item.type,
          fop_violations: [],
          unsafe_actions: [],
        },
        status: "queued",
        created_at: offsetTime(25 + index),
      },
    ];
  });
}

function event(
  id: string,
  offsetMinutes: number,
  kind: string,
  actor: CaseEvent["actor"],
  payload: Record<string, unknown>,
  langsmithRunId: string | null = null,
): CaseEvent {
  return {
    id,
    kind,
    actor,
    created_at: offsetTime(offsetMinutes),
    langsmith_run_id: langsmithRunId,
    payload,
  };
}

function toolCall(
  tool: string,
  status: string,
  input: Record<string, unknown>,
  output: Record<string, unknown> | null,
  latencyMs: number,
  retryCount = 0,
) {
  return {
    tool,
    status,
    input,
    output,
    latency_ms: latencyMs,
    retry_count: retryCount,
  };
}

function offsetTime(minutes: number): string {
  return new Date(new Date(DEMO_TIME).getTime() + minutes * 60_000).toISOString();
}
