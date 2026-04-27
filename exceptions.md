# Ecommerce Exception Map
*Full reference of agent-automatable exceptions across the order lifecycle*

---

## Pre-Fulfillment Exceptions

*Order placed, not yet picked/packed at the warehouse. The window between checkout and the warehouse touching the order.*

### Fraud & Risk

**1. Medium-risk fraud triage (gray-zone orders)**
- **Trigger:** Payment processor (Stripe Radar, Signifyd, Shopify) flags as medium risk
- **Manual today:** Pull customer history, IP/email signals, order patterns, decide approve/cancel
- **Systems:** Shopify + payment processor + email lookup tools
- **Volume:** 10–50/week for mid-size DTC, much higher for high-AOV categories
- **Why agent:** Judgment-heavy, multi-source context, merchant-specific risk tolerance

**2. High-value order review**
- **Trigger:** Order value crosses merchant-defined threshold ($500+ typical, varies by category)
- **Manual today:** Pull context from multiple sources, decide approve/hold/cancel
- **Systems:** Shopify + payment processor + customer history + fraud tools
- **Volume:** Daily occurrence for high-AOV brands (jewelry, electronics, luxury)
- **Why agent:** Multi-source context synthesis; getting it wrong either way is expensive

**3. Velocity / pattern abuse detection**
- **Trigger:** Same customer, address, card, or IP placing repeated orders in a short window
- **Manual today:** Spot the pattern, decide if it's a reseller, bot, or legit
- **Systems:** Shopify + customer history + fraud tools
- **Why agent:** Pattern recognition across orders across time is exactly the LLM sweet spot

**4. Restricted / banned customer detection**
- **Trigger:** Known problem customer (chargeback history, return abuse) places a new order
- **Manual today:** Cross-reference email/phone/address against internal blocklist; cancel or hold
- **Systems:** Shopify + helpdesk + internal CRM/spreadsheet
- **Volume:** Low, but high stakes per case

---

### Payment Edge Cases

**5. Authorization succeeded, capture-at-fulfillment fails**
- **Trigger:** Merchant uses auth-then-capture; card expired/canceled/declined at time of capture
- **Manual today:** Email customer for new payment method, hold inventory, decide timeout policy
- **Systems:** Stripe + Shopify + email + 3PL
- **Volume:** Real issue for auth-at-checkout merchants (jewelry, custom goods, B2B)

**6. Partial payment capture failure**
- **Trigger:** Split shipment; second capture fails after first already shipped
- **Manual today:** Decide whether to ship remaining, refund partial, or contact customer
- **Systems:** Stripe + Shopify + 3PL

**7. Currency / FX mismatch**
- **Trigger:** International order paid in one currency, captured in another; FX rate moved
- **Manual today:** Reconcile the difference, sometimes contact customer
- **Systems:** Payment processor + accounting + Shopify

---

### Inventory & Catalog

**8. Inventory conflict (item went OOS between checkout and fulfillment)**
- **Trigger:** Product sold beyond available stock due to sync delay, oversell, or simultaneous orders
- **Manual today:** Decide split-ship, hold-until-restock, substitute, or cancel — then communicate
- **Systems:** Shopify + 3PL + Klaviyo/email + sometimes ad platform
- **Volume:** Extremely high, especially during launches, promos, BFCM
- **Why agent:** Top-3 daily fire for almost every DTC brand; multi-party coordination

**9. Bundle / kit assembly issues**
- **Trigger:** Order includes a bundle SKU but one component is OOS
- **Manual today:** Decide to substitute the component, delay the bundle, or partial-ship
- **Systems:** Shopify + 3PL + product catalog

**10. Pre-order / backorder coordination**
- **Trigger:** Customer ordered a pre-order item alongside in-stock items in the same order
- **Manual today:** Decide to ship in-stock items now or hold; communicate timeline; handle delays as date slips
- **Systems:** Shopify + 3PL + Klaviyo

**11. Discontinued SKU still on storefront**
- **Trigger:** Customer orders something the merchant pulled but didn't fully delist
- **Manual today:** Notify customer, refund, suggest alternative, fix the listing
- **Systems:** Shopify + product catalog + email

---

### Shipping & Logistics Setup

**12. Shipping method unavailable for destination**
- **Trigger:** Customer chose expedited shipping to an address the carrier doesn't service (rural, PO box, international)
- **Manual today:** Contact customer, downgrade method or upgrade carrier, refund or charge difference
- **Systems:** Shopify + carrier + email

