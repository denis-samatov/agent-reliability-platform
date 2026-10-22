# Business case (model with explicit assumptions)

No real organization's data is used. Measured inputs come from `results/eval.json`
(synthetic scenarios); everything else is an assumption to replace with real data.

## Where value comes from
1. **Deflected handling time** for tickets the agent completes (classification, lookup,
   preparing the action) - a human only approves.
2. **Avoided incidents**: duplicate or unauthorized changes that a naive agent loop would make
   (double grants after retries, actions for the wrong user/tenant). Not quantified here;
   the suite shows the platform prevents them in the tested scenarios.
3. **Cheaper change management**: versions are compared on the same suite before rollout.

## Model (per month)

    saved_hours  = tickets x a x (t_manual - t_approve) / 60
    llm_cost     = tickets x tokens_per_ticket x price_per_token
    net_saving   = saved_hours x loaded_cost_per_hour - llm_cost - platform_cost

| symbol | meaning | source |
|---|---|---|
| `a` | share of tickets completed without manual handling beyond approval | measured: task completion of the chosen version on the suite (optimistic: synthetic) |
| `tokens_per_ticket` | LLM tokens per run | measured: `tokens_mean` in `results/EVAL.md` |
| `t_manual`, `t_approve` | minutes per ticket today / to approve a prepared action | assumption |
| `price_per_token` | external provider price, or 0 for a local model | assumption (local CPU model: no monetary cost measured) |

Sensitivity matters more than the point estimate: a 10-point drop in `a` on real tickets
changes the result more than any token price.
