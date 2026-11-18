// Subscriptions. "checkout" sends a signed-in firm to Stripe to pay for the plan it chose; the account opens when
// Stripe tells stripe-webhook the payment went through. "portal" opens Stripe's page for changing the card or cancelling.
import { PLANS, planChoice } from "../_shared/core.js";
import { caller, Refused, serve, setAccess, SITE, stripe } from "../_shared/svc.ts";

Deno.serve((req) => serve(req, async () => {
  const user = await caller(req), body = await req.json().catch(() => ({}));
  const meta = user.app_metadata || {}, firm = user.user_metadata || {};

  let customer = meta.stripe_customer as string | undefined;
  if (body.action === "portal") {
    if (!customer) throw new Refused("There is no payment on file for this account yet.");
    const s = await stripe("POST", "billing_portal/sessions", { customer, return_url: SITE + "/account.html" });
    return { url: s.url };
  }

  if (body.action !== "checkout") throw new Refused("Unknown action.");
  if (meta.access === "active" && meta.stripe_subscription) throw new Refused("This account already has an active subscription.");
  const choice = planChoice(body.plan ?? firm.plan, body.state ?? firm.plan_state);
  if (!choice) throw new Refused("Choose a plan, and a state for the single-state plan, then try again.");
  if (!customer) {
    const c = await stripe("POST", "customers", { email: user.email, name: firm.firm || firm.full_name || undefined, metadata: { user_id: user.id } }, "cust-" + user.id);
    customer = c.id;
    await setAccess(user.id, { stripe_customer: customer });
  }
  const yearly = body.bill === "year" || (body.bill === undefined && firm.bill === "year");
  const plan = PLANS[choice.plan], tag = { user_id: user.id, plan: choice.plan, state: choice.state };
  const s = await stripe("POST", "checkout/sessions", {
    mode: "subscription", customer, client_reference_id: user.id,
    line_items: [{ quantity: 1, price_data: { currency: "usd", unit_amount: yearly ? plan.year_cents : plan.cents, recurring: { interval: yearly ? "year" : "month" },
      product_data: { name: plan.name + (choice.state ? " (" + choice.state.replace(/\b\w/g, (c: string) => c.toUpperCase()) + ")" : "") } } }],
    subscription_data: { metadata: tag, description: "Renews automatically until cancelled. Postcards and owner lookups you order are added to the next invoice." },
    metadata: tag,
    success_url: SITE + "/account.html?paid=1", cancel_url: SITE + "/account.html?subscribe=1",
  });
  return { url: s.url };
}));