**13. Restricted product to destination**
- **Trigger:** Customer in a state/country that restricts the product (alcohol, supplements, vape, hazmat)
- **Manual today:** Cancel and refund with explanation; sometimes flag for legal review
- **Systems:** Shopify + compliance lookup + email

**14. Hazmat / oversized item routing**
- **Trigger:** Order contains a hazmat or oversized item requiring special carrier or warehouse routing
- **Manual today:** Route to correct fulfillment partner; sometimes split the order
- **Systems:** Shopify + multiple 3PLs + carrier

**15. Multi-warehouse routing decisions**
- **Trigger:** Order has items split across warehouses, or could be fulfilled from multiple locations
- **Manual today:** Decide which warehouse(s) ship which items based on stock, cost, and speed
- **Systems:** Shopify + multiple 3PLs/warehouses
- **Why agent:** Optimization problem with merchant-specific tradeoffs

---

### Customer-Initiated Changes

**16. Address change requests post-order**
- **Trigger:** Customer emails/chats asking to change shipping address after placing order
- **Manual today:** Check fulfillment status, update if not yet picked, coordinate with 3PL if already in flight
- **Systems:** Helpdesk + Shopify + 3PL + email
- **Volume:** High, especially during gifting seasons

**17. Item add / remove / swap requests**
- **Trigger:** "Can I add this to my order?" "Can I change size from M to L?"
- **Manual today:** Check status, modify or cancel-and-recreate, handle payment delta, coordinate with 3PL
- **Systems:** Helpdesk + Shopify + Stripe + 3PL

**18. Order cancellation requests**
- **Trigger:** Customer wants to cancel before shipment
- **Manual today:** Check status, cancel if possible, refund, restock inventory; if already shipped, redirect to returns flow
- **Systems:** Helpdesk + Shopify + Stripe + 3PL

**19. Combine multiple orders**
- **Trigger:** Customer placed two orders close together, asks to merge for combined shipping
- **Manual today:** Cancel one, modify the other, refund extra shipping
- **Systems:** Helpdesk + Shopify + Stripe + 3PL

**20. Gift wrap / personalization adds**
- **Trigger:** Customer asks to add gift wrap, message, or personalization after ordering
- **Manual today:** Check fulfillment status, add the request, coordinate with warehouse
- **Systems:** Helpdesk + Shopify + 3PL

---

### Discount & Promotion Edge Cases

**21. Discount code didn't apply / retroactive discount request**
- **Trigger:** Customer emails saying their code didn't work or they forgot to use one
- **Manual today:** Validate code applicability, decide to honor, issue partial refund
- **Systems:** Helpdesk + Shopify + Stripe

**22. Price drop after purchase**
- **Trigger:** Item went on sale within X days of purchase; customer asks for the price difference
- **Manual today:** Check policy window, validate purchase price, issue partial refund
- **Systems:** Helpdesk + Shopify + Stripe

**23. Wholesale / B2B pricing not applied**
- **Trigger:** B2B customer ordered through retail flow; pricing wasn't tier-applied
- **Manual today:** Recalculate, refund difference
- **Systems:** Helpdesk + Shopify + B2B platform

---

### Subscription Edge Cases

**24. Skipped / paused subscription that processed anyway**
- **Trigger:** Customer requested skip, system charged anyway
- **Manual today:** Refund, update subscription, apologize
- **Systems:** Recharge/Skio + Shopify + Stripe + helpdesk

**25. Subscription product change requests**
- **Trigger:** Customer wants to swap flavor/scent/size for next ship before it processes
- **Manual today:** Update subscription before next charge
- **Systems:** Subscription platform + helpdesk

**26. Subscription billing / shipping address mismatch**
- **Trigger:** Customer has different billing and shipping addresses, only updated one
- **Manual today:** Identify mismatch, contact customer to clarify
- **Systems:** Subscription platform + Shopify

---

## Fulfillment Exceptions

*Order is at the 3PL/warehouse but hasn't shipped or is having issues during the pick/pack/ship process.*

### Stuck or Stalled Orders

**27. Order not picked within SLA window**
- **Trigger:** Order has been at 3PL for 24+ hours without status change
- **Manual today:** Someone notices, contacts 3PL, asks why
- **Systems:** 3PL dashboard + Shopify + email/Slack to 3PL
- **Volume:** High — one of the most common ops fires

