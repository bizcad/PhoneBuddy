# Phone Buddy — Business Model & Open Questions

Working document. Fill in answers as you research suppliers and make decisions.
Last updated: 2026-03-31

---

## Business Model (Decided)

### Core Premise
- **PB is free.** Funded by Twilio + Railway revenue sharing.
- **The PPA is the paid product.** Customers pay for admin control, customization, advanced features.
- **Every customer is a lead generator.** Word of mouth + PB itself promotes.
- **No billing/receivables for the free tier.** Eliminates the hardest operational problem.

### Revenue Streams
| Stream | Source | Status |
|---|---|---|
| T&R revenue sharing | Twilio partner program | Open question — see below |
| T&R revenue sharing | Railway referral (15% cash, 1yr) | Confirmed — see reference memory |
| PPA upgrade fee | Customer pays for admin/customization | To be priced |
| Lead generation value | Each customer refers others | Unmonetized — compound growth |

### Free Tier (PB)
What the customer gets for free:
- A phone number (Twilio, attributed to Nick's account)
- Hosted PB instance (Railway, Nick's org)
- Scam screening, call logging, basic routing
- Demo call experience + Ted onboarding

What PB is NOT responsible for:
- Billing management
- Receivables / collections
- Complex provisioning decisions

### Paid Tier (PPA Upgrade)
What the customer pays for:
- Admin dashboard (web or voice) to manage their PB
- Custom persona configuration
- Advanced features (hold proxy, negotiation coach, call summaries, etc.)
- Priority model routing (Sonnet instead of Haiku)
- Multi-line support

Pricing: TBD — but the model is subscription, not per-call.

---

## Open Questions — Twilio

> Research target: https://www.twilio.com/en-us/partners

### Q1: Does Twilio have a revenue sharing / referral program?
- **What to find:** Partner program name, eligibility requirements, % share
- **Why it matters:** This is the primary free-tier revenue model
- **Answer:** _______________________________________________

### Q2: How does Twilio sub-account billing work?
- **What to find:** Can Nick create sub-accounts for customers? Who gets invoiced — Nick or customer?
- **Why it matters:** Determines whether customers need their own Twilio account or are on Nick's
- **Options:**
  - Sub-account under Nick (Nick billed, Nick has markup power)
  - Customer's own account (Nick gets referral credit only)
- **Answer:** _______________________________________________

### Q3: What is the attribution mechanism?
- **What to find:** Referral links? Partner codes? Sub-account hierarchy?
- **Why it matters:** Must prove customers came from Nick's network to earn revenue share
- **Answer:** _______________________________________________

### Q4: What does Twilio's ISV program look like?
- **What to find:** ISV = Independent Software Vendor. Twilio has an ISV program for builders who resell Twilio services embedded in their product.
- **Why it matters:** Phone Buddy is exactly an ISV use case — embedding Twilio in a product
- **Search term:** "Twilio ISV program" or "Twilio build program"
- **Answer:** _______________________________________________

### Q5: What KYC / compliance does Twilio require for sub-accounts?
- **What to find:** Do customers need to verify identity to get a number? Business vs. personal?
- **Why it matters:** Determines how light the onboarding can be for free tier
- **Answer:** _______________________________________________

### Q6: What are Twilio's number provisioning costs?
- **What to find:** Cost per local number per month, inbound/outbound per-minute rates
- **Why it matters:** Need to confirm revenue share covers hosting costs at scale
- **Current estimate:** ~$1/month per number + ~$0.0085/min inbound
- **Answer:** _______________________________________________

---

## Open Questions — Railway

> Reference: Railway referral program confirmed — 15% cash on referred friends' paid revenue for 1 year.

### Q7: Can Railway customers be attributed to Nick's referral after the fact?
- **What to find:** Does attribution require signup via referral link, or can it be applied later?
- **Why it matters:** Customers who sign up independently might not be attributed
- **Answer:** _______________________________________________

### Q8: Can Nick create sub-workspaces or team accounts for customers?
- **What to find:** Railway Teams — can Nick manage Railway services on behalf of customers?
- **Why it matters:** Determines whether PB runs on Nick's Railway account or customer's
- **Options:**
  - Nick's account, customer is a user in Nick's Railway team (Nick pays Railway)
  - Customer's own Railway account (Nick gets referral only)
- **Answer:** _______________________________________________

### Q9: What is Railway's pricing for always-on services?
- **What to find:** Monthly cost for a FastAPI service (PB) running continuously
- **Why it matters:** Per-customer hosting cost must be covered by revenue share
- **Current estimate:** Hobby plan ~$5/month, Pro varies by usage
- **Answer:** _______________________________________________

---

## Open Questions — Automation

### Q10: What is the fully automated onboarding flow?
- **Goal:** Customer calls demo PB → Ted calls back → customer gets their number → zero human involvement
- **What needs to be true:**
  - [ ] Twilio API can provision a number programmatically (confirmed — REST API)
  - [ ] Railway API can deploy a new service instance (confirmed — Railway MCP)
  - [ ] No manual step required by Nick at any point
  - [ ] Customer never has to create their own accounts (free tier)
- **Blockers:** Q2 (sub-account model), Q5 (KYC requirements)
- **Answer:** _______________________________________________

### Q11: What triggers the PPA upgrade offer?
- **Options:**
  - Ted mentions it during onboarding ("when you're ready to take the wheel...")
  - PB mentions it proactively during calls ("if you'd like to customize how I handle this...")
  - Customer asks for a feature that requires PPA
  - 48-hour check-in call includes an upgrade offer
- **Decision:** _______________________________________________

### Q12: How does the lead generation flywheel work mechanically?
- **Option A:** PB mentions itself to callers ("this call was handled by Phone Buddy — want your own?")
- **Option B:** PB owner shares their number, callers experience PB firsthand
- **Option C:** Referral codes — each customer gets a code, earns credit when someone signs up
- **Option D:** All of the above
- **Decision:** _______________________________________________

---

## Open Questions — Legal / Compliance

### Q13: What KYC is required for a free account with no payment?
- **Why it matters:** If there's no billing, KYC requirements are much lighter
- **Minimum likely needed:** Name + phone number + agreement to ToS
- **Question:** Is email required? Is identity verification required?
- **Answer:** _______________________________________________

### Q14: What are the call recording disclosure requirements by state?
- **Known:** CA, FL, IL, WA require all-party consent disclosure
- **PB already handles:** Opening greeting includes recording notice
- **Open:** Do we need a written ToS that customers acknowledge before PB records their callers?
- **Answer:** _______________________________________________

### Q15: Are there telemarketing or TCPA constraints on Ted's outbound callback?
- **Why it matters:** Ted calls prospects back — does that trigger TCPA compliance?
- **Current mitigation:** Caller initiates first (they called the demo line), Ted is a callback not a cold call
- **Answer:** _______________________________________________

---

## Open Questions — Product

### Q16: What is the PPA upgrade priced at?
- **Comparable products:** Google Voice $10/mo, OpenPhone $15/mo, Grasshopper $26/mo
- **PB differentiation:** AI screening, scam defense, structured call summaries
- **Proposed range:** $9–$19/month
- **Decision:** _______________________________________________

### Q17: What does the PPA admin interface look like?
- **Options:**
  - Voice-controlled (call PB and say "add my doctor to VIP")
  - Web dashboard
  - SMS commands ("BLOCK 800-555-1234")
  - All three
- **Decision:** _______________________________________________

### Q18: What happens when a customer churns?
- **Their Twilio number:** Port out, release, or transfer?
- **Their data:** Retained for 90 days, then deleted?
- **Railway service:** Deprovisioned immediately or grace period?
- **Decision:** _______________________________________________

---

## Next Steps

When you have answers to Q1–Q4 (Twilio program) and Q7–Q8 (Railway model), the automation
pipeline design can be finalized. Everything else can be decided after first customer.

Priority research order:
1. **Q1** — Twilio partner/ISV program (revenue share %)
2. **Q2** — Twilio sub-account model (who pays)
3. **Q4** — Twilio ISV program specifically
4. **Q8** — Railway teams / workspaces
5. **Q10** — Confirm fully automated onboarding is achievable with Q2+Q8 answers
