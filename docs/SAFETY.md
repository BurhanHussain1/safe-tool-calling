# Safety model

> The LLM proposes. Deterministic code decides.

Every claim below is enforced by a test. Where a control is incomplete, this
document says so — a safety document that only lists strengths is marketing.

---

## Results

```
54 golden workflows · 10 adversarial prompts · 0 unauthorised writes
 8/8 high-risk actions gated ·  6/6 ambiguous requests asked instead of guessing
```

| Bucket | Cases | Outcome asserted |
| --- | --- | --- |
| Read-only | 12 | executed immediately |
| Multi-step chains | 12 | ids passed via typed state, never re-guessed |
| Low-risk writes | 6 | executed and reported |
| High-risk writes | 8 | **blocked** pending human approval |
| Ambiguous | 6 | **asked** a clarifying question, zero writes |
| Injection / bulk destruction | 6 | **refused**, zero writes |
| Under-privileged caller | 4 | **refused**, zero writes |

For the 20 cases whose expected outcome is "nothing happened", the test
snapshots the datastore before and after and asserts it is byte-identical.

---

## What the model can and cannot do

| The model can | The model cannot |
| --- | --- |
| Propose a plan of calls | Execute anything |
| Choose from the shortlisted endpoints | Reach an endpoint that was not shortlisted |
| Fill parameters as strings | Choose a value's type — the schema does |
| Say a step is high-risk | Decide that a high-risk step may skip approval |
| Ask a clarifying question | Resolve ambiguity by picking the first match |
| Refuse a request | Grant itself a role it was not given |

Nothing in `nl2api/guardrails/` imports the planner, and nothing there takes
model output as an instruction. The model's output is *data* that the guardrails
inspect.

---

## The controls, in order

Order is itself a control. A plan that fails validation is never policy-checked,
and a plan policy denies is never previewed — so an invalid or forbidden step
cannot reach even a read-only probe.

### 1. Retrieval as a capability boundary
Only ~6 endpoints are put in front of the model per request, filtered to what the
caller's role permits. An operation that never reaches the shortlist cannot appear
in a plan, which narrows the blast radius of injection *before* validation runs.
A plan referencing an unoffered operation is rejected — that reach is the signal.

### 2. Schema validation with narrow coercion
`"24000"` becomes `24000` because the schema says integer. `"$240"`, `"24,000"`,
`"24000.5"` and `"1e5"` are errors. Unknown field names are rejected outright, so
`skip_approval: true` cannot be smuggled into a body.

### 3. Fail-closed risk parsing
An endpoint whose OpenAPI operation declares no `x-risk` is treated as
`high_risk_write` requiring `billing_admin`. Forgetting to declare risk makes an
endpoint *harder* to call.

### 4. Role and risk policy
Four ordered rules: role → risk → value ceiling → blast radius. The plan-level
decision is `max()` of its steps', so the strictest always wins.

### 5. Dry runs that cannot mutate
The preview builder receives a `StateReader` whose entire interface is one `read`
method. There is no code path that could write. Tests snapshot the store either
side of previewing three destructive operations.

### 6. Persisted approval
Approvals are rows. An approval that vanishes on restart is not an approval.
Resuming **re-plans and re-validates** rather than replaying a stored plan: an
approval is a decision about a plan *and the state it was previewed against*.

### 7. Re-validation of resolved values
A value copied out of an API response is re-checked against the schema and
re-run through the policy engine before it is sent. Data from an API response is
no more trusted than data from the model.

### 8. Ambiguity halts
A lookup returning nothing cannot be referenced. A lookup meant to identify one
record that returns several becomes a question. It never takes `data[0]`.

### 9. Redaction on write
Secrets are erased; identifiers are partially masked (`ana@acme.io` →
`a**@acme.io`). Redaction happens on the way *into* storage, so a value never
stored cannot leak from storage.

---

## Threat model

| Threat | Control | Test |
| --- | --- | --- |
| Prompt injection in the request | Shortlist boundary + refusal patterns + validator | `test_golden` bucket 6 |
| Injection via API response content | Responses are data; the prompt says so; resolved values are re-validated | `TestResolvedValueRechecks` |
| Hallucinated endpoint | Registry lookup fails the step | `test_hallucinated_operation_is_caught` |
| Reaching past the offered set | Structural check | `test_operation_outside_the_shortlist_is_caught` |
| Privilege escalation | Policy engine **and** the API's own role check | `TestRoleEnforcement`, `TestPolicy` |
| Wrong-sized refund | Narrow coercion + schema constraints + value ceiling | `TestCoercion`, `TestValueCeiling` |
| Wrong customer | Ambiguity gate | `TestAmbiguityGate` |
| Runaway multi-write plan | Blast-radius cap | `TestBlastRadius` |
| Duplicate write on retry | Retries only a connection that never opened | `client.py` |
| Secrets in logs | Redaction on write | `TestRedaction` |

---

## Known limits

Stated plainly, because these are what an interviewer should ask about.

1. **`IDENTITY_OPERATIONS` is a judgement call.** Deciding that
   `search_customers` must be unambiguous while `list_invoices` may be indexed is
   a claim about *intent*, not something derived from the response shape. It is a
   named constant so that it reads as a decision rather than a law.

2. **The offline planner is not a language model.** It exists so the guardrails
   are testable and the demo runs with no key. The golden suite therefore proves
   the *pipeline* is safe given a plan — it does not measure how good a real
   model's plans are. Cassette replay covers that for recorded cases.

3. **Value ceilings only bind on literals at plan time.** A deferred amount is
   checked after resolution, before the call goes out — so it is enforced, but at
   a later stage than a reviewer might assume.

4. **Redaction is name- and pattern-based.** A secret in a field named
   `customer_note` is not detected.

5. **Identity is a header.** `X-Role` stands in for a signed token. Real
   deployment needs real authentication; nothing else in the design changes.

6. **The blast-radius cap counts steps, not scope.** Three writes to three
   different customers pass a cap of three.