**28. Pick failure (item not findable on shelf)**
- **Trigger:** 3PL says they can't find the item even though system shows stock
- **Manual today:** Investigate inventory discrepancy, decide to substitute, hold, or cancel
- **Systems:** 3PL + Shopify + inventory system

**29. Pack failure (item damaged at warehouse)**
- **Trigger:** 3PL flags a unit as damaged during pack
- **Manual today:** Route a replacement unit, update inventory, adjust expected ship date
- **Systems:** 3PL + Shopify + inventory

**30. Quality control failure**
- **Trigger:** 3PL or QC team flags an item as defective before ship
- **Manual today:** Hold the order, notify customer of delay, find replacement unit
- **Systems:** 3PL + Shopify + helpdesk

---

### Label & Shipping Issues

**31. Label generation failure**
- **Trigger:** Shipping API call to carrier fails (bad address, weight mismatch, account issue)
- **Manual today:** 3PL flags it, ops figures out why, fixes data and retries
- **Systems:** 3PL + carrier API + Shopify

**32. Carrier rate or service unavailable**
- **Trigger:** Chosen shipping method unavailable at fulfillment time (carrier capacity, weather, service suspension)
- **Manual today:** Pick alternative carrier/service, sometimes contact customer about delay or cost change
- **Systems:** 3PL + carrier + Shopify + email

**33. Weight / dimension mismatch (catalog vs. actual)**
- **Trigger:** Actual package dimensions don't match what was rated at checkout; carrier rejects or merchant gets surprise billing
- **Manual today:** Reconcile, sometimes contact customer for additional charge
- **Systems:** 3PL + carrier + Shopify + accounting

**34. Hazmat documentation missing**
- **Trigger:** Hazmat shipment requires forms; forms are missing or invalid
- **Manual today:** Ops generates docs, sometimes coordinates with compliance
- **Systems:** 3PL + compliance + carrier

---

### Multi-Shipment Coordination

**35. Split shipment decisions during pack**
- **Trigger:** 3PL identifies that order can't ship complete from one location or box
- **Manual today:** Decide whether to split (extra cost) or hold for consolidation
- **Systems:** 3PL + Shopify + customer comms

**36. Inventory transfer between warehouses needed**
- **Trigger:** Order is sitting at warehouse A but needs an item from warehouse B
- **Manual today:** Initiate inter-warehouse transfer, communicate timeline
- **Systems:** Multiple 3PLs + Shopify + accounting

---

### Inventory Sync Drift

**37. System stock vs. physical stock mismatch**
- **Trigger:** Cycle count or pick failure reveals inventory drift between systems
- **Manual today:** Investigate, adjust system counts, decide on backorder/cancellation strategy
- **Systems:** 3PL + Shopify + ERP/inventory tool

**38. Oversell during high-traffic events**
- **Trigger:** Launch or BFCM caused real-time inventory to lag; multiple orders for the last unit
- **Manual today:** Decide which order(s) to fulfill, cancel/refund others, notify customers
- **Systems:** Shopify + 3PL + Klaviyo + helpdesk
- **Volume:** Low frequency, very high stakes (PR risk during launches)

---

### Fulfillment Partner Issues

**39. 3PL outage or backlog**
- **Trigger:** 3PL behind by days due to volume spike, system issue, or weather
- **Manual today:** Communicate status to customers proactively, update expected delivery dates, sometimes route around 3PL
- **Systems:** 3PL + Klaviyo + helpdesk + Shopify

**40. Wrong item picked**
- **Trigger:** Caught at QC or reported by customer after delivery
- **Manual today:** Send replacement, recover wrong item, adjust inventory, sometimes compensate customer
- **Systems:** 3PL + Shopify + helpdesk + carrier

---

## Post-Ship Exceptions

*Order has shipped. This is the highest volume bucket — most ops time gets spent here.*

### Shipment In-Transit Issues

**41. Stuck in transit (no scan update for X days)**
- **Trigger:** Tracking hasn't updated in 3–5+ days
- **Manual today:** Check carrier, decide to wait or send replacement, communicate to customer (often proactively)
- **Systems:** Carrier + Shopify + helpdesk + 3PL
- **Volume:** Very high

**42. WISMO ("where is my order")**
- **Trigger:** Customer inquiry asking for order status
- **Manual today:** Pull tracking, summarize status, respond
- **Systems:** Helpdesk + carrier + Shopify
- **Volume:** Single largest support ticket category in ecommerce

