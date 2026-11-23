// Subscriptions. "checkout" sends a signed-in firm to Stripe to pay for the plan it chose; the account opens when
// Stripe tells stripe-webhook the payment went through. "portal" opens Stripe's page for changing the card or cancelling.
// "card" saves a card, with no charge, for an account that is open without a subscription.
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

  if (body.action === "card") {
    // An open account with no subscription (one we opened by hand) puts a card on file so postcards and lookups can be charged.
    // Nothing is charged here. The customer is only written to the account once Stripe confirms the card was saved.
    if (meta.access !== "active") throw new Refused("Subscribe first; the card you pay with is used for postcards and lookups too.");
    if (customer) throw new Refused("This account already has a card on file. Use Change card to replace it.");
    let pending = meta.stripe_pending as string | undefined;
    if (!pending) {
      pending = (await stripe("POST", "customers", { email: user.email, name: firm.firm || firm.full_name || undefined, metadata: { user_id: user.id } }, "cust-" + user.id)).id;
      await setAccess(user.id, { stripe_pending: pending });
    }
    const s = await stripe("POST", "checkout/sessions", { mode: "setup", customer: pending, client_reference_id: user.id, currency: "usd",
      metadata: { user_id: user.id }, success_url: SITE + "/account.html?card=1", cancel_url: SITE + "/account.html" });
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
    // A promotion code box on the payment page, for monthly plans only: a "50% off for three months" code applied to a
    // yearly plan would take half off the whole year in one go.
    ...(yearly ? {} : { allow_promotion_codes: true }),
    success_url: SITE + "/account.html?paid=1", cancel_url: SITE + "/account.html?subscribe=1",
  });
  return { url: s.url };
}));
