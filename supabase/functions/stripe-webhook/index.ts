// Stripe calls this when a payment or a subscription changes. It is the only thing that opens or closes an account
// for payment reasons. Every call must carry Stripe's signature; anything else is turned away.
import { accessFor, stripeSigned } from "../_shared/core.js";
import { admin, setAccess, stripe } from "../_shared/svc.ts";

Deno.serve(async (req) => {
  const raw = await req.text();
  if (!(await stripeSigned(raw, req.headers.get("stripe-signature"), Deno.env.get("STRIPE_WEBHOOK_SECRET") || "")))
    return new Response("bad signature", { status: 400 });
  const event = JSON.parse(raw), obj = event.data.object;
  try {
    if (event.type === "checkout.session.completed" && obj.mode === "subscription" && obj.payment_status === "paid") {
      const sub = await stripe("GET", "subscriptions/" + obj.subscription);
      const m = sub.metadata || {}, uid = obj.client_reference_id || m.user_id;
      if (uid) {
        await setAccess(uid, { access: accessFor(sub.status), plan: m.plan === "all" ? "all" : "single", state: m.plan === "all" ? "" : (m.state || ""),
          stripe_customer: obj.customer, stripe_subscription: sub.id, paid_through: sub.current_period_end || null });
        // The card that paid becomes the customer's default, so postcards and lookups can go on later invoices.
        if (sub.default_payment_method) await stripe("POST", "customers/" + obj.customer, { invoice_settings: { default_payment_method: sub.default_payment_method } });
      }
    } else if (event.type === "customer.subscription.updated" || event.type === "customer.subscription.deleted") {
      const uid = (obj.metadata || {}).user_id;
      if (uid) {
        const { data } = await admin().auth.admin.getUserById(uid);
        const current = ((data.user && data.user.app_metadata) || {}).stripe_subscription;
        // An old, cancelled subscription must never close an account that has since paid for a new one.
        if (!current || current === obj.id)
          await setAccess(uid, { access: event.type.endsWith("deleted") ? "none" : accessFor(obj.status), stripe_subscription: obj.id, paid_through: obj.current_period_end || null });
      }
    } else if (event.type === "invoice.paid" && obj.customer) {
      // Usage that was waiting on this invoice is now paid for.
      await admin().from("usage").update({ invoiced: true }).eq("stripe_customer", obj.customer).eq("invoiced", false).lte("created_at", new Date(obj.created * 1000).toISOString());
    }
  } catch (e) {
    console.error(event.type, e);
    return new Response("failed, send again", { status: 500 });      // Stripe retries for three days
  }
  return new Response(JSON.stringify({ received: true }), { headers: { "Content-Type": "application/json" } });
});