**43. Lost in transit**
- **Trigger:** Package shows no movement for 7–10+ days, or carrier marks lost
- **Manual today:** File claim with carrier, decide on replacement vs. refund, communicate
- **Systems:** Carrier + Shopify + helpdesk + 3PL

**44. Damaged in transit**
- **Trigger:** Customer reports damage on arrival, often with photos
- **Manual today:** Validate damage, decide replacement/refund/partial credit, file carrier claim if applicable
- **Systems:** Helpdesk + carrier + Shopify + 3PL

**45. Customs / duties hold (international)**
- **Trigger:** International shipment held by customs; duties owed or paperwork issue
- **Manual today:** Communicate to customer, sometimes provide additional docs or pay duties on customer's behalf
- **Systems:** Carrier + customer email + Shopify

**46. Wrong address delivery / package returned to sender**
- **Trigger:** Carrier returns package due to bad address or refused delivery
- **Manual today:** Contact customer, update address, reship at customer or merchant cost; or refund
- **Systems:** Carrier + Shopify + helpdesk + 3PL

**47. Address intercept request**
- **Trigger:** Customer realizes address is wrong after shipment, asks to redirect
- **Manual today:** Contact carrier for intercept service, pay intercept fee, coordinate update
- **Systems:** Carrier + helpdesk + Shopify

---

### Delivery Disputes

**48. "Delivered but not received"**
- **Trigger:** Carrier marked delivered, customer says it never arrived
- **Manual today:** Check delivery photo and GPS, customer history, neighborhood data, decide to refund/replace/file claim
- **Systems:** Carrier + helpdesk + Shopify
- **Volume:** High and rising (porch piracy + carrier laxity)

**49. Delivered to wrong address**
- **Trigger:** Tracking shows delivered to an incorrect address (carrier error)
- **Manual today:** File carrier claim, send replacement, sometimes try to retrieve original
- **Systems:** Carrier + Shopify + helpdesk

**50. Delayed delivery beyond promised window**
- **Trigger:** Order didn't arrive by the expected date (especially around holidays)
- **Manual today:** Proactive comms or reactive to inquiry; sometimes refund shipping or offer credit
- **Systems:** Carrier + Klaviyo + Shopify + helpdesk

---

### Returns Intake

**51. Return request via email / chat (off-portal)**
- **Trigger:** Customer emails asking to return, doesn't use the returns portal
- **Manual today:** Educate them on the portal or process manually
- **Systems:** Helpdesk + returns platform + Shopify

**52. Return eligibility edge cases**
- **Trigger:** Outside return window, used product, final-sale item, etc.
- **Manual today:** Apply policy with judgment, sometimes make exceptions for VIPs
- **Systems:** Helpdesk + Shopify + returns platform

**53. Exchange requests (return A, send B)**
- **Trigger:** Customer wants to swap size/color/product
- **Manual today:** Validate stock for new item, generate return label, create replacement order, coordinate the swap
- **Systems:** Helpdesk + returns platform + Shopify + 3PL + Stripe

---

### Returns Processing

**54. Return arrived without RMA**
- **Trigger:** Customer returns package without going through the formal process
- **Manual today:** Identify the return, link to the order, decide to honor
- **Systems:** 3PL + returns platform + Shopify

**55. Return arrived in wrong condition**
- **Trigger:** 3PL receives return but item is damaged, used beyond policy, or wrong item
- **Manual today:** Decide partial refund, no refund, or honor anyway; communicate
- **Systems:** 3PL + returns platform + helpdesk + Shopify

**56. Return never arrived**
- **Trigger:** Customer initiated return weeks ago; package never received at warehouse
- **Manual today:** Investigate carrier tracking, decide to refund anyway or wait
- **Systems:** Returns platform + carrier + 3PL + helpdesk

**57. Refund coordination on return receipt**
- **Trigger:** Return received and approved; refund needs to process
- **Manual today:** Trigger refund in Stripe, update order in Shopify, restock inventory if applicable, notify customer
- **Systems:** 3PL + Shopify + Stripe + returns platform

---

### Subscription Lifecycle (Post-Ship)

**58. Subscription churn risk signals**
- **Trigger:** Customer skipped twice, downgraded, or sent an angry support ticket
- **Manual today:** Retention team reaches out with offer or save flow
- **Systems:** Subscription platform + helpdesk + Klaviyo

**59. Subscription cancellation requests**
- **Trigger:** Customer wants to cancel
- **Manual today:** Process cancellation, optionally offer save, update billing
- **Systems:** Subscription platform + helpdesk + Klaviyo

---

### Financial & Dispute Issues

**60. Chargeback received**
- **Trigger:** Payment processor notifies of chargeback
- **Manual today:** Compile evidence package, draft response, submit before deadline
- **Systems:** Stripe + Shopify + carrier + helpdesk
- **Note:** Dedicated Phase 2 pack — listed here for completeness

**61. Refund disputes (customer says refund didn't process)**
- **Trigger:** Customer claims they didn't receive refund, but Stripe shows it processed
- **Manual today:** Pull Stripe receipt, explain timing (3–10 business days), sometimes escalate to bank
- **Systems:** Stripe + helpdesk + Shopify

**62. Loyalty / points redemption issues**
- **Trigger:** Points didn't apply, customer claims they should have, or rewards didn't trigger
- **Manual today:** Validate in loyalty platform, manually credit if appropriate
- **Systems:** Loyalty platform (Smile, Yotpo) + Shopify + helpdesk

---

### Reviews & Reputation

**63. Negative review with operational issue**
- **Trigger:** 1–2 star review mentions a specific order problem
- **Manual today:** Identify the order, reach out to customer privately, resolve, request review update
- **Systems:** Review platform + helpdesk + Shopify

**64. Public social media complaint**
- **Trigger:** Customer @-mentions brand on Twitter/Instagram with order issue
- **Manual today:** Identify customer/order, respond publicly with empathy, take to DM, resolve
- **Systems:** Social monitoring + helpdesk + Shopify
- **Why agent:** Time-sensitive, requires brand voice, multi-system context

---

### Retroactive Issues

**65. Customer reports issue weeks later**
- **Trigger:** Someone surfaces an old order problem that fell through the cracks
- **Manual today:** Dig up the order, reconstruct what happened, decide on resolution given time elapsed
- **Systems:** Shopify + helpdesk + carrier history + 3PL

**66. Recall or quality issue affecting shipped orders**
- **Trigger:** Merchant identifies a batch of products with a defect
- **Manual today:** Identify all affected customers, communicate, coordinate replacements/refunds
- **Systems:** Shopify + Klaviyo + 3PL + Stripe
- **Volume:** Rare, but very high stakes when it happens

---

## Phase 1 Priority Tiers

### Tier 1 — Must-haves for the wedge
*Top of the daily fire pile. High volume, cross-system, judgment-heavy.*

| # | Exception |
|---|---|
| 41 | Stuck in transit |
| 42 | WISMO |
| 48 | Delivered but not received |
| 16 | Address change requests post-order |
| 17 | Item add / remove / swap requests |
| 18 | Order cancellation requests |
| 8 | Inventory conflict (OOS after checkout) |
| 27 | Order not picked within SLA |
| 1 | Medium-risk fraud triage |
| 44 | Damaged in transit |

### Tier 2 — High value, slightly narrower
*Real pain but lower frequency or better-handled by existing point solutions.*

| # | Exception |
|---|---|
| 2 | High-value order review |
| 3 | Velocity / pattern abuse detection |
| 19 | Combine multiple orders |
| 31 | Label generation failure |
| 46 | Package returned to sender |
| 51 | Return request intake (off-portal) |
| 52 | Return eligibility edge cases |
| 55 | Return arrived in wrong condition |

### Tier 3 — Phase 2 packs
*Important but better as dedicated workflow packs once the core wedge is stable.*

| # | Exception | Pack |
|---|---|---|
| 60 | Chargeback received | Chargeback Defense Pack |
| 53–57 | Full returns processing | Returns & Exchange Pack |
| 24–26, 58–59 | Subscription lifecycle | Subscription Pack |
| 66 | Recall coordination | Bulk Comms Pack |

---

## Key Insight

**Post-ship is where the volume lives. Pre-fulfillment is where the high-stakes judgment lives.**

A complete Phase 1 product needs to span both — post-ship gives you daily-use stickiness, while pre-fulfillment cases (fraud triage, OOS conflicts) give you the "this saved us from a disaster" moments that drive renewals and referrals.

---

*Cross-reference: Product Roadmap v0.1 — Phase 1 wedge definition*